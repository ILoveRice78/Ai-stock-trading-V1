"""Monte Carlo test: run the strategy on many randomly generated markets.

Each simulated market has an index with a random yearly trend (mean +8%, sd 10%,
roughly the spread of real 3-year stock market averages) and random volatility
(12-30% a year), plus stocks that partly follow the index (beta 0.8-1.3) and partly
move on their own. Prices include overnight gaps and intraday highs and lows.

Prices are a random walk: there are no patterns for any strategy to exploit beyond
the overall drift, so this measures risk and the cost of trading, not skill. Only a
backtest on real prices can show whether the trend rules add anything.
"""

from __future__ import annotations

from dataclasses import replace
from multiprocessing import Pool
from typing import Any

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import Config

WARMUP_DAYS = 300
INDEX = "INDEX"


def make_market(seed: int, days: int, n_stocks: int) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex, float]:
    rng = np.random.default_rng(seed)
    n = WARMUP_DAYS + days
    idx = pd.bdate_range("2020-01-01", periods=n)
    trend = rng.normal(0.08, 0.10)
    m_sigma = rng.uniform(0.12, 0.30) / np.sqrt(252)
    market = rng.normal(trend / 252, m_sigma, n)

    def bars(daily_ret: np.ndarray, start_price: float, sigma_day: float) -> pd.DataFrame:
        overnight = daily_ret * 0.3 + rng.normal(0, sigma_day * 0.3, n)
        close = start_price * np.exp(np.cumsum(daily_ret))
        prev_close = np.concatenate([[start_price], close[:-1]])
        open_ = prev_close * np.exp(overnight)
        wick = np.abs(rng.normal(0, sigma_day * 0.5, (2, n)))
        return pd.DataFrame({
            "open": open_,
            "high": np.maximum(open_, close) * np.exp(wick[0]),
            "low": np.minimum(open_, close) * np.exp(-wick[1]),
            "close": close,
            "volume": np.full(n, 5_000_000.0),
        }, index=idx)

    data = {INDEX: bars(market, 100.0, m_sigma)}
    for k in range(n_stocks):
        beta = rng.uniform(0.8, 1.3)
        own_trend = rng.normal(0, 0.10) / 252
        own_sigma = rng.uniform(0.15, 0.35) / np.sqrt(252)
        ret = beta * market + rng.normal(own_trend, own_sigma, n)
        data[f"S{k:02d}"] = bars(ret, rng.uniform(40, 300), float(np.std(ret)))
    return data, idx, trend


def run_one(task: tuple[Config, int, int, int]) -> dict[str, Any]:
    cfg, seed, days, n_stocks = task
    data, idx, trend = make_market(seed, days, n_stocks)
    stocks = tuple(s for s in data if s != INDEX)
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=stocks, benchmark=INDEX))
    result = run_backtest(data, cfg, idx[WARMUP_DAYS].date(), idx[-1].date())
    s = result.stats
    return {
        "seed": seed,
        "market_trend": trend,
        "end_equity": s["end_equity"],
        "return": s["total_return"],
        "max_drawdown": s["max_drawdown"],
        "win_rate": s.get("win_rate", float("nan")),
        "trend_trades": s.get("trend_trades", 0),
        "avg_invested": s["avg_invested"],
        "index_return": result.benchmark_stats["total_return"],
        "index_drawdown": result.benchmark_stats["max_drawdown"],
        "hold_return": result.hold_stats["total_return"],
        "hold_drawdown": result.hold_stats["max_drawdown"],
    }


def simulate(
    cfg: Config, runs: int = 100, years: int = 3, seed: int = 0,
    n_stocks: int | None = None, processes: int | None = None,
) -> pd.DataFrame:
    n_stocks = n_stocks or len(cfg.watchlist.symbols)
    tasks = [(cfg, seed + k, 252 * years, n_stocks) for k in range(runs)]
    if processes == 1 or runs == 1:
        rows = [run_one(t) for t in tasks]
    else:
        with Pool(processes) as pool:
            rows = pool.map(run_one, tasks)
    return pd.DataFrame(rows)
