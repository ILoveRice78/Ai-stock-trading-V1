import numpy as np
import pandas as pd
import pytest

from trader.indicators import atr, macd, rsi, sma

from .conftest import random_walk


def test_sma_matches_manual_mean():
    s = pd.Series([1.0, 2, 3, 4, 5])
    out = sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[-1] == pytest.approx(4.0)


def test_rsi_extremes_and_bounds():
    up = pd.Series(np.arange(1.0, 60.0))
    down = pd.Series(np.arange(60.0, 1.0, -1))
    flat = pd.Series(np.full(40, 10.0))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)
    assert rsi(flat).iloc[-1] == pytest.approx(50.0)
    r = rsi(pd.Series(random_walk(500))).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_rsi_is_causal():
    s = pd.Series(random_walk(300, seed=3))
    full = rsi(s)
    truncated = rsi(s.iloc[:200])
    pd.testing.assert_series_equal(full.iloc[:200], truncated)


def test_atr_constant_range():
    n = 50
    close = pd.Series(np.full(n, 100.0))
    high, low = close + 1, close - 1
    assert atr(high, low, close, 14).iloc[-1] == pytest.approx(2.0)


def test_macd_shapes():
    s = pd.Series(random_walk(100))
    line, signal, hist = macd(s)
    assert len(line) == len(signal) == len(hist) == 100
    assert (hist.dropna() == (line - signal).dropna()).all()
