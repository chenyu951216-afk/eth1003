"""Freqtrade trade-schedule cross-check of frozen ETH EMA12/720.

The main cash-flow/liquidation audit is separate because Freqtrade does not
provide scheduled monthly external deposits or exact historical EasiCoin risk.
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class FrozenEma135(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    startup_candle_count = 0
    can_short = False
    process_only_new_candles = True
    minimal_roi = {"0": 1000.0}
    # The account-level Cross liquidation check is performed separately from
    # Freqtrade's trade-level stop. -100 keeps this artificial stop inactive.
    stoploss = -100.0
    trailing_stop = False
    use_exit_signal = True
    exit_profit_only = False

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        dataframe["ema12"] = close.ewm(span=12, adjust=False).mean()
        dataframe["ema720"] = close.ewm(span=720, adjust=False).mean()
        dataframe["valid_history"] = np.arange(len(dataframe)) >= 3600
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["valid_history"] &
                      (dataframe["ema12"] > dataframe["ema720"]), "enter_long"] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["valid_history"] &
                      (dataframe["ema12"] <= dataframe["ema720"]), "exit_long"] = 1
        return dataframe

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return min(150.0, max_leverage)

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float, entry_tag: str | None,
                            side: str, **kwargs) -> float:
        equity = self.wallets.get_total("USDT")
        notional = equity * 0.0135 * leverage
        qty = math.floor(notional / current_rate / 0.01 + 1e-10) * 0.01
        if qty < 0.01:
            return 0.0
        return min(qty * current_rate / leverage, max_stake)
