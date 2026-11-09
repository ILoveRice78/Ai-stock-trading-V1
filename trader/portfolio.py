"""Account and position models for paper and live trading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import state
from .config import Config


class AccountError(RuntimeError):
    pass


@dataclass
class Position:
    symbol: str
    quantity: float
    avg_cost: float
    entry_date: date | None = None
    scaled_out: bool = False  # part already sold at the take-profit target


@dataclass
class Account:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    source: str = "paper"
    reported_equity: float | None = None  # live snapshots report equity directly
    as_of: datetime | None = None

    def equity(self, prices: dict[str, float]) -> float:
        if self.reported_equity is not None:
            return self.reported_equity
        value = self.cash
        for pos in self.positions.values():
            price = prices.get(pos.symbol, pos.avg_cost)
            value += pos.quantity * price
        return value


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


# ---------------------------------------------------------------- paper account

def load_paper_account(cfg: Config) -> Account:
    raw = state.read_json(state.paper_portfolio_path())
    if raw is None:
        return Account(cash=cfg.risk.starting_paper_cash, source="paper")
    positions = {
        sym: Position(
            symbol=sym,
            quantity=float(p["quantity"]),
            avg_cost=float(p["avg_cost"]),
            entry_date=_parse_date(p.get("entry_date")),
            scaled_out=bool(p.get("scaled_out", False)),
        )
        for sym, p in raw.get("positions", {}).items()
    }
    return Account(cash=float(raw["cash"]), positions=positions, source="paper")


def save_paper_account(account: Account) -> None:
    state.write_json(
        state.paper_portfolio_path(),
        {
            "cash": round(account.cash, 4),
            "positions": {
                sym: {
                    "quantity": p.quantity,
                    "avg_cost": round(p.avg_cost, 4),
                    "entry_date": p.entry_date.isoformat() if p.entry_date else None,
                    "scaled_out": p.scaled_out,
                }
                for sym, p in sorted(account.positions.items())
            },
            "updated_at": state.utcnow().isoformat(),
        },
    )


# ----------------------------------------------------------------- live account

def save_live_snapshot(
    equity: float, cash: float, positions: list[tuple[str, float, float]]
) -> dict[str, Any]:
    """Record the Robinhood agentic account as read through the MCP tools."""
    if equity <= 0:
        raise AccountError("equity must be positive")
    if cash < 0:
        raise AccountError("cash must not be negative")
    snapshot = {
        "as_of": state.utcnow().isoformat(),
        "equity": float(equity),
        "cash": float(cash),
        "positions": [
            {"symbol": sym.upper(), "quantity": float(qty), "avg_cost": float(cost)}
            for sym, qty, cost in positions
            if float(qty) > 0
        ],
    }
    state.write_json(state.live_account_path(), snapshot)
    state.log_event("live_account_snapshot", equity=equity, cash=cash, positions=snapshot["positions"])
    return snapshot


def entry_dates_from_journal(journal: list[dict[str, Any]], mode: str) -> dict[str, date]:
    """Entry date of each currently open position, from the journal's buy/sell history."""
    return {sym: info[0] for sym, info in open_positions_from_journal(journal, mode).items()}


def open_positions_from_journal(
    journal: list[dict[str, Any]], mode: str
) -> dict[str, tuple[date, bool]]:
    """(entry date, scaled_out) of each open position, from the journal's buy/sell history."""
    entries: dict[str, date] = {}
    scaled: set[str] = set()
    for rec in journal:
        if rec.get("mode") != mode or rec.get("event") not in ("fill", "order_submitted"):
            continue
        sym = str(rec.get("symbol", "")).upper()
        day = _parse_date(rec.get("trade_date") or rec.get("ts"))
        if not sym or day is None:
            continue
        if rec.get("side") == "buy":
            entries.setdefault(sym, day)
        elif rec.get("side") == "sell" and rec.get("closes_position", True):
            entries.pop(sym, None)
            scaled.discard(sym)
        elif rec.get("side") == "sell":
            scaled.add(sym)
    return {sym: (day, sym in scaled) for sym, day in entries.items()}


def load_live_account(cfg: Config, now: datetime | None = None) -> Account:
    raw = state.read_json(state.live_account_path())
    if raw is None:
        raise AccountError(
            "no live account snapshot. Read the Robinhood agentic account with the MCP tools "
            "and record it with `python -m trader account --equity ... --cash ... --position SYM:QTY:AVG`"
        )
    as_of = datetime.fromisoformat(raw["as_of"])
    now = now or datetime.now(timezone.utc)
    max_age = timedelta(minutes=cfg.orders.account_max_age_minutes)
    if now - as_of > max_age:
        raise AccountError(
            f"live account snapshot is stale (from {as_of.isoformat()}); "
            "re-read the Robinhood account and record it again"
        )
    history = open_positions_from_journal(state.read_journal(), "live")
    positions = {}
    for p in raw.get("positions", []):
        sym = p["symbol"].upper()
        entry_date, scaled_out = history.get(sym, (None, False))
        positions[sym] = Position(
            symbol=sym,
            quantity=float(p["quantity"]),
            avg_cost=float(p["avg_cost"]),
            entry_date=entry_date,
            scaled_out=scaled_out,
        )
    return Account(
        cash=float(raw["cash"]),
        positions=positions,
        source="live",
        reported_equity=float(raw["equity"]),
        as_of=as_of,
    )


def load_account(cfg: Config) -> Account:
    return load_live_account(cfg) if cfg.is_live else load_paper_account(cfg)


# --------------------------------------------------------------- equity history

def record_equity(mode: str, equity: float, day: date) -> None:
    state.append_jsonl(
        state.equity_history_path(),
        {"ts": state.utcnow().isoformat(), "date": day.isoformat(), "mode": mode, "equity": equity},
    )


def equity_reference(mode: str, today: date, current_equity: float) -> tuple[float, float]:
    """Return (start-of-day equity, peak equity) for the daily-loss and drawdown limits."""
    history = [
        r for r in state.read_jsonl(state.equity_history_path()) if r.get("mode") == mode
    ]
    prior = [r for r in history if r["date"] < today.isoformat()]
    day_start = float(prior[-1]["equity"]) if prior else current_equity
    peak = max([float(r["equity"]) for r in history] + [current_equity])
    return day_start, peak


def trades_today(mode: str, today: date) -> int:
    count = 0
    for rec in state.read_journal():
        if rec.get("mode") != mode or rec.get("event") not in ("fill", "order_submitted"):
            continue
        if rec.get("event") == "fill" and mode == "live":
            continue  # live orders are counted once, when submitted
        if str(rec.get("trade_date") or rec.get("ts", ""))[:10] == today.isoformat():
            count += 1
    return count


def buys_blocked_today(mode: str, today: date) -> dict[str, str]:
    """Symbols the bot must not buy again today (prevents churn and duplicate orders)."""
    blocked: dict[str, str] = {}
    for rec in state.read_journal():
        if rec.get("mode") != mode or rec.get("event") not in ("fill", "order_submitted"):
            continue
        if str(rec.get("trade_date") or rec.get("ts", ""))[:10] != today.isoformat():
            continue
        sym = str(rec.get("symbol", "")).upper()
        if rec.get("side") == "sell":
            blocked[sym] = "sold earlier today; not re-buying the same day"
        elif rec.get("event") == "order_submitted":
            blocked.setdefault(sym, "a buy order was already placed today")
    return blocked
