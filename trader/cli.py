"""Command line interface: `python -m trader <command>`."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import state, tickets
from .config import Config, ConfigError, load_config
from .portfolio import (
    AccountError,
    buys_blocked_today,
    equity_reference,
    load_account,
    load_paper_account,
    record_equity,
    save_live_snapshot,
    trades_today,
)

DISCLAIMER = "Not financial advice. Trading can lose money; past results do not predict future ones."


# ------------------------------------------------------------------- helpers

def _table(rows: Iterable[Iterable[Any]], headers: list[str]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def _money(x: float | None) -> str:
    return "" if x is None else f"${x:,.2f}"


def _pct(x: float | None) -> str:
    return "" if x is None else f"{x:+.1%}"


def _now_local(cfg: Config) -> datetime:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(cfg.general.timezone))


def _load_frames(cfg: Config, symbols: Iterable[str]):
    """Fetch bars, drop today's partial bar, add indicators."""
    from .data import DataError, get_bars, split_completed
    from .strategy import add_indicators

    now = datetime.now(timezone.utc)
    frames, latest, errors = {}, {}, {}
    for sym in sorted(set(symbols)):
        try:
            bars = get_bars(sym, cfg)
            completed, latest_price = split_completed(bars, now, cfg.general.timezone)
            frames[sym] = add_indicators(completed, cfg.strategy)
            if latest_price is not None:
                latest[sym] = latest_price
        except (DataError, ValueError, OSError) as exc:
            errors[sym] = str(exc)
    return frames, latest, errors


# ------------------------------------------------------------------ commands

def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    now = _now_local(cfg)
    kill, why = state.kill_switch_active()
    print(f"Mode:            {cfg.general.mode.upper()}")
    print(f"Trading enabled: {cfg.general.trading_enabled}")
    print(f"Kill switch:     {'ON - ' + why if kill else 'off'}")
    print(f"Data source:     {cfg.data.source}")
    print(f"Local time:      {now:%Y-%m-%d %H:%M %Z}")
    print(f"Trades today:    {trades_today(cfg.general.mode, now.date())}")
    try:
        account = load_account(cfg)
        print(f"Cash:            {_money(account.cash)}")
        if account.reported_equity is not None:
            print(f"Equity:          {_money(account.reported_equity)} (snapshot {account.as_of:%H:%M UTC})")
        if account.positions:
            print("Positions:")
            print(_table(
                [[p.symbol, f"{p.quantity:g}", _money(p.avg_cost), p.entry_date or "?"]
                 for p in account.positions.values()],
                ["symbol", "qty", "avg cost", "entry date"],
            ))
        else:
            print("Positions:       none")
    except AccountError as exc:
        print(f"Account:         {exc}")
    open_t = tickets.open_tickets()
    if open_t:
        print("Open order tickets:")
        print(_ticket_table(open_t))
    return 0


def _ticket_table(ts: list[dict[str, Any]]) -> str:
    return _table(
        [[t["id"], t["side"].upper(), t["symbol"], f"{t['quantity']:g}", t["order_type"],
          _money(t.get("limit_price")), _money(t.get("notional")), t["reason"]] for t in ts],
        ["ticket", "side", "symbol", "qty", "type", "limit", "approx $", "reason"],
    )


def cmd_analyze(cfg: Config, args: argparse.Namespace) -> int:
    from .strategy import evaluate

    try:
        held = load_account(cfg).positions
    except AccountError:
        held = {}
    symbols = args.symbols or list(cfg.watchlist.symbols) + list(held)
    frames, latest, errors = _load_frames(cfg, symbols)
    rows = []
    for sym in sorted(frames):
        pos = held.get(sym)
        sig = evaluate(
            sym, frames[sym], cfg.strategy, held=pos is not None,
            entry_date=pos.entry_date if pos else None,
            entry_price=pos.avg_cost if pos else None,
            stop_loss_pct=cfg.risk.stop_loss_pct,
            current_price=latest.get(sym),
        )
        if pos is not None:
            signal = "SELL" if sig.exit else "hold"
            notes = "; ".join(sig.exit) or f"paid {pos.avg_cost:.2f}, sell at {sig.stop_price:.2f}"
        elif not sig.ready:
            signal, notes = "-", sig.note
        else:
            signal = "BUY" if sig.entry else "-"
            notes = "; ".join(sig.entry) or ("uptrend, waiting for entry" if sig.trend_up else "no uptrend")
        rows.append([sym, sig.date, f"{sig.close:.2f}", "up" if sig.trend_up else "down",
                     f"{sig.rsi:.0f}", f"{sig.score:+.2f}", signal, notes])
    print(_table(rows, ["symbol", "bar date", "close", "trend", "RSI", "score", "signal", "notes"]))
    for sym, err in errors.items():
        print(f"! {sym}: {err}")
    return 0


def cmd_plan(cfg: Config, args: argparse.Namespace) -> int:
    from .planner import build_plan

    kill, why = state.kill_switch_active()
    if kill:
        print(f"Kill switch is ON ({why}). No plan. Run `python -m trader resume` to re-enable.")
        return 1
    try:
        account = load_account(cfg)
    except AccountError as exc:
        print(f"Cannot plan: {exc}")
        return 1

    now = _now_local(cfg)
    today = now.date()
    frames, latest, errors = _load_frames(cfg, list(cfg.watchlist.symbols) + list(account.positions))
    prices = {s: float(f["close"].iloc[-1]) for s, f in frames.items() if not f.empty}
    prices.update(latest)
    equity = account.equity(prices)
    day_start, peak = equity_reference(cfg.general.mode, today, equity)
    done = trades_today(cfg.general.mode, today)
    plan = build_plan(
        account, frames, cfg, today, day_start, peak,
        current_prices=latest,
        no_buy=buys_blocked_today(cfg.general.mode, today),
    )
    record_equity(cfg.general.mode, plan.equity, today)

    issued = tickets.issue_tickets(
        [
            {
                "symbol": o.symbol, "side": o.side, "quantity": o.quantity,
                "order_type": o.order_type, "limit_price": o.limit_price,
                "ref_price": round(o.ref_price, 4), "notional": round(o.notional, 2),
                "reason": o.reason, "stop_price": o.stop_price,
                "closes_position": o.side == "sell",
            }
            for o in plan.orders
        ],
        mode=cfg.general.mode,
        ttl_minutes=cfg.orders.ticket_ttl_minutes,
    )

    print(f"=== {cfg.general.mode.upper()} plan for {today} ===")
    print(f"Equity {_money(plan.equity)} | cash {_money(plan.cash)} | "
          f"today {_pct(plan.equity / day_start - 1 if day_start else None)} | "
          f"from peak {_pct(plan.equity / peak - 1 if peak else None)} | trades so far today {done}")
    for h in plan.halts:
        print(f"!! NEW BUYS HALTED: {h}")
    if issued:
        print("\nApproved orders (tickets expire in "
              f"{cfg.orders.ticket_ttl_minutes} min):")
        print(_ticket_table(issued))
        if cfg.is_live:
            print("\nNext: review the BUYs (veto with `python -m trader veto SYMBOL --reason ...`), then place "
                  "each remaining ticket with the Robinhood place_equity_order tool using EXACTLY the "
                  "symbol, side, quantity, order type and limit price shown.")
        else:
            print("\nNext: review the BUYs (veto with `python -m trader veto SYMBOL --reason ...`), then "
                  "run `python -m trader paper-fill`.")
    else:
        print("\nNo orders right now. Doing nothing is a valid outcome. Stop losses are checked "
              "against the current price each run, so it is fine to run this again later today.")
    if plan.skipped:
        print("\nSkipped:")
        for sym, why_skipped in plan.skipped:
            print(f"  {sym}: {why_skipped}")
    for sym, err in errors.items():
        print(f"! data error {sym}: {err}")
    return 0


def cmd_veto(cfg: Config, args: argparse.Namespace) -> int:
    vetoed = tickets.veto(args.symbol, args.reason)
    if not vetoed:
        print(f"No open BUY ticket for {args.symbol.upper()}.")
        return 1
    print(f"Vetoed {len(vetoed)} ticket(s) for {args.symbol.upper()}: {args.reason}")
    return 0


def cmd_paper_fill(cfg: Config, args: argparse.Namespace) -> int:
    from .data import DataError, get_bars
    from .paper import paper_fill

    if cfg.is_live:
        print("config.toml is in LIVE mode; paper-fill only works in paper mode.")
        return 1
    open_t = [t for t in tickets.open_tickets() if t.get("mode") == "paper"]
    if not open_t:
        print("No open paper tickets. Run `python -m trader plan` first.")
        return 0
    prices = {}
    for sym in {t["symbol"] for t in open_t}:
        try:
            prices[sym] = float(get_bars(sym, cfg)["close"].iloc[-1])
        except (DataError, ValueError, OSError) as exc:
            print(f"! no price for {sym}: {exc}")
    results = paper_fill(cfg, prices)
    print(_table(
        [[r["side"].upper(), r["symbol"], r["status"], f"{r.get('quantity', 0):g}",
          _money(r.get("price")), _money(r.get("realized_pnl")), r.get("reason", "")] for r in results],
        ["side", "symbol", "status", "qty", "price", "realized P&L", "note"],
    ))
    account = load_paper_account(cfg)
    print(f"\nPaper cash now {_money(account.cash)}; {len(account.positions)} open position(s).")
    return 0


def cmd_account(cfg: Config, args: argparse.Namespace) -> int:
    positions = []
    for spec in args.position or []:
        try:
            sym, qty, cost = spec.split(":")
            positions.append((sym, float(qty), float(cost)))
        except ValueError:
            print(f"Bad --position {spec!r}; use SYMBOL:QUANTITY:AVG_COST, e.g. AAPL:3:212.50")
            return 1
    try:
        snap = save_live_snapshot(args.equity, args.cash, positions)
    except AccountError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Recorded live account snapshot at {snap['as_of']}: equity {_money(snap['equity'])}, "
          f"cash {_money(snap['cash'])}, {len(snap['positions'])} position(s).")
    return 0


def cmd_record_fill(cfg: Config, args: argparse.Namespace) -> int:
    now = _now_local(cfg)
    state.log_event(
        "fill", mode=cfg.general.mode, symbol=args.symbol.upper(), side=args.side,
        quantity=args.quantity, price=args.price, order_id=args.order_id,
        ticket_id=args.ticket, trade_date=now.date().isoformat(),
    )
    if args.ticket:
        tickets.update_ticket(args.ticket, status="filled", fill_price=args.price)
    print(f"Recorded {args.side} {args.quantity:g} {args.symbol.upper()} @ {_money(args.price)}")
    return 0


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    from .backtest import run_backtest
    from .data import DataError, get_bars

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365 * args.years)
    warmup_start = start - timedelta(days=int(cfg.strategy.sma_slow * 1.6) + 30)
    symbols = sorted(set(args.symbols or cfg.watchlist.symbols) | {cfg.watchlist.benchmark})
    bars = {}
    for sym in symbols:
        try:
            bars[sym] = get_bars(sym, cfg, start=warmup_start, end=end)
        except (DataError, ValueError, OSError) as exc:
            print(f"! skipping {sym}: {exc}")
    if not bars:
        print("No data; cannot backtest.")
        return 1
    if args.symbols:
        # trade only the requested symbols; the benchmark is always loaded for comparison
        from dataclasses import replace
        cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(args.symbols)))

    result = run_backtest(bars, cfg, start, end)
    s, b = result.stats, result.benchmark_stats or {}
    print(f"=== Backtest {start} -> {end} | {len(cfg.watchlist.symbols)} symbols | "
          f"start {_money(cfg.risk.starting_paper_cash)} ===")
    rows = [
        ["Total return", _pct(s.get("total_return")), _pct(b.get("total_return"))],
        ["CAGR", _pct(s.get("cagr")), _pct(b.get("cagr"))],
        ["Max drawdown", _pct(s.get("max_drawdown")), _pct(b.get("max_drawdown"))],
        ["Sharpe", f"{s.get('sharpe', 0):.2f}", f"{b.get('sharpe', 0):.2f}" if b else ""],
        ["Ending equity", _money(s.get("end_equity")), _money(b.get("end_equity"))],
    ]
    print(_table(rows, ["metric", "strategy", f"buy & hold {cfg.watchlist.benchmark}"]))
    if s.get("closed_trades"):
        print(f"\nClosed trades {s['closed_trades']} | win rate {s['win_rate']:.0%} | "
              f"avg win {_pct(s['avg_win'])} | avg loss {_pct(s['avg_loss'])} | "
              f"profit factor {s['profit_factor']:.2f} | avg hold {s['avg_days_held']:.0f} days | "
              f"in market {s['time_in_market']:.0%} of days")
    else:
        print("\nNo closed trades in this window.")
    if b and s.get("total_return", 0) < b.get("total_return", 0):
        print(f"\nThe strategy did NOT beat buy-and-hold {cfg.watchlist.benchmark} here. "
              "Be honest with yourself about that before risking real money.")
    out_dir = state.state_dir() / "backtests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(out_dir / "equity.csv")
    if not result.trades.empty:
        result.trades.to_csv(out_dir / "trades.csv", index=False)
    print(f"\nEquity curve and trades saved to {out_dir}")
    print(DISCLAIMER)
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    from .backtest import curve_stats
    from .data import DataError, get_bars

    import pandas as pd

    mode = cfg.general.mode
    history = [r for r in state.read_jsonl(state.equity_history_path()) if r.get("mode") == mode]
    if not history:
        print(f"No {mode} equity history yet. Run `python -m trader plan` daily to build one.")
        return 0
    daily = pd.Series({pd.Timestamp(r["date"]): float(r["equity"]) for r in history}).sort_index()
    stats = curve_stats(daily) if len(daily) > 1 else {}
    print(f"=== {mode.upper()} performance since {daily.index[0].date()} ({len(daily)} trading days logged) ===")
    print(f"Equity: {_money(float(daily.iloc[0]))} -> {_money(float(daily.iloc[-1]))} "
          f"({_pct(float(daily.iloc[-1]) / float(daily.iloc[0]) - 1)}), max drawdown {_pct(stats.get('max_drawdown'))}")
    try:
        bench = get_bars(cfg.watchlist.benchmark, cfg, start=daily.index[0].date())["close"]
        bench = bench[bench.index >= daily.index[0]]
        if len(bench) > 1:
            print(f"Buy & hold {cfg.watchlist.benchmark} over the same period: "
                  f"{_pct(float(bench.iloc[-1]) / float(bench.iloc[0]) - 1)}")
    except (DataError, ValueError, OSError) as exc:
        print(f"(benchmark unavailable: {exc})")
    fills = [r for r in state.read_journal() if r.get("event") == "fill" and r.get("mode") == mode
             and r.get("realized_pnl") is not None]
    if fills:
        pnl = [float(r["realized_pnl"]) for r in fills]
        wins = sum(1 for p in pnl if p > 0)
        print(f"Closed trades: {len(pnl)} | win rate {wins / len(pnl):.0%} | realized P&L {_money(sum(pnl))}")
    print(DISCLAIMER)
    return 0


def cmd_kill(cfg: Config, args: argparse.Namespace) -> int:
    state.set_kill_switch(args.reason)
    print(f"Kill switch ON: {args.reason}. All Robinhood orders are now blocked.")
    return 0


def cmd_resume(cfg: Config, args: argparse.Namespace) -> int:
    if state.clear_kill_switch():
        print("Kill switch cleared.")
    else:
        print("Kill switch was not on.")
    return 0


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m trader", description="AI-assisted swing trader for Robinhood.")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="mode, kill switch, account, open tickets")

    a = sub.add_parser("analyze", help="show today's signals without creating orders")
    a.add_argument("symbols", nargs="*", type=str.upper)

    sub.add_parser("plan", help="build today's risk-checked orders and issue tickets")

    v = sub.add_parser("veto", help="cancel an approved BUY ticket")
    v.add_argument("symbol")
    v.add_argument("--reason", required=True)

    sub.add_parser("paper-fill", help="simulate filling open paper tickets")

    acct = sub.add_parser("account", help="record the live Robinhood agentic account snapshot")
    acct.add_argument("--equity", type=float, required=True, help="total account value")
    acct.add_argument("--cash", type=float, required=True, help="cash / buying power available")
    acct.add_argument("--position", action="append", metavar="SYM:QTY:AVG_COST")

    rf = sub.add_parser("record-fill", help="record a live fill in the journal")
    rf.add_argument("--symbol", required=True)
    rf.add_argument("--side", required=True, choices=["buy", "sell"])
    rf.add_argument("--quantity", type=float, required=True)
    rf.add_argument("--price", type=float, required=True)
    rf.add_argument("--order-id")
    rf.add_argument("--ticket")

    b = sub.add_parser("backtest", help="test the strategy on historical data")
    b.add_argument("--start", help="YYYY-MM-DD")
    b.add_argument("--end", help="YYYY-MM-DD")
    b.add_argument("--years", type=int, default=3)
    b.add_argument("--symbols", nargs="*", type=str.upper)

    sub.add_parser("report", help="performance so far vs. the benchmark")

    k = sub.add_parser("kill", help="emergency stop: block all orders")
    k.add_argument("--reason", default="manual")
    sub.add_parser("resume", help="clear the kill switch")
    return p


COMMANDS = {
    "status": cmd_status, "analyze": cmd_analyze, "plan": cmd_plan, "veto": cmd_veto,
    "paper-fill": cmd_paper_fill, "account": cmd_account, "record-fill": cmd_record_fill,
    "backtest": cmd_backtest, "report": cmd_report, "kill": cmd_kill, "resume": cmd_resume,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return COMMANDS[args.command](cfg, args)
