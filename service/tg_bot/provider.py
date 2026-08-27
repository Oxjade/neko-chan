"""Provider presets, key validation, and a minimal chat-completion helper.

Validation = one 5-token chat call against the user's provider with their key.
401/403 -> rejected, 429 -> rate limited, 2xx -> valid.
"""

import time

import requests

from tg_config import PROVIDER_PRESETS


class ProviderError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind  # invalid | rate_limited | network | unknown
        super().__init__(message)


def resolve_provider(provider: str, base_url: str | None, model: str | None) -> tuple[str, str]:
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        return preset["base_url"], model or preset["model"]
    return (base_url or "").rstrip("/"), model or "gpt-4o-mini"


def validate_key(provider: str, api_key: str, base_url: str | None = None,
                 model: str | None = None, timeout: float = 25.0) -> str:
    """Returns the model name on success; raises ProviderError otherwise."""
    base, resolved_model = resolve_provider(provider, base_url, model)
    if not base:
        raise ProviderError("invalid", "custom provider requires a base URL")
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": resolved_model,
                "messages": [{"role": "user", "content": "say OK"}],
                "max_tokens": 5,
            },
            timeout=timeout,
        )
    except requests.Timeout:
        raise ProviderError("network", "provider timed out")
    except requests.RequestException as exc:
        raise ProviderError("network", f"cannot reach provider: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 - any transport failure is a network error
        raise ProviderError("network", f"provider call failed: {type(exc).__name__}")

    if resp.status_code in (401, 403):
        raise ProviderError("invalid", "provider rejected the key (401/403)")
    if resp.status_code == 429:
        raise ProviderError("rate_limited", "provider is rate limited")
    if resp.status_code >= 500:
        raise ProviderError("network", f"provider server error ({resp.status_code})")
    if resp.status_code != 200:
        raise ProviderError("unknown", f"unexpected status {resp.status_code}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        raise ProviderError("unknown", "malformed provider response")
    if not content:
        raise ProviderError("unknown", "empty provider response")
    return resolved_model


def chat_completion(provider: str, api_key: str, system: str, user: str,
                    base_url: str | None = None, model: str | None = None,
                    timeout: float = 60.0) -> str:
    """Full completion call used by per-user agent runners."""
    base, resolved_model = resolve_provider(provider, base_url, model)
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": resolved_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def wait_for_retry(seconds: float = 60.0):
    time.sleep(seconds)