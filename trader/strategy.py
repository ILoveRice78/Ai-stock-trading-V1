"""The trading strategy: a long-only daily swing system.

Buy only when a stock is in an uptrend (price above the 200-day average and the
50-day above the 200-day), and then only on one of two triggers:

* pullback: RSI dipped below `rsi_pullback` in the last few days and has just
  recovered above it, with price closing up on the day;
* breakout: price closes at a new `breakout_lookback`-day high on above-average volume.

Sell when the trend breaks (price below the 200-day or 50-day below 200-day) or
when price falls `trailing_stop_atr` ATRs below the highest close since entry.

Signals use completed daily bars only and are meant to be acted on the next session.
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
FALLBACK_STOP_LOOKBACK = 20  # bars used for the trailing stop when the entry date is unknown


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


def trailing_stop_price(row: pd.Series, highest_close: float, cfg: StrategyConfig) -> float:
    return highest_close - cfg.trailing_stop_atr * float(row["atr"])


def exit_reasons(row: pd.Series, highest_close: float, cfg: StrategyConfig) -> list[str]:
    reasons = []
    close = float(row["close"])
    if pd.notna(row["sma_slow"]) and close < row["sma_slow"]:
        reasons.append("closed below 200-day average (trend broken)")
    if pd.notna(row["sma_fast"]) and pd.notna(row["sma_slow"]) and row["sma_fast"] < row["sma_slow"]:
        reasons.append("50-day average fell below 200-day (death cross)")
    if pd.notna(row["atr"]):
        stop = trailing_stop_price(row, highest_close, cfg)
        if close < stop:
            reasons.append(f"trailing stop hit (close {close:.2f} < stop {stop:.2f})")
    return reasons


def highest_close_since(ind: pd.DataFrame, entry_date: date | None) -> float:
    if entry_date is not None:
        since = ind.loc[ind.index >= pd.Timestamp(entry_date), "close"]
        if not since.empty:
            return float(since.max())
    return float(ind["close"].iloc[-FALLBACK_STOP_LOOKBACK:].max())


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
) -> SymbolSignal:
    """Evaluate the most recent row of an indicator frame."""
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
    if len(ind) < min_history(cfg) or pd.isna(row["sma_slow"]) or pd.isna(row["atr"]):
        sig.ready = False
        sig.note = f"not enough history ({len(ind)} bars, need {min_history(cfg)})"
        return sig
    if held:
        highest = highest_close_since(ind, entry_date)
        sig.stop_price = trailing_stop_price(row, highest, cfg)
        sig.exit = exit_reasons(row, highest, cfg)
    else:
        sig.entry = entry_reasons(row)
    return sig
