"""Turn signals + account state into target weights and the orders to reach them.

Every watchlist stock has a target share of the account:

* core:  core_pct / N, always held (N = number of watchlist stocks);
* trend: trend_pct / N extra, while the stock is in the trend book.

Each run the planner updates the trend book (exits, then entries), then trades each
stock toward its target, but only when it is more than `rebalance_band_pct` of one
core slice away, so small price drifts don't cause trades. Stocks not on the
watchlist are sold.

This is a pure function of its inputs, so live trading, paper trading and the
backtester all use exactly the same rules.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from .config import Config, RiskConfig
from .portfolio import Account
from .strategy import SymbolSignal, evaluate

STALE_DATA_DAYS = 5  # don't buy a symbol whose latest bar is older than this


def empty_trend_book() -> dict[str, Any]:
    """{"positions": {sym: {"entry_price", "entry_date"}}, "last_exit": {sym: iso date}}"""
    return {"positions": {}, "last_exit": {}}


@dataclass
class TrendTrade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    reason: str

    @property
    def ret(self) -> float:
        return self.exit_price / self.entry_price - 1


@dataclass
class PlannedOrder:
    symbol: str
    side: str  # "buy" | "sell"
    quantity: float
    order_type: str  # "limit" | "market"
    limit_price: float | None
    ref_price: float
    notional: float
    reason: str
    closes_position: bool = False


@dataclass
class Plan:
    session_date: date
    equity: float
    cash: float
    orders: list[PlannedOrder] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)
    signals: dict[str, SymbolSignal] = field(default_factory=dict)
    targets: dict[str, float] = field(default_factory=dict)  # target weight per symbol
    weights: dict[str, float] = field(default_factory=dict)  # current weight per symbol
    trend_book: dict[str, Any] = field(default_factory=empty_trend_book)
    trend_entries: list[str] = field(default_factory=list)
    trend_exits: list[TrendTrade] = field(default_factory=list)


def _round_qty(qty: float, fractional: bool) -> float:
    if qty <= 0 or math.isnan(qty):
        return 0.0
    if fractional:
        return math.floor(qty * 10_000) / 10_000
    return float(math.floor(qty))


def _not_buyable(sig: SymbolSignal, risk: RiskConfig, session_date: date) -> str | None:
    if (session_date - sig.date).days > STALE_DATA_DAYS:
        return f"price data is stale (last bar {sig.date})"
    if sig.close < risk.min_price:
        return f"price {sig.close:.2f} below min_price {risk.min_price}"
    if not sig.avg_dollar_volume >= risk.min_avg_dollar_volume:
        return "average dollar volume too low (illiquid)"
    return None


def _halts(cfg: Config, equity: float, day_start_equity: float, peak_equity: float) -> list[str]:
    risk, halts = cfg.risk, []
    if not cfg.general.trading_enabled:
        halts.append("trading_enabled = false in config.toml")
    if day_start_equity > 0:
        change = equity / day_start_equity - 1
        if change <= -risk.daily_loss_limit_pct / 100:
            halts.append(f"daily loss limit: equity {change:.1%} today (limit -{risk.daily_loss_limit_pct}%)")
    if peak_equity > 0:
        drawdown = equity / peak_equity - 1
        if drawdown <= -risk.max_drawdown_pct / 100:
            halts.append(f"max drawdown: equity {drawdown:.1%} below peak (limit -{risk.max_drawdown_pct}%)")
    return halts


def build_plan(
    account: Account,
    frames: dict[str, pd.DataFrame],
    cfg: Config,
    session_date: date,
    day_start_equity: float,
    peak_equity: float,
    trend_book: dict[str, Any] | None = None,
    current_prices: dict[str, float] | None = None,
    no_buy: dict[str, str] | None = None,
) -> Plan:
    """Build the orders for this run.

    frames: indicator frames (strategy.add_indicators) per symbol, ending at the last
            *completed* daily bar.
    session_date: the trading day the orders will be placed in.
    trend_book: which stocks currently hold a trend slice (see empty_trend_book).
    current_prices: latest prices (e.g. intraday) for stops and sizing.
    no_buy: symbols that must not be bought this run, with the reason.
    """
    pf, risk, ocfg = cfg.portfolio, cfg.risk, cfg.orders
    current_prices = current_prices or {}
    no_buy = no_buy or {}
    book = copy.deepcopy(trend_book) if trend_book else empty_trend_book()
    trend_pos, last_exit = book["positions"], book["last_exit"]
    watch = list(cfg.watchlist.symbols)
    core_w = pf.core_pct / 100 / len(watch)
    trend_w = pf.trend_pct / 100 / len(watch)
    buffer = ocfg.limit_buffer_pct / 100
    slip = ocfg.slippage_bps / 10_000
    use_limit = ocfg.order_type == "limit"

    prices = {sym: float(df["close"].iloc[-1]) for sym, df in frames.items() if not df.empty}
    prices.update(current_prices)
    equity = account.equity(prices)
    plan = Plan(session_date=session_date, equity=equity, cash=account.cash)
    plan.halts = _halts(cfg, equity, day_start_equity, peak_equity)
    halted = bool(plan.halts)
    events: dict[str, str] = {}

    # 1. Trend exits.
    for sym in sorted(trend_pos):
        info = trend_pos[sym]
        frame = frames.get(sym)
        if sym not in watch:
            reasons = ["removed from watchlist"]
        elif trend_w == 0:
            reasons = ["trend part turned off (trend_pct = 0)"]
        elif frame is None or frame.empty:
            plan.skipped.append((sym, "trend slice held, but no price data; cannot check exits"))
            continue
        else:
            sig = evaluate(
                sym, frame, cfg.strategy, in_trend=True,
                entry_date=date.fromisoformat(info["entry_date"]),
                entry_price=float(info["entry_price"]),
                stop_loss_pct=risk.stop_loss_pct,
                current_price=current_prices.get(sym),
            )
            plan.signals[sym] = sig
            reasons = sig.exit
        if not reasons:
            continue
        del trend_pos[sym]
        last_exit[sym] = session_date.isoformat()
        exit_price = prices.get(sym, float(info["entry_price"]))
        plan.trend_exits.append(TrendTrade(
            sym, info["entry_date"], float(info["entry_price"]),
            session_date.isoformat(), exit_price, "; ".join(reasons),
        ))
        events[sym] = "trend exit: " + "; ".join(reasons)

    # 2. Trend entries: every buyable watchlist stock in an uptrend.
    for sym in watch:
        frame = frames.get(sym)
        if frame is None or frame.empty:
            plan.skipped.append((sym, "no price data"))
            continue
        if sym not in plan.signals:
            plan.signals[sym] = evaluate(sym, frame, cfg.strategy)
        sig = plan.signals[sym]
        if trend_w == 0 or sym in trend_pos or sym in events or not sig.ready or not sig.trend_up:
            if not sig.ready:
                plan.skipped.append((sym, sig.note))
            continue
        last = last_exit.get(sym)
        wait = max(1, pf.reentry_wait_days)  # never re-enter on the day of an exit
        if last and (session_date - date.fromisoformat(last)).days < wait:
            plan.skipped.append((sym, f"in uptrend, but its trend slice was sold on {last}; waiting"))
            continue
        blocker = (
            "new buys halted" if halted else no_buy.get(sym) or _not_buyable(sig, risk, session_date)
        )
        if blocker:
            plan.skipped.append((sym, f"in uptrend, but {blocker}"))
            continue
        trend_pos[sym] = {"entry_price": prices[sym], "entry_date": session_date.isoformat()}
        plan.trend_entries.append(sym)
        events[sym] = "trend entry: in uptrend"

    # 3. Targets.
    plan.targets = {sym: core_w + (trend_w if sym in trend_pos else 0.0) for sym in watch}
    for sym in account.positions:
        plan.targets.setdefault(sym, 0.0)
    band = pf.rebalance_band_pct / 100 * (core_w or trend_w) * equity

    # 4. Orders: sells first, then buys with the cash that leaves.
    sells: list[PlannedOrder] = []
    buys: list[tuple[float, str]] = []
    for sym, target in sorted(plan.targets.items()):
        price = prices.get(sym)
        held = account.positions[sym].quantity if sym in account.positions else 0.0
        if price is None or price <= 0:
            if held:
                plan.skipped.append((sym, "held, but no price; cannot rebalance"))
            continue
        current = held * price
        plan.weights[sym] = current / equity if equity > 0 else 0.0
        diff = target * equity - current
        event = events.get(sym, "")
        if target == 0 and held > 0:
            qty = held
            events.setdefault(sym, "not on the watchlist")
        elif event.startswith("trend exit") and diff < 0:
            # a stop or trend exit always sells the trend slice, however small the drift
            qty = min(held, _round_qty(-diff / price, risk.fractional_shares))
        elif event.startswith("trend entry") and diff > 0:
            buys.append((diff, sym))
            continue
        elif event or abs(diff) < band:
            continue
        elif diff < 0:
            qty = min(held, _round_qty(-diff / price, risk.fractional_shares))
        else:
            buys.append((diff, sym))
            continue
        if qty <= 0:
            continue
        limit = round(price * (1 - buffer), 2) if use_limit else None
        reason = events.get(sym, f"rebalance to {target:.1%} (now {current / equity:.1%})")
        sells.append(PlannedOrder(
            sym, "sell", qty, ocfg.order_type, limit, price, qty * price, reason,
            closes_position=qty >= held - 1e-9,
        ))
    plan.orders.extend(sells)

    proceeds = sum(o.quantity * (o.limit_price or o.ref_price * (1 - slip)) for o in sells)
    available = account.cash + proceeds - equity * risk.min_cash_reserve_pct / 100
    for diff, sym in sorted(buys, reverse=True):
        sig = plan.signals.get(sym)
        blocker = (
            "no price data" if sig is None else
            "new buys halted" if halted else
            no_buy.get(sym) or _not_buyable(sig, risk, session_date)
        )
        if blocker:
            plan.skipped.append((sym, f"below target, but {blocker}"))
            continue
        price = prices[sym]
        limit = round(price * (1 + buffer), 2) if use_limit else None
        est = limit if limit is not None else price * (1 + slip)
        affordable = max(0.0, available)
        qty = _round_qty(min(diff, affordable) / est, risk.fractional_shares)
        if qty <= 0 or qty * est < 1.0:
            if affordable < 1.0:
                why = "no cash left after the cash reserve"
            elif not risk.fractional_shares and est > min(diff, affordable):
                why = (f"one share (${est:,.2f}) costs more than the "
                       f"{'gap to its target' if diff <= affordable else 'cash left'} "
                       "(set fractional_shares = true)")
            else:
                why = "not enough cash"
            plan.skipped.append((sym, f"below target, but {why}"))
            continue
        target = plan.targets[sym]
        reason = events.get(sym, f"rebalance to {target:.1%} (now {plan.weights[sym]:.1%})")
        plan.orders.append(PlannedOrder(sym, "buy", qty, ocfg.order_type, limit, price, qty * est, reason))
        available -= qty * est

    plan.trend_book = book
    return plan
