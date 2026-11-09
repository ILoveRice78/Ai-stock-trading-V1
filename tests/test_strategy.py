from datetime import date

import numpy as np

from trader.strategy import add_indicators, evaluate, min_history

from .conftest import make_bars


def up_bars(n=260):
    return make_bars(np.linspace(100, 200, n))


def test_uptrend_detected(cfg):
    sig = evaluate("T", add_indicators(up_bars(), cfg.strategy), cfg.strategy)
    assert sig.ready and sig.trend_up


def test_downtrend_detected(cfg):
    bars = make_bars(np.linspace(200, 100, 260))
    sig = evaluate("T", add_indicators(bars, cfg.strategy), cfg.strategy)
    assert sig.ready and not sig.trend_up


def test_not_ready_with_short_history(cfg):
    sig = evaluate("T", add_indicators(make_bars(np.linspace(100, 120, 100)), cfg.strategy), cfg.strategy)
    assert not sig.ready and "history" in sig.note
    assert min_history(cfg.strategy) > 100


def test_trend_slice_stop_loss_uses_current_price(cfg):
    bars = up_bars()
    ind = add_indicators(bars, cfg.strategy)
    kw = dict(in_trend=True, entry_date=bars.index[-1].date(), entry_price=200.0, stop_loss_pct=10.0)
    assert evaluate("T", ind, cfg.strategy, current_price=181.0, **kw).exit == []
    sig = evaluate("T", ind, cfg.strategy, current_price=179.0, **kw)
    assert sig.stop_price == 180.0
    assert any("stop loss hit" in r for r in sig.exit)


def test_stop_loss_can_be_turned_off(cfg):
    bars = up_bars()
    ind = add_indicators(bars, cfg.strategy)
    sig = evaluate("T", ind, cfg.strategy, in_trend=True, entry_date=bars.index[-1].date(),
                   entry_price=200.0, stop_loss_pct=0.0, current_price=150.0)
    assert sig.stop_price is None
    assert not any("stop" in r for r in sig.exit)


def test_trailing_stop_takes_over_on_winners(cfg):
    closes = list(np.linspace(100, 200, 260)) + [190, 180, 170]
    bars = make_bars(closes)
    ind = add_indicators(bars, cfg.strategy)
    sig = evaluate("T", ind, cfg.strategy, in_trend=True, entry_date=bars.index[50].date(),
                   entry_price=120.0, stop_loss_pct=10.0)
    assert sig.stop_price > 120.0
    assert any("trailing stop hit" in r for r in sig.exit)


def test_trend_break_exit(cfg):
    closes = list(np.linspace(100, 200, 260)) + list(np.linspace(200, 120, 60))
    bars = make_bars(closes)
    ind = add_indicators(bars, cfg.strategy)
    sig = evaluate("T", ind, cfg.strategy, in_trend=True, entry_date=bars.index[-1].date(),
                   entry_price=float(closes[-1]), stop_loss_pct=0.0)
    assert any("trend broken" in r for r in sig.exit)


def test_signal_date_is_last_bar(cfg):
    bars = up_bars()
    sig = evaluate("T", add_indicators(bars, cfg.strategy), cfg.strategy)
    assert sig.date == bars.index[-1].date() and isinstance(sig.date, date)
