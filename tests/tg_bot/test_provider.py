import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "service", "tg_bot"))

import pytest
from unittest.mock import patch

from provider import ProviderError, resolve_provider, validate_key


class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status_code = status
        self._body = body

    def json(self):
        import json

        return json.loads(self._body)


def test_presets_resolve():
    base, model = resolve_provider("openai", None, None)
    assert base == "https://api.openai.com/v1"
    assert model == "gpt-4o-mini"
    base, model = resolve_provider("custom", "https://x.example/v1", "my-model")
    assert base == "https://x.example/v1" and model == "my-model"


def test_valid_key_ok():
    with patch("provider.requests.post") as mock:
        mock.return_value = FakeResp(200, b'{"choices":[{"message":{"content":"OK"}}]}')
        model = validate_key("openai", "sk-test")
    assert model == "gpt-4o-mini"


def test_invalid_key_rejected():
    with patch("provider.requests.post") as mock:
        mock.return_value = FakeResp(401)
        with pytest.raises(ProviderError) as e:
            validate_key("openai", "sk-bad")
    assert e.value.kind == "invalid"


def test_rate_limited():
    with patch("provider.requests.post") as mock:
        mock.return_value = FakeResp(429)
        with pytest.raises(ProviderError) as e:
            validate_key("openai", "sk-x")
    assert e.value.kind == "rate_limited"


def test_network_error():
    with patch("provider.requests.post") as mock:
        mock.side_effect = RuntimeError("boom")
        with pytest.raises(ProviderError) as e:
            validate_key("openai", "sk-x")
    assert e.value.kind == "network"


def test_custom_requires_url():
    with pytest.raises(ProviderError) as e:
        validate_key("custom", "sk-x", base_url=None)
    assert e.value.kind == "invalid"


def test_deepseek_preset_resolves():
    base, model = resolve_provider("deepseek", None, None)
    assert base == "https://api.deepseek.com/v1"
    assert model == "deepseek-chat"


def test_claude_preset_resolves():
    base, model = resolve_provider("claude", None, None)
    assert base == "https://api.anthropic.com/v1"
    assert model == "claude-3-5-sonnet-latest"


def test_claude_uses_messages_api():
    """Claude is NOT OpenAI-compatible — must call /messages with x-api-key."""
    from provider import _anthropic_completion
    with patch("provider.requests.post") as mock:
        mock.return_value = FakeResp(200, b'{"content":[{"text":"OK"}]}')
        model = validate_key("claude", "sk-ant-test")
    assert model == "claude-3-5-sonnet-latest"
    # the anthropic call must hit /messages with the x-api-key header
    url = mock.call_args[0][0]
    headers = mock.call_args[1]["headers"]
    assert url.endswith("/messages")
    assert headers.get("x-api-key") == "sk-ant-test"
    assert "anthropic-version" in headers