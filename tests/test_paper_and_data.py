import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from trader import state, tickets
from trader.data import load_bars_file, normalize_bars, split_completed
from trader.paper import paper_fill
from trader.portfolio import (
    buys_blocked_today,
    entry_dates_from_journal,
    equity_reference,
    load_paper_account,
    record_equity,
    trades_today,
)


def issue_paper(side="buy", symbol="AAPL", quantity=10, limit=100.0):
    return tickets.issue_tickets(
        [{"symbol": symbol, "side": side, "quantity": quantity, "order_type": "limit",
          "limit_price": limit, "ref_price": 99.5, "notional": quantity * limit, "reason": "t",
          "stop_price": None, "closes_position": side == "sell"}],
        mode="paper", ttl_minutes=60,
    )[0]


def test_paper_buy_then_sell(cfg):
    issue_paper("buy", quantity=10, limit=100.0)
    [r] = paper_fill(cfg, {"AAPL": 99.0})
    assert r["status"] == "filled"
    acct = load_paper_account(cfg)
    assert acct.positions["AAPL"].quantity == 10
    assert acct.cash == pytest.approx(10_000 - 10 * 99.0 * 1.0005)

    issue_paper("sell", quantity=10, limit=105.0)
    [r] = paper_fill(cfg, {"AAPL": 110.0})
    assert r["status"] == "filled" and r["realized_pnl"] > 0
    acct = load_paper_account(cfg)
    assert "AAPL" not in acct.positions
    assert acct.cash > 10_000


def test_paper_buy_above_limit_unfilled(cfg):
    issue_paper("buy", limit=100.0)
    [r] = paper_fill(cfg, {"AAPL": 103.0})
    assert r["status"] == "unfilled"
    assert load_paper_account(cfg).cash == 10_000


def test_paper_fill_ignores_live_tickets(cfg):
    tickets.issue_tickets(
        [{"symbol": "AAPL", "side": "buy", "quantity": 1, "order_type": "limit", "limit_price": 100,
          "ref_price": 99, "notional": 100, "reason": "t", "stop_price": None, "closes_position": False}],
        mode="live", ttl_minutes=60,
    )
    assert paper_fill(cfg, {"AAPL": 99.0}) == []


def test_journal_derived_state(cfg):
    today = date(2026, 11, 9)
    state.log_event("fill", mode="paper", symbol="MSFT", side="buy", trade_date="2026-11-02")
    state.log_event("fill", mode="paper", symbol="AAPL", side="buy", trade_date="2026-11-03")
    state.log_event("fill", mode="paper", symbol="AAPL", side="sell", closes_position=True, trade_date="2026-11-09")
    state.log_event("order_submitted", mode="live", symbol="NVDA", side="buy", trade_date="2026-11-09")
    assert entry_dates_from_journal(state.read_journal(), "paper") == {"MSFT": date(2026, 11, 2)}
    assert trades_today("paper", today) == 1
    assert trades_today("live", today) == 1
    assert set(buys_blocked_today("paper", today)) == {"AAPL"}  # sold today
    assert "already placed" in buys_blocked_today("live", today)["NVDA"]
    assert buys_blocked_today("paper", date(2026, 11, 10)) == {}

    record_equity("paper", 10_000, date(2026, 11, 5))
    record_equity("paper", 11_000, date(2026, 11, 6))
    record_equity("paper", 10_500, date(2026, 11, 9))
    day_start, peak = equity_reference("paper", today, 10_400)
    assert day_start == 11_000 and peak == 11_000


def test_robinhood_historicals_json(tmp_path):
    bars = {"historicals": [
        {"begins_at": "2026-11-05T14:30:00Z", "open_price": "10.0", "high_price": "11", "low_price": "9.5",
         "close_price": "10.5", "volume": 1000},
        {"begins_at": "2026-11-06T14:30:00Z", "open_price": "10.5", "high_price": "12", "low_price": "10",
         "close_price": "11.5", "volume": 2000},
    ]}
    path = tmp_path / "AAPL.json"
    path.write_text(json.dumps(bars))
    df = load_bars_file(path)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].tolist() == [10.5, 11.5]
    assert df.index[0] == pd.Timestamp("2026-11-05")


def test_csv_with_capitalized_columns(tmp_path):
    path = tmp_path / "X.csv"
    path.write_text("Date,Open,High,Low,Close,Volume\n2026-01-02,1,2,0.5,1.5,100\n")
    assert load_bars_file(path)["close"].iloc[0] == 1.5


def test_normalize_rejects_missing_columns():
    with pytest.raises(Exception):
        normalize_bars(pd.DataFrame({"date": ["2026-01-02"], "close": [1.0]}))


def test_split_completed_drops_todays_partial_bar():
    idx = pd.to_datetime(["2026-11-05", "2026-11-06"])
    df = pd.DataFrame({"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1.0, 2.0], "volume": [1, 1]}, index=idx)
    during = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
    after = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)   # 17:00 ET
    completed, latest = split_completed(df, during, "America/New_York")
    assert len(completed) == 1 and latest == 2.0
    completed, _ = split_completed(df, after, "America/New_York")
    assert len(completed) == 2
