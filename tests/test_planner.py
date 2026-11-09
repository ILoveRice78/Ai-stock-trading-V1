from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from trader.planner import build_plan, empty_trend_book
from trader.portfolio import Account, Position
from trader.strategy import add_indicators

from .conftest import make_bars

UP = np.linspace(100, 200, 260)      # ends in an uptrend at 200
DOWN = np.linspace(200, 100, 260)    # ends in a downtrend at 100


def setup(cfg, shapes, **portfolio):
    """cfg with these watchlist symbols, and indicator frames shaped as given."""
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(shapes)))
    if portfolio:
        cfg = replace(cfg, portfolio=replace(cfg.portfolio, **portfolio))
    frames = {s: add_indicators(make_bars(c), cfg.strategy) for s, c in shapes.items()}
    session = next(iter(frames.values())).index[-1].date() + timedelta(days=1)
    return cfg, frames, session


def plan(cfg, account, frames, session, book=None, day_start=None, peak=None, **kw):
    prices = {s: float(f["close"].iloc[-1]) for s, f in frames.items()}
    prices.update(kw.get("current_prices") or {})
    eq = account.equity(prices)
    return build_plan(account, frames, cfg, session,
                      day_start if day_start is not None else eq,
                      peak if peak is not None else eq,
                      trend_book=book, **kw)


def test_core_targets_and_first_buys(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN, "B": DOWN, "C": DOWN, "D": DOWN})
    cfg = replace(cfg, risk=replace(cfg.risk, fractional_shares=False))
    p = plan(cfg, Account(cash=10_000), frames, session)
    core = cfg.portfolio.core_pct / 100 / 4
    assert p.targets == {s: pytest.approx(core) for s in "ABCD"}
    assert p.trend_entries == []
    assert sorted(o.symbol for o in p.orders) == list("ABCD")
    assert all(o.side == "buy" and o.limit_price == round(100 * 1.005, 2) for o in p.orders)
    assert all(o.notional <= core * 10_000 + 1e-6 for o in p.orders)
    assert sum(o.notional for o in p.orders) <= 10_000 * 0.95


def test_uptrend_gets_a_trend_slice(cfg):
    cfg, frames, session = setup(cfg, {"A": UP, "B": DOWN})
    p = plan(cfg, Account(cash=10_000), frames, session)
    core, trend = cfg.portfolio.core_pct / 200, cfg.portfolio.trend_pct / 200
    assert p.trend_entries == ["A"]
    assert p.trend_book["positions"]["A"]["entry_price"] == 200.0
    assert p.targets["A"] == pytest.approx(core + trend)
    assert p.targets["B"] == pytest.approx(core)
    a = next(o for o in p.orders if o.symbol == "A")
    assert "trend entry" in a.reason


def test_no_trade_when_close_to_target(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN, "B": DOWN})
    # each at 40% of a $10,000 account at $100
    account = Account(cash=2_000, positions={"A": Position("A", 40, 100), "B": Position("B", 41, 100)})
    assert plan(cfg, account, frames, session).orders == []


def test_rebalance_sells_overweight(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN, "B": DOWN})
    cfg = replace(cfg, risk=replace(cfg.risk, fractional_shares=False))
    account = Account(cash=0, positions={"A": Position("A", 70, 100), "B": Position("B", 30, 100)})
    p = plan(cfg, account, frames, session)
    sell = next(o for o in p.orders if o.side == "sell")
    assert sell.symbol == "A" and sell.quantity == 30 and not sell.closes_position
    assert "rebalance" in sell.reason
    buy = next(o for o in p.orders if o.side == "buy")
    assert buy.symbol == "B"  # funded by the sale


def test_stock_not_on_watchlist_is_sold(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN})
    frames["OLD"] = frames["A"]
    account = Account(cash=0, positions={"OLD": Position("OLD", 50, 100)})
    p = plan(cfg, account, frames, session)
    old = next(o for o in p.orders if o.symbol == "OLD")
    assert old.side == "sell" and old.quantity == 50 and old.closes_position
    assert "not on the watchlist" in old.reason
    assert any(o.symbol == "A" and o.side == "buy" for o in p.orders)


def held_in_trend(cfg, entry_price=200.0, entry_offset=0):
    cfg, frames, session = setup(cfg, {"A": UP, "B": DOWN})
    core, trend = cfg.portfolio.core_pct / 200, cfg.portfolio.trend_pct / 200
    qty_a = round((core + trend) * 10_000 / 200)
    qty_b = round(core * 10_000 / 100)
    account = Account(cash=10_000 - qty_a * 200 - qty_b * 100,
                      positions={"A": Position("A", qty_a, 200), "B": Position("B", qty_b, 100)})
    entry_date = (session - timedelta(days=entry_offset)).isoformat()
    book = {"positions": {"A": {"entry_price": entry_price, "entry_date": entry_date}}, "last_exit": {}}
    return cfg, frames, session, account, book


def test_trend_slice_stop_loss_sells_back_to_core(cfg):
    cfg, frames, session, account, book = held_in_trend(cfg)
    p = plan(cfg, account, frames, session, book=book, current_prices={"A": 179.0})
    assert [t.symbol for t in p.trend_exits] == ["A"]
    assert "stop loss hit" in p.trend_exits[0].reason
    assert "A" not in p.trend_book["positions"]
    assert p.trend_book["last_exit"]["A"] == session.isoformat()
    sell = next(o for o in p.orders if o.symbol == "A")
    assert sell.side == "sell" and not sell.closes_position  # core slice is kept
    assert "trend exit" in sell.reason


def test_trend_slice_kept_above_stop(cfg):
    cfg, frames, session, account, book = held_in_trend(cfg)
    p = plan(cfg, account, frames, session, book=book, current_prices={"A": 185.0})
    assert p.trend_exits == [] and "A" in p.trend_book["positions"]


def test_reentry_waits_after_exit(cfg):
    cfg, frames, session = setup(cfg, {"A": UP, "B": DOWN})
    book = empty_trend_book()
    book["last_exit"]["A"] = (session - timedelta(days=3)).isoformat()
    p = plan(cfg, Account(cash=10_000), frames, session, book=book)
    assert p.trend_entries == []
    assert any("waiting" in why for sym, why in p.skipped if sym == "A")
    book["last_exit"]["A"] = (session - timedelta(days=cfg.portfolio.reentry_wait_days)).isoformat()
    assert plan(cfg, Account(cash=10_000), frames, session, book=book).trend_entries == ["A"]


def test_halts_block_buys_but_not_sells(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN, "B": DOWN})
    account = Account(cash=0, positions={"A": Position("A", 70, 100), "B": Position("B", 30, 100)})
    p = plan(cfg, account, frames, session, day_start=11_000)
    assert any("daily loss" in h for h in p.halts)
    assert [o.side for o in p.orders] == ["sell"]
    p = plan(cfg, account, frames, session, peak=12_500)
    assert any("drawdown" in h for h in p.halts)
    assert all(o.side == "sell" for o in p.orders)


def test_trading_disabled_blocks_buys(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN})
    cfg = replace(cfg, general=replace(cfg.general, trading_enabled=False))
    p = plan(cfg, Account(cash=10_000), frames, session)
    assert p.orders == [] and p.halts


def test_no_buy_list_is_respected(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN, "B": DOWN})
    p = plan(cfg, Account(cash=10_000), frames, session, no_buy={"A": "sold earlier today"})
    assert [o.symbol for o in p.orders] == ["B"]


def test_small_account_needs_fractional_shares(cfg):
    cfg, frames, session = setup(cfg, {"A": UP, "B": UP})
    cfg = replace(cfg, risk=replace(cfg.risk, fractional_shares=False))
    p = plan(cfg, Account(cash=300), frames, session)
    assert p.orders == []
    assert any("one share" in why and "fractional_shares" in why for _, why in p.skipped)
    cfg = replace(cfg, risk=replace(cfg.risk, fractional_shares=True))
    p = plan(cfg, Account(cash=300), frames, session)
    assert len(p.orders) == 2 and all(0 < o.quantity < 1 for o in p.orders)


def test_illiquid_or_stale_not_bought(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN})
    frames["A"] = add_indicators(make_bars(DOWN, volume=1_000), cfg.strategy)
    p = plan(cfg, Account(cash=10_000), frames, session)
    assert p.orders == [] and any("illiquid" in why for _, why in p.skipped)
    cfg, frames, session = setup(cfg, {"A": DOWN})
    p = plan(cfg, Account(cash=10_000), frames, session + timedelta(days=30))
    assert p.orders == [] and any("stale" in why for _, why in p.skipped)


def test_market_orders_have_no_limit(cfg):
    cfg, frames, session = setup(cfg, {"A": DOWN})
    cfg = replace(cfg, orders=replace(cfg.orders, order_type="market"))
    p = plan(cfg, Account(cash=10_000), frames, session)
    assert p.orders[0].limit_price is None and p.orders[0].order_type == "market"


def test_plan_does_not_mutate_trend_book(cfg):
    cfg, frames, session = setup(cfg, {"A": UP})
    book = empty_trend_book()
    plan(cfg, Account(cash=10_000), frames, session, book=book)
    assert book == empty_trend_book()
