from dataclasses import replace
from datetime import timedelta

import pandas as pd

from trader import state, tickets
from trader.backtest import curve_stats, run_backtest
from trader.cli import main
from trader.simulate import make_market

from .conftest import make_bars, random_walk

SYMBOLS = ["AAA", "BBB", "CCC", "SPY"]


def universe(n=900):
    out = {}
    for i, sym in enumerate(SYMBOLS):
        closes = random_walk(n, drift=0.0006, vol=0.015, start=50 + 20 * i, seed=10 + i)
        out[sym] = make_bars(closes, start="2022-01-03", volume=3_000_000.0)
    return out


def bt_cfg(cfg, symbols=("AAA", "BBB", "CCC")):
    return replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(symbols), benchmark="SPY"))


def test_backtest_runs_and_reports(cfg):
    bars = universe()
    cfg = bt_cfg(cfg)
    start, end = bars["SPY"].index[300].date(), bars["SPY"].index[-1].date()
    result = run_backtest(bars, cfg, start, end)
    assert result.equity.index[0].date() >= start
    assert (result.equity > 0).all()
    for key in ("total_return", "cagr", "max_drawdown", "sharpe", "trend_trades",
                "orders_filled", "avg_invested"):
        assert key in result.stats
    assert result.stats["avg_invested"] > 0.75
    assert result.benchmark is not None and result.hold is not None
    assert result.stats["orders_filled"] >= 3


def test_core_only_tracks_buy_and_hold(cfg):
    """With no trend slice, the bot is a rebalanced buy-and-hold of the watchlist."""
    data, idx, _ = make_market(seed=3, days=500, n_stocks=6)
    stocks = tuple(s for s in data if s != "INDEX")
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=stocks, benchmark="INDEX"),
                  portfolio=replace(cfg.portfolio, core_pct=95.0, trend_pct=0.0),
                  risk=replace(cfg.risk, max_drawdown_pct=100.0, daily_loss_limit_pct=100.0))
    result = run_backtest(data, cfg, idx[300].date(), idx[-1].date())
    assert result.stats["avg_invested"] > 0.9
    assert result.stats["trend_trades"] == 0
    bot, hold = result.stats["total_return"], result.hold_stats["total_return"]
    assert abs(bot - 0.95 * hold) < 0.15


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
        df.loc[df.index > cutoff, ["open", "high", "low", "close"]] *= 0.5
        altered[sym] = df
    changed = run_backtest(altered, cfg, start, end)
    pd.testing.assert_series_equal(baseline.equity.loc[:cutoff], changed.equity.loc[:cutoff])


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
    assert "target" in capsys.readouterr().out
    assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "PAPER plan" in out and "Approved orders" in out
    open_t = tickets.open_tickets()
    assert {t["symbol"] for t in open_t} == {"AAA", "BBB", "CCC"}
    assert main(["paper-fill"]) == 0
    assert main(["status"]) == 0
    # a second run right after should find everything on target
    assert main(["plan"]) == 0
    assert "No orders right now" in capsys.readouterr().out
    assert main(["report"]) == 0
    assert main(["backtest", "--years", "1"]) == 0
    out = capsys.readouterr().out
    assert "Backtest" in out and "hold same stocks" in out and "hold SPY" in out
    assert main(["simulate", "--runs", "2", "--years", "1", "--processes", "1"]) == 0
    assert "Made money in" in capsys.readouterr().out


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
