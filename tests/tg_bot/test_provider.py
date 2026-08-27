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