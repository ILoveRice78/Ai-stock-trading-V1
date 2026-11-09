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


def plan_for(cfg, account, frames, day_start=None, peak=None, session=None, current=None, no_buy=None):
    eq = account.equity({s: float(f["close"].iloc[-1]) for s, f in frames.items()})
    return build_plan(
        account, frames, cfg, session or session_after(frames),
        day_start if day_start is not None else eq, peak if peak is not None else eq,
        current_prices=current, no_buy=no_buy,
    )


def test_position_sizing_uses_risk_budget_and_caps(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    frames = buy_frames(cfg, ["AAA"])
    plan = plan_for(cfg, Account(cash=10_000), frames)
    assert len(plan.orders) == 1
    o = plan.orders[0]
    row = frames["AAA"].iloc[-1]
    limit = round(row["close"] * 1.005, 2)
    by_risk = 10_000 * 0.01 / (limit * 0.05)  # 1% of equity lost at a 5% stop
    by_size = 10_000 * 0.15 / limit
    by_cash = (10_000 - 500) / limit
    assert o.side == "buy" and o.order_type == "limit"
    assert o.limit_price == limit
    assert o.quantity == math.floor(min(by_risk, by_size, by_cash))
    assert o.notional <= 10_000 * 0.15 + 1e-6
    assert o.stop_price == round(limit * 0.95, 2)


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


def test_no_daily_trade_cap(cfg):
    symbols = [f"S{i}" for i in range(8)]
    cfg = with_watchlist(cfg, symbols, max_open_positions=10, max_position_pct=10.0)
    plan = plan_for(cfg, Account(cash=100_000), buy_frames(cfg, symbols))
    assert len(plan.orders) == 8


def test_no_buy_list_is_respected(cfg):
    cfg = with_watchlist(cfg, ["AAA", "BBB"])
    plan = plan_for(cfg, Account(cash=10_000), buy_frames(cfg, ["AAA", "BBB"]),
                    no_buy={"AAA": "sold earlier today; not re-buying the same day"})
    assert [o.symbol for o in plan.orders] == ["BBB"]
    assert any("sold earlier today" in why for sym, why in plan.skipped if sym == "AAA")


def test_buy_skipped_if_price_already_fell_past_stop(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    frames = buy_frames(cfg, ["AAA"])
    close = float(frames["AAA"]["close"].iloc[-1])
    plan = plan_for(cfg, Account(cash=10_000), frames, current={"AAA": close * 0.94})
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


def uptrend_frames(cfg):
    bars = make_bars(np.linspace(100, 200, 260))
    return bars, {"HELD": add_indicators(bars, cfg.strategy)}


def test_stop_loss_uses_current_price(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    bars, frames = uptrend_frames(cfg)
    session = bars.index[-1].date() + timedelta(days=1)
    account = Account(cash=5_000, positions={"HELD": Position("HELD", 10, 200.0, entry_date=session)})
    # down 4% from purchase price: hold
    plan = plan_for(cfg, account, frames, session=session, current={"HELD": 192.0})
    assert plan.orders == []
    # down 6%: sell, even though it was bought today (same-day trades are allowed)
    plan = plan_for(cfg, account, frames, session=session, current={"HELD": 188.0})
    [order] = plan.orders
    assert order.side == "sell" and order.quantity == 10
    assert "stop loss hit" in order.reason
    assert order.limit_price == round(188.0 * 0.995, 2)


def test_stop_loss_is_five_percent_of_purchase_price(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    bars, frames = uptrend_frames(cfg)
    account = Account(cash=5_000, positions={"HELD": Position("HELD", 10, 200.0, entry_date=bars.index[-1].date())})
    plan = plan_for(cfg, account, frames)
    assert plan.signals["HELD"].stop_price == 190.0


def test_trailing_stop_takes_over_on_winners(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    held_bars = crash_frame(cfg)
    frames = {"HELD": add_indicators(held_bars, cfg.strategy)}
    # bought at 120, ran to 200: the stop has moved up well above the 5% loss stop
    account = Account(cash=5_000, positions={"HELD": Position("HELD", 10, 120.0, entry_date=held_bars.index[50].date())})
    plan = plan_for(cfg, account, frames)
    sig = plan.signals["HELD"]
    assert sig.stop_price > 120.0
    assert any("trailing stop hit" in r for r in sig.exit)


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


def test_take_profit_sells_half_then_break_even_stop(cfg):
    cfg = with_watchlist(cfg, ["AAA"])
    bars, frames = uptrend_frames(cfg)
    session = bars.index[-1].date() + timedelta(days=1)
    pos = Position("HELD", 10, 200.0, entry_date=bars.index[-1].date())
    plan = plan_for(cfg, Account(cash=5_000, positions={"HELD": pos}), frames,
                    session=session, current={"HELD": 211.0})
    [order] = plan.orders
    assert order.side == "sell" and order.quantity == 5 and not order.closes_position
    assert "break-even" in order.reason

    pos = Position("HELD", 5, 200.0, entry_date=bars.index[-1].date(), scaled_out=True)
    plan = plan_for(cfg, Account(cash=5_000, positions={"HELD": pos}), frames,
                    session=session, current={"HELD": 199.5})
    [order] = plan.orders
    assert order.quantity == 5 and order.closes_position
    assert "break-even stop hit" in order.reason


def test_take_profit_on_one_share_sells_it(cfg):
    from trader.planner import take_profit_quantity

    assert take_profit_quantity(1, 50, fractional=False) == 1
    assert take_profit_quantity(7, 50, fractional=False) == 4
    assert take_profit_quantity(0.5, 50, fractional=True) == 0.25
    assert take_profit_quantity(10, 100, fractional=False) == 10
