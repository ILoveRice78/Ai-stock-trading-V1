"""The trading strategy: a long-only daily swing system.

Buy only when a stock is in an uptrend (price above the 200-day average and the
50-day above the 200-day), and then only on one of two triggers:

* pullback: RSI dipped below `rsi_pullback` in the last few days and has just
  recovered above it, with price closing up on the day;
* breakout: price closes at a new `breakout_lookback`-day high on above-average volume.

Sell when the trend breaks (price below the 200-day or 50-day below 200-day) or
when the price hits the stop:

* stop loss: `stop_loss_pct` below the purchase price, checked against the current
  price each time the bot runs (so it can trigger intraday);
* profit lock: once the trailing stop (`trailing_stop_atr` ATRs below the highest
  close since entry) is above the purchase price, it replaces the stop loss.

Entry and trend signals use completed daily bars only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .indicators import atr, rsi, sma

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def min_history(cfg: StrategyConfig) -> int:
    return max(cfg.sma_slow, cfg.breakout_lookback, cfg.momentum_lookback, 63) + 5


def add_indicators(bars: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    df = bars.loc[:, list(REQUIRED_COLUMNS)].astype(float).copy()
    close, volume = df["close"], df["volume"]

    df["sma_fast"] = sma(close, cfg.sma_fast)
    df["sma_slow"] = sma(close, cfg.sma_slow)
    df["rsi"] = rsi(close, cfg.rsi_period)
    df["atr"] = atr(df["high"], df["low"], close, cfg.atr_period)
    df["high_n"] = close.rolling(cfg.breakout_lookback, min_periods=cfg.breakout_lookback).max()
    df["vol_avg"] = volume.rolling(cfg.volume_lookback, min_periods=cfg.volume_lookback).mean()
    df["avg_dollar_volume"] = (close * volume).rolling(
        cfg.volume_lookback, min_periods=cfg.volume_lookback
    ).mean()
    df["momentum"] = close / close.shift(cfg.momentum_lookback) - 1.0
    df["volatility"] = close.pct_change().rolling(63, min_periods=63).std() * math.sqrt(252)

    df["trend_up"] = (close > df["sma_slow"]) & (df["sma_fast"] > df["sma_slow"])
    recent_rsi_low = df["rsi"].shift(1).rolling(3, min_periods=1).min()
    df["pullback_entry"] = (
        df["trend_up"]
        & (recent_rsi_low < cfg.rsi_pullback)
        & (df["rsi"] >= cfg.rsi_pullback)
        & (close > close.shift(1))
    )
    df["breakout_entry"] = (
        df["trend_up"]
        & (close >= df["high_n"])
        & (volume > df["vol_avg"] * cfg.volume_mult)
    )
    vol = df["volatility"].replace(0.0, np.nan)
    df["score"] = (df["momentum"] / vol).fillna(df["momentum"])
    return df


def entry_reasons(row: pd.Series) -> list[str]:
    reasons = []
    if bool(row.get("pullback_entry", False)):
        reasons.append(f"pullback in uptrend (RSI recovered to {row['rsi']:.0f})")
    if bool(row.get("breakout_entry", False)):
        reasons.append("breakout to new high on strong volume")
    return reasons


def stop_level(
    entry_price: float,
    highest_close: float,
    atr_value: float,
    cfg: StrategyConfig,
    stop_loss_pct: float,
) -> tuple[float, str]:
    """Price at which a held position is sold, and which stop it is."""
    loss_stop = entry_price * (1 - stop_loss_pct / 100)
    if atr_value > 0:  # False for NaN
        trail = highest_close - cfg.trailing_stop_atr * atr_value
        if trail > entry_price:
            return trail, "trailing stop"
    return loss_stop, "stop loss"


def trend_exit_reasons(row: pd.Series) -> list[str]:
    reasons = []
    close = float(row["close"])
    if pd.notna(row["sma_slow"]) and close < row["sma_slow"]:
        reasons.append("closed below 200-day average (trend broken)")
    if pd.notna(row["sma_fast"]) and pd.notna(row["sma_slow"]) and row["sma_fast"] < row["sma_slow"]:
        reasons.append("50-day average fell below 200-day (death cross)")
    return reasons


def highest_close_since(ind: pd.DataFrame, entry_date: date | None, entry_price: float) -> float:
    """Highest close since the purchase (never below the purchase price)."""
    if entry_date is None:
        # unknown purchase date: trail only from the latest close onward
        return max(entry_price, float(ind["close"].iloc[-1]))
    since = ind.loc[ind.index >= pd.Timestamp(entry_date), "close"]
    return max(entry_price, float(since.max())) if not since.empty else entry_price


@dataclass
class SymbolSignal:
    symbol: str
    date: date
    close: float
    atr: float
    rsi: float
    trend_up: bool
    score: float
    avg_dollar_volume: float
    entry: list[str] = field(default_factory=list)
    exit: list[str] = field(default_factory=list)
    stop_price: float | None = None
    ready: bool = True
    note: str = ""


def _num(value: object) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")
    return out


def evaluate(
    symbol: str,
    ind: pd.DataFrame,
    cfg: StrategyConfig,
    held: bool = False,
    entry_date: date | None = None,
    entry_price: float | None = None,
    stop_loss_pct: float = 5.0,
    current_price: float | None = None,
) -> SymbolSignal:
    """Evaluate the most recent completed bar of an indicator frame.

    For a held position, `current_price` (e.g. the live intraday price) is checked
    against the stop; without it, the last close is used.
    """
    row = ind.iloc[-1]
    sig = SymbolSignal(
        symbol=symbol,
        date=ind.index[-1].date(),
        close=_num(row["close"]),
        atr=_num(row["atr"]),
        rsi=_num(row["rsi"]),
        trend_up=bool(row["trend_up"]),
        score=_num(row["score"]),
        avg_dollar_volume=_num(row["avg_dollar_volume"]),
    )
    ready = len(ind) >= min_history(cfg) and pd.notna(row["sma_slow"]) and pd.notna(row["atr"])
    if not ready:
        sig.ready = False
        sig.note = f"not enough history ({len(ind)} bars, need {min_history(cfg)})"
    if held:
        # the stop loss applies even without enough history for the trend indicators
        paid = entry_price if entry_price is not None else sig.close
        highest = highest_close_since(ind, entry_date, paid)
        stop, kind = stop_level(paid, highest, sig.atr, cfg, stop_loss_pct)
        sig.stop_price = stop
        price = current_price if current_price is not None else sig.close
        if ready:
            sig.exit = trend_exit_reasons(row)
        if price <= stop:
            sig.exit.append(f"{kind} hit (price {price:.2f} <= stop {stop:.2f})")
    elif ready:
        sig.entry = entry_reasons(row)
    return sig
