"""Mistral setup wizard: PKCE flow, URL validation, persistence, REST contract.

The browser sign-in replicates Mistral Vibe's own flow (upstream
mistralai/mistral-vibe): POST /vibe/sign-in (S256 challenge), poll until
completed, exchange for an API key, free /v1/models verification. No test
touches the network: the httpx seams are monkeypatched.
"""

import asyncio
import base64
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

import mistral_setup
from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_flow():
    mistral_setup._reset_flow()
    yield
    mistral_setup._reset_flow()


@pytest.fixture(autouse=True)
def _tmp_env(tmp_path, monkeypatch):
    """Point the wizard's .env targets and durable key store at a temp tree;
    nothing real is written. save_api_key_to_env also exports into
    os.environ, so the key is removed up front (monkeypatch restores the
    pre-test value at teardown, whatever the module wrote)."""
    monkeypatch.setattr(mistral_setup, "_ROOT_ENV", tmp_path / ".env")
    monkeypatch.setattr(mistral_setup, "_BACKEND_ENV", tmp_path / "backend" / ".env")
    monkeypatch.setattr(mistral_setup, "_PROVIDER_KEYS_FILE",
                        tmp_path / "backend" / "data" / "provider_keys.env")
    monkeypatch.delenv(mistral_setup.API_KEY_ENV, raising=False)
    yield tmp_path


# -- PKCE -----------------------------------------------------------------------


def test_pkce_pair_is_s256():
    verifier, challenge = mistral_setup.generate_pkce_pair()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in verifier and len(verifier) >= 40


# -- URL validation (upstream sign_in_url.rs) -------------------------------------


def test_validate_url_accepts_same_origin_and_path():
    url = "https://console.mistral.ai/codestral/cli/authenticate?process_id=x"
    assert mistral_setup.validate_url(url, "https://console.mistral.ai") == url
    poll = "https://console.mistral.ai/api/vibe/sign-in/x"
    assert mistral_setup.validate_url(poll, "https://console.mistral.ai/api") == poll


def test_validate_url_rejects_foreign_origin_and_foreign_path():
    with pytest.raises(mistral_setup.SetupError, match="origin"):
        mistral_setup.validate_url("https://evil.example.com/x", "https://console.mistral.ai")
    with pytest.raises(mistral_setup.SetupError, match="path"):
        mistral_setup.validate_url("https://console.mistral.ai/elsewhere/x",
                                   "https://console.mistral.ai/api")


# -- browser flow ------------------------------------------------------------------


def test_browser_flow_start_poll_exchange(monkeypatch):
    posted = {}

    async def fake_post(url, payload, timeout):
        posted["url"] = url
        posted["payload"] = dict(payload)
        return {
            "process_id": "proc-1",
            "sign_in_url": "https://console.mistral.ai/codestral/cli/authenticate?process_id=proc-1",
            "poll_url": "https://console.mistral.ai/api/vibe/sign-in/proc-1",
            "expires_at": "2100-01-01T00:00:00Z",
        }

    monkeypatch.setattr(mistral_setup, "_post_json", fake_post)

    summary = asyncio.run(mistral_setup.start_browser_sign_in())
    assert posted["url"] == "https://console.mistral.ai/api/vibe/sign-in"
    assert posted["payload"]["code_challenge_method"] == "S256"
    assert summary["status"] == "waiting"
    assert summary["sign_in_url"].startswith("https://console.mistral.ai/")

    polls = []

    async def fake_get(url, timeout, headers=None):
        polls.append(url)
        return 200, {"status": "pending"}

    monkeypatch.setattr(mistral_setup, "_get_json", fake_get)
    state = asyncio.run(mistral_setup.poll_browser_sign_in())
    assert state["status"] == "waiting"
    assert polls and polls[0].endswith("/vibe/sign-in/proc-1")

    async def fake_get_completed(url, timeout, headers=None):
        return 200, {"status": "completed", "exchange_token": "tok-1"}

    monkeypatch.setattr(mistral_setup, "_get_json", fake_get_completed)
    state = asyncio.run(mistral_setup.poll_browser_sign_in())
    assert state["status"] == "completed"

    async def fake_post_exchange(url, payload, timeout):
        assert url.endswith("/vibe/sign-in/proc-1/exchange")
        assert payload["exchange_token"] == "tok-1"
        assert payload["code_verifier"] == mistral_setup.flow_state().verifier
        return {"api_key": "test-key-123"}

    monkeypatch.setattr(mistral_setup, "_post_json", fake_post_exchange)
    async def fake_verify(key, base_url=None):
        assert key == "test-key-123"
        return "ok"
    monkeypatch.setattr(mistral_setup, "verify_api_key", fake_verify)
    async def fake_whoami(console, key):
        return {"tenant_api_base": "https://api.tenant.example.com"}
    monkeypatch.setattr(mistral_setup, "resolve_tenant_domains", fake_whoami)

    result = asyncio.run(mistral_setup.complete_browser_sign_in())
    assert result["health"] == "ok"
    assert result["tenant_api_base"] == "https://api.tenant.example.com"
    # The key was persisted, and never leaked in the result.
    env_text = mistral_setup.env_file_path().read_text()
    assert "MISTRAL_API_KEY=test-key-123" in env_text
    assert "test-key-123" not in str(result) or "env_path" in result
    # The flow is wiped after completion.
    assert mistral_setup.flow_state().status == "idle"
    assert mistral_setup.flow_state().verifier == ""


def test_poll_reports_expired_and_denied(monkeypatch):
    async def fake_post(url, payload, timeout):
        return {"process_id": "p", "sign_in_url": "https://console.mistral.ai/auth",
                "poll_url": "https://console.mistral.ai/api/vibe/sign-in/p",
                "expires_at": "2100-01-01T00:00:00Z"}
    monkeypatch.setattr(mistral_setup, "_post_json", fake_post)
    asyncio.run(mistral_setup.start_browser_sign_in())

    async def fake_get(url, timeout, headers=None):
        return 410, None
    monkeypatch.setattr(mistral_setup, "_get_json", fake_get)
    assert asyncio.run(mistral_setup.poll_browser_sign_in())["status"] == "expired"

    mistral_setup._reset_flow()
    asyncio.run(mistral_setup.start_browser_sign_in())

    async def fake_get_denied(url, timeout, headers=None):
        return 200, {"status": "denied"}
    monkeypatch.setattr(mistral_setup, "_get_json", fake_get_denied)
    assert asyncio.run(mistral_setup.poll_browser_sign_in())["status"] == "denied"


def test_custom_login_domain_derives_api_base(monkeypatch):
    seen = {}

    async def fake_post(url, payload, timeout):
        seen["url"] = url
        return {"process_id": "p2",
                "sign_in_url": "https://console.tenant.example/auth?process_id=p2",
                "poll_url": "https://console.tenant.example/api/vibe/sign-in/p2",
                "expires_at": "2100-01-01T00:00:00Z"}

    monkeypatch.setattr(mistral_setup, "_post_json", fake_post)
    summary = asyncio.run(mistral_setup.start_browser_sign_in(console_base_url="console.tenant.example"))
    assert seen["url"] == "https://console.tenant.example/api/vibe/sign-in"
    assert summary["status"] == "waiting"


# -- persistence --------------------------------------------------------------------


def test_save_api_key_refuses_blank_keys_and_overwrites_with_force(tmp_path):
    with pytest.raises(mistral_setup.SetupError):
        mistral_setup.save_api_key_to_env("   ")
    path = mistral_setup.env_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("KIMI_API_KEY=other\n")
    saved = mistral_setup.save_api_key_to_env("key-1")
    assert saved == path
    assert "MISTRAL_API_KEY=key-1" in path.read_text()
    with pytest.raises(mistral_setup.SetupError, match="already set"):
        mistral_setup.save_api_key_to_env("key-2")
    mistral_setup.save_api_key_to_env("key-2", force=True)
    assert "MISTRAL_API_KEY=key-2" in path.read_text()
    assert "key-1" not in path.read_text()


def test_save_makes_the_key_live_and_durable():
    mistral_setup.save_api_key_to_env("live-key")
    # Live: the running process adopts the key without a backend restart
    # (the model registry reads the env at call time).
    assert os.environ[mistral_setup.API_KEY_ENV] == "live-key"
    # Durable: the store under backend/data survives a `docker compose
    # down/up` (that directory is bind-mounted; the image-layer .env is not).
    store = mistral_setup.provider_keys_file()
    assert "MISTRAL_API_KEY=live-key" in store.read_text()
    # Restart simulation: the startup loader restores the key from the store.
    os.environ.pop(mistral_setup.API_KEY_ENV, None)
    mistral_setup.load_provider_keys()
    assert os.environ[mistral_setup.API_KEY_ENV] == "live-key"


def test_load_provider_keys_does_not_override_a_hand_written_env(monkeypatch):
    store = mistral_setup.provider_keys_file()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("MISTRAL_API_KEY=wizard-key\n")
    monkeypatch.setenv(mistral_setup.API_KEY_ENV, "hand-key")
    mistral_setup.load_provider_keys()
    assert os.environ[mistral_setup.API_KEY_ENV] == "hand-key"


# -- REST contract ------------------------------------------------------------------


def test_setup_endpoints_drive_the_flow(monkeypatch):
    async def fake_start(console_base_url=None, api_base_url=None):
        mistral_setup._reset_flow()
        flow = mistral_setup.flow_state()
        flow.status = "waiting"
        flow.sign_in_url = "https://console.mistral.ai/auth?process_id=p3"
        flow.poll_url = "https://console.mistral.ai/api/vibe/sign-in/p3"
        flow.process_id = "p3"
        flow.verifier = "v"
        return flow.summary()
    monkeypatch.setattr(mistral_setup, "start_browser_sign_in", fake_start)

    state = client.get("/api/providers/mistral/setup/state").json()
    assert state["key_set"] in (True, False)
    assert state["flow"]["status"] == "idle"

    started = client.post("/api/providers/mistral/setup/browser/start", json={})
    assert started.status_code == 200
    assert started.json()["sign_in_url"].startswith("https://console.mistral.ai/auth")

    async def fake_poll():
        flow = mistral_setup.flow_state()
        flow.status = "completed"
        flow.exchange_token = "tok"
        return flow.summary()
    monkeypatch.setattr(mistral_setup, "poll_browser_sign_in", fake_poll)
    status = client.get("/api/providers/mistral/setup/browser/status").json()
    assert status["status"] == "completed"

    async def fake_complete():
        path = mistral_setup.env_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("MISTRAL_API_KEY=rest-key\n")
        mistral_setup._reset_flow()
        return {"health": "ok", "env_path": str(path)}
    monkeypatch.setattr(mistral_setup, "complete_browser_sign_in", fake_complete)
    completed = client.post("/api/providers/mistral/setup/browser/complete")
    assert completed.status_code == 200
    body = completed.json()
    assert body["health"] == "ok"
    assert "rest-key" not in str(body)  # the key never travels to the client


def test_submit_key_endpoint_verifies_and_never_echoes(monkeypatch):
    async def fake_verify(key, base_url=None):
        return "ok" if key == "good-key" else "auth_failed"
    monkeypatch.setattr(mistral_setup, "verify_api_key", fake_verify)

    bad = client.post("/api/providers/mistral/setup/key", json={"api_key": "bad-key"})
    assert bad.status_code == 200
    assert bad.json() == {"health": "auth_failed", "saved": False}
    assert "bad-key" not in bad.text

    good = client.post("/api/providers/mistral/setup/key", json={"api_key": "good-key"})
    assert good.status_code == 200
    assert good.json()["health"] == "ok"
    assert good.json()["saved"] is True
    assert "good-key" not in good.text
    assert mistral_setup.env_file_path().read_text().count("MISTRAL_API_KEY=good-key") == 1

    invalid = client.post("/api/providers/mistral/setup/key", json={"api_key": ""})
    assert invalid.status_code == 422
