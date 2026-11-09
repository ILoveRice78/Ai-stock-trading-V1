# AI Stock Trader for Robinhood

An AI-assisted stock portfolio for **Robinhood Agentic Trading**, Robinhood's official way
to let an AI agent trade stocks from a separate, pre-funded account.

- **Python rules** decide what to hold: most of the money stays invested in a list of
  stocks all the time (the **core**), and stocks in an uptrend get extra (the **trend** part).
- **Claude** (in Claude Code) reviews each buy for things the numbers miss, like earnings
  next week or a pending buyout, vetoes bad ones, and places the approved orders through
  Robinhood's MCP connector.
- **A trade guard** checks every order before it reaches Robinhood and blocks anything the
  rules didn't approve.

It starts in **paper mode** (simulated money). You move to real money only when you decide to.

> ### Read this first
> Most trading bots, including professional ones, **do not beat simply buying and holding
> an index fund like SPY**. That's why this bot keeps most of its money invested instead of
> trying to time the market. It still loses money when stocks fall, sometimes a lot:
> in simulated tests its worst drop from a peak was over 50%. Only invest money you can
> afford to lose, paper trade first, and compare everything against buy-and-hold SPY: the
> `backtest`, `simulate` and `report` commands do this for you. **Not financial advice.**

---

## How it works

Every trading day you type `/trade` in Claude Code, which runs:

```
1. Read account   Robinhood MCP (live) or the paper portfolio
2. Plan           python -m trader plan
                  daily bars -> trends -> target % per stock -> risk limits
                  -> approved order TICKETS for stocks that drifted off target
3. Review         Claude checks earnings, buyouts, halts, bad data
                  and may only VETO a buy (never add or enlarge one)
4. Execute        paper: python -m trader paper-fill
                  live:  Robinhood place_equity_order -> TRADE GUARD
                         (blocks any order without a matching ticket)
5. Report         python -m trader report (vs. buy-and-hold SPY)
```

**The portfolio** (long only, individual stocks):

| Part | Share of the money | What it holds |
|---|---|---|
| **Core** | 80% | Every watchlist stock in equal amounts, **all the time**. Never stopped out. |
| **Trend** | 15% | Extra in each stock that's in an **uptrend** (price above its 200-day average, 50-day above the 200-day) |
| Cash | 5% | Reserve |

With 12 stocks, each gets 6.7% as core. Stocks in an uptrend get another 1.25%.

- **Trend exits:** a stock's trend slice (only the extra part) is sold when its uptrend
  ends, when it falls **10%** below the price the slice was bought at (the stop loss),
  or when its profit lock is hit. The profit lock is a trailing stop 3 ATRs below its
  highest close, used once that trailing stop is above the purchase price. After a trend
  exit, the bot waits 10 days before giving that stock a trend slice again.
- **Rebalancing:** a stock is only traded when it drifts more than 25% of one core slice
  away from its target, so normal daily moves don't cause trades.
- **Stops are checked on every run** against the current price, so run it a few times a day.

**Why this design:** an earlier version traded in and out of positions and tried to win
at least half its trades. In 100 simulated 3-year markets it made only +6% on average,
against +34% for just holding the index, because it sat in cash too much. Keeping most of
the money invested fixed that:

| Same 100 simulated markets, $10,000 each, 3 years | Old swing trader | **This version** | Hold the index |
|---|---|---|---|
| Made money | 55 | **64** | 60 |
| Average return | +6% | **+46%** | +34% |
| Middle (median) result | +1% | **+12%** | +14% |
| Range | -16% to +66% | **-41% to +398%** | -60% to +300% |
| Worst drop along the way | -17% | **-56%** | — |
| Trend-trade win rate | 45% | **37%** | — |

The cost is clear in the last two rows: staying invested means riding out market drops, and
the win rate is lower. Simply holding the same stocks did even better on average (+62%),
because the bot keeps about 17% out of the market. These are random-walk markets with no
real trends, so they can't show whether the trend part helps on real stocks. Run
`python -m trader backtest` on real prices to judge that.

**Risk limits** (all in `config.toml`):

| Limit | Default |
|---|---|
| Stop loss | trend slice only: sell it at 10% below its purchase price (the core is never stopped out) |
| Trades per day | no limit; same-day buy and sell is allowed |
| No churning | won't re-buy a stock it sold earlier the same day, or place a second buy while one is pending |
| Daily loss limit (no buys) | -3% |
| Max drawdown from peak (no buys) | -15% (sells and stops still work) |
| Cash reserve | 5% |
| No penny stocks / illiquid stocks | price ≥ $5, ≥ $20M daily volume |
| Stocks only | no ETFs, options, crypto, margin, or shorting |

> Stops are checked **each time the bot runs**. They are not stop orders sitting at
> Robinhood. If a stock drops fast between runs, or opens lower after bad overnight news,
> the sale happens at the next run and can be worse than the stop level.

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

It prints return, CAGR, max drawdown and Sharpe **next to holding the same stocks and
holding SPY**. If the bot doesn't beat SPY on a risk-adjusted basis, think hard before
going further. Backtests are optimistic: real fills, gaps and news will be worse.

To see the range of outcomes across many random markets (no internet needed):

```bash
python -m trader simulate --runs 100
```

## Step 2: Paper trade for a few weeks

Open Claude Code in this folder and run the routine a few times per trading day, for
example around 10:00 AM, 12:30 PM and 3:30 PM Eastern. Each run re-checks the stops
against the current price and trades any stock that has drifted off its target:

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

1. In `config.toml`, set `mode = "live"`. The first live run buys the whole portfolio at
   once. To ease in, fund the agentic account gradually: the bot invests whatever is there.
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
| Circuit breakers | Daily loss limit and max drawdown stop all buying; stops still sell |
| Agentic account | Robinhood limits the agent to the balance you put in it |
| Approvals | Claude Code permission prompts plus Robinhood's own trade approvals |
| Journal | Every ticket, veto, order and fill is logged in `state/journal.jsonl` |

## Commands

| Command | What it does |
|---|---|
| `python -m trader status` | Mode, kill switch, account, open tickets |
| `python -m trader analyze` | Each stock's trend, target %, current % and stop, without ordering |
| `python -m trader plan` | Build the risk-checked orders (tickets) to get back to target |
| `python -m trader veto SYM --reason "..."` | Cancel a buy ticket |
| `python -m trader paper-fill` | Simulate fills for open paper tickets |
| `python -m trader account --equity E --cash C --position SYM:QTY:AVG` | Record the live agentic account (Claude does this) |
| `python -m trader record-fill ...` | Record a live fill in the journal |
| `python -m trader backtest [--years N] [--symbols ...]` | Test the rules on real history vs. buy-and-hold |
| `python -m trader simulate [--runs 100]` | Run many backtests on random simulated markets |
| `python -m trader report` | Performance so far vs. the benchmark |
| `python -m trader kill` / `resume` | Emergency stop on/off |

## Customizing

- **Watchlist:** edit `[watchlist] symbols` in `config.toml`. Stick to liquid large-cap stocks.
- **Core vs. trend split:** `core_pct` and `trend_pct` in `[portfolio]`. More core means
  closer to buy-and-hold: more return in rising markets and deeper drops in falling ones.
  On simulated markets, 65/30 averaged +42% with a -27% average worst drop; 80/15 averaged
  +46% (-29%); all core averaged +49% (-31%). Compare on real prices with `backtest`.
- **Stop loss:** `stop_loss_pct` in `[risk]` (trend slice only; 0 = off). In simulations
  5%, 10% and no stop gave almost the same returns; tighter stops lowered the win rate.
- **Fractional shares:** on by default, so every stock can sit at its target. Robinhood
  may only accept fractional orders as market or dollar-based orders. If your limit orders
  for partial shares are rejected, set `order_type = "market"`, or turn fractional shares
  off, which needs a much larger account.
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

The tests cover the indicators, trend signals, stops, targets and rebalancing, every
circuit breaker, the guard's allow/block rules (including fail-closed), paper fills, data
parsing, a no-lookahead check on the backtester, and an end-to-end CLI run.

---

*Not financial advice. This software is provided as-is with no warranty. Trading
involves risk of loss, and you are responsible for every trade it places.*
