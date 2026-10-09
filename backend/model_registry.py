"""Provider/model registry (Phase 0 of the multi-provider plan).

Maps a short key to everything needed to build an OpenAI-compatible client:
display name, base URL, the env var holding the API key, the provider-side
model id, and optional extra HTTP headers. The default entry ("kimi-k3")
reproduces the historical behavior exactly: Kimi Code endpoint, key from
KIMI_API_KEY, overridable via KIMI_BASE_URL / KIMI_MODEL (read at call time,
not import time, so tests can monkeypatch the environment).

The "mistral-vibe" entry follows the connection flow of Mistral's own CLI
coding agent (github.com/mistralai/mistral-vibe, vibe/core/config/_defaults.py
and schema.py): key from MISTRAL_API_KEY (DEFAULT_MISTRAL_API_ENV_KEY), server
https://api.mistral.ai (DEFAULT_MISTRAL_SERVER_URL, OpenAI-compatible /v1),
default active model mistral-medium-3-5 (Vibe's active_model), overridable via
MISTRAL_BASE_URL / MISTRAL_MODEL. Its free health probe is
GET /v1/models (verified live to return 401 without a valid key).

`verify_model(key)` proactively checks that the configured key works using a
FREE authenticated GET per provider (OpenRouter: /auth/key credit metadata;
Kimi: /models — both verified to return 401 without a valid key, so no
paid completion call is ever made). Results are cached for HEALTH_TTL_S.
"""
import asyncio
import os
import time

import httpx

DEFAULT_MODEL_KEY = "kimi-k3"

_KIMI_DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
_KIMI_DEFAULT_MODEL_ID = "k3"

# Mistral Vibe connection flow (upstream: mistralai/mistral-vibe,
# vibe/core/config/_defaults.py): the API key env var and the server URL are
# the agent's own defaults, and the default active model is its schema's
# active_model. The Mistral platform API is OpenAI-compatible.
_MISTRAL_DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
_MISTRAL_DEFAULT_MODEL_ID = "mistral-medium-3-5"

_REGISTRY = {
    "kimi-k3": {
        "display_name": "Kimi K3 (direct)",
        "api_key_env": "KIMI_API_KEY",
        "health_path": "/models",
        # Streams private reasoning via the reasoning_content delta field;
        # thinking tokens are billed against the same completion budget.
        "reasoning": True,
    },
    "mistral-vibe": {
        "display_name": "Mistral (Vibe default)",
        "api_key_env": "MISTRAL_API_KEY",
        "health_path": "/models",
        # mistral-medium-3-5 supports adjustable reasoning, but the default
        # completion does not think: plain-string content, no headroom needed.
        "reasoning": False,
    },
    "mistral-glm-5.3": {
        # GLM 5.3 hosted by Mistral (third-party model on the Mistral
        # platform API, docs.mistral.ai: "Send a request to the Chat
        # Completions API with the zai-glm-5-3 model name"). Same key.
        "display_name": "GLM 5.3 (Mistral hosted)",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "model_id": "zai-glm-5-3",
        "health_path": "/models",
        # A reasoning model that ALWAYS thinks (docs.mistral.ai: GLM 5.3
        # accepts reasoning_effort low|high|max and rejects "none"), and it
        # streams thinking as typed chunks inside delta.content rather than a
        # reasoning field. Thinking shares the completion-token budget, so
        # requests need reasoning-sized output headroom.
        "reasoning": True,
        "reasoning_efforts": ["low", "high", "max"],
        # GLM always thinks and the platform's serving default effort is
        # high: measured ~30k thinking tokens on a small design round, which
        # exhausts a 120k run budget in two rounds. Low effort keeps simple
        # tasks fast and inside budget; raise it per machine with the
        # GLM_REASONING_EFFORT env override (validated against the list above).
        "reasoning_effort_default": "low",
    },
    "openrouter-kimi-k2.7-code": {
        "display_name": "Kimi K2.7 Code (OpenRouter)",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_id": "moonshotai/kimi-k2.7-code",
        "health_path": "/auth/key",
        "reasoning": False,
    },
    "openrouter-kimi-k3": {
        "display_name": "Kimi K3 (OpenRouter)",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_id": "moonshotai/kimi-k3",
        "health_path": "/auth/key",
        "reasoning": True,
    },
}


def get_model(key: str) -> dict:
    """Resolve a registry key to a full entry; raises KeyError on unknown."""
    entry = dict(_REGISTRY[key])  # KeyError propagates for unknown keys
    if key == "kimi-k3":
        entry["base_url"] = os.environ.get("KIMI_BASE_URL", _KIMI_DEFAULT_BASE_URL)
        entry["model_id"] = os.environ.get("KIMI_MODEL", _KIMI_DEFAULT_MODEL_ID)
    elif key == "mistral-vibe":
        entry["base_url"] = os.environ.get("MISTRAL_BASE_URL", _MISTRAL_DEFAULT_BASE_URL)
        entry["model_id"] = os.environ.get("MISTRAL_MODEL", _MISTRAL_DEFAULT_MODEL_ID)
    elif key == "mistral-glm-5.3":
        # Per-machine reasoning-effort override, read at call time like the
        # other provider env knobs. Invalid values are filtered downstream by
        # the chat loop against the entry's reasoning_efforts list.
        effort = os.environ.get("GLM_REASONING_EFFORT")
        if effort:
            entry["reasoning_effort_default"] = effort
    return entry


def list_models() -> list[dict]:
    """All registry entries with an availability flag (api key env var set)."""
    return [
        {
            "key": key,
            "display_name": entry["display_name"],
            "available": bool(os.environ.get(entry["api_key_env"])),
            "default": key == DEFAULT_MODEL_KEY,
            # Effort capabilities for the per-project effort selector.
            "reasoning_efforts": list(entry.get("reasoning_efforts") or []),
            "reasoning_effort_default": entry.get("reasoning_effort_default"),
        }
        for key, entry in _REGISTRY.items()
    ]


# -- proactive key verification (free auth-check GETs, cached) ----------------

HEALTH_TTL_S = 60        # cache probe results so frontend polling doesn't hammer
_HEALTH_TIMEOUT_S = 5    # per-provider probe timeout cap

# key -> (monotonic timestamp, status). Only probe outcomes are cached; "no_key"
# is derived from the env each call and never stored.
_health_cache: dict[str, tuple[float, str]] = {}


async def _probe(url: str, api_key: str) -> int:
    """GET a provider's free auth-check endpoint; returns the HTTP status code."""
    async with httpx.AsyncClient(timeout=_HEALTH_TIMEOUT_S) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        return response.status_code


async def verify_model(key: str) -> str:
    """Health of a registry entry: ok | auth_failed | unreachable | unknown | no_key.

    Never logs or returns the key or response bodies — only the status word.
    """
    entry = get_model(key)
    api_key = os.environ.get(entry["api_key_env"])
    if not api_key:
        return "no_key"
    now = time.monotonic()
    cached = _health_cache.get(key)
    if cached and now - cached[0] < HEALTH_TTL_S:
        return cached[1]
    url = entry["base_url"].rstrip("/") + entry["health_path"]
    try:
        status = await _probe(url, api_key)
    except Exception:  # DNS/connect/timeout — provider (or network) down
        health = "unreachable"
    else:
        if 200 <= status < 300:
            health = "ok"
        elif status in (401, 403):
            health = "auth_failed"
        elif status == 404:
            # Probe endpoint doesn't exist at this base URL — the key can't be
            # verified without a paid completion call, which we never make.
            health = "unknown"
        else:
            health = "unreachable"
    _health_cache[key] = (now, health)
    return health


async def verify_all() -> dict[str, str]:
    """Verify every registry entry concurrently; key -> health status."""
    keys = list(_REGISTRY)
    statuses = await asyncio.gather(*(verify_model(key) for key in keys))
    return dict(zip(keys, statuses))
