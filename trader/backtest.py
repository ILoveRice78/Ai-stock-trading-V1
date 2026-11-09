"""Portfolio backtester that uses the exact same planner as live trading.

Each day: fill yesterday's orders at today's open (respecting limit prices), let
stop losses fire during the day (as they would with the bot running several times a
day), mark the portfolio to market at the close, then build tomorrow's plan from
bars up to and including today. No future data is ever visible to the planner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .config import Config
from .planner import PlannedOrder, build_plan
from .portfolio import Account, Position
from .strategy import add_indicators, stop_level


@dataclass
class BacktestResult:
    equity: pd.Series
    benchmark: pd.Series | None
    trades: pd.DataFrame
    stats: dict[str, float]
    benchmark_stats: dict[str, float] | None


def curve_stats(equity: pd.Series) -> dict[str, float]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    first, last = float(equity.iloc[0]), float(equity.iloc[-1])
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    rets = equity.pct_change().dropna()
    std = float(rets.std())
    drawdown = equity / equity.cummax() - 1
    return {
        "start_equity": first,
        "end_equity": last,
        "total_return": last / first - 1,
        "cagr": (last / first) ** (1 / years) - 1 if last > 0 else -1.0,
        "max_drawdown": float(drawdown.min()),
        "sharpe": float(rets.mean()) / std * math.sqrt(252) if std > 0 else 0.0,
        "volatility": std * math.sqrt(252),
    }


def _fill_price(order: PlannedOrder, bar: pd.Series, slip: float) -> float | None:
    o, h, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
    limit = order.limit_price
    if order.side == "buy":
        if limit is None:
            return o * (1 + slip)
        if o <= limit:
            return min(o * (1 + slip), limit)
        return limit if low <= limit else None
    if limit is None:
        return o * (1 - slip)
    if o >= limit:
        return max(o * (1 - slip), limit)
    return limit if h >= limit else None


def run_backtest(
    bars: dict[str, pd.DataFrame], cfg: Config, start: date, end: date
) -> BacktestResult:
    indicators = {sym: add_indicators(df, cfg.strategy) for sym, df in bars.items() if not df.empty}
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    calendar = sorted(
        {d for df in indicators.values() for d in df.index if start_ts <= d <= end_ts}
    )
    if len(calendar) < 2:
        raise ValueError("not enough trading days in the backtest window")
    slip = cfg.orders.slippage_bps / 10_000

    cash = cfg.risk.starting_paper_cash
    positions: dict[str, Position] = {}
    pending: list[PlannedOrder] = []
    trades: list[dict] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    peak = prev_equity = cash
    last_close: dict[str, float] = {}
    last_atr: dict[str, float] = {}
    highest: dict[str, float] = {}  # highest close since entry, per held symbol

    def close_position(sym: str, day: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(sym)
        highest.pop(sym, None)
        cash += pos.quantity * price
        trades.append({
            "date": day, "symbol": sym, "side": "sell", "quantity": pos.quantity,
            "price": price, "reason": reason,
            "pnl": (price - pos.avg_cost) * pos.quantity,
            "return": price / pos.avg_cost - 1,
            "days_held": (day.date() - pos.entry_date).days if pos.entry_date else None,
        })

    for i, day in enumerate(calendar):
        # 1. execute yesterday's plan at today's open
        for order in pending:
            df = bars.get(order.symbol)
            if df is None or day not in df.index:
                continue
            price = _fill_price(order, df.loc[day], slip)
            if price is None:
                continue
            if order.side == "buy":
                qty = order.quantity
                if qty * price > cash:
                    qty = math.floor(cash / price) if not cfg.risk.fractional_shares else cash / price
                if qty <= 0:
                    continue
                cash -= qty * price
                positions[order.symbol] = Position(order.symbol, qty, price, entry_date=day.date())
                highest[order.symbol] = price
                trades.append({"date": day, "symbol": order.symbol, "side": "buy", "quantity": qty,
                               "price": price, "reason": order.reason})
            elif order.symbol in positions:
                close_position(order.symbol, day, price, order.reason)
        pending = []

        # 1b. stops fire during the day, including on positions bought this morning
        for sym in list(positions):
            df = bars.get(sym)
            if df is None or day not in df.index:
                continue
            pos = positions[sym]
            stop, kind = stop_level(
                pos.avg_cost, highest[sym], last_atr.get(sym, float("nan")),
                cfg.strategy, cfg.risk.stop_loss_pct,
            )
            bar = df.loc[day]
            if float(bar["open"]) <= stop:
                hit = float(bar["open"])  # gapped through the stop
            elif float(bar["low"]) <= stop:
                hit = stop
            else:
                continue
            close_position(sym, day, hit * (1 - slip), f"{kind} hit intraday")

        # 2. mark to market at the close
        frames = {}
        for sym, ind in indicators.items():
            n = int(ind.index.searchsorted(day, side="right"))
            if n == 0:
                continue
            frames[sym] = ind.iloc[:n]
            last_close[sym] = float(ind["close"].iloc[n - 1])
            last_atr[sym] = float(ind["atr"].iloc[n - 1])
        for sym in positions:
            highest[sym] = max(highest[sym], last_close.get(sym, highest[sym]))
        equity = cash + sum(p.quantity * last_close.get(s, p.avg_cost) for s, p in positions.items())
        equity_points.append((day, equity))
        peak = max(peak, equity)

        # 3. plan the next session using only data up to today
        if i + 1 < len(calendar):
            account = Account(cash=cash, positions=dict(positions), source="backtest")
            plan = build_plan(
                account, frames, cfg,
                session_date=calendar[i + 1].date(),
                day_start_equity=prev_equity,
                peak_equity=peak,
            )
            pending = plan.orders
        prev_equity = equity

    equity_curve = pd.Series(dict(equity_points), name="equity").sort_index()
    trades_df = pd.DataFrame(trades)
    stats = curve_stats(equity_curve)
    stats.update(_trade_stats(trades_df))
    stats["time_in_market"] = 1 - _fraction_of_days_flat(trades_df, equity_curve)

    bench_sym = cfg.watchlist.benchmark
    benchmark = benchmark_stats = None
    if bench_sym in bars:
        closes = bars[bench_sym]["close"]
        closes = closes[(closes.index >= calendar[0]) & (closes.index <= calendar[-1])]
        if len(closes) > 1:
            benchmark = closes / closes.iloc[0] * cfg.risk.starting_paper_cash
            benchmark.name = bench_sym
            benchmark_stats = curve_stats(benchmark)
    return BacktestResult(equity_curve, benchmark, trades_df, stats, benchmark_stats)


def _trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty or "pnl" not in trades:
        return {"closed_trades": 0}
    closed = trades.dropna(subset=["pnl"])
    wins, losses = closed[closed["pnl"] > 0], closed[closed["pnl"] <= 0]
    gross_loss = float(-losses["pnl"].sum())
    return {
        "closed_trades": len(closed),
        "win_rate": len(wins) / len(closed) if len(closed) else 0.0,
        "avg_win": float(wins["return"].mean()) if len(wins) else 0.0,
        "avg_loss": float(losses["return"].mean()) if len(losses) else 0.0,
        "profit_factor": float(wins["pnl"].sum()) / gross_loss if gross_loss > 0 else float("inf"),
        "avg_days_held": float(closed["days_held"].mean()) if len(closed) else 0.0,
    }


def _fraction_of_days_flat(trades: pd.DataFrame, equity: pd.Series) -> float:
    """Fraction of days with no open positions."""
    if trades.empty:
        return 1.0
    holding = pd.Series(0, index=equity.index)
    open_count = 0
    events = trades.groupby("date")["side"].apply(list)
    for day in equity.index:
        for side in events.get(day, []):
            open_count += 1 if side == "buy" else -1
        holding.loc[day] = open_count
    return float(np.mean(holding.values == 0))
