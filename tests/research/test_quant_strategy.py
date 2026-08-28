"""Tests for the validated quant strategy engine (momentum20 + funding-carry overlay)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "agent"))

import pytest

from quant_strategy import (
    momentum20_return,
    realized_vol,
    risk_sized_units,
    momentum_decision,
    scan_momentum_book,
    pick_decision,
    adaptive_take_pct,
    trailing_stop_pct,
    trail_check,
    MOMENTUM_THRESHOLD,
    TARGET_VOL_ANNUAL,
)


def _uptrend_closes(n=40, gain=0.005):
    """Closes that end 20d return > 2%."""
    px = 100.0
    out = []
    for _ in range(n):
        out.append(px)
        px *= 1 + gain
    return out


def _flat_closes(n=40):
    return [100.0] * n


def _downtrend_closes(n=40, loss=0.003):
    px = 100.0
    out = []
    for _ in range(n):
        out.append(px)
        px *= 1 - loss
    return out


def test_momentum20_return_positive_in_uptrend():
    closes = _uptrend_closes()
    r20 = momentum20_return(closes)
    assert r20 > MOMENTUM_THRESHOLD


def test_momentum20_return_zero_with_insufficient_data():
    assert momentum20_return([100.0, 101.0, 102.0]) == 0.0


def test_momentum20_flat_closes_no_signal():
    assert momentum20_return(_flat_closes()) <= MOMENTUM_THRESHOLD


def test_momentum_decision_longs_on_momentum():
    d = momentum_decision("BTC", "crypto", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=120.0)
    assert d.action == "buy"
    assert d.qty > 0
    assert d.stop_pct == 8.0 and d.take_pct == 24.0  # 1:3 frozen


def test_momentum_decision_stays_cash_when_flat():
    d = momentum_decision("BTC", "crypto", _flat_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=100.0)
    assert d.action == "hold"
    assert d.qty == 0.0


def test_momentum_decision_never_shorts():
    d = momentum_decision("BTC", "crypto", _downtrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=90.0)
    assert d.action != "short"  # cash preferred over unvalidated directional short
    assert d.action in ("hold", "sell")


def test_momentum_decision_exits_when_momentum_decays():
    # already long, momentum has decayed -> flat the position
    d = momentum_decision("ETH", "crypto", _flat_closes(), 100000.0,
                          has_long=True, has_short=False, current_price=100.0)
    assert d.action == "sell"


def test_momentum_decision_holds_when_already_long_and_intact():
    d = momentum_decision("ETH", "crypto", _uptrend_closes(), 100000.0,
                          has_long=True, has_short=False, current_price=120.0)
    assert d.action == "hold"


def test_momentum_decision_crypto_only():
    d = momentum_decision("AAPL", "us-stock", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=200.0)
    assert d.action == "hold"  # no evidence on equities -> capital preserved


def test_risk_sizing_caps_notional_at_max_position():
    qty, why = risk_sized_units(100000.0, entry=100.0, stop_pct=8.0, take_pct=24.0)
    notional = qty * 100.0
    assert 0 < notional <= 30000.0  # <= 30% of equity


def test_scan_momentum_book_respects_book_cap():
    closes = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    prices = {"BTC": 100.0, "ETH": 100.0}
    portfolio = {"cash": 100000.0, "positions": []}
    decisions = scan_momentum_book(portfolio, closes, prices)
    buys = [d for d in decisions if d.action == "buy"]
    total = sum(d.qty * prices[d.symbol] for d in buys)
    # correlated BTC+ETH longs are ONE book: combined notional must stay <= 30%
    assert total <= 30000.0
    # and a single oversized demand gets demoted to hold
    big = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    big_prices = {"BTC": 100.0, "ETH": 100.0}
    # force book over-cap: shrink equity so 2 positions would exceed 30%
    small = {"cash": 50000.0, "positions": []}
    decisions2 = scan_momentum_book(small, big, big_prices)
    buys2 = [d for d in decisions2 if d.action == "buy"]
    total2 = sum(d.qty * big_prices[d.symbol] for d in buys2)
    assert total2 <= 15000.0  # 30% of 50k


def test_pick_decision_prefers_exit_then_strongest_entry():
    from quant_strategy import QuantDecision
    buys = [QuantDecision("buy", "SOL", 5.0, 8.0, 24.0, "momentum20 LONG (20d +5.00%)"),
            QuantDecision("buy", "BTC", 1.0, 8.0, 24.0, "momentum20 LONG (20d +3.00%)")]
    pick = pick_decision(buys)
    assert pick.symbol == "SOL"  # strongest 20d wins
    # exit wins over entry
    sell = QuantDecision("sell", "ETH", 0.0, 0.0, 0.0, "decayed")
    pick2 = pick_decision([*buys, sell])
    assert pick2.action == "sell" and pick2.symbol == "ETH"


# ---------------- adaptive take-profit + trailing stop ----------------

def test_adaptive_take_pct_scales_with_volatility():
    # normal vol -> spec 3R (24%)
    assert adaptive_take_pct(TARGET_VOL_ANNUAL) == pytest.approx(24.0)
    # calm vol -> wider target (let it run)
    assert adaptive_take_pct(0.10) > 24.0
    # high vol -> NEVER below the frozen 3R spec (24%)
    assert adaptive_take_pct(0.40) == pytest.approx(24.0)
    assert adaptive_take_pct(5.0) >= 24.0
    # None/zero -> base spec
    assert adaptive_take_pct(None) == 24.0


def test_trailing_stop_activates_after_1R_and_locks_gain():
    # 80k entry, stop 8% (1R = 6.4k). At +10% gain, trail activates.
    assert trailing_stop_pct(80000, 84000) is None  # +5%, below 1R
    assert trailing_stop_pct(80000, 88000) == pytest.approx(4.0)  # +10%, 0.5R trail
    # trail is from the peak, so it ratchets as the peak rises
    assert trailing_stop_pct(80000, 100000) == pytest.approx(4.0)


def test_trail_check_exits_on_retrace_from_peak():
    positions = [{"symbol": "BTC", "quantity": 0.1, "entry_price": 80000,
                  "current_price": 100000, "high_price": 100000}]
    # price fell 4.5% from the 100k peak -> 95.5k, below 4% trail -> SELL
    exits = trail_check(positions, {"BTC": 95500})
    assert len(exits) == 1 and exits[0].action == "sell"
    assert "trailing stop" in exits[0].reasoning
    # still above trail -> no exit
    assert trail_check(positions, {"BTC": 97000}) == []
    # not enough gain yet -> no exit
    flat = [{"symbol": "BTC", "quantity": 0.1, "entry_price": 80000,
             "current_price": 82000, "high_price": 82000}]
    assert trail_check(flat, {"BTC": 81000}) == []


def test_adaptive_take_used_in_momentum_decision():
    d = momentum_decision("BTC", "crypto", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=120.0)
    assert d.action == "buy"
    # take target adapts with volatility but stays >= 1.5R (12%)
    assert d.take_pct >= 12.0 and d.take_pct <= 40.0
    assert d.stop_pct == 8.0


# ---------------- sentiment tail-risk adjuster ----------------

def test_sentiment_risk_adjust_no_change_in_middle():
    from quant_strategy import sentiment_risk_adjust
    s, t, note = sentiment_risk_adjust(8.0, 24.0, 73.0)  # current: Greed 73
    assert s == 8.0 and t == 24.0
    assert note == ""


def test_sentiment_risk_adjust_extreme_greed_tightens():
    from quant_strategy import sentiment_risk_adjust
    s, t, note = sentiment_risk_adjust(8.0, 24.0, 95.0)
    assert s < 8.0  # stop tightened
    assert t < 24.0  # target cut
    assert t >= s * 1.5  # never below 1.5R (Kelly-positive floor)
    assert "greed" in note


def test_sentiment_risk_adjust_extreme_fear_widens_stop():
    from quant_strategy import sentiment_risk_adjust
    s, t, note = sentiment_risk_adjust(8.0, 24.0, 10.0)
    assert s > 8.0  # stop widened
    assert t == 24.0  # target kept
    assert "fear" in note


def test_sentiment_adjust_applied_in_momentum_decision():
    # extreme greed -> tighter stop/target on the entry
    d = momentum_decision("BTC", "crypto", _uptrend_closes(), 100000.0,
                          has_long=False, has_short=False, current_price=120.0,
                          fear_greed=95.0)
    assert d.action == "buy"
    assert d.stop_pct < 8.0
    assert d.take_pct < 24.0


# ---------------- scenario engine (GBM barrier math) ----------------

from quant_strategy import (
    build_scenarios, scenario_matrix, pick_best_scenario,
    barrier_win_prob, estimate_drift_vol, TradeScenario,
)


def test_barrier_prob_flat_is_distance_odds():
    # no drift -> P(win) = stop_dist / (stop_dist + target_dist) = 1/4 for 1:3
    p = barrier_win_prob(100.0, 124.0, 92.0, 0.0, 0.5)
    assert 0.22 < p < 0.30


def test_barrier_prob_uptrend_prefers_long():
    # positive drift -> long P(win) high, short P(win) low
    p_long = barrier_win_prob(100.0, 124.0, 92.0, 3.0, 0.5)
    p_short = barrier_win_prob(100.0, 76.0, 108.0, 3.0, 0.5)
    assert p_long > 0.6
    assert p_short < 0.4


def test_barrier_prob_downtrend_prefers_short():
    p_long = barrier_win_prob(100.0, 124.0, 92.0, -3.0, 0.5)
    p_short = barrier_win_prob(100.0, 76.0, 108.0, -3.0, 0.5)
    assert p_short > 0.6
    assert p_long < 0.4


def test_barrier_prob_is_bounded():
    for drift in (-5.0, -1.0, 0.0, 1.0, 5.0):
        for target, stop in [(124, 92), (76, 108)]:
            p = barrier_win_prob(100.0, float(target), float(stop), drift, 0.5)
            assert 0.05 <= p <= 0.75


def test_time_exit_hard_cut_after_max_hold():
    from datetime import datetime, timedelta, timezone
    from quant_strategy import time_exit_check
    old = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    pos = [{"symbol": "BTC", "quantity": 0.1, "entry_price": 80000,
            "current_price": 79500, "opened_at": old}]
    exits = time_exit_check(pos, {"BTC": 79500}, max_hold_days=5)
    assert len(exits) == 1
    assert "time stop" in exits[0].reasoning


def test_time_exit_profit_take_banks_green():
    from datetime import datetime, timedelta, timezone
    from quant_strategy import time_exit_check
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    pos = [{"symbol": "AAPL", "quantity": 18, "entry_price": 314,
            "current_price": 316, "opened_at": old}]
    exits = time_exit_check(pos, {"AAPL": 316}, max_hold_days=5, profit_take_days=3)
    assert len(exits) == 1
    assert "profit take" in exits[0].reasoning


def test_time_exit_does_not_trigger_prematurely():
    from datetime import datetime, timedelta, timezone
    from quant_strategy import time_exit_check
    recent = datetime.now(timezone.utc).isoformat()
    pos = [{"symbol": "BTC", "quantity": 0.1, "entry_price": 80000,
            "current_price": 79500, "opened_at": recent}]
    assert time_exit_check(pos, {"BTC": 79500}) == []


def test_build_scenarios_returns_long_and_short():
    up = _uptrend_closes()
    sc = build_scenarios("BTC", up, 120.0)
    assert len(sc) == 2
    dirs = {s.direction for s in sc}
    assert dirs == {"long", "short"}
    for s in sc:
        assert s.R >= 1.3  # vol-based target keeps reward/risk >= 1.3:1
        assert 0.05 <= s.p_win <= 0.75
        assert s.ev == pytest.approx(s.p_win * s.R - (1 - s.p_win))
        # reachable levels: stop/target must be within sane bounds, not 24% fantasy
        stop_pct = abs(s.entry - s.stop) / s.entry * 100
        take_pct = abs(s.target - s.entry) / s.entry * 100
        assert 1.0 <= stop_pct <= 6.5
        assert 2.0 <= take_pct <= 11.0


def test_scenario_matrix_and_best_pick():
    closes = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    prices = {"BTC": 120.0, "ETH": 100.0}
    matrix = scenario_matrix(closes, prices)
    assert len(matrix) == 4  # 2 symbols x (long+short)
    best = pick_best_scenario(matrix, has_long={}, has_short={})
    assert best is not None
    assert best.ev > 0
    # best should be a LONG in the uptrend (not short)
    assert best.direction == "long"


def test_pick_best_scenario_respects_existing_position():
    closes = {"BTC": _uptrend_closes(), "ETH": _uptrend_closes()}
    prices = {"BTC": 120.0, "ETH": 100.0}
    matrix = scenario_matrix(closes, prices)
    # already long BTC -> should not pick the BTC long again
    best = pick_best_scenario(matrix, has_long={"BTC": True}, has_short={})
    assert best is not None
    assert not (best.symbol == "BTC" and best.direction == "long")
    # if nothing actionable, returns None
    none = pick_best_scenario(matrix, has_long={"BTC": True, "ETH": True},
                              has_short={"BTC": True, "ETH": True})
    assert none is None