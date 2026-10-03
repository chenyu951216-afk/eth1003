"""One current-hour strategy decision; no replay of missed historical orders."""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from .execution import Execution, Ledger, OrderBlocked, TradeIntent, oid
from .strategy import evaluate, fetch_complete_closes, target_notional
from .exchange.signals import fmt, floor_step, decimal as D


class Runner:
    def __init__(self, execution: Execution, data_dir: str | Path):
        self.execution = execution
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def signal(self):
        async with httpx.AsyncClient(base_url='https://fapi.binance.com', timeout=25) as client:
            response = await client.get('/fapi/v1/time')
            response.raise_for_status()
            now_ms = int(response.json()['serverTime'])
            closes = await fetch_complete_closes(client, now_ms,
                                                  self.data_dir / 'binance_eth_1h_closes.json')
        return evaluate(closes, now_ms), now_ms

    async def decide(self, *, allow_post: bool = False, signal=None, now_ms=None):
        if signal is None:
            signal, now_ms = await self.signal()
        now_ms = now_ms or int(time.time() * 1000)
        result = {'bar_open_ms': signal.bar_open_ms, 'decided_at_ms': signal.decided_at_ms,
                  'observation_lag_ms': now_ms - signal.decided_at_ms,
                  'ema12': signal.ema_fast, 'ema720': signal.ema_slow,
                  'desired': 'long' if signal.desired_long else 'flat',
                  'live_order_post_enabled_for_this_call': allow_post}
        if not 0 <= result['observation_lag_ms'] < 3_600_000:
            return result | {'action':'blocked','reason':'SIGNAL_HOUR_EXPIRED'}
        rows = self.execution.ledger.rows()
        for row in sorted(rows,key=lambda x: 0 if x['kind']=='close' else 1):
            if row['kind'] not in {'entry','close'}:
                continue
            if row['state'] in {'uncertain','needs_reconcile','submitting','accepted','pending',
                                'partially_filled','protection_unverified','open',
                                'position_missing_after_fill'}:
                try:
                    await self.execution.reconcile(row['oid'])
                except Exception as exc:
                    return result | {'action':'blocked','reason':'RECONCILIATION_REQUIRED',
                                     'clientOid':row['oid'],'detail':str(exc)}
        rows = self.execution.ledger.rows()
        if any(x['kind'] in {'entry','close'} and x['state'] in {'uncertain','needs_reconcile','submitting','accepted','pending',
                               'position_missing_after_fill','protection_unverified'} for x in rows):
            return result | {'action':'blocked','reason':'UNRESOLVED_EXCHANGE_STATE'}
        entry_rows = [x for x in rows if x['kind']=='entry' and x['state'] in {'open','partially_filled'}]
        if len(entry_rows) > 1:
            return result | {'action':'blocked','reason':'MULTIPLE_LOCAL_POSITIONS'}
        entry = entry_rows[0] if entry_rows else None
        if not entry:
            snapshot = await self.execution.b.account_snapshot('ETHUSDT')
            for key, reason in [('positions','EXISTING_EXCHANGE_POSITION'),
                                ('orders','EXISTING_EXCHANGE_ORDER'),
                                ('plans','UNOWNED_EXCHANGE_PLAN')]:
                if any(str(x.get('symbol','')).upper()=='ETHUSDT' for x in snapshot[key]):
                    return result | {'action':'blocked','reason':reason}
        if signal.desired_long and entry:
            return result | {'action':'hold','entry_oid':entry['oid']}
        if not signal.desired_long and not entry:
            return result | {'action':'flat'}
        if entry:
            try:
                if entry['state']=='partially_filled' and entry['payload'].get('entry_status') not in {'canceled','cancelled'}:
                    existing_cancel=any(x['kind']=='cancel' and x['payload'].get('entry_oid')==entry['oid']
                                        for x in rows)
                    if existing_cancel:
                        return result | {'action':'blocked','reason':'WAITING_FOR_ENTRY_CANCEL'}
                    canceled=await self.execution.cancel(entry['oid'],signal.bar_open_ms,allow_post=allow_post)
                    return result | {'action':'cancel_unfilled_remainder','execution':canceled}
                close = await self.execution.close(entry['oid'], signal.bar_open_ms,
                                                   allow_post=allow_post)
                return result | {'action':'exit','execution':close}
            except OrderBlocked as exc:
                return result | {'action':'blocked','reason':exc.reason,'details':exc.details}
        # The only path that opens a position. Sizing is once: equity * 1.35% * 150.
        snapshot = await self.execution.b.account_snapshot('ETHUSDT')
        quote = await self.execution.b.ticker('ETHUSDT')
        ask = D(quote['ask'])
        raw_notional = target_notional(snapshot['balance']['accountEquity'])
        qty = floor_step(raw_notional / ask, '0.01')
        if qty <= 0:
            return result | {'action':'blocked','reason':'BELOW_BACKTEST_0P01_ETH_STEP'}
        notional = qty * ask
        intent = TradeIntent(symbol='ETHUSDT', side='long', order_type='market',
                             notional_usdt=fmt(notional),
                             client_order_id=oid('eth-ema-12-720',signal.bar_open_ms,'entry'),
                             decision_bar_ms=signal.bar_open_ms, entry=fmt(ask))
        try:
            opened = await self.execution.submit(intent, allow_post=allow_post)
            return result | {'action':'enter','requested_notional_usdt':fmt(notional),
                             'execution':opened}
        except OrderBlocked as exc:
            return result | {'action':'blocked','reason':exc.reason,'details':exc.details}


def make_runner(bitget, data_dir: str | Path):
    data_dir = Path(data_dir)
    return Runner(Execution(bitget, Ledger(data_dir / 'orders.sqlite')), data_dir)
