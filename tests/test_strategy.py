from datetime import date

import numpy as np

from trader.strategy import add_indicators, evaluate, min_history

from .conftest import make_bars


def uptrend_then_pullback(n_up=260, drops=4, drop_pct=0.015, rebound_pct=0.02):
    closes = list(np.linspace(100, 200, n_up))
    for _ in range(drops):
        closes.append(closes[-1] * (1 - drop_pct))
    closes.append(closes[-1] * (1 + rebound_pct))
    return np.array(closes)


def test_pullback_in_uptrend_is_a_buy(cfg):
    ind = add_indicators(make_bars(uptrend_then_pullback()), cfg.strategy)
    sig = evaluate("TEST", ind, cfg.strategy)
    assert sig.ready and sig.trend_up
    assert any("pullback" in r for r in sig.entry), (sig.rsi, sig.entry)


def test_no_buy_in_downtrend(cfg):
    closes = uptrend_then_pullback()[::-1]  # same shape, falling
    ind = add_indicators(make_bars(closes), cfg.strategy)
    sig = evaluate("TEST", ind, cfg.strategy)
    assert not sig.trend_up
    assert sig.entry == []


def test_breakout_on_volume_is_a_buy(cfg):
    rng = np.random.default_rng(1)
    closes = np.linspace(100, 150, 260) * (1 + rng.normal(0, 0.004, 260))
    closes = np.append(closes, closes.max() * 1.03)
    volume = np.full(len(closes), 4_000_000.0)
    volume[-1] = 9_000_000.0
    ind = add_indicators(make_bars(closes, volume=volume), cfg.strategy)
    sig = evaluate("TEST", ind, cfg.strategy)
    assert any("breakout" in r for r in sig.entry)


def test_trailing_stop_exit(cfg):
    closes = list(np.linspace(100, 200, 260))
    for _ in range(3):
        closes.append(closes[-1] * 0.95)
    bars = make_bars(closes)
    ind = add_indicators(bars, cfg.strategy)
    entry = bars.index[240].date()
    sig = evaluate("TEST", ind, cfg.strategy, held=True, entry_date=entry)
    assert any("trailing stop" in r for r in sig.exit)
    assert sig.stop_price is not None and sig.stop_price > sig.close


def test_hold_while_trend_intact(cfg):
    bars = make_bars(np.linspace(100, 200, 260))
    ind = add_indicators(bars, cfg.strategy)
    sig = evaluate("TEST", ind, cfg.strategy, held=True, entry_date=bars.index[230].date())
    assert sig.exit == []


def test_not_ready_with_short_history(cfg):
    ind = add_indicators(make_bars(np.linspace(100, 120, 100)), cfg.strategy)
    sig = evaluate("TEST", ind, cfg.strategy)
    assert not sig.ready and "history" in sig.note
    assert min_history(cfg.strategy) > 100


def test_signal_date_is_last_bar(cfg):
    bars = make_bars(uptrend_then_pullback())
    sig = evaluate("TEST", add_indicators(bars, cfg.strategy), cfg.strategy)
    assert sig.date == bars.index[-1].date()
    assert isinstance(sig.date, date)
