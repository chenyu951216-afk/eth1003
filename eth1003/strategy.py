"""Frozen Binance ETHUSDT 1h EMA12/720 long-or-flat signal and 1.35% sizing.

Only fully closed candles are admitted. No historical candle creates a past order.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

HOUR_MS = 3_600_000
START_MS = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
ALLOCATION = Decimal('0.0135')
TARGET_LEVERAGE = 150


@dataclass(frozen=True)
class Signal:
    bar_open_ms: int
    decided_at_ms: int
    desired_long: bool
    ema_fast: float
    ema_slow: float
    last_close: float
    candles: int


def evaluate(closes: list[tuple[int, float]], now_ms: int) -> Signal:
    if len(closes) < 3600:
        raise ValueError('NEED_3600_COMPLETE_HOURLY_BARS')
    if any(not (price > 0) for _, price in closes):
        raise ValueError('INVALID_CLOSE')
    if any(b - a != HOUR_MS for (a, _), (b, _) in zip(closes, closes[1:])):
        raise ValueError('MISSING_OR_DUPLICATE_HOUR')
    last = closes[-1][0]
    if last + HOUR_MS > now_ms or now_ms - (last + HOUR_MS) >= HOUR_MS:
        raise ValueError('LAST_COMPLETE_HOUR_MISSING_OR_FUTURE')
    fast = slow = float(closes[0][1])
    af, aslow = 2 / 13, 2 / 721
    for _, close in closes[1:]:
        fast = af * close + (1 - af) * fast
        slow = aslow * close + (1 - aslow) * slow
    return Signal(last, last + HOUR_MS, fast > slow, fast, slow,
                  float(closes[-1][1]), len(closes))


def target_notional(account_equity_usdt: str) -> Decimal:
    equity = Decimal(str(account_equity_usdt))
    if not equity.is_finite() or equity <= 0:
        raise ValueError('ACCOUNT_EQUITY_NOT_POSITIVE')
    return equity * ALLOCATION * TARGET_LEVERAGE


async def fetch_complete_closes(client: httpx.AsyncClient, now_ms: int,
                                cache_path: str | Path | None = None) -> list[tuple[int, float]]:
    """Fetch Binance futures hourly closes from 2020 and reject any gap.

    Full-history seeding avoids changing EMA720 when the program restarts.
    This public feed is the frozen backtest signal source; execution quotes come
    independently from Bitget.
    """
    latest = (now_ms // HOUR_MS - 1) * HOUR_MS
    cursor = START_MS
    closes: list[tuple[int, float]] = []
    if cache_path and Path(cache_path).exists():
        raw = json.loads(Path(cache_path).read_text(encoding='utf-8'))
        closes = [(int(t), float(p)) for t, p in raw]
        if closes and (closes[0][0] != START_MS or
                       any(b-a != HOUR_MS for (a,_),(b,_) in zip(closes,closes[1:]))):
            raise ValueError('LOCAL_CANDLE_CACHE_GAP')
        closes = [(t,p) for t,p in closes if t <= latest]
        if closes: cursor = closes[-1][0] + HOUR_MS
    while cursor <= latest:
        response = await client.get('/fapi/v1/klines', params={
            'symbol': 'ETHUSDT', 'interval': '1h', 'startTime': cursor,
            'endTime': latest + HOUR_MS - 1, 'limit': 1500})
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise ValueError('BINANCE_HOURLY_HISTORY_INCOMPLETE')
        for row in rows:
            opened = int(row[0])
            if opened < cursor or opened > latest or int(row[6]) >= now_ms:
                continue
            if closes and opened != closes[-1][0] + HOUR_MS:
                raise ValueError('BINANCE_HOURLY_HISTORY_GAP')
            closes.append((opened, float(row[4])))
        new_cursor = int(rows[-1][0]) + HOUR_MS
        if new_cursor <= cursor:
            raise ValueError('BINANCE_HISTORY_PAGINATION_STALLED')
        cursor = new_cursor
    if not closes or closes[-1][0] != latest:
        raise ValueError('LATEST_COMPLETE_BINANCE_HOUR_UNAVAILABLE')
    if cache_path:
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(closes, separators=(',',':')), encoding='utf-8')
        temp.replace(path)
    return closes
