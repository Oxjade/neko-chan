"""Provider presets, key validation, and a minimal chat-completion helper.

Validation = one 5-token chat call against the user's provider with their key.
401/403 -> rejected, 429 -> rate limited, 2xx -> valid.
For OpenRouter, `openrouter/auto` routes to a PAID model and a free-tier key gets
402 (insufficient credits) - which used to surface as a confusing "key rejected".
So OpenRouter validation falls back through a list of free models until one
accepts the key, and returns that model name for the runner to use.
"""

import time

import requests

from tg_config import PROVIDER_PRESETS

# OpenRouter free models, tried in order when the preset model needs credits.
# Kept intentionally small (fast validation); each is 200/0.7 rate-limited
# per day on free tier, so one will usually accept the key.
OPENROUTER_FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "minimax/minimax-m3:free",
    "z-ai/glm-5.2:free",
]


class ProviderError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind  # invalid | rate_limited | network | unknown
        super().__init__(message)


def resolve_provider(provider: str, base_url: str | None, model: str | None) -> tuple[str, str]:
    preset = PROVIDER_PRESETS.get(provider)
    if preset:
        return preset["base_url"], model or preset["model"]
    return (base_url or "").rstrip("/"), model or "gpt-4o-mini"


def _anthropic_completion(base: str, api_key: str, model: str, system: str | None,
                          user: str, max_tokens: int, timeout: float) -> requests.Response:
    """Anthropic uses /v1/messages + x-api-key (NOT OpenAI /chat/completions)."""
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user}]}
    if system:
        body["system"] = system
    return requests.post(
        f"{base.rstrip('/')}/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )


def _chat_once(base: str, api_key: str, model: str, timeout: float):
    return requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "say OK"}],
            "max_tokens": 5,
        },
        timeout=timeout,
    )


def _parse_ok(resp: requests.Response, provider: str) -> bool:
    try:
        if provider == "claude":
            content = resp.json()["content"][0]["text"]
        else:
            content = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return False
    return bool(content)


def validate_key(provider: str, api_key: str, base_url: str | None = None,
                 model: str | None = None, timeout: float = 25.0) -> str:
    """Returns the model name on success; raises ProviderError otherwise.

    One flow for every provider: the user pastes a key, and this picks a model
    that accepts it. OpenRouter free-tier keys are tested against the preset
    model first, then each free model in OPENROUTER_FREE_MODELS, and the first
    that returns 200 becomes the model the runner uses."""
    base, resolved_model = resolve_provider(provider, base_url, model)
    if not base:
        raise ProviderError("invalid", "custom provider requires a base URL")

    candidates = [resolved_model]
    if provider == "openrouter":
        candidates = [resolved_model] + [m for m in OPENROUTER_FREE_MODELS if m != resolved_model]

    for cand in candidates:
        try:
            if provider == "claude":
                resp = _anthropic_completion(base, api_key, cand, None, "say OK",
                                             max_tokens=5, timeout=timeout)
            else:
                resp = _chat_once(base, api_key, cand, timeout)
        except requests.Timeout:
            if provider == "openrouter" and len(candidates) > 1:
                continue
            raise ProviderError("network", "provider timed out")
        except requests.RequestException as exc:
            if provider == "openrouter" and len(candidates) > 1:
                continue
            raise ProviderError("network", f"cannot reach provider: {type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001
            if provider == "openrouter" and len(candidates) > 1:
                continue
            raise ProviderError("network", f"provider call failed: {type(exc).__name__}")

        # OpenRouter free tier: a paid-model routing (402) or per-day free cap
        # (429) is NOT a bad key - try the next free model.
        if resp.status_code in (402, 400, 403) and provider == "openrouter" and cand != resolved_model:
            continue
        if resp.status_code in (401, 403):
            raise ProviderError("invalid", "provider rejected the key (401/403)")
        if resp.status_code == 402:
            raise ProviderError("rate_limited", "provider needs credits for that model")
        if resp.status_code == 429:
            raise ProviderError("rate_limited", "provider is rate limited")
        if resp.status_code >= 500:
            if provider == "openrouter" and len(candidates) > 1:
                continue
            raise ProviderError("network", f"provider server error ({resp.status_code})")
        if resp.status_code != 200:
            raise ProviderError("unknown", f"unexpected status {resp.status_code}")
        if not _parse_ok(resp, provider):
            # openrouter/auto often routes to reasoning models that return
            # content:null (text lives in `reasoning`) - not an error, just not
            # usable for our 5-token probe, so try the next free model.
            if provider == "openrouter" and len(candidates) > 1:
                continue
            raise ProviderError("unknown", "empty provider response")
        return cand

    raise ProviderError("invalid", "provider rejected the key")


def chat_completion(provider: str, api_key: str, system: str, user: str,
                    base_url: str | None = None, model: str | None = None,
                    timeout: float = 60.0) -> str:
    """Full completion call used by per-user agent runners."""
    base, resolved_model = resolve_provider(provider, base_url, model)
    if provider == "claude":
        resp = _anthropic_completion(base, api_key, resolved_model, system, user,
                                     max_tokens=2000, timeout=timeout)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
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