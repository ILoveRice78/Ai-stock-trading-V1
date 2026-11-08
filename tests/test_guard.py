import io
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from trader import guard, state, tickets

TOOL = "mcp__robinhood-trading__place_equity_order"


@pytest.fixture
def live_cfg(cfg):
    return replace(cfg, general=replace(cfg.general, mode="live"))


def issue(side="buy", symbol="AAPL", quantity=10, limit=200.0, mode="live", ttl=60):
    return tickets.issue_tickets(
        [{
            "symbol": symbol, "side": side, "quantity": quantity, "order_type": "limit",
            "limit_price": limit, "ref_price": 199.0, "notional": quantity * (limit or 199.0),
            "reason": "test", "stop_price": None, "closes_position": side == "sell",
        }],
        mode=mode, ttl_minutes=ttl,
    )[0]


def order(**kw):
    base = {"symbol": "AAPL", "side": "buy", "quantity": 10, "type": "limit", "limit_price": 199.5}
    base.update(kw)
    return {"tool_name": TOOL, "tool_input": base}


@pytest.mark.parametrize(
    "name,kind",
    [
        ("mcp__robinhood-trading__get_portfolio", "read"),
        ("mcp__robinhood-trading__get_equity_orders", "read"),
        ("mcp__robinhood-trading__get_equity_historicals", "read"),
        ("mcp__robinhood-trading__cancel_equity_order", "cancel"),
        ("mcp__robinhood-trading__place_equity_order", "stock_order"),
        ("mcp__robinhood-trading__place_option_order", "blocked"),
        ("mcp__robinhood-trading__place_crypto_order", "blocked"),
        ("mcp__robinhood-trading__transfer_funds", "blocked"),
        ("mcp__robinhood-trading__update_settings", "blocked"),
    ],
)
def test_classify(name, kind):
    assert guard.classify(name) == kind


def test_reads_always_allowed(cfg):
    assert guard.check({"tool_name": "mcp__robinhood__get_accounts", "tool_input": {}}, cfg).allowed


def test_paper_mode_blocks_real_orders(cfg):
    issue(mode="paper")
    d = guard.check(order(), cfg)
    assert not d.allowed and "PAPER" in d.reason


def test_live_order_without_ticket_blocked(live_cfg):
    d = guard.check(order(), live_cfg)
    assert not d.allowed and "no approved BUY ticket" in d.reason


def test_live_order_matching_ticket_allowed_once(live_cfg):
    t = issue()
    assert guard.check(order(), live_cfg).allowed
    stored = {x["id"]: x for x in tickets.load_tickets()}[t["id"]]
    assert stored["status"] == "submitted"
    events = [e for e in state.read_journal() if e["event"] == "order_submitted"]
    assert len(events) == 1 and events[0]["symbol"] == "AAPL"
    # the same ticket cannot be used twice
    assert not guard.check(order(), live_cfg).allowed


def test_quantity_above_ticket_blocked(live_cfg):
    issue(quantity=10)
    d = guard.check(order(quantity=11), live_cfg)
    assert not d.allowed and "exceeds" in d.reason


def test_smaller_quantity_allowed(live_cfg):
    issue(quantity=10)
    assert guard.check(order(quantity="5"), live_cfg).allowed


def test_limit_above_approved_blocked(live_cfg):
    issue(limit=200.0)
    d = guard.check(order(limit_price=205.0), live_cfg)
    assert not d.allowed and "above the approved" in d.reason


def test_market_buy_blocked_when_limit_approved(live_cfg):
    issue()
    d = guard.check(order(type="market", limit_price=None), live_cfg)
    assert not d.allowed and "market buys" in d.reason


def test_wrong_side_blocked(live_cfg):
    issue(side="buy")
    assert not guard.check(order(side="sell"), live_cfg).allowed


def test_sell_matching_ticket_allowed(live_cfg):
    issue(side="sell", limit=190.0)
    assert guard.check(order(side="SELL", type="market", limit_price=None), live_cfg).allowed


def test_stop_orders_blocked(live_cfg):
    issue()
    d = guard.check(order(stop_price=150.0), live_cfg)
    assert not d.allowed


def test_expired_ticket_blocked(live_cfg):
    issue(ttl=60)
    later = state.utcnow() + timedelta(minutes=61)
    assert not guard.check(order(), live_cfg, now=later).allowed


def test_kill_switch_blocks_everything(live_cfg):
    issue()
    state.set_kill_switch("test")
    d = guard.check(order(), live_cfg)
    assert not d.allowed and "kill switch" in d.reason


def test_vetoed_ticket_blocked(live_cfg):
    issue()
    tickets.veto("AAPL", "earnings tomorrow")
    assert not guard.check(order(), live_cfg).allowed


def test_nested_dollar_amount_order(live_cfg):
    issue(quantity=10, limit=200.0)  # notional 2000
    payload = {"tool_name": TOOL, "tool_input": {
        "order": {"symbol": "aapl", "side": "buy", "type": "limit", "price": "199.00",
                  "dollar_based_amount": {"amount": "1500.00", "currency_code": "USD"}}}}
    assert guard.check(payload, live_cfg).allowed


def test_dollar_amount_above_ticket_blocked(live_cfg):
    issue(quantity=10, limit=200.0)
    payload = {"tool_name": TOOL, "tool_input": {
        "symbol": "AAPL", "side": "buy", "type": "limit", "limit_price": 199.0, "dollar_amount": 5000}}
    assert not guard.check(payload, live_cfg).allowed


def test_options_shaped_input_blocked(live_cfg):
    issue()
    assert not guard.check(order(strike_price=200, expiration_date="2026-12-18"), live_cfg).allowed


def test_option_tool_blocked_even_in_live(live_cfg):
    d = guard.check({"tool_name": "mcp__robinhood__place_option_order", "tool_input": {}}, live_cfg)
    assert not d.allowed


def run_main(monkeypatch, capsys, payload_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload_text))
    code = guard.main()
    return code, capsys.readouterr().err


def test_main_fails_closed_on_garbage(monkeypatch, capsys, write_config):
    write_config(general={"mode": '"live"'})
    code, err = run_main(monkeypatch, capsys, json.dumps({"tool_name": TOOL, "tool_input": {"foo": 1}}))
    assert code == 2 and "BLOCKED" in err


def test_main_fails_closed_on_invalid_json(monkeypatch, capsys):
    code, err = run_main(monkeypatch, capsys, "{not json")
    assert code == 2 and "guard error" in err


def test_main_allows_reads(monkeypatch, capsys):
    code, _ = run_main(monkeypatch, capsys, json.dumps({"tool_name": "mcp__robinhood__get_portfolio"}))
    assert code == 0


def test_main_end_to_end_live(monkeypatch, capsys, write_config):
    write_config(general={"mode": '"live"'})
    issue()
    code, _ = run_main(monkeypatch, capsys, json.dumps(order()))
    assert code == 0
