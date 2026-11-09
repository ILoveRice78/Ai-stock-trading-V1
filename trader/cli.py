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
    load_trend_book,
    record_equity,
    save_live_snapshot,
    save_trend_book,
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
    book = load_trend_book(cfg.general.mode)["positions"]
    print(f"Trend slices:    {', '.join(sorted(book)) or 'none'}")
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


def _make_plan(cfg: Config):
    """Load account, prices and trend book, and build the plan (nothing is saved)."""
    from .planner import build_plan

    account = load_account(cfg)
    today = _now_local(cfg).date()
    book = load_trend_book(cfg.general.mode)
    symbols = list(cfg.watchlist.symbols) + list(account.positions) + list(book["positions"])
    frames, latest, errors = _load_frames(cfg, symbols)
    prices = {s: float(f["close"].iloc[-1]) for s, f in frames.items() if not f.empty}
    prices.update(latest)
    equity = account.equity(prices)
    day_start, peak = equity_reference(cfg.general.mode, today, equity)
    plan = build_plan(
        account, frames, cfg, today, day_start, peak, trend_book=book,
        current_prices=latest, no_buy=buys_blocked_today(cfg.general.mode, today),
    )
    return plan, account, errors, day_start, peak


def _portfolio_table(plan) -> str:
    rows = []
    for sym in sorted(plan.targets):
        sig = plan.signals.get(sym)
        in_trend = sym in plan.trend_book["positions"]
        trend = "-" if sig is None or not sig.ready else ("up" if sig.trend_up else "down")
        slice_ = "core + trend" if in_trend else ("core" if plan.targets[sym] > 0 else "sell")
        stop = ""
        if in_trend and sig is not None and sig.stop_price:
            stop = f"{sig.stop_price:.2f}"
        orders = [o for o in plan.orders if o.symbol == sym]
        action = "; ".join(f"{o.side.upper()} {o.quantity:g}" for o in orders) or "-"
        rows.append([sym, "" if sig is None else f"{sig.close:.2f}", trend, slice_,
                     f"{plan.targets[sym]:.1%}", f"{plan.weights.get(sym, 0):.1%}", stop, action])
    return _table(rows, ["symbol", "close", "trend", "holding", "target", "now", "trend stop", "action"])


def cmd_analyze(cfg: Config, args: argparse.Namespace) -> int:
    try:
        plan, _, errors, _, _ = _make_plan(cfg)
    except AccountError as exc:
        print(f"Cannot analyze: {exc}")
        return 1
    print(_portfolio_table(plan))
    print("\n(Nothing was saved or ordered. Run `python -m trader plan` to act on this.)")
    for sym, err in errors.items():
        print(f"! {sym}: {err}")
    return 0


def cmd_plan(cfg: Config, args: argparse.Namespace) -> int:
    kill, why = state.kill_switch_active()
    if kill:
        print(f"Kill switch is ON ({why}). No plan. Run `python -m trader resume` to re-enable.")
        return 1
    try:
        plan, account, errors, day_start, peak = _make_plan(cfg)
    except AccountError as exc:
        print(f"Cannot plan: {exc}")
        return 1

    mode, today = cfg.general.mode, plan.session_date
    record_equity(mode, plan.equity, today)
    save_trend_book(mode, plan.trend_book)
    for sym in plan.trend_entries:
        state.log_event("trend_entry", mode=mode, symbol=sym, trade_date=today.isoformat(),
                        price=plan.trend_book["positions"][sym]["entry_price"])
    for t in plan.trend_exits:
        state.log_event("trend_exit", mode=mode, symbol=t.symbol, trade_date=today.isoformat(),
                        entry_price=t.entry_price, price=t.exit_price, ret=round(t.ret, 6),
                        reason=t.reason)

    issued = tickets.issue_tickets(
        [
            {
                "symbol": o.symbol, "side": o.side, "quantity": o.quantity,
                "order_type": o.order_type, "limit_price": o.limit_price,
                "ref_price": round(o.ref_price, 4), "notional": round(o.notional, 2),
                "reason": o.reason, "closes_position": o.side == "sell" and o.closes_position,
            }
            for o in plan.orders
        ],
        mode=mode,
        ttl_minutes=cfg.orders.ticket_ttl_minutes,
    )

    print(f"=== {mode.upper()} plan for {today} ===")
    print(f"Equity {_money(plan.equity)} | cash {_money(plan.cash)} | "
          f"today {_pct(plan.equity / day_start - 1 if day_start else None)} | "
          f"from peak {_pct(plan.equity / peak - 1 if peak else None)} | "
          f"trades so far today {trades_today(mode, today)}")
    pf = cfg.portfolio
    print(f"Core {pf.core_pct:g}% across {len(cfg.watchlist.symbols)} stocks, trend {pf.trend_pct:g}% "
          f"for stocks in an uptrend. Trend slices now: "
          f"{', '.join(sorted(plan.trend_book['positions'])) or 'none'}")
    for h in plan.halts:
        print(f"!! NEW BUYS HALTED: {h}")
    if issued:
        print(f"\nApproved orders (tickets expire in {cfg.orders.ticket_ttl_minutes} min):")
        print(_ticket_table(issued))
        if cfg.is_live:
            print("\nNext: review the BUYs (veto with `python -m trader veto SYMBOL --reason ...`), then place "
                  "each remaining ticket with the Robinhood place_equity_order tool using EXACTLY the "
                  "symbol, side, quantity, order type and limit price shown. Place SELLs before BUYs.")
        else:
            print("\nNext: review the BUYs (veto with `python -m trader veto SYMBOL --reason ...`), then "
                  "run `python -m trader paper-fill`.")
    else:
        print("\nNo orders right now: every stock is close enough to its target. Stops are checked "
              "against the current price each run, so it is fine to run this again later today.")
    if plan.skipped:
        print("\nNotes:")
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
    from dataclasses import replace

    from .backtest import run_backtest
    from .data import DataError, get_bars

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365 * args.years)
    warmup_start = start - timedelta(days=int(cfg.strategy.sma_slow * 1.6) + 30)
    if args.symbols:
        cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(args.symbols)))
    symbols = sorted(set(cfg.watchlist.symbols) | {cfg.watchlist.benchmark})
    bars = {}
    for sym in symbols:
        try:
            bars[sym] = get_bars(sym, cfg, start=warmup_start, end=end)
        except (DataError, ValueError, OSError) as exc:
            print(f"! skipping {sym}: {exc}")
    if not bars:
        print("No data; cannot backtest.")
        return 1

    result = run_backtest(bars, cfg, start, end)
    s, b, h = result.stats, result.benchmark_stats or {}, result.hold_stats or {}
    print(f"=== Backtest {start} -> {end} | {len(cfg.watchlist.symbols)} stocks | "
          f"start {_money(cfg.risk.starting_paper_cash)} ===")
    rows = [
        [label, fmt(s.get(key)), fmt(h.get(key)), fmt(b.get(key))]
        for label, key, fmt in [
            ("Total return", "total_return", _pct), ("CAGR", "cagr", _pct),
            ("Max drawdown", "max_drawdown", _pct),
            ("Sharpe", "sharpe", lambda x: "" if x is None else f"{x:.2f}"),
            ("Ending equity", "end_equity", _money),
        ]
    ]
    print(_table(rows, ["metric", "bot", "hold same stocks", f"hold {cfg.watchlist.benchmark}"]))
    if s.get("trend_trades"):
        print(f"\nTrend trades {s['trend_trades']} | win rate {s['win_rate']:.0%} | "
              f"avg win {_pct(s['avg_win'])} | avg loss {_pct(s['avg_loss'])}")
    print(f"Orders filled {s['orders_filled']} | average invested {s['avg_invested']:.0%}")
    if b and s.get("total_return", 0) < b.get("total_return", 0):
        print(f"\nThe bot did NOT beat buy-and-hold {cfg.watchlist.benchmark} here. "
              "Be honest with yourself about that before risking real money.")
    out_dir = state.state_dir() / "backtests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity.to_csv(out_dir / "equity.csv")
    if not result.trades.empty:
        result.trades.to_csv(out_dir / "trades.csv", index=False)
    print(f"\nEquity curve and trades saved to {out_dir}")
    print(DISCLAIMER)
    return 0


def cmd_simulate(cfg: Config, args: argparse.Namespace) -> int:
    import pandas as pd

    from .simulate import simulate

    print(f"Running {args.runs} backtests on randomly generated {args.years}-year markets "
          f"with {_money(cfg.risk.starting_paper_cash)} each...")
    df = simulate(cfg, runs=args.runs, years=args.years, seed=args.seed, processes=args.processes)
    r = df["return"]
    print(f"\nMade money in {(r > 0).sum()} of {len(df)} | lost money in {(r < 0).sum()}")

    def row(label: str, ret: pd.Series, dd: pd.Series) -> list[str]:
        return [label, f"{(ret > 0).sum()}", _pct(ret.mean()), _pct(ret.median()),
                f"{_pct(ret.min())} to {_pct(ret.max())}", _pct(dd.mean())]

    print(_table([
        row("bot", r, df["max_drawdown"]),
        row("hold the same stocks", df["hold_return"], df["hold_drawdown"]),
        row("hold the index", df["index_return"], df["index_drawdown"]),
    ], ["", "made money", "average", "median", "range", "avg max drawdown"]))
    cash = cfg.risk.starting_paper_cash
    print(f"\nBot: {_money(cash)} became {_money(df['end_equity'].min())} to "
          f"{_money(df['end_equity'].max())} (average {_money(df['end_equity'].mean())})")
    print(f"Bot beat holding the same stocks in {(r > df['hold_return']).sum()} tests and "
          f"the index in {(r > df['index_return']).sum()}")
    print(f"Trend-trade win rate averaged {df['win_rate'].mean():.0%}; "
          f"average invested {df['avg_invested'].mean():.0%}")

    df["market"] = pd.cut(df["market_trend"], [-10, 0, 0.08, 10],
                          labels=["falling", "flat/modest", "rising"])
    g = df.groupby("market", observed=True)
    print("\n" + _table(
        [[m, len(x), (x["return"] > 0).sum(), _pct(x["return"].mean()),
          _pct(x["hold_return"].mean()), _pct(x["index_return"].mean())] for m, x in g],
        ["market", "tests", "bot made money", "bot avg", "hold stocks avg", "index avg"],
    ))
    out = state.state_dir() / "simulations" / f"{datetime.now():%Y%m%d-%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nAll results saved to {out}")
    print("These are random-walk markets: they measure risk and trading costs, not whether the "
          "trend rules work on real stocks. Check that with `python -m trader backtest`.")
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
    exits = [r for r in state.read_journal() if r.get("event") == "trend_exit" and r.get("mode") == mode]
    if exits:
        rets = [float(r["ret"]) for r in exits]
        wins = sum(1 for x in rets if x > 0)
        print(f"Trend trades closed: {len(rets)} | win rate {wins / len(rets):.0%} | "
              f"average {_pct(sum(rets) / len(rets))}")
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
    p = argparse.ArgumentParser(prog="python -m trader", description="AI-assisted core + trend stock portfolio for Robinhood.")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="mode, kill switch, account, open tickets")

    sub.add_parser("analyze", help="show targets, trends and planned trades without ordering")

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

    sm = sub.add_parser("simulate", help="run many backtests on randomly generated markets")
    sm.add_argument("--runs", type=int, default=100)
    sm.add_argument("--years", type=int, default=3)
    sm.add_argument("--seed", type=int, default=0)
    sm.add_argument("--processes", type=int, help="parallel workers (default: all CPUs)")

    sub.add_parser("report", help="performance so far vs. the benchmark")

    k = sub.add_parser("kill", help="emergency stop: block all orders")
    k.add_argument("--reason", default="manual")
    sub.add_parser("resume", help="clear the kill switch")
    return p


COMMANDS = {
    "status": cmd_status, "analyze": cmd_analyze, "plan": cmd_plan, "veto": cmd_veto,
    "paper-fill": cmd_paper_fill, "account": cmd_account, "record-fill": cmd_record_fill,
    "backtest": cmd_backtest, "simulate": cmd_simulate, "report": cmd_report, "kill": cmd_kill, "resume": cmd_resume,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    return COMMANDS[args.command](cfg, args)
