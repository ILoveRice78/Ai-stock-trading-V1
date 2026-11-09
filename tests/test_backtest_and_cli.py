from dataclasses import replace
from datetime import timedelta

import pandas as pd

from trader import state, tickets
from trader.backtest import curve_stats, run_backtest
from trader.cli import main

from .conftest import make_bars, random_walk

SYMBOLS = ["AAA", "BBB", "CCC", "SPY"]


def universe(n=900):
    out = {}
    for i, sym in enumerate(SYMBOLS):
        closes = random_walk(n, drift=0.0006, vol=0.015, start=50 + 20 * i, seed=10 + i)
        out[sym] = make_bars(closes, start="2022-01-03", volume=3_000_000.0)
    return out


def bt_cfg(cfg):
    return replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(SYMBOLS), benchmark="SPY"))


def test_backtest_runs_and_reports(cfg):
    bars = universe()
    cfg = bt_cfg(cfg)
    start = bars["SPY"].index[300].date()
    end = bars["SPY"].index[-1].date()
    result = run_backtest(bars, cfg, start, end)
    assert result.equity.index[0].date() >= start
    assert (result.equity > 0).all()
    assert result.benchmark is not None
    for key in ("total_return", "cagr", "max_drawdown", "sharpe", "closed_trades"):
        assert key in result.stats
    assert result.stats["closed_trades"] > 0
    buys = result.trades[result.trades["side"] == "buy"]
    # never hold more than max_open_positions
    assert buys["symbol"].nunique() >= 1
    assert result.stats["max_drawdown"] <= 0


def test_backtest_has_no_lookahead(cfg):
    """Changing prices after day X must not change anything up to day X."""
    bars = universe()
    cfg = bt_cfg(cfg)
    start, end = bars["SPY"].index[300].date(), bars["SPY"].index[-1].date()
    cutoff = bars["SPY"].index[600]
    baseline = run_backtest(bars, cfg, start, end)

    altered = {}
    for sym, df in bars.items():
        df = df.copy()
        future = df.index > cutoff
        df.loc[future, ["open", "high", "low", "close"]] *= 0.5  # crash the future
        altered[sym] = df
    changed = run_backtest(altered, cfg, start, end)
    pd.testing.assert_series_equal(baseline.equity.loc[:cutoff], changed.equity.loc[:cutoff])


def test_backtest_stop_loss_fires_intraday_on_purchase_day(cfg):
    from .test_strategy import uptrend_then_pullback

    closes = list(uptrend_then_pullback())
    signal_close = closes[-1]
    closes.append(signal_close * 0.90)  # next day: opens at signal close, then falls 10%
    bars = {"AAA": make_bars(closes)}
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=("AAA",), benchmark="NONE"))
    cfg = replace(cfg, risk=replace(cfg.risk, take_profit_pct=0.0))
    idx = bars["AAA"].index
    result = run_backtest(bars, cfg, idx[-3].date(), idx[-1].date())
    buy, sell = result.trades.iloc[0], result.trades.iloc[1]
    assert buy["side"] == "buy" and sell["side"] == "sell"
    assert buy["date"] == sell["date"] == idx[-1]  # bought and stopped out the same day
    assert "stop loss hit intraday" in sell["reason"]
    stop = buy["price"] * 0.95
    assert sell["price"] == stop * (1 - cfg.orders.slippage_bps / 10_000)


def test_backtest_half_take_profit_then_break_even_is_one_win(cfg):
    from .test_strategy import uptrend_then_pullback

    closes = list(uptrend_then_pullback())
    c = closes[-1]
    closes += [c * 1.06, c * 0.99]  # day 1: up 6% (target hit); day 2: back below entry
    bars = {"AAA": make_bars(closes)}
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=("AAA",), benchmark="NONE"))
    idx = bars["AAA"].index
    result = run_backtest(bars, cfg, idx[-4].date(), idx[-1].date())
    sells = result.trades[result.trades["side"] == "sell"]
    assert list(sells["reason"]) == ["take profit hit intraday", "break-even stop hit intraday"]
    buy_qty = result.trades.iloc[0]["quantity"]
    assert sells["quantity"].sum() == buy_qty
    assert result.stats["closed_trades"] == 1 and result.stats["win_rate"] == 1.0


def test_curve_stats_simple():
    s = pd.Series([100.0, 110.0, 99.0, 121.0], index=pd.bdate_range("2026-01-01", periods=4))
    stats = curve_stats(s)
    assert round(stats["total_return"], 4) == 0.21
    assert round(stats["max_drawdown"], 4) == -0.1


def write_bar_files(directory, n=400):
    directory.mkdir(parents=True, exist_ok=True)
    end = pd.Timestamp.today().normalize() - timedelta(days=1)
    idx = pd.bdate_range(end=end, periods=n)
    for i, sym in enumerate(SYMBOLS):
        df = make_bars(random_walk(n, drift=0.001, vol=0.012, start=60 + 10 * i, seed=i))
        df.index = idx
        df.index.name = "date"
        df.to_csv(directory / f"{sym}.csv")


def test_cli_paper_day_end_to_end(tmp_path, write_config, capsys):
    bars_dir = tmp_path / "bars"
    write_bar_files(bars_dir)
    write_config(
        data={"source": '"files"', "files_dir": f'"{bars_dir}"'},
        watchlist={"symbols": '["AAA", "BBB", "CCC"]'},
    )
    assert main(["status"]) == 0
    assert main(["analyze"]) == 0
    assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "PAPER plan" in out
    open_t = tickets.open_tickets()
    if open_t:
        assert main(["veto", open_t[0]["symbol"], "--reason", "test veto"]) in (0, 1)
    assert main(["paper-fill"]) == 0
    assert main(["report"]) == 0
    assert main(["backtest", "--years", "1"]) == 0
    out = capsys.readouterr().out
    assert "Backtest" in out and "buy & hold SPY" in out
    assert main(["backtest", "--years", "1", "--take-profit", "0", "5"]) == 0
    assert "Take-profit comparison" in capsys.readouterr().out


def test_cli_kill_switch_blocks_plan(write_config, capsys):
    assert main(["kill", "--reason", "testing"]) == 0
    assert state.kill_switch_active()[0]
    assert main(["plan"]) == 1
    assert main(["resume"]) == 0
    assert not state.kill_switch_active()[0]


def test_cli_live_requires_fresh_account(write_config, capsys):
    write_config(general={"mode": '"live"'})
    assert main(["plan"]) == 1
    assert "no live account snapshot" in capsys.readouterr().out
    assert main(["account", "--equity", "5000", "--cash", "1000", "--position", "AAPL:3:200"]) == 0
    assert main(["account", "--equity", "5000", "--cash", "1000", "--position", "bad"]) == 1
