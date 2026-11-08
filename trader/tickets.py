"""Approved order tickets.

`python -m trader plan` writes one ticket per risk-checked order. In live mode the
trade guard hook only lets a Robinhood order through if it matches an open ticket,
so Claude cannot place an order the rules did not approve.

Standard library only (imported by the trade guard hook).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from . import state

OPEN = "approved"


def load_tickets() -> list[dict[str, Any]]:
    return state.read_json(state.tickets_path(), default=[])


def save_tickets(tickets: list[dict[str, Any]]) -> None:
    state.write_json(state.tickets_path(), tickets)


def is_open(ticket: dict[str, Any], now: datetime) -> bool:
    return ticket.get("status") == OPEN and datetime.fromisoformat(ticket["expires_at"]) > now


def issue_tickets(orders: list[dict[str, Any]], mode: str, ttl_minutes: int) -> list[dict[str, Any]]:
    """Replace any still-open tickets with a fresh set for this plan."""
    now = state.utcnow()
    tickets = load_tickets()
    for t in tickets:
        if t.get("status") == OPEN:
            t["status"] = "superseded"
    new = []
    for order in orders:
        ticket = {
            "id": uuid.uuid4().hex[:10],
            "mode": mode,
            "status": OPEN,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
            **order,
        }
        new.append(ticket)
    # keep a bounded history so the file stays small
    save_tickets((tickets + new)[-500:])
    for t in new:
        state.log_event("ticket_issued", **{k: v for k, v in t.items() if k != "status"})
    return new


def open_tickets(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or state.utcnow()
    return [t for t in load_tickets() if is_open(t, now)]


def update_ticket(ticket_id: str, **changes: Any) -> dict[str, Any] | None:
    tickets = load_tickets()
    for t in tickets:
        if t["id"] == ticket_id:
            t.update(changes)
            save_tickets(tickets)
            return t
    return None


def veto(symbol: str, reason: str) -> list[dict[str, Any]]:
    """Cancel open BUY tickets for a symbol (Claude's judgment step)."""
    symbol = symbol.upper()
    now = state.utcnow()
    tickets = load_tickets()
    vetoed = []
    for t in tickets:
        if t["symbol"] == symbol and t["side"] == "buy" and is_open(t, now):
            t["status"] = "vetoed"
            t["veto_reason"] = reason
            vetoed.append(t)
    if vetoed:
        save_tickets(tickets)
        state.log_event("veto", symbol=symbol, reason=reason, tickets=[t["id"] for t in vetoed])
    return vetoed
