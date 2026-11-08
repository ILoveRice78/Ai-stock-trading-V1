"""Simulated execution of approved tickets against the paper portfolio."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import state, tickets
from .config import Config
from .portfolio import Position, load_paper_account, save_paper_account

DUST = 1e-6


def paper_fill(cfg: Config, prices: dict[str, float], now: datetime | None = None) -> list[dict[str, Any]]:
    """Fill every open paper ticket at the latest price (with slippage and limit checks)."""
    now = now or state.utcnow()
    trade_date = now.astimezone(ZoneInfo(cfg.general.timezone)).date()
    slip = cfg.orders.slippage_bps / 10_000
    account = load_paper_account(cfg)
    results = []

    for t in tickets.open_tickets(now):
        if t.get("mode") != "paper":
            continue
        sym, side, limit = t["symbol"], t["side"], t.get("limit_price")
        price = prices.get(sym)

        def unfilled(reason: str) -> None:
            tickets.update_ticket(t["id"], status="unfilled", unfilled_reason=reason)
            results.append({"ticket": t["id"], "symbol": sym, "side": side, "status": "unfilled", "reason": reason})

        if price is None:
            unfilled("no current price")
            continue

        if side == "buy":
            if limit is not None and price > limit:
                unfilled(f"price {price:.2f} is above limit {limit:.2f}")
                continue
            fill = price * (1 + slip)
            if limit is not None:
                fill = min(fill, limit)
            qty = float(t["quantity"])
            cost = qty * fill
            if cost > account.cash + DUST:
                unfilled(f"not enough paper cash ({account.cash:.2f} < {cost:.2f})")
                continue
            pos = account.positions.get(sym)
            if pos:
                total = pos.quantity + qty
                pos.avg_cost = (pos.avg_cost * pos.quantity + fill * qty) / total
                pos.quantity = total
            else:
                account.positions[sym] = Position(sym, qty, fill, entry_date=trade_date)
            account.cash -= cost
            realized = None
            closes = False
        else:
            pos = account.positions.get(sym)
            if pos is None:
                unfilled("no paper position to sell")
                continue
            if limit is not None and price < limit:
                unfilled(f"price {price:.2f} is below limit {limit:.2f}")
                continue
            fill = price * (1 - slip)
            if limit is not None:
                fill = max(fill, limit)
            qty = min(float(t["quantity"]), pos.quantity)
            realized = (fill - pos.avg_cost) * qty
            account.cash += qty * fill
            pos.quantity -= qty
            closes = pos.quantity <= DUST
            if closes:
                del account.positions[sym]

        tickets.update_ticket(t["id"], status="filled", fill_price=round(fill, 4), filled_at=now.isoformat())
        state.log_event(
            "fill",
            mode="paper",
            ticket_id=t["id"],
            symbol=sym,
            side=side,
            quantity=qty,
            price=round(fill, 4),
            realized_pnl=None if realized is None else round(realized, 2),
            closes_position=closes,
            trade_date=trade_date.isoformat(),
            reason=t.get("reason", ""),
        )
        results.append(
            {"ticket": t["id"], "symbol": sym, "side": side, "status": "filled", "quantity": qty,
             "price": fill, "realized_pnl": realized}
        )

    save_paper_account(account)
    return results
