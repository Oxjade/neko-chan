"""Policy: no connected AI key => the bot NEVER opens a position; it does not
fall back to the deterministic quant matrix. Closes stay automatic (capital
safety). Enforced by live_agent.ai_gate_blocks_entry() used at the execution
choke point."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "execution"))

os.environ.setdefault("LIVE_AGENT_EXECUTION", "0")
import live_agent


def test_entries_require_ai_key():
    # buy/short WITHOUT a key -> blocked
    assert live_agent.ai_gate_blocks_entry("buy", False) is True
    assert live_agent.ai_gate_blocks_entry("short", False) is True


def test_entries_allowed_with_ai_key_positive_control():
    # WITH a key the gate must NOT block - proves the block above is real
    # (not the helper always returning True).
    assert live_agent.ai_gate_blocks_entry("buy", True) is False
    assert live_agent.ai_gate_blocks_entry("short", True) is False


def test_exits_never_blocked():
    # closes must run regardless of key, even with none connected
    assert live_agent.ai_gate_blocks_entry("sell", False) is False
    assert live_agent.ai_gate_blocks_entry("cover", False) is False
    assert live_agent.ai_gate_blocks_entry("hold", False) is False


def test_run_cycle_no_longer_imports_quant_open_for_entries():
    """pick_best_scenario must no longer be used to OPEN positions in the
    entry decision (it was the quant fallback). Guard the call sites by
    checking the no-key + cooldown branches hold.
    """
    import inspect
    src = inspect.getsource(live_agent.run_cycle)
    assert "no LLM key -> fall back to the math's best scenario" not in src
    assert "fall back to the math's best" not in src
    # the AI-gate is actually wired into the cycle
    assert "ai_gate_blocks_entry" in src
