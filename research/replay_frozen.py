"""Fixed 1.35% frozen ETH account replay; no parameter search or API orders.

The replay() loop is copied verbatim from the reviewed historical sizing program.
"""
from __future__ import annotations
import json
import math
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from numba import njit

HERE=Path(__file__).parent
DATA=HERE/'data'

@njit(cache=True)
def replay(opens, highs, closes, mark_lows, rates, signal, month_change,
           family, size, cap, fee, slip, capture):
    n = len(opens)
    path = np.empty(n, dtype=np.float64) if capture else np.empty(0, dtype=np.float64)
    cash = 100.0
    qty = entry = 0.0
    fees = funding = gross = max_entry = max_qty = max_position = 0.0
    min_headroom = 1e100
    entries = exits = wins = losses = 0
    deposits = 0.0
    nav_units = 100.0
    nav_peak = 1.0
    max_nav_dd = 0.0
    worst_day_idx = -1
    for t in range(n):
        op = opens[t]
        if month_change[t]:
            deposit_amount = 300.0 * month_change[t]
            before = cash + qty * (op - entry)
            if before <= 0:
                return 1, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            nav_units += deposit_amount / (before / nav_units)
            cash += deposit_amount
            deposits += deposit_amount
        if qty != 0.0 and rates[t] != 0.0:
            payment = -qty * op * rates[t]
            cash += payment
            funding += payment
        if qty != 0.0 and signal[t] == 0:
            fill = op * (1.0 - slip)
            pnl = qty * (fill - entry)
            close_fee = qty * fill * fee
            cash += pnl - close_fee
            gross += pnl
            fees += close_fee
            exits += 1
            qty = 0.0
            entry = 0.0
        if signal[t] != 0 and qty == 0.0:
            fill = op * (1.0 + slip)
            if family == 0:
                target = size
            else:
                target = cash * size * 150.0
                if family == 2 and target > cap:
                    target = cap
            units = math.floor(target / fill / 0.01 + 1e-10) * 0.01
            if units < 0.01:
                return 2, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            notional = units * fill
            open_fee = notional * fee
            margin = notional / 150.0
            if units > 1000.0:
                return 3, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            if margin + open_fee > cash:
                return 4, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            if notional > 1000000.0:
                return 5, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            cash -= open_fee
            fees += open_fee
            qty = units
            entry = fill
            entries += 1
            if notional > max_entry:
                max_entry = notional
            if units > max_qty:
                max_qty = units
        if qty != 0.0:
            mark_notional = qty * mark_lows[t]
            if mark_notional > 1000000.0:
                return 6, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            mm = 0.004 if mark_notional > 500000.0 else 0.0025
            low_eq = cash + qty * (mark_lows[t] - entry)
            headroom = low_eq - mark_notional * (mm + fee)
            if headroom < min_headroom:
                min_headroom = headroom
            if headroom <= 0.0:
                return 7, t, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path
            position = qty * highs[t]
            if position > max_position:
                max_position = position
            eq = cash + qty * (closes[t] - entry)
        else:
            eq = cash
        nav = eq / nav_units
        if nav > nav_peak:
            nav_peak = nav
        dd = (nav_peak - nav) / nav_peak
        if dd > max_nav_dd:
            max_nav_dd = dd
            worst_day_idx = t
        if capture:
            path[t] = eq
    if qty != 0.0:
        fill = closes[-1] * (1.0 - slip)
        pnl = qty * (fill - entry)
        close_fee = qty * fill * fee
        cash += pnl - close_fee
        gross += pnl
        fees += close_fee
        exits += 1
        if capture:
            path[-1] = cash
    return 0, worst_day_idx, cash, deposits, fees, funding, gross, max_entry, max_qty, max_position, min_headroom, max_nav_dd, entries, exits, wins, losses, path


def frozen_signal(bars):
    close=pd.Series(bars[:,4])
    fast=close.ewm(span=12,adjust=False).mean().to_numpy()
    slow=close.ewm(span=720,adjust=False).mean().to_numpy()
    raw=np.where(fast>slow,1,0).astype(np.int8)
    raw[:3600]=0
    signal=np.zeros(len(raw),np.int8)
    signal[1:]=raw[:-1]
    return signal


def run():
    hashes={'eth_hourly_2020_2026.npz':
            '7f65c6a99a6895281ad2a2d6fb6e9b4bef33ead6193d0b0a85306065b8e7a644',
            'eth_binance_mark_hourly_2020_2026.npz':
            '0cb1225944f4fcbd55bdd23e4c9af56a28fbdd8143100a951c0f9eeeaab2947f'}
    for name,expected in hashes.items():
        if hashlib.sha256((DATA/name).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Frozen historical data fingerprint changed: '+name)
    with np.load(DATA/'eth_hourly_2020_2026.npz') as z:
        bars,rates=z['bars'],z['funding']
    with np.load(DATA/'eth_binance_mark_hourly_2020_2026.npz') as z:
        marks=z['marks']
    if len(bars)!=59160 or bars[0,0]!=1577836800000 or bars[-1,0]!=1790809200000:
        raise RuntimeError('Frozen period changed')
    if not np.array_equal(bars[:,0].astype(np.int64),marks[:,0].astype(np.int64)):
        raise RuntimeError('Mark and trade-price hours do not align')
    if not np.array_equal(bars[:,0].astype(np.int64),np.arange(1577836800000,1790812800000,3600000)):
        raise RuntimeError('Hourly history is incomplete')
    signal=frozen_signal(bars)
    first=int(np.flatnonzero(signal)[0])
    months=np.array([datetime.fromtimestamp(x/1000,timezone.utc).year*12+
        datetime.fromtimestamp(x/1000,timezone.utc).month for x in bars[:,0]],dtype=np.int32)
    changed=np.r_[False,months[1:]!=months[:-1]]
    changed=changed&(np.arange(len(signal))>first)
    result=replay(bars[:,1],bars[:,2],bars[:,4],marks[:,3],rates,signal,changed,
                  1,0.0135,0.0,0.0005,0.0002,False)
    if result[0]!=0 or abs(result[2]-158130.8155724802)>0.01 or result[13]!=121:
        raise RuntimeError('Frozen 1.35% result differs from reviewed baseline')
    return {'period':'2020-01-01 to 2026-09-30 UTC',
            'market':'Binance ETHUSDT perpetual historical proxy',
            'starting_usdt':100,'hypothetical_monthly_deposit_usdt':300,
            'contributions_usdt':100+float(result[3]),
            'ending_usdt':float(result[2]),
            'trading_net_usdt':float(result[2])-100-float(result[3]),
            'completed_trades':int(result[13]),
            'fee_usdt':float(result[4]),'funding_usdt':float(result[5]),
            'max_drawdown':float(result[11]),
            'historical_not_live':True}

if __name__=='__main__':print(json.dumps(run(),ensure_ascii=False,indent=2))
