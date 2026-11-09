"""Portfolio backtester that uses the exact same planner as live trading.

Each day: fill yesterday's orders at today's open (respecting limit prices; sells
before buys), mark the portfolio to market at the close, then plan the next session
from bars up to and including today. No future data is ever visible to the planner.
Stops are checked once a day at the close here; live runs check them on every run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .config import Config
from .planner import PlannedOrder, TrendTrade, build_plan, empty_trend_book
from .portfolio import Account, Position
from .strategy import add_indicators


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame  # every fill
    trend_trades: list[TrendTrade]
    stats: dict[str, float]
    benchmark: pd.Series | None
    benchmark_stats: dict[str, float] | None
    hold: pd.Series | None  # buy & hold the same watchlist stocks, equal amounts
    hold_stats: dict[str, float] | None


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


def trend_trade_stats(trades: list[TrendTrade]) -> dict[str, float]:
    if not trades:
        return {"trend_trades": 0}
    rets = [t.ret for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    return {
        "trend_trades": len(rets),
        "win_rate": len(wins) / len(rets),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
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


def _buy_and_hold(bars: dict[str, pd.DataFrame], symbols: list[str], calendar: list, cash: float):
    closes = []
    for sym in symbols:
        if sym not in bars:
            continue
        c = bars[sym]["close"].reindex(calendar).ffill()
        if pd.notna(c.iloc[0]):
            closes.append(c / c.iloc[0])
    if not closes:
        return None
    return pd.concat(closes, axis=1).mean(axis=1) * cash


def run_backtest(
    bars: dict[str, pd.DataFrame], cfg: Config, start: date, end: date
) -> BacktestResult:
    indicators = {sym: add_indicators(df, cfg.strategy) for sym, df in bars.items() if not df.empty}
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    calendar = sorted({d for df in indicators.values() for d in df.index if start_ts <= d <= end_ts})
    if len(calendar) < 2:
        raise ValueError("not enough trading days in the backtest window")
    slip = cfg.orders.slippage_bps / 10_000
    start_cash = cfg.risk.starting_paper_cash

    cash = start_cash
    positions: dict[str, Position] = {}
    pending: list[PlannedOrder] = []
    book = empty_trend_book()
    fills: list[dict] = []
    trend_trades: list[TrendTrade] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    invested: list[float] = []
    peak = prev_equity = cash
    last_close: dict[str, float] = {}

    for i, day in enumerate(calendar):
        # 1. execute yesterday's plan at today's open (sells come first in the plan)
        for order in pending:
            df = bars.get(order.symbol)
            if df is None or day not in df.index:
                continue
            price = _fill_price(order, df.loc[day], slip)
            if price is None:
                continue
            pos = positions.get(order.symbol)
            if order.side == "buy":
                qty = order.quantity
                if qty * price > cash:
                    qty = cash / price if cfg.risk.fractional_shares else math.floor(cash / price)
                if qty <= 0:
                    continue
                cash -= qty * price
                if pos:
                    pos.avg_cost = (pos.avg_cost * pos.quantity + price * qty) / (pos.quantity + qty)
                    pos.quantity += qty
                else:
                    positions[order.symbol] = Position(order.symbol, qty, price, entry_date=day.date())
            else:
                if pos is None:
                    continue
                qty = min(order.quantity, pos.quantity)
                cash += qty * price
                pos.quantity -= qty
                if pos.quantity <= 1e-9:
                    del positions[order.symbol]
            fills.append({"date": day, "symbol": order.symbol, "side": order.side,
                          "quantity": qty, "price": price, "reason": order.reason})
        pending = []

        # 2. mark to market at the close
        frames = {}
        for sym, ind in indicators.items():
            n = int(ind.index.searchsorted(day, side="right"))
            if n == 0:
                continue
            frames[sym] = ind.iloc[:n]
            last_close[sym] = float(ind["close"].iloc[n - 1])
        equity = cash + sum(p.quantity * last_close.get(s, p.avg_cost) for s, p in positions.items())
        equity_points.append((day, equity))
        invested.append(1 - cash / equity if equity > 0 else 0.0)
        peak = max(peak, equity)

        # 3. plan the next session using only data up to today
        if i + 1 < len(calendar):
            plan = build_plan(
                Account(cash=cash, positions=dict(positions), source="backtest"), frames, cfg,
                session_date=calendar[i + 1].date(), day_start_equity=prev_equity,
                peak_equity=peak, trend_book=book,
            )
            book = plan.trend_book
            trend_trades.extend(plan.trend_exits)
            pending = plan.orders
        prev_equity = equity

    equity_curve = pd.Series(dict(equity_points), name="equity").sort_index()
    stats = curve_stats(equity_curve)
    stats.update(trend_trade_stats(trend_trades))
    stats["orders_filled"] = len(fills)
    stats["avg_invested"] = sum(invested) / len(invested)

    benchmark = benchmark_stats = None
    bench_sym = cfg.watchlist.benchmark
    if bench_sym in bars:
        benchmark = _buy_and_hold(bars, [bench_sym], calendar, start_cash)
        benchmark_stats = curve_stats(benchmark) if benchmark is not None else None
    hold = _buy_and_hold(bars, list(cfg.watchlist.symbols), calendar, start_cash)
    hold_stats = curve_stats(hold) if hold is not None else None
    return BacktestResult(equity_curve, pd.DataFrame(fills), trend_trades, stats,
                          benchmark, benchmark_stats, hold, hold_stats)
