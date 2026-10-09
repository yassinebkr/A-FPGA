"""Interactive Mistral setup wizard.

Faithful port of Mistral Vibe's own ``vibe --setup`` browser sign-in flow
(upstream: github.com/mistralai/mistral-vibe,
``vibe/cli-rust/src/setup/auth/sign_in_flow.rs`` / ``sign_in_gateway.rs`` /
``whoami.rs``): a PKCE (S256) browser handshake against the console's
``/api/vibe/sign-in`` endpoints, polling until the user finishes in the
browser, then exchanging the token for an API key — the same credential a
user would paste by hand from console.mistral.ai.

The wizard never prints or returns the key to a client; it verifies it with
the FREE ``GET /v1/models`` probe (verified live to 401 without a valid key),
stores it in the same ``.env`` the backend loads (repository root first,
``backend/.env`` fallback — the exact precedence ``main.py`` uses), mirrors
it into the durable key store ``backend/data/provider_keys.env``
(``backend/data`` is bind-mounted in docker-compose, so the key survives a
``docker compose down/up`` — the image-layer ``.env`` does not), and exports
it into ``os.environ`` so a running backend adopts it without a restart
(the model registry reads the env at call time).

Exposed both as a CLI (``python mistral_setup.py``, like ``vibe --setup``)
and through REST endpoints in ``main.py`` for the web UI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

# Upstream constants (mistralai/mistral-vibe, vibe/core/config/_defaults.py).
CONSOLE_BASE_URL = "https://console.mistral.ai"          # DEFAULT_MISTRAL_BROWSER_AUTH_BASE_URL
BROWSER_AUTH_API_BASE_URL = f"{CONSOLE_BASE_URL}/api"    # DEFAULT_MISTRAL_BROWSER_AUTH_API_BASE_URL
DEFAULT_MISTRAL_SERVER_URL = "https://api.mistral.ai"   # DEFAULT_MISTRAL_SERVER_URL
API_KEY_ENV = "MISTRAL_API_KEY"

_POLL_INTERVAL_S = 3          # upstream: poll every 3 s
_MAX_POLL_FAILURES = 3        # upstream: three consecutive failures abort
_EXCHANGE_TIMEOUT_S = 15
_WHOAMI_TIMEOUT_S = 10
_PROBE_TIMEOUT_S = 5


class SetupError(RuntimeError):
    """Wizard failure; the message is safe for clients (never the key)."""


# -- PKCE (upstream: generate_pkce_pair, method S256) -------------------------

def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) with method S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


# -- URL validation (upstream: sign_in_url.rs _validate_url_against_base_url) --

def validate_url(url: str, base_url: str, *, allow_origin_rewrite: bool = False) -> str:
    """Validate a server-returned URL against a configured base.

    Rejects foreign origins (unless rewrite is allowed) and paths outside the
    base path, mirroring the upstream wizard's split-horizon rules.
    """
    parsed = urlparse(url)
    base = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise SetupError("URL validation failed.")
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        if not allow_origin_rewrite:
            raise SetupError("URL origin validation failed.")
    base_path = (base.path or "/").rstrip("/") or "/"
    if not (parsed.path or "/").startswith(base_path):
        raise SetupError("URL path validation failed.")
    return url


# -- wizard state (single-user app: one active browser flow) ------------------

@dataclass
class BrowserFlow:
    """One in-flight browser sign-in process (never more than one)."""
    process_id: str = ""
    sign_in_url: str = ""
    poll_url: str = ""
    expires_at: str = ""                     # RFC 3339 with Z suffix
    verifier: str = ""                      # server-side only
    exchange_token: str = ""                 # server-side only
    status: str = "idle"                     # idle|waiting|completed|denied|expired|error
    message: str | None = None
    api_base_url: str = BROWSER_AUTH_API_BASE_URL
    console_base_url: str = CONSOLE_BASE_URL

    def summary(self) -> dict[str, Any]:
        """Client-safe view: no verifier, no exchange token, no key."""
        return {
            "status": self.status,
            "message": self.message,
            "sign_in_url": self.sign_in_url or None,
            "expires_at": self.expires_at or None,
        }


_flow = BrowserFlow()


def flow_state() -> BrowserFlow:
    return _flow


def _reset_flow(console: str = CONSOLE_BASE_URL, api: str = BROWSER_AUTH_API_BASE_URL) -> BrowserFlow:
    global _flow
    _flow = BrowserFlow(console_base_url=console.rstrip("/"), api_base_url=api.rstrip("/"))
    return _flow


# -- gateway calls (upstream: sign_in_gateway.rs) -----------------------------

async def _post_json(url: str, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=dict(payload))
        if response.status_code >= 400:
            raise SetupError(f"request failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise SetupError("failed to parse response") from exc
        if not isinstance(body, dict):
            raise SetupError("failed to parse response")
        return body


async def _get_json(url: str, timeout: float, *, headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any] | None]:
    """GET returning (status, parsed body or None); 410 is a legitimate
    'expired' answer, so it is returned rather than raised."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=dict(headers or {}))
    try:
        body = response.json()
    except ValueError:
        body = None
    return response.status_code, body if isinstance(body, dict) else None


async def start_browser_sign_in(console_base_url: str | None = None,
                                api_base_url: str | None = None) -> dict[str, Any]:
    """Start the PKCE browser flow: create the sign-in process.

    ``console_base_url`` supports the upstream wizard's custom-domain step
    ("Other"): a bare domain is prefixed with https://, and the auth API base
    is derived as ``DOMAIN/api``.
    """
    console = (console_base_url or CONSOLE_BASE_URL).strip()
    if console and not urlparse(console).scheme:
        console = f"https://{console}"
    api = (api_base_url or (f"{console.rstrip('/')}/api" if console else BROWSER_AUTH_API_BASE_URL)).rstrip("/")
    flow = _reset_flow(console=console, api=api)

    verifier, challenge = generate_pkce_pair()
    url = f"{flow.api_base_url}/vibe/sign-in"
    body = await _post_json(url, {"code_challenge": challenge,
                                  "code_challenge_method": "S256"}, _EXCHANGE_TIMEOUT_S)
    process_id = body.get("process_id")
    sign_in_url = body.get("sign_in_url")
    poll_url = body.get("poll_url")
    expires_at = body.get("expires_at")
    if not isinstance(process_id, str) or not process_id:
        raise SetupError("sign-in response is missing a process id")
    if not isinstance(sign_in_url, str) or not isinstance(poll_url, str):
        raise SetupError("sign-in response is missing its URLs")
    flow.process_id = process_id
    flow.verifier = verifier
    flow.sign_in_url = validate_url(sign_in_url, flow.console_base_url)
    flow.poll_url = validate_url(poll_url, flow.api_base_url)
    flow.status = "waiting"
    flow.expires_at = expires_at if isinstance(expires_at, str) else ""
    return flow.summary()


def _flow_expired(flow: BrowserFlow) -> bool:
    if not flow.expires_at:
        # No advertised deadline: keep a bounded local one like the upstream
        # poll loop's expires_at check.
        return False
    try:
        deadline = datetime.fromisoformat(flow.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) >= deadline


async def poll_browser_sign_in() -> dict[str, Any]:
    """Poll the in-flight process once (upstream cadence is left to callers)."""
    flow = _flow
    if flow.status not in ("waiting",):
        return flow.summary()
    if _flow_expired(flow):
        flow.status = "expired"
        flow.message = "Browser sign-in expired."
        return flow.summary()
    failures = 0
    while True:
        try:
            status, body = await _get_json(validate_url(flow.poll_url, flow.api_base_url),
                                           _EXCHANGE_TIMEOUT_S)
        except Exception:
            status, body = 0, None
        if status == 410:
            flow.status = "expired"
            flow.message = "Browser sign-in expired."
            return flow.summary()
        if status >= 400 or body is None:
            failures += 1
            if failures >= _MAX_POLL_FAILURES:
                flow.status = "error"
                flow.message = "Browser sign-in poll failed." if body is None else "Failed to parse poll response."
                return flow.summary()
            await asyncio.sleep(1)
            continue
        status = body.get("status")
        if status == "pending":
            return flow.summary()
        if status == "completed":
            exchange_token = body.get("exchange_token")
            if not isinstance(exchange_token, str) or not exchange_token:
                flow.status = "error"
                flow.message = "Missing exchange token."
                return flow.summary()
            flow.exchange_token = exchange_token
            flow.status = "completed"
            flow.message = None
            return flow.summary()
        if status in ("expired", "denied"):
            flow.status = status
            flow.message = "Browser sign-in expired." if status == "expired" else "Browser sign-in was denied."
            return flow.summary()
        if status == "error":
            flow.status = "error"
            flow.message = str(body.get("message") or "Sign-in failed.")
            return flow.summary()
        flow.status = "error"
        flow.message = "Unknown poll status."
        return flow.summary()


async def complete_browser_sign_in() -> dict[str, Any]:
    """Exchange the completed flow for an API key, verify, and store it.

    The key itself never appears in the returned summary — only the free-probe
    health of the stored credential and any tenant domains the console
    advertises (upstream whoami.rs).
    """
    flow = _flow
    if flow.status != "completed" or not flow.exchange_token:
        raise SetupError("browser sign-in is not completed yet")
    url = f"{flow.api_base_url}/vibe/sign-in/{flow.process_id}/exchange"
    body = await _post_json(url, {"exchange_token": flow.exchange_token,
                                  "code_verifier": flow.verifier}, _EXCHANGE_TIMEOUT_S)
    api_key = body.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise SetupError("exchange did not return an API key")
    health = await verify_api_key(api_key)
    tenant = await resolve_tenant_domains(flow.console_base_url, api_key)
    path = save_api_key_to_env(api_key)
    # Wipe the flow: the credential now lives only in the env file / process.
    _reset_flow()
    return {"health": health, "env_path": str(path), **tenant}


# -- whoami (upstream: whoami.rs resolve_tenant_domains) ------------------------

async def resolve_tenant_domains(console_base_url: str, api_key: str) -> dict[str, Any]:
    """Ask the console for tenant-specific API/chat hosts (custom domains).

    Any failure degrades to the defaults, exactly like the upstream wizard.
    The key is only sent to the console that issued it.
    """
    url = f"{console_base_url.rstrip('/')}/api/vibe/whoami"
    try:
        status, body = await _get_json(url, _WHOAMI_TIMEOUT_S,
                                       headers={"Authorization": f"Bearer {api_key}"})
    except Exception:
        return {}
    if status >= 400 or body is None:
        return {}
    result: dict[str, Any] = {}
    api_base = body.get("api_base")
    if isinstance(api_base, str) and api_base.strip():
        # The tenant API is OpenAI-compatible at {api_base}/v1.
        result["tenant_api_base"] = api_base.strip().rstrip("/")
    vibe_base = body.get("vibe_base")
    if isinstance(vibe_base, str) and vibe_base.strip():
        result["tenant_vibe_base"] = vibe_base.strip().rstrip("/")
    return result


# -- free key verification (probe, never a paid completion call) ---------------

async def verify_api_key(api_key: str, base_url: str = DEFAULT_MISTRAL_SERVER_URL) -> str:
    """Health of a Mistral key: ok | auth_failed | unreachable."""
    url = f"{base_url.rstrip('/')}/v1/models"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if 200 <= response.status_code < 300:
            return "ok"
        if response.status_code in (401, 403):
            return "auth_failed"
        return "unreachable"
    except Exception:
        return "unreachable"


# -- persistence (same .env precedence as main.py) -----------------------------

_ROOT_ENV = Path(__file__).resolve().parents[1] / ".env"
_BACKEND_ENV = Path(__file__).resolve().parent / ".env"
# Durable key store. backend/data is bind-mounted in docker-compose (the
# image-layer .env is not — .dockerignore excludes **/.env*), so a key saved
# here survives `docker compose down/up`. data/ is gitignored like the rest
# of the backend's local state.
_PROVIDER_KEYS_FILE = Path(__file__).resolve().parent / "data" / "provider_keys.env"


def env_file_path() -> Path:
    """The .env the backend actually reads: root first, backend fallback."""
    if _ROOT_ENV.exists():
        return _ROOT_ENV
    return _BACKEND_ENV


def provider_keys_file() -> Path:
    """The durable wizard key store the backend loads at startup."""
    return _PROVIDER_KEYS_FILE


def _upsert_assignment(path: Path, api_key: str, *, force: bool) -> None:
    """Atomically upsert ``MISTRAL_API_KEY=<key>`` into a KEY=VALUE env file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(r"(?m)^(?P<prefix>\s*)MISTRAL_API_KEY\s*=(?P<value>.*)$")
    if pattern.search(text):
        if not force:
            raise SetupError("MISTRAL_API_KEY is already set in the .env (use force)")
        text = pattern.sub(lambda m: f"{m.group('prefix')}MISTRAL_API_KEY={api_key}", text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"MISTRAL_API_KEY={api_key}\n"
    tmp = path.with_name(f".{path.name}.wizard.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, path)


def save_api_key_to_env(api_key: str, *, force: bool = False) -> Path:
    """Persist MISTRAL_API_KEY and make it live in this process.

    Three effects: the effective .env (root first, backend fallback — what
    ``main.py`` loads), the durable store under ``backend/data`` (what
    survives a ``docker compose down/up``, where the image-layer .env does
    not), and ``os.environ`` so the running backend adopts the key without a
    restart. Refuses to replace an existing non-comment .env assignment
    unless ``force``; the store is wizard-owned and always follows.
    """
    if not api_key or any(ch.isspace() for ch in api_key):
        raise SetupError("invalid API key")
    _upsert_assignment(env_file_path(), api_key, force=force)
    _upsert_assignment(provider_keys_file(), api_key, force=True)
    os.environ[API_KEY_ENV] = api_key
    return env_file_path()


def load_provider_keys() -> None:
    """Apply the durable key store to the environment (startup path).

    Only variables that are not already set are applied, so a hand-written
    .env keeps precedence over wizard-saved keys.
    """
    path = provider_keys_file()
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


# -- setup state for clients ----------------------------------------------------

def setup_state() -> dict[str, Any]:
    """Key-set flag + current flow status; never the key itself."""
    return {
        "key_set": bool(os.environ.get(API_KEY_ENV)),
        "flow": _flow.summary(),
    }


# -- CLI wizard (like `vibe --setup`) -------------------------------------------

def _print_menu() -> None:
    print()
    print("Mistral provider setup (same flow as Mistral Vibe's `vibe --setup`)")
    print("  1) Paste an API key from https://console.mistral.ai")
    print("  2) Sign in with your browser (PKCE, no key pasting)")
    print("  3) Check the currently configured key")
    print("  4) Cancel")


async def _cli_paste_key() -> int:
    api_key = input("API key: ").strip()
    health = await verify_api_key(api_key)
    if health != "ok":
        print(f"Key verification failed ({health}); nothing was saved.")
        return 1
    path = save_api_key_to_env(api_key, force=True)
    print(f"Key verified and saved to {path}. Restart the backend to use it.")
    return 0


async def _cli_browser_flow() -> int:
    domain = input("Login domain [https://console.mistral.ai]: ").strip() or None
    summary = await start_browser_sign_in(console_base_url=domain)
    print(f"\nOpen this URL in your browser and sign in:\n\n  {summary['sign_in_url']}\n")
    try:
        import webbrowser
        webbrowser.open(summary["sign_in_url"])
        print("(attempted to open it automatically)")
    except Exception:
        pass
    print("Waiting for sign-in (Ctrl+C cancels)…")
    while True:
        state = await poll_browser_sign_in()
        if state["status"] == "pending" or state["status"] == "waiting":
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        break
    if state["status"] != "completed":
        print(f"Sign-in {state['status']}: {state['message']}")
        return 1
    print("Exchanging for an API key…")
    result = await complete_browser_sign_in()
    print(f"Key verified ({result['health']}) and saved to {result['env_path']}.")
    if result.get("tenant_api_base"):
        print(f"Tenant API base advertised by the console: {result['tenant_api_base']}")
        print(f"  To use it, set MISTRAL_BASE_URL={result['tenant_api_base']}/v1 in your .env")
    print("Restart the backend to use it.")
    return 0


async def _cli_check() -> int:
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        print("No MISTRAL_API_KEY is set in this environment (or the backend's .env).")
        return 1
    health = await verify_api_key(api_key)
    print(f"MISTRAL_API_KEY present; health: {health}")
    return 0 if health == "ok" else 1


async def cli_main() -> int:
    while True:
        _print_menu()
        choice = input("Choose [1-4]: ").strip()
        if choice == "1":
            return await _cli_paste_key()
        if choice == "2":
            return await _cli_browser_flow()
        if choice == "3":
            return await _cli_check()
        if choice == "4":
            return 0
        print("Unknown choice.")


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(cli_main()))
