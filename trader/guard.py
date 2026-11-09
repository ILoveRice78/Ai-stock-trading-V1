"""Trade guard: a Claude Code PreToolUse hook for every Robinhood MCP tool call.

Claude Code runs this before any `mcp__*robinhood*` tool (see .claude/settings.json)
and passes the tool call as JSON on stdin. Exit code 0 lets the call proceed;
exit code 2 blocks it and shows our message to Claude.

Rules:
* Read-only tools (get_*, list_*, ...) and cancels are always allowed.
* Stock orders are allowed only in live mode, with no kill switch, and only if they
  match an open ticket from `python -m trader plan` (same symbol and side, no more
  shares, no worse limit price).
* Anything else that could move money (options, crypto, transfers, unknown write
  tools) is blocked.
* Any error inside the guard blocks the call (fail closed).

Standard library only, so it runs even outside the project's virtualenv.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import state, tickets
from .config import Config, load_config

READ_PREFIXES = (
    "get_", "list_", "search_", "fetch_", "read_", "lookup_", "find_", "describe_", "check_",
    "view_", "show_",
)
ORDER_WORDS = ("place", "submit", "order", "buy", "sell", "trade")
NON_STOCK_WORDS = ("option", "crypto", "future", "event", "prediction", "forex", "bond")
WRITE_WORDS = (
    "create", "replace", "modify", "update", "transfer", "withdraw", "deposit", "send",
    "set_", "enable", "disable", "approve", "delete", "close", "liquidate", "exercise",
) + ORDER_WORDS

SYMBOL_KEYS = ("symbol", "ticker", "instrument_symbol", "stock_symbol")
SIDE_KEYS = ("side", "action", "direction", "order_side", "transaction_type")
QTY_KEYS = ("quantity", "qty", "shares", "share_quantity", "number_of_shares", "units")
AMOUNT_KEYS = (
    "dollar_amount", "dollar_based_amount", "notional", "amount_in_dollars", "dollars", "amount",
)
TYPE_KEYS = ("order_type", "type")
LIMIT_KEYS = ("limit_price", "price")
STOP_KEYS = ("stop_price", "trigger_price", "trail_amount", "trailing_peg")

LIMIT_PRICE_TOLERANCE = 0.002  # allow 0.2% for tick rounding
AMOUNT_TOLERANCE = 0.01


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def classify(tool_name: str) -> str:
    short = tool_name.split("__")[-1].lower()
    if short.startswith(READ_PREFIXES):
        return "read"
    if short.startswith("cancel"):
        return "cancel"
    if any(w in short for w in NON_STOCK_WORDS) and any(w in short for w in WRITE_WORDS):
        return "blocked"
    if any(w in short for w in ORDER_WORDS):
        return "stock_order"
    if any(w in short for w in WRITE_WORDS):
        return "blocked"
    return "read"


def _walk(obj: Any, depth: int = 0) -> list[tuple[str, Any]]:
    """Flatten nested dicts into (leaf_key, value) pairs, shallowest first."""
    if not isinstance(obj, dict) or depth > 4:
        return []
    pairs = [(str(k).lower(), v) for k, v in obj.items()]
    for _, v in list(pairs):
        if isinstance(v, dict):
            pairs.extend(_walk(v, depth + 1))
    return pairs


def _find(pairs: list[tuple[str, Any]], keys: tuple[str, ...]) -> Any:
    for key in keys:
        for k, v in pairs:
            if k == key and v not in (None, ""):
                if isinstance(v, dict) and "amount" in v:
                    return v["amount"]
                if isinstance(v, dict):
                    continue
                return v
    return None


def _to_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"could not read {field_name} from {value!r}") from exc


@dataclass
class ParsedOrder:
    symbol: str
    side: str
    quantity: float | None
    amount: float | None
    order_type: str
    limit_price: float | None
    has_stop: bool


def parse_order(tool_input: dict[str, Any]) -> ParsedOrder:
    pairs = _walk(tool_input)
    keys = {k for k, _ in pairs}
    if "legs" in keys or any(w in k for k in keys for w in ("option", "crypto", "strike", "expiration")):
        raise ValueError("order input looks like an options/crypto order")

    symbol = _find(pairs, SYMBOL_KEYS)
    if not symbol or not isinstance(symbol, str):
        raise ValueError(f"could not find the stock symbol in the order (fields: {sorted(keys)})")

    side_raw = str(_find(pairs, SIDE_KEYS) or "").strip().lower()
    if side_raw in ("buy", "b", "long", "buy_to_open"):
        side = "buy"
    elif side_raw in ("sell", "s", "sell_to_close"):
        side = "sell"
    else:
        raise ValueError(f"could not tell if the order is a buy or sell (side={side_raw!r})")

    quantity = _to_float(_find(pairs, QTY_KEYS), "quantity")
    amount = _to_float(_find(pairs, AMOUNT_KEYS), "dollar amount")
    if quantity is None and amount is None:
        raise ValueError(f"order has no share quantity or dollar amount (fields: {sorted(keys)})")
    if (quantity is not None and quantity <= 0) or (amount is not None and amount <= 0):
        raise ValueError("order size must be positive")

    limit_price = _to_float(_find(pairs, LIMIT_KEYS), "limit price")
    has_stop = _find(pairs, STOP_KEYS) is not None
    type_raw = str(_find(pairs, TYPE_KEYS) or "").strip().lower()
    if not type_raw:
        type_raw = "limit" if limit_price is not None else "market"
    if type_raw in ("lmt",):
        type_raw = "limit"
    if type_raw in ("mkt",):
        type_raw = "market"

    return ParsedOrder(
        symbol=symbol.strip().upper(),
        side=side,
        quantity=quantity,
        amount=amount,
        order_type=type_raw,
        limit_price=limit_price,
        has_stop=has_stop,
    )


def _check_against_ticket(order: ParsedOrder, ticket: dict[str, Any]) -> str | None:
    """Return a reason string if the order does not fit the ticket, else None."""
    if order.quantity is not None and order.quantity > float(ticket["quantity"]) + 1e-6:
        return f"quantity {order.quantity:g} exceeds the approved {ticket['quantity']:g} shares"
    if order.amount is not None:
        max_amount = float(ticket["notional"]) * (1 + AMOUNT_TOLERANCE)
        if order.amount > max_amount:
            return f"dollar amount {order.amount:.2f} exceeds the approved {ticket['notional']:.2f}"
    if order.side == "buy":
        approved_limit = ticket.get("limit_price")
        if approved_limit is not None:
            if order.order_type != "limit" or order.limit_price is None:
                return f"plan approved a LIMIT buy at {approved_limit:.2f}; market buys are not allowed"
            if order.limit_price > approved_limit * (1 + LIMIT_PRICE_TOLERANCE):
                return f"limit {order.limit_price:.2f} is above the approved {approved_limit:.2f}"
        elif order.limit_price is not None and order.limit_price > float(ticket["ref_price"]) * 1.02:
            return f"limit {order.limit_price:.2f} is more than 2% above the last close"
    return None


def _trade_date(cfg: Config, now: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(cfg.general.timezone)).date().isoformat()
    except Exception:
        return now.date().isoformat()


def check(payload: dict[str, Any], cfg: Config | None = None, now: datetime | None = None) -> Decision:
    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input") or {}
    kind = classify(tool_name)
    if kind in ("read", "cancel"):
        return Decision(True)
    if kind == "blocked":
        return Decision(
            False,
            f"`{tool_name}` is not a stock order. This bot only trades stocks and never "
            "moves money, trades options/crypto, or changes account settings.",
        )

    cfg = cfg or load_config()
    now = now or state.utcnow()
    active, why = state.kill_switch_active()
    if active:
        return Decision(False, f"kill switch is ON ({why}). The user must run `python -m trader resume`.")
    if not cfg.general.trading_enabled:
        return Decision(False, "trading_enabled = false in config.toml.")
    if not cfg.is_live:
        return Decision(
            False,
            "config.toml is in PAPER mode, so no real orders are allowed. "
            "Use `python -m trader paper-fill` to simulate the plan.",
        )
    if not isinstance(tool_input, dict):
        return Decision(False, "order input is not a JSON object")

    try:
        order = parse_order(tool_input)
    except ValueError as exc:
        return Decision(False, f"could not verify this order: {exc}")
    if order.has_stop or order.order_type not in ("limit", "market"):
        return Decision(False, f"order type {order.order_type!r} is not allowed; use limit or market")

    candidates = [
        t for t in tickets.open_tickets(now)
        if t["symbol"] == order.symbol and t["side"] == order.side and t.get("mode") == "live"
    ]
    if not candidates:
        return Decision(
            False,
            f"no approved {order.side.upper()} ticket for {order.symbol}. Only orders produced by "
            "`python -m trader plan` (and not vetoed or expired) can be placed. Do not retry with "
            "different parameters; report this to the user.",
        )
    problems = []
    for ticket in candidates:
        problem = _check_against_ticket(order, ticket)
        if problem is None:
            tickets.update_ticket(
                ticket["id"], status="submitted", submitted_at=now.isoformat(), submitted_input=tool_input
            )
            state.log_event(
                "order_submitted",
                mode="live",
                ticket_id=ticket["id"],
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                amount=order.amount,
                order_type=order.order_type,
                limit_price=order.limit_price,
                trade_date=_trade_date(cfg, now),
                closes_position=ticket.get("closes_position", order.side == "sell"),
            )
            return Decision(True)
        problems.append(problem)
    return Decision(False, f"{order.symbol} order does not match its approved ticket: {'; '.join(problems)}")


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        decision = check(payload)
    except Exception as exc:  # fail closed
        decision = Decision(False, f"trade guard error, blocking to be safe: {type(exc).__name__}: {exc}")
    if decision.allowed:
        return 0
    print(f"BLOCKED by trade guard: {decision.reason}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
