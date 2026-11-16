"""Slow trend agent: signal, entries on a cross only, reversal and stop exits."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for sub in ("service/agent", "service/tg_bot"):
    if str(ROOT / sub) not in sys.path:
        sys.path.insert(0, str(ROOT / sub))

import trend_agent as ta  # noqa: E402
from paper_gateway import PaperGateway  # noqa: E402
from paper_store import PaperStore  # noqa: E402

BOT = 7


def bars_from(closes):
    return [{"t": i * ta.BAR_MS, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c}
            for i, c in enumerate(closes)]


def falling_then_rising(extra_up=0):
    """Long downtrend, then a rally just long enough to cross on the last bar."""
    closes = [200 - i * 0.2 for i in range(400)]
    while True:
        closes.append(closes[-1] * 1.01)
        sig = ta.signal(bars_from(closes))
        if sig["trend"] > 0:
            break
    closes += [closes[-1] * 1.01] * extra_up
    return closes


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "EXPORT_DIR", tmp_path)
    gw = PaperGateway(PaperStore(str(tmp_path / "reg.db")))
    gw.store.ensure_portfolio(BOT)
    return gw, ta.Journal(BOT)


def cycle(gw, journal, closes, price=None):
    bars = bars_from(closes)
    return ta.run_cycle(BOT, ["BTC"], gw, journal, fetch_bars=lambda s: bars,
                        fetch_mids=lambda: {"BTC": price or closes[-1]})


def test_signal_needs_history():
    assert ta.signal(bars_from([100.0] * 50))["trend"] == 0


def test_signal_marks_only_the_crossing_bar():
    closes = falling_then_rising()
    assert ta.signal(bars_from(closes)) ["crossed"] is True
    assert ta.signal(bars_from(closes[:-1]))["trend"] == -1
    later = ta.signal(bars_from(falling_then_rising(extra_up=3)))
    assert later["trend"] == 1 and later["crossed"] is False


def test_no_entry_in_the_middle_of_a_trend(setup):
    gw, journal = setup
    cycle(gw, journal, falling_then_rising(extra_up=3))
    assert gw.store.positions(BOT) == []


def test_opens_on_cross_then_holds_then_closes_on_reversal(setup):
    gw, journal = setup
    closes = falling_then_rising()
    assert "opened long BTC" in cycle(gw, journal, closes)[0]
    pos = gw.store.positions(BOT)[0]
    assert pos["direction"] == "long" and pos["leverage"] == 1.0
    assert pos["stop_loss"] < pos["entry_price"]
    # unlevered: the whole account goes into the single watched coin
    assert pos["qty"] * pos["entry_price"] == pytest.approx(980.0, rel=0.01)

    cycle(gw, journal, closes + [closes[-1] * 1.01] * 2)          # still up: hold
    assert len(gw.store.positions(BOT)) == 1

    down = closes + [closes[-1] * 1.01] * 2
    while ta.signal(bars_from(down))["trend"] > 0:
        down.append(down[-1] * 0.985)
    lines = cycle(gw, journal, down, price=pos["entry_price"] * 1.2)  # above the stop
    assert any("trend reversed" in line for line in lines)
    held = gw.store.positions(BOT)
    assert len(held) == 1 and held[0]["direction"] == "short"       # flipped on the cross


def test_stop_closes_and_waits(setup):
    gw, journal = setup
    closes = falling_then_rising()
    cycle(gw, journal, closes)
    stop = gw.store.positions(BOT)[0]["stop_loss"]
    lines = cycle(gw, journal, closes + [closes[-1]], price=stop * 0.99)
    assert any("stop hit" in line for line in lines)
    assert gw.store.positions(BOT) == []


def test_journal_is_readable_by_the_console(setup, tmp_path, monkeypatch):
    gw, journal = setup
    cycle(gw, journal, falling_then_rising())
    sys.path.insert(0, str(ROOT / "service" / "server"))
    import web_core
    monkeypatch.setattr(web_core, "EXPORT_DIR", tmp_path)
    decisions = web_core.recent_decisions(BOT)
    assert decisions[0]["action"] == "buy" and decisions[0]["filled"] is True
    assert web_core.latest_prices(BOT)["BTC"]["price"] > 0
