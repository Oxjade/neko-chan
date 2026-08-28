"""Tests for human-readable error mapping + network kill-switch admin flow."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest

from messages import humanize_error


@pytest.mark.parametrize("raw,expected_fragment", [
    ("Short position entry price is missing", "Couldn't open the short"),
    ("stop_loss_pct/take_profit_pct can only be set when opening (buy/short) a position.",
     "Closing trades can't carry a stop/target"),
    ("US market is closed. Current time (ET): 2026-08-26 19:05:58", "Mon–Fri 9:30–16:00 ET"),
    ("Unable to fetch current price for BTC", "Couldn't get a live price"),
    ("Leverage must be between 1 and 10", "1–10x"),
    ("daily trade limit reached", "resume tomorrow"),
    ("position size cap exceeded", "stayed flat"),
    ("duplicate idempotency_key abc", "wasn't sent twice"),
])
def test_humanize_error_maps_known_errors(raw, expected_fragment):
    out = humanize_error(raw)
    assert expected_fragment in out


def test_humanize_error_falls_back_gracefully():
    out = humanize_error("Weird venue error 0x42")
    assert "Weird venue error" in out
    assert out.startswith("⚠️") or "Weird" in out


def test_humanize_error_empty():
    assert "Unknown error" in humanize_error("")
    assert "Unknown error" in humanize_error(None)


def test_humanize_error_truncates_long_unknown():
    long_err = "x" * 2000
    out = humanize_error(long_err)
    assert len(out) <= 305  # max_len + prefix/suffix
    assert out.endswith("…")


def test_humanize_error_never_exposes_raw_key_or_secret():
    # a raw error must never echo a full token-ish value back verbatim
    raw = "connection failed auth token sk-1234567890abcdef1234567890abcdef12345678"
    out = humanize_error(raw)
    assert "sk-1234567890abcdef1234567890abcdef" not in out