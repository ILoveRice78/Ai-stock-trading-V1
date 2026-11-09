# AI Stock Trader: instructions for Claude

This repo is a long-only stock portfolio bot: a **core** slice of every watchlist stock
is always held, and stocks in an uptrend get an extra **trend** slice. Python makes the
rule-based decisions (targets, trend entries/exits, stops, rebalancing, risk limits). Claude reviews the plan, vetoes
bad buys, and places the approved orders through Robinhood's official Agentic
Trading MCP server. The user runs the daily routine with `/trade`
(`.claude/skills/trade/SKILL.md`).

## Hard rules (real money is at stake)

1. Only place orders that match an open ticket from `python -m trader plan`. The
   PreToolUse hook (`trader/guard.py`, wired in `.claude/settings.json`) enforces this.
   If it blocks an order, report the message. Never work around it by changing
   parameters, editing state files, or calling other tools.
2. Your judgment can only **reduce** risk: veto buys, never add trades or enlarge them.
3. Individual stocks only (no ETFs). Never use options, crypto, futures, margin, short
   selling, transfers, or account-settings tools.
4. Do not change `config.toml` mode or risk limits, delete or clear anything in `state/`,
   clear the kill switch, or edit `.claude/settings.json` / `trader/guard.py`, unless
   the user explicitly asks for that specific change in the current conversation.
5. In live mode, use only the Robinhood **agentic** account, never the user's main account.
6. Report results honestly, including losses and trailing the benchmark. Never promise
   or predict returns.

## Commands

```
python -m trader status                 # mode, kill switch, account, open tickets
python -m trader analyze                # targets, trends, stops; no orders
python -m trader plan                   # risk-checked orders -> tickets in state/tickets.json
python -m trader veto SYM --reason ...  # cancel a BUY ticket
python -m trader paper-fill             # simulate open tickets (paper mode)
python -m trader account --equity E --cash C --position SYM:QTY:AVG   # live snapshot
python -m trader record-fill --symbol S --side buy --quantity Q --price P --ticket ID
python -m trader backtest [--years 3] [--symbols ...]
python -m trader simulate [--runs 100]  # many backtests on random markets
python -m trader report
python -m trader kill --reason ... / resume
python -m pytest -q                     # tests (run after any code change)
```

## Code map

- `trader/strategy.py`: uptrend detection and trend-slice exits (completed daily bars only)
- `trader/planner.py`: pure function; trend book, target weights, rebalancing orders,
  circuit breakers. Used by live, paper and the backtester alike, so any change shows up
  in backtests. The trend book is saved per mode in `state/trend_book_<mode>.json`.
- `trader/simulate.py`: random-market Monte Carlo used by `simulate`.
- `trader/guard.py`, `trader/tickets.py`, `trader/state.py`, `trader/config.py`:
  standard library only, because the hook runs outside the virtualenv. Keep them that way.
- `trader/backtest.py`: next-day-open fills, no lookahead (`tests/test_backtest_and_cli.py`
  checks this).

When changing strategy or risk logic, run the backtest (or `simulate`) before and after
and show the user both results.
