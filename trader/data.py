"""Daily price data: Yahoo Finance (via yfinance) or local CSV/JSON files.

File loading accepts plain OHLCV CSVs and the JSON shape Robinhood uses for
historicals (begins_at / open_price / close_price / ...), so bars fetched with the
Robinhood MCP `get_equity_historicals` tool can be saved and used directly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from . import state
from .config import PROJECT_ROOT, Config

MARKET_CLOSE = time(16, 0)
CACHE_TTL = timedelta(minutes=15)

_ALIASES = {
    "date": ("date", "begins_at", "timestamp", "time", "datetime", "day"),
    "open": ("open", "open_price"),
    "high": ("high", "high_price"),
    "low": ("low", "low_price"),
    "close": ("close", "close_price", "adj_close", "adjclose"),
    "volume": ("volume",),
}


class DataError(RuntimeError):
    pass


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame indexed by naive date with float open/high/low/close/volume."""
    df = df.copy()
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    if not isinstance(df.index, pd.DatetimeIndex):
        date_col = next((c for c in _ALIASES["date"] if c in df.columns), None)
        if date_col is None:
            raise DataError(f"no date column found (columns: {list(df.columns)})")
        df = df.set_index(date_col)
    out = pd.DataFrame(index=df.index)
    for target in ("open", "high", "low", "close", "volume"):
        source = next((c for c in _ALIASES[target] if c in df.columns), None)
        if source is None:
            raise DataError(f"no {target} column found (columns: {list(df.columns)})")
        out[target] = pd.to_numeric(df[source], errors="coerce")
    idx = pd.to_datetime(out.index, utc=False)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    out.index = pd.DatetimeIndex(idx).normalize()
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    return out.dropna(subset=["close"])


def load_bars_file(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".json":
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in ("historicals", "bars", "data", "results", "candles"):
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
        if not isinstance(raw, list):
            raise DataError(f"{path}: expected a list of bars")
        return normalize_bars(pd.DataFrame(raw))
    return normalize_bars(pd.read_csv(path))


def _files_source(symbol: str, cfg: Config) -> pd.DataFrame:
    base = Path(cfg.data.files_dir)
    if not base.is_absolute():
        base = PROJECT_ROOT / base
    for ext in (".csv", ".json"):
        path = base / f"{symbol}{ext}"
        if path.exists():
            return load_bars_file(path)
    raise DataError(f"no bar file for {symbol} in {base} (expected {symbol}.csv or {symbol}.json)")


def _yfinance_source(symbol: str, start: date, end: date | None) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise DataError("yfinance is not installed; run `pip install -r requirements.txt`") from exc
    kwargs: dict[str, Any] = {"start": start.isoformat(), "auto_adjust": True, "interval": "1d"}
    if end is not None:
        kwargs["end"] = (end + timedelta(days=1)).isoformat()
    try:
        hist = yf.Ticker(symbol).history(**kwargs)
    except Exception as exc:  # yfinance raises many network/parse error types
        raise DataError(f"Yahoo Finance download failed for {symbol}: {exc}") from exc
    if hist is None or hist.empty:
        raise DataError(f"Yahoo Finance returned no data for {symbol}")
    return normalize_bars(hist)


def _cache_path(symbol: str, start: date, end: date | None) -> Path:
    tag = f"{start.isoformat()}_{end.isoformat() if end else 'latest'}"
    return state.state_dir() / "cache" / f"{symbol}_{tag}.csv"


def get_bars(
    symbol: str, cfg: Config, start: date | None = None, end: date | None = None
) -> pd.DataFrame:
    symbol = symbol.upper()
    if cfg.data.source == "files":
        df = _files_source(symbol, cfg)
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        return df
    start = start or (date.today() - timedelta(days=cfg.data.history_days))
    cache = _cache_path(symbol, start, end)
    if cache.exists():
        age = datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)
        if end is not None or age < CACHE_TTL:
            return load_bars_file(cache)
    df = _yfinance_source(symbol, start, end)
    cache.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache)
    return df


def split_completed(df: pd.DataFrame, now: datetime, tz: str) -> tuple[pd.DataFrame, float | None]:
    """Split off today's still-forming bar.

    Returns (completed_bars, latest_price). During market hours the last row from
    Yahoo is a partial bar for today; signals must only use completed bars.
    """
    if df.empty:
        return df, None
    local_now = now.astimezone(ZoneInfo(tz))
    latest_price = float(df["close"].iloc[-1])
    last_day = df.index[-1].date()
    if last_day >= local_now.date() and local_now.time() < MARKET_CLOSE:
        return df.iloc[:-1], latest_price
    return df, latest_price
