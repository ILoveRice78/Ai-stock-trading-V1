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

**Strategy** (long only, individual stocks, buy signals from daily bars):
- Only buys stocks in an **uptrend**: price above its 200-day average, 50-day above 200-day.
- **Entry** on a *pullback* (RSI dips below 40 and recovers) or a *breakout* (new 55-day
  high on above-average volume). Candidates are ranked by risk-adjusted 3-month momentum.
- **Stop loss:** sells if a stock falls **5% below what you paid**. This is checked against
  the current price every time the bot runs, so run it several times a day.
- **Take profit:** when a stock is **up 5%**, sell **half**. The stop on the other half moves
  up to the purchase price, so the trade almost always ends as a win (only a sudden drop of
  more than 5% below your purchase price, before the next run, could turn it into a loss).
  The other half keeps riding in case it turns into a big winner. This is what keeps the win rate around 50%.
- **Profit lock:** once a winner has risen enough that its trailing stop (3 ATRs below the
  highest close since you bought) is above your purchase price, that trailing stop replaces
  the 5% stop, so a winner can't turn into a loss.
- **Trend exit:** sells when the trend breaks (below the 200-day, or 50-day crosses under 200-day).

**Risk limits** (all in `config.toml`):

| Limit | Default |
|---|---|
| Stop loss | sell at 5% below the purchase price |
| Take profit | sell half at +5%; the rest gets a break-even stop |
| Max size of one position | 15% of equity (so a stopped-out trade costs about 0.75% of equity) |
| Max open positions | 6 |
| Trades per day | no limit; same-day buy and sell is allowed |
| No churning | won't re-buy a stock it sold earlier the same day, or place a second buy while one is pending |
| Daily loss limit (no new buys) | -3% |
| Max drawdown from peak (no new buys) | -15% |
| Cash reserve | 5% |
| No penny stocks / illiquid stocks | price ≥ $5, ≥ $20M daily volume |
| Stocks only | no ETFs, options, crypto, margin, or shorting |

> The 5% stop is checked **each time the bot runs**. It is not a stop order sitting at
> Robinhood. If a stock drops fast between runs, or opens lower after bad overnight news,
> the sale happens at the next run and can be **worse than -5%**. Running more often
> narrows that gap but can't close it.

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

Open Claude Code in this folder and run the routine a few times per trading day, for
example around 10:00 AM, 12:30 PM and 3:30 PM Eastern. Each run re-checks the 5% stop
losses against the current price and acts on any new signals:

```
claude
> /trade
```

Claude builds the plan, reviews the buys, simulates the fills and summarizes the day.
Check progress any time with `python -m trader report`.

Want paper trading fully automatic, without Claude's review? Schedule the rules alone:

```cron
# every 30 minutes from 10:00 to 15:30 ET on weekdays (adjust to your timezone)
0,30 10-15 * * 1-5  cd /path/to/Ai-stock-trading-V1 && .venv/bin/python -m trader plan && .venv/bin/python -m trader paper-fill
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

1. In `config.toml`, set `mode = "live"`. Consider lowering `max_position_pct` for the
   first month.
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
| `python -m trader backtest --take-profit 0 4 5 6 8` | Compare take-profit levels: win rate vs. total return |
| `python -m trader report` | Performance so far vs. the benchmark |
| `python -m trader kill` / `resume` | Emergency stop on/off |

## Customizing

- **Watchlist:** edit `[watchlist] symbols` in `config.toml`. Stick to liquid large-cap stocks.
- **Stop loss:** `stop_loss_pct` in `[risk]`. Tighter stops get triggered by normal daily
  swings more often. Backtest any change.
- **Take profit / win rate:** `take_profit_pct` and `take_profit_sell_pct` in `[risk]`. A
  closer target raises the win rate but cuts profits; selling everything at the target
  (`take_profit_sell_pct = 100`) cuts them much more than selling half. On simulated data:
  no target won 33% (+52%), sell-all at +5% won 51% (+8%), sell-half at +5% won 51% (+21%).
  Run `python -m trader backtest --take-profit 0 4 5 6 8` on real prices, and if the win
  rate at +5% comes out below 50%, try +4%.
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
- **Same-day trading rules**: FINRA's old "pattern day trader" rule ($25,000 minimum) was
  eliminated effective June 4, 2026, though brokers are phasing it out through 2027. In a
  **cash** account, frequently selling stock bought with unsettled money can still cause
  "good faith violations". Check how Robinhood handles your agentic account.
- **Intraday prices**: Yahoo Finance prices can lag a few minutes behind the market.

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
