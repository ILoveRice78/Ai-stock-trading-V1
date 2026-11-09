"""Signals for the trend part of the portfolio.

A stock is in an **uptrend** when its price is above its 200-day average and the
50-day average is above the 200-day. While a stock is in an uptrend the portfolio
holds an extra "trend" slice of it on top of its core slice.

The trend slice is sold (back down to the core slice) when:

* the trend breaks: price closes below the 200-day, or the 50-day crosses below it;
* stop loss: the price falls `stop_loss_pct` below where the trend slice was bought
  (0 turns this off), checked against the current price on every run;
* profit lock: once the trailing stop (`trailing_stop_atr` ATRs below the highest
  close since the trend slice was bought) is above that purchase price, it replaces
  the stop loss.

Trend signals use completed daily bars only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .config import StrategyConfig
from .indicators import atr, sma

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def min_history(cfg: StrategyConfig) -> int:
    return cfg.sma_slow + 5


def add_indicators(bars: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    df = bars.loc[:, list(REQUIRED_COLUMNS)].astype(float).copy()
    close, volume = df["close"], df["volume"]
    df["sma_fast"] = sma(close, cfg.sma_fast)
    df["sma_slow"] = sma(close, cfg.sma_slow)
    df["atr"] = atr(df["high"], df["low"], close, cfg.atr_period)
    df["avg_dollar_volume"] = (close * volume).rolling(
        cfg.volume_lookback, min_periods=cfg.volume_lookback
    ).mean()
    df["trend_up"] = (close > df["sma_slow"]) & (df["sma_fast"] > df["sma_slow"])
    return df


def stop_level(
    entry_price: float,
    highest_close: float,
    atr_value: float,
    cfg: StrategyConfig,
    stop_loss_pct: float,
) -> tuple[float, str]:
    """Price at which the trend slice is sold, and which stop it is.

    stop_loss_pct = 0 means no loss stop (only the profit lock and trend exits).
    """
    loss_stop = entry_price * (1 - stop_loss_pct / 100) if stop_loss_pct > 0 else 0.0
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
        return max(entry_price, float(ind["close"].iloc[-1]))
    since = ind.loc[ind.index >= pd.Timestamp(entry_date), "close"]
    return max(entry_price, float(since.max())) if not since.empty else entry_price


@dataclass
class SymbolSignal:
    symbol: str
    date: date
    close: float
    atr: float
    trend_up: bool
    avg_dollar_volume: float
    exit: list[str] = field(default_factory=list)
    stop_price: float | None = None
    ready: bool = True
    note: str = ""


def _num(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("nan")


def evaluate(
    symbol: str,
    ind: pd.DataFrame,
    cfg: StrategyConfig,
    in_trend: bool = False,
    entry_date: date | None = None,
    entry_price: float | None = None,
    stop_loss_pct: float = 0.0,
    current_price: float | None = None,
) -> SymbolSignal:
    """Evaluate the most recent completed bar.

    For a stock whose trend slice is held (`in_trend`), also check the exits, with
    `current_price` (e.g. intraday) used for the stops when given.
    """
    row = ind.iloc[-1]
    sig = SymbolSignal(
        symbol=symbol,
        date=ind.index[-1].date(),
        close=_num(row["close"]),
        atr=_num(row["atr"]),
        trend_up=bool(row["trend_up"]),
        avg_dollar_volume=_num(row["avg_dollar_volume"]),
    )
    ready = len(ind) >= min_history(cfg) and pd.notna(row["sma_slow"]) and pd.notna(row["atr"])
    if not ready:
        sig.ready = False
        sig.note = f"not enough history for trend signals ({len(ind)} bars, need {min_history(cfg)})"
    if in_trend:
        paid = entry_price if entry_price is not None else sig.close
        highest = highest_close_since(ind, entry_date, paid)
        stop, kind = stop_level(paid, highest, sig.atr, cfg, stop_loss_pct)
        sig.stop_price = stop if stop > 0 else None
        price = current_price if current_price is not None else sig.close
        if ready:
            sig.exit = trend_exit_reasons(row)
        if stop > 0 and price <= stop:
            sig.exit.append(f"{kind} hit (price {price:.2f} <= stop {stop:.2f})")
    return sig
