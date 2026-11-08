from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trader.config import load_config


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test gets its own empty state directory."""
    monkeypatch.setenv("TRADER_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """Write a minimal config.toml (unset values use code defaults); returns its path.

    Values are raw TOML, e.g. write_config(general={"mode": '"live"'}).
    """

    def _write(**sections):
        text = ""
        for section, values in sections.items():
            text += f"[{section}]\n" + "".join(f"{k} = {v}\n" for k, v in values.items())
        path = tmp_path / "config.toml"
        path.write_text(text)
        monkeypatch.setenv("TRADER_CONFIG", str(path))
        return path

    return _write


def make_bars(
    closes, start="2022-01-03", volume=5_000_000.0, spread=0.01
) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    high = np.maximum(opens, closes) * (1 + spread)
    low = np.minimum(opens, closes) * (1 - spread)
    vol = np.full(len(closes), volume) if np.isscalar(volume) else np.asarray(volume, float)
    return pd.DataFrame(
        {"open": opens, "high": high, "low": low, "close": closes, "volume": vol}, index=idx
    )


def random_walk(n, drift=0.0005, vol=0.012, start=100.0, seed=0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
