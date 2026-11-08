import math
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from trader.planner import build_plan
from trader.portfolio import Account, Position
from trader.strategy import add_indicators

from .conftest import make_bars
from .test_strategy import uptrend_then_pullback


def with_watchlist(cfg, symbols, **risk):
    cfg = replace(cfg, watchlist=replace(cfg.watchlist, symbols=tuple(symbols)))
    if risk:
        cfg = replace(cfg, risk=replace(cfg.risk, **risk))
    return cfg


def buy_frames(cfg, symbols, volume=5_000_000.0):
    return {s: add_indicators(make_bars(uptrend_then_pullback(), volume=volume), cfg.strategy) for s in symbols}


def session_after(frames):
    last = next(iter(frames.values())).index[-1].date()
    return last + timedelta(days=1)


def plan_for(cfg, account, frames, trades_done=0, day_start=None, peak=None, session=None):
    eq = account.equity({s: float(f["close"].iloc[-1]) for s, f in frames.items()})
    return build_plan(
        account, frames, cfg, session or session_after(frames), trades_done,
        day_start if day_start is not None else eq, peak if peak is not None else eq,
    )


def test_position_sizing_uses_risk_budget_and_caps(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    frames = buy_frames(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), frames)
    assert len(plan.orders) == 1
    o = plan.orders[0]
    row = frames["AAA"].iloc[-1]
    limit = round(row["close"] * 1.005, 2)
    by_risk = 10_000 * 0.01 / (2.0 * row["atr"])
    by_size = 10_000 * 0.15 / limit
    by_cash = (10_000 - 500) / limit
    assert o.side == "buy" and o.order_type == "limit"
    assert o.limit_price == limit
    assert o.quantity == math.floor(min(by_risk, by_size, by_cash))
    assert o.notional <= 10_000 * 0.15 + 1e-6
    assert o.stop_price < row["close"]


def test_fractional_shares_for_small_accounts(cfg):
    cfg = with_watchlist(cfg, ["AAA"], fractional_shares=True)
    frames = buy_frames(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=300), frames)
    assert len(plan.orders) == 1
    assert 0 < plan.orders[0].quantity < 1


def test_small_account_without_fractional_skips(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=300), buy_frames(cfg, ["AAA"]))
    assert plan.orders == []
    assert any("fractional" in why for _, why in plan.skipped)


def test_daily_loss_limit_halts_buys(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"]), day_start=10_500)
    assert plan.orders == []
    assert any("daily loss" in h for h in plan.halts)


def test_max_drawdown_halts_buys(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"]), peak=12_500)
    assert plan.orders == []
    assert any("drawdown" in h for h in plan.halts)


def test_trading_disabled_halts_buys(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    cfg = replace(cfg, general=replace(cfg.general, trading_enabled=False))
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"]))
    assert plan.orders == [] and plan.halts


def test_daily_trade_limit(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"]), trades_done=cfg.risk.max_trades_per_day)
    assert plan.orders == []


def test_max_open_positions_and_ranking(cfg):
    symbols = ["AAA", "BBB", "CCC"]
    cfg = with_watchlist(cfg, symbols, max_open_positions=2, max_position_pct=10.0)
    plan = plan_for(cfg, Account(cash=100_000), buy_frames(cfg, symbols))
    assert len(plan.orders) == 2
    assert any("max open positions" in why for _, why in plan.skipped)


def test_illiquid_symbol_skipped(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"], volume=1_000))
    assert plan.orders == []
    assert any("illiquid" in why for _, why in plan.skipped)


def crash_frame(cfg):
    closes = list(np.linspace(100, 200, 260)) + [190, 180, 170]
    return make_bars(closes)


def test_exit_is_planned_before_entries(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    held_bars = crash_frame(cfg)
    frames = buy_frames(cfg, ["AAA"])
    frames["HELD"] = add_indicators(held_bars, cfg.strategy)
    account = Account(cash=5_000, positions={
        "HELD": Position("HELD", 10, 150.0, entry_date=held_bars.index[200].date())
    })
    plan = plan_for(cfg, account, frames, session=held_bars.index[-1].date() + timedelta(days=1))
    assert plan.orders[0].side == "sell" and plan.orders[0].symbol == "HELD"
    assert plan.orders[0].quantity == 10
    assert plan.orders[0].limit_price < plan.orders[0].ref_price


def test_no_same_day_exit(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    held_bars = crash_frame(cfg)
    session = held_bars.index[-1].date() + timedelta(days=1)
    frames = {"HELD": add_indicators(held_bars, cfg.strategy)}
    account = Account(cash=5_000, positions={"HELD": Position("HELD", 10, 150.0, entry_date=session)})
    plan = plan_for(cfg, account, frames, session=session)
    assert plan.orders == []
    assert any("no day trades" in why for _, why in plan.skipped)


def test_stale_data_skipped(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    frames = buy_frames(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), frames, session=session_after(frames) + timedelta(days=30))
    assert plan.orders == []
    assert any("stale" in why for _, why in plan.skipped)


def test_market_orders_have_no_limit(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    cfg = replace(cfg, orders=replace(cfg.orders, order_type="market"))
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA"]))
    assert plan.orders[0].limit_price is None
    assert plan.orders[0].order_type == "market"


@pytest.mark.parametrize("cash", [0.0, 100.0])
def test_never_spends_cash_reserve(cfg, cash):
    cfg = with_watchlist(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=cash), buy_frames(cfg, ["AAA"]))
    assert plan.orders == []
