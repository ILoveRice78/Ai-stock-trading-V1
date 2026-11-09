"""Turn signals + account state into a concrete, risk-checked order plan.

This is a pure function of its inputs, so the live bot, paper trading and the
backtester all use exactly the same rules. It is safe to run several times a day:
stop losses are checked against the current price on every run.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .config import Config
from .portfolio import Account
from .strategy import SymbolSignal, evaluate

STALE_DATA_DAYS = 5  # skip a symbol if its latest bar is older than this


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
    stop_price: float | None = None
    score: float | None = None


@dataclass
class Plan:
    session_date: date
    equity: float
    cash: float
    orders: list[PlannedOrder] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)
    signals: dict[str, SymbolSignal] = field(default_factory=dict)


def _round_qty(qty: float, fractional: bool) -> float:
    if qty <= 0 or math.isnan(qty):
        return 0.0
    if fractional:
        return math.floor(qty * 10_000) / 10_000
    return float(math.floor(qty))


def build_plan(
    account: Account,
    frames: dict[str, pd.DataFrame],
    cfg: Config,
    session_date: date,
    day_start_equity: float,
    peak_equity: float,
    current_prices: dict[str, float] | None = None,
    no_buy: dict[str, str] | None = None,
) -> Plan:
    """Build the orders for this run.

    frames: indicator frames (output of strategy.add_indicators) per symbol, ending
            at the last *completed* daily bar.
    session_date: the trading day the orders will be placed in.
    current_prices: latest prices (e.g. intraday) used to check stop losses.
    no_buy: symbols that must not be bought this run, with the reason
            (e.g. sold earlier today, or a buy order is already pending).
    """
    risk, orders_cfg = cfg.risk, cfg.orders
    current_prices = current_prices or {}
    no_buy = no_buy or {}
    buffer = orders_cfg.limit_buffer_pct / 100.0
    use_limit = orders_cfg.order_type == "limit"

    prices = {sym: float(df["close"].iloc[-1]) for sym, df in frames.items() if not df.empty}
    prices.update(current_prices)
    equity = account.equity(prices)
    plan = Plan(session_date=session_date, equity=equity, cash=account.cash)

    # 1. Exits first: protecting capital beats finding new trades.
    for sym, pos in sorted(account.positions.items()):
        frame = frames.get(sym)
        if frame is None or frame.empty:
            plan.skipped.append((sym, "held, but no price data; cannot evaluate exit"))
            continue
        sig = evaluate(
            sym, frame, cfg.strategy, held=True, entry_date=pos.entry_date,
            entry_price=pos.avg_cost, stop_loss_pct=risk.stop_loss_pct,
            current_price=current_prices.get(sym),
        )
        plan.signals[sym] = sig
        if not sig.exit:
            continue
        price = current_prices.get(sym, sig.close)
        limit = round(price * (1 - buffer), 2) if use_limit else None
        plan.orders.append(
            PlannedOrder(
                symbol=sym,
                side="sell",
                quantity=pos.quantity,
                order_type=orders_cfg.order_type,
                limit_price=limit,
                ref_price=price,
                notional=pos.quantity * price,
                reason="; ".join(sig.exit),
            )
        )

    # 2. Circuit breakers that stop new buys.
    if not cfg.general.trading_enabled:
        plan.halts.append("trading_enabled = false in config.toml")
    if day_start_equity > 0:
        day_change = equity / day_start_equity - 1
        if day_change <= -risk.daily_loss_limit_pct / 100:
            plan.halts.append(
                f"daily loss limit: equity {day_change:.1%} today (limit -{risk.daily_loss_limit_pct}%)"
            )
    if peak_equity > 0:
        drawdown = equity / peak_equity - 1
        if drawdown <= -risk.max_drawdown_pct / 100:
            plan.halts.append(
                f"max drawdown: equity {drawdown:.1%} below peak (limit -{risk.max_drawdown_pct}%)"
            )

    # 3. Entries, best-ranked first.
    candidates: list[SymbolSignal] = []
    for sym in cfg.watchlist.symbols:
        if sym in account.positions:
            continue
        frame = frames.get(sym)
        if frame is None or frame.empty:
            plan.skipped.append((sym, "no price data"))
            continue
        sig = evaluate(sym, frame, cfg.strategy)
        plan.signals[sym] = sig
        if not sig.ready:
            plan.skipped.append((sym, sig.note))
            continue
        if not sig.entry:
            continue
        if sym in no_buy:
            plan.skipped.append((sym, f"buy signal, but {no_buy[sym]}"))
            continue
        if (session_date - sig.date).days > STALE_DATA_DAYS:
            plan.skipped.append((sym, f"price data is stale (last bar {sig.date})"))
            continue
        if sig.close < risk.min_price:
            plan.skipped.append((sym, f"price {sig.close:.2f} below min_price {risk.min_price}"))
            continue
        if not sig.avg_dollar_volume >= risk.min_avg_dollar_volume:
            plan.skipped.append((sym, "average dollar volume too low (illiquid)"))
            continue
        current = current_prices.get(sym)
        if current is not None and current <= sig.close * (1 - risk.stop_loss_pct / 100):
            plan.skipped.append((sym, f"buy signal, but price already fell {risk.stop_loss_pct}% since"))
            continue
        candidates.append(sig)
    candidates.sort(key=lambda s: s.score, reverse=True)

    slots = risk.max_open_positions - len(account.positions)
    available_cash = account.cash - equity * risk.min_cash_reserve_pct / 100
    slippage = orders_cfg.slippage_bps / 10_000
    stop_fraction = risk.stop_loss_pct / 100

    for sig in candidates:
        why = "; ".join(sig.entry)
        if plan.halts:
            plan.skipped.append((sig.symbol, f"buy signal ({why}) but new buys halted"))
            continue
        if slots <= 0:
            plan.skipped.append((sig.symbol, f"buy signal ({why}) but max open positions reached"))
            continue
        limit = round(sig.close * (1 + buffer), 2) if use_limit else None
        est_price = limit if limit is not None else sig.close * (1 + slippage)
        # size so that hitting the stop loss costs about risk_per_trade_pct of equity
        qty_by_risk = equity * risk.risk_per_trade_pct / 100 / (est_price * stop_fraction)
        qty_by_size = equity * risk.max_position_pct / 100 / est_price
        qty_by_cash = max(0.0, available_cash) / est_price
        qty = _round_qty(min(qty_by_risk, qty_by_size, qty_by_cash), risk.fractional_shares)
        if qty <= 0 or qty * est_price < 1.0:
            hint = "" if risk.fractional_shares else " (try fractional_shares = true)"
            plan.skipped.append((sig.symbol, f"buy signal but not enough cash for a position{hint}"))
            continue
        notional = qty * est_price
        plan.orders.append(
            PlannedOrder(
                symbol=sig.symbol,
                side="buy",
                quantity=qty,
                order_type=orders_cfg.order_type,
                limit_price=limit,
                ref_price=sig.close,
                notional=notional,
                reason=why,
                stop_price=round(est_price * (1 - stop_fraction), 2),
                score=sig.score,
            )
        )
        available_cash -= notional
        slots -= 1

    return plan
