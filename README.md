# AI Stock Trader for Robinhood

An AI-assisted trading bot for **Robinhood Agentic Trading**, Robinhood's official way
to let an AI agent trade stocks from a separate, pre-funded account.

- **Python rules** pick trades and size them: a trend-following swing strategy with strict
  risk limits.
- **Claude** (in Claude Code) reviews each buy for things the numbers miss, like earnings
  next week or a pending buyout, vetoes bad ones, and places the approved orders through
  Robinhood's MCP connector.
- **A trade guard** checks every order before it reaches Robinhood and blocks anything the
  rules didn't approve.

It starts in **paper mode** (simulated money). You move to real money only when you decide to.

> ### Read this first
> Most trading bots, including professional ones, **do not beat simply buying and holding
> an index fund like SPY**. This bot is a disciplined, transparent starting point, not a
> money machine. Expect losing trades and losing months. Only trade money you can afford
> to lose, paper trade for weeks first, and compare everything against buy-and-hold SPY:
> the `backtest` and `report` commands do this for you. Short-term gains are taxed as
> ordinary income. **Not financial advice.**

---

## How it works

Every trading day you type `/trade` in Claude Code, which runs:

```
1. Read account   Robinhood MCP (live) or the paper portfolio
2. Plan           python -m trader plan
                  daily bars -> signals -> position sizing -> risk limits
                  -> approved order TICKETS
3. Review         Claude checks earnings, buyouts, halts, bad data
                  and may only VETO a buy (never add or enlarge one)
4. Execute        paper: python -m trader paper-fill
                  live:  Robinhood place_equity_order -> TRADE GUARD
                         (blocks any order without a matching ticket)
5. Report         python -m trader report (vs. buy-and-hold SPY)
```

**Strategy** (long only, daily bars, positions held days to weeks):
- Only buys stocks in an **uptrend**: price above its 200-day average, 50-day above 200-day.
- **Entry** on a *pullback* (RSI dips below 40 and recovers) or a *breakout* (new 55-day
  high on above-average volume). Candidates are ranked by risk-adjusted 3-month momentum.
- **Exit** when the trend breaks (below the 200-day, or 50-day crosses under 200-day) or
  price falls 3 ATRs below its highest close since entry (trailing stop).

**Risk limits** (all in `config.toml`):

| Limit | Default |
|---|---|
| Max loss per trade if the stop is hit | 1% of equity |
| Max size of one position | 15% of equity |
| Max open positions | 6 |
| Max trades per day | 4 |
| Daily loss limit (no new buys) | -3% |
| Max drawdown from peak (no new buys) | -15% |
| Cash reserve | 5% |
| No penny stocks / illiquid stocks | price ≥ $5, ≥ $20M daily volume |
| No day trades | never sells a position bought the same day |
| Stocks/ETFs only | no options, crypto, margin, or shorting |

---

## Setup

You need Python 3.11+ and [Claude Code](https://claude.com/claude-code).

```bash
git clone https://github.com/ILoveRice78/Ai-stock-trading-V1.git
cd Ai-stock-trading-V1
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                # all tests should pass
python -m trader status
```

## Step 1: Backtest

See how the rules would have done on the last few years of real prices:

```bash
python -m trader backtest --years 3
python -m trader backtest --start 2020-01-01 --end 2022-12-31   # include a bear market
```

It prints return, CAGR, max drawdown, Sharpe and trade stats **next to buy-and-hold SPY**.
If the strategy doesn't beat SPY on a risk-adjusted basis, think hard before going further.
Backtests are optimistic: real fills, gaps and news will be worse.

## Step 2: Paper trade for a few weeks

Open Claude Code in this folder and run the routine once per trading day, ideally after
10:00 AM Eastern so the opening volatility has settled:

```
claude
> /trade
```

Claude builds the plan, reviews the buys, simulates the fills and summarizes the day.
Check progress any time with `python -m trader report`.

Want paper trading fully automatic, without Claude's review? Schedule the rules alone:

```cron
# weekdays 10:15 ET (adjust to your timezone)
15 10 * * 1-5  cd /path/to/Ai-stock-trading-V1 && .venv/bin/python -m trader plan && .venv/bin/python -m trader paper-fill
```

## Step 3: Connect Robinhood Agentic Trading

Robinhood launched [Agentic Trading](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
in 2026 as a beta. Your AI agent trades only from a separate **agentic account** that you
fund, and can never touch more than that balance. Check Robinhood's support article for
the current steps. At the time of writing:

1. In the Robinhood app, set up Agentic Trading. Access may be rolling out by invite.
   Create the **agentic account** and fund it with a small amount you can afford to lose.
2. Add the MCP server to Claude Code:
   ```bash
   claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
   ```
3. In Claude Code, type `/mcp`, select `robinhood-trading`, and authenticate. Confirm the
   login page is on robinhood.com, and approve it in the Robinhood app.
4. **Turn on trade approvals** in the agentic account's Agent Settings, so every order
   needs a tap on your phone. This is a second safety net on top of the trade guard.

## Step 4: Go live (small)

Only after weeks of paper trading you're comfortable with:

1. In `config.toml`, set `mode = "live"`. Consider lowering `max_position_pct` and
   `max_trades_per_day` for the first month.
2. Run `/trade` in Claude Code and watch every step. Claude reads the agentic account,
   plans, reviews, and places the approved orders. Claude Code asks your permission
   before each Robinhood order, and Robinhood asks for your approval if you turned
   that on.
3. Run it supervised for at least a few weeks before considering any automation.

**Emergency stop:** `python -m trader kill --reason "..."` blocks every order immediately
until you run `python -m trader resume`. You can always sell manually in the Robinhood app.

---

## Safety design

| Layer | What it does |
|---|---|
| Paper mode default | No real orders are possible until you edit `config.toml` |
| Order tickets | `plan` writes approved orders with exact symbol, side, max quantity and max limit price, expiring after 2 hours |
| Trade guard hook (`trader/guard.py`) | Runs before **every** Robinhood MCP tool call. Reads and cancels pass. A stock order passes only in live mode, with the kill switch off, and if it matches an open ticket. Options, crypto, transfers and settings changes are always blocked. **Any error blocks the order (fails closed).** |
| Veto-only AI | Claude can cancel buys but cannot add or enlarge trades |
| Circuit breakers | Daily loss limit, max drawdown, daily trade cap, position limits |
| Agentic account | Robinhood limits the agent to the balance you put in it |
| Approvals | Claude Code permission prompts plus Robinhood's own trade approvals |
| Journal | Every ticket, veto, order and fill is logged in `state/journal.jsonl` |

## Commands

| Command | What it does |
|---|---|
| `python -m trader status` | Mode, kill switch, account, open tickets |
| `python -m trader analyze [SYM ...]` | Today's signals without creating orders |
| `python -m trader plan` | Build today's risk-checked orders (tickets) |
| `python -m trader veto SYM --reason "..."` | Cancel a buy ticket |
| `python -m trader paper-fill` | Simulate fills for open paper tickets |
| `python -m trader account --equity E --cash C --position SYM:QTY:AVG` | Record the live agentic account (Claude does this) |
| `python -m trader record-fill ...` | Record a live fill in the journal |
| `python -m trader backtest [--years N] [--symbols ...]` | Test the rules on history vs. buy-and-hold |
| `python -m trader report` | Performance so far vs. the benchmark |
| `python -m trader kill` / `resume` | Emergency stop on/off |

## Customizing

- **Watchlist:** edit `[watchlist] symbols` in `config.toml`. Stick to liquid large caps and ETFs.
- **Small account:** set `fractional_shares = true` so the bot can buy partial shares.
  Robinhood may only accept fractional orders as market or dollar-based orders.
- **Strategy rules:** `trader/strategy.py`. Backtest before and after every change, and
  beware of tuning parameters until the backtest looks great: that's *overfitting*, and it
  won't hold up live.
- **Data:** `[data] source = "yfinance"` (free Yahoo Finance daily bars) or `"files"` to read
  CSV/JSON bars from `data/bars/`, including Robinhood's historicals format.

## Troubleshooting

- **"could not find the stock symbol/quantity in the order"**: Robinhood may have
  renamed a field in `place_equity_order`. Add the new field name to the lists at the top
  of `trader/guard.py` (`SYMBOL_KEYS`, `QTY_KEYS`, ...) and run the tests.
- **Every Robinhood call is blocked with a guard error**: the hook runs `python3`. Make
  sure Python 3.11+ is installed as `python3`. On Windows, change `python3` to `python`
  in `.claude/settings.json`.
- **Yahoo Finance errors**: usually temporary rate limits. Try again later or switch to
  `source = "files"`.
- **Robinhood orders rejected while reads work**: the order must go to the
  agentic-enabled account.

## Development

```bash
python -m pytest -q
```

The tests cover the indicators, signals, sizing and every circuit breaker, the guard's
allow/block rules (including fail-closed), paper fills, data parsing, a no-lookahead check
on the backtester, and an end-to-end CLI run.

---

*Not financial advice. This software is provided as-is with no warranty. Trading
involves risk of loss, and you are responsible for every trade it places.*
