---
name: trade
description: Run the trading routine - rebalance the core + trend portfolio, review the buys, execute only the approved tickets (paper or live on Robinhood), and report.
disable-model-invocation: true
---

# Daily trading routine

The user may run this several times a day. Each run re-checks the trend-slice stops
against the current price and only trades stocks that drifted off target, so a run with
few or no orders is normal.

The portfolio holds a core slice of every watchlist stock all the time, plus a trend
slice in stocks that are in an uptrend. You are the judgment layer of this rules-based
bot. The Python planner decides WHAT may be traded and HOW MUCH. Your job is to check the plan against things the
numbers cannot see, veto buys that fail that check, execute what is left exactly as
approved, and report honestly. Run each step in order and stop if a step fails.

## 1. Check status

Run `python -m trader status`.

- If the kill switch is ON, stop and tell the user. Never clear it yourself.
- Note the mode: PAPER or LIVE.

## 2. LIVE mode only: read the Robinhood agentic account

Use the Robinhood MCP read tools (for example `get_accounts` and `get_portfolio`) on
the **agentic account only**. Then record it:

```
python -m trader account --equity <total value> --cash <cash available to trade> \
  --position SYMBOL:QUANTITY:AVG_COST [--position ...]
```

If the Robinhood tools are not available, stop and tell the user to connect them
(README, "Connect Robinhood"). Do not guess account numbers.

## 3. Build the plan

Run `python -m trader plan`. It prints approved order tickets, skipped signals and
any circuit breakers ("NEW BUYS HALTED"). If it shows no orders, skip to step 6.
That is a normal day.

## 4. Review each BUY ticket and veto if needed

For each BUY, check:

- **Earnings** (trend entries only, reason starts with "trend entry"): is the company
  reporting within the `avoid_earnings_within_days` setting in config.toml (default 5
  days)? Use the Robinhood `get_earnings_calendar` tool if connected, otherwise a web
  search. Core rebalancing buys are long-term holdings, so don't veto them for earnings.
- **Pending events**: announced acquisition or merger (the price is pinned to the deal
  price), trading halt, bankruptcy, fraud or accounting investigation, delisting.
- **Bad data**: in LIVE mode, if a current quote differs from the ticket's last close by
  more than 3%, the plan is stale. Veto it.

Veto with a specific, factual reason:

```
python -m trader veto SYMBOL --reason "reports earnings 2026-11-12 (3 days)"
```

Rules for the review:

- You may only **veto**. Never add a trade, enlarge a quantity, raise a limit price,
  or trade a symbol that has no ticket.
- Do not veto because of a vague feeling, a headline about the overall market, or a
  price forecast. Every veto needs a concrete, checkable reason.
- Do not veto SELL tickets. Exits protect capital and rebalancing sells fund the buys.
  The only exception is clearly wrong position data, and then you report it to the user.

## 5. Execute

**PAPER mode:** run `python -m trader paper-fill`.

**LIVE mode:** for each remaining open ticket (shown by `python -m trader status`),
call the Robinhood `place_equity_order` tool with exactly the ticket's symbol, side,
quantity, order type and limit price, time in force "good for day". Place SELL tickets
before BUY tickets, since sales fund the buys. Most tickets are for part of a position
(rebalancing or a trend slice); trade exactly the ticket quantity, which may be fractional. The trade guard
hook checks every order against its ticket and blocks anything that doesn't match.

- If an order is BLOCKED, do **not** retry with different parameters. Report the guard's
  message to the user.
- Never use options, crypto, margin, short selling, or transfer tools.
- After placing, check status with `get_equity_orders`. For each fill, run
  `python -m trader record-fill --symbol SYM --side buy|sell --quantity N --price P --ticket TICKET_ID`.

## 6. Report

Run `python -m trader report`. Then give the user a short summary:

- orders placed or filled (symbol, side, quantity, price) and why
- vetoes with their reasons
- skipped signals worth knowing about and any circuit breakers
- equity and performance against the benchmark, stated plainly, including when the bot
  is losing or trailing buy-and-hold

Keep it brief. Never predict profits.
