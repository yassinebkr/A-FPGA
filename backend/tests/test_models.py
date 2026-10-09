"""Tests for the provider/model registry and per-project model selection."""
import json

import pytest
from fastapi.testclient import TestClient

import agent_core
import model_registry
from main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fake_probe(monkeypatch):
    """Never hit the network in tests: default every health probe to HTTP 200
    and start each test with an empty health cache. Individual tests override
    the fake via their own monkeypatch."""
    model_registry._health_cache.clear()

    async def fake_probe(url, api_key):
        return 200

    monkeypatch.setattr(model_registry, "_probe", fake_probe)
    yield
    model_registry._health_cache.clear()


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    """Point project persistence at a temp dir and clean up in-memory state."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    yield tmp_path
    for sid in [s for s in agent_core._histories if s.startswith("proj-")]:
        agent_core._histories.pop(sid, None)
        agent_core._project_names.pop(sid, None)
        agent_core._project_models.pop(sid, None)


# -- registry ---------------------------------------------------------------

def test_registry_lists_all_entries():
    models = model_registry.list_models()
    keys = [m["key"] for m in models]
    assert keys == ["kimi-k3", "mistral-vibe", "mistral-glm-5.3",
                    "openrouter-kimi-k2.7-code", "openrouter-kimi-k3"]
    default = [m for m in models if m["default"]]
    assert [m["key"] for m in default] == [model_registry.DEFAULT_MODEL_KEY]
    for m in models:
        assert m["display_name"]
        assert isinstance(m["available"], bool)


def test_registry_availability_follows_env(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("KIMI_API_KEY", "dummy")
    monkeypatch.setenv("MISTRAL_API_KEY", "dummy")
    models = {m["key"]: m for m in model_registry.list_models()}
    assert models["kimi-k3"]["available"] is True
    assert models["mistral-vibe"]["available"] is True
    assert models["openrouter-kimi-k3"]["available"] is False


def test_get_model_unknown_key():
    with pytest.raises(KeyError):
        model_registry.get_model("no-such-model")


def test_get_model_kimi_env_overrides(monkeypatch):
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    monkeypatch.setenv("KIMI_MODEL", "kimi-for-coding")
    entry = model_registry.get_model("kimi-k3")
    assert entry["base_url"] == "https://api.moonshot.ai/v1"
    assert entry["model_id"] == "kimi-for-coding"
    assert entry["api_key_env"] == "KIMI_API_KEY"


def test_get_model_mistral_vibe_defaults(monkeypatch):
    """The Mistral entry follows Mistral Vibe's own connection flow
    (mistralai/mistral-vibe: MISTRAL_API_KEY, api.mistral.ai,
    active model mistral-medium-3-5)."""
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    monkeypatch.delenv("MISTRAL_MODEL", raising=False)
    entry = model_registry.get_model("mistral-vibe")
    assert entry["base_url"] == "https://api.mistral.ai/v1"
    assert entry["model_id"] == "mistral-medium-3-5"
    assert entry["api_key_env"] == "MISTRAL_API_KEY"
    assert entry["health_path"] == "/models"


def test_get_model_mistral_env_overrides(monkeypatch):
    monkeypatch.setenv("MISTRAL_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("MISTRAL_MODEL", "devstral-custom")
    entry = model_registry.get_model("mistral-vibe")
    assert entry["base_url"] == "https://api.example.com/v1"
    assert entry["model_id"] == "devstral-custom"


def test_mistral_health_statuses(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "dummy")
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    probes = {}

    async def fake_probe(url, api_key):
        return probes[url]

    monkeypatch.setattr(model_registry, "_probe", fake_probe)
    # The probe must hit the free /v1/models endpoint, never a completion.
    import asyncio
    probes["https://api.mistral.ai/v1/models"] = 200
    assert asyncio.run(model_registry.verify_model("mistral-vibe")) == "ok"
    model_registry._health_cache.clear()
    probes["https://api.mistral.ai/v1/models"] = 401
    assert asyncio.run(model_registry.verify_model("mistral-vibe")) == "auth_failed"
    # No key at all: honest no_key, without touching the network.
    monkeypatch.delenv("MISTRAL_API_KEY")
    model_registry._health_cache.clear()
    assert asyncio.run(model_registry.verify_model("mistral-vibe")) == "no_key"


def test_models_endpoint():
    response = client.get("/api/models")
    assert response.status_code == 200
    keys = [m["key"] for m in response.json()]
    assert "kimi-k3" in keys


# -- REST: model on create/patch ---------------------------------------------

def test_create_project_with_model(projects_dir):
    response = client.post("/api/projects",
                           json={"name": "OR project", "model": "openrouter-kimi-k3"})
    assert response.status_code == 201
    assert response.json()["model"] == "openrouter-kimi-k3"

    data = json.loads((projects_dir / f"{response.json()['id']}.json").read_text())
    assert data["model"] == "openrouter-kimi-k3"


def test_create_project_unknown_model_422(projects_dir):
    response = client.post("/api/projects",
                           json={"name": "X", "model": "no-such-model"})
    assert response.status_code == 422
    assert client.get("/api/projects").json() == []  # nothing was created


def test_create_project_default_model_not_stored(projects_dir):
    response = client.post("/api/projects", json={"name": "Legacy-style"})
    assert response.status_code == 201
    assert response.json()["model"] == model_registry.DEFAULT_MODEL_KEY
    data = json.loads((projects_dir / f"{response.json()['id']}.json").read_text())
    assert "model" not in data  # absent means default, as before


def test_patch_project_model(projects_dir):
    project = client.post("/api/projects", json={"name": "P"}).json()
    response = client.patch(f"/api/projects/{project['id']}",
                            json={"model": "openrouter-kimi-k2.7-code"})
    assert response.status_code == 200
    assert response.json()["model"] == "openrouter-kimi-k2.7-code"
    data = json.loads((projects_dir / f"{project['id']}.json").read_text())
    assert data["model"] == "openrouter-kimi-k2.7-code"


def test_patch_project_unknown_model_422(projects_dir):
    project = client.post("/api/projects", json={"name": "P"}).json()
    response = client.patch(f"/api/projects/{project['id']}", json={"model": "bogus"})
    assert response.status_code == 422
    data = json.loads((projects_dir / f"{project['id']}.json").read_text())
    assert "model" not in data


def test_patch_project_model_not_found(projects_dir):
    response = client.patch("/api/projects/proj-doesnotexist",
                            json={"model": "openrouter-kimi-k3"})
    assert response.status_code == 404


def test_health_reports_default_model_and_count():
    body = client.get("/health").json()
    assert body["model"] == model_registry.get_model("kimi-k3")["model_id"]
    assert body["models_available"] == sum(
        1 for m in model_registry.list_models() if m["available"])


# -- per-session client resolution --------------------------------------------

def test_session_model_key_legacy_project(projects_dir):
    """A project JSON without a "model" field resolves to the default."""
    project = client.post("/api/projects", json={"name": "Legacy"}).json()
    agent_core._histories.pop(project["id"], None)  # force reload from disk
    assert agent_core._session_model_key(project["id"]) == model_registry.DEFAULT_MODEL_KEY


def test_session_model_key_from_project(projects_dir):
    project = client.post("/api/projects",
                          json={"name": "OR", "model": "openrouter-kimi-k3"}).json()
    agent_core._histories.pop(project["id"], None)  # force reload from disk
    agent_core._project_models.pop(project["id"], None)
    assert agent_core._session_model_key(project["id"]) == "openrouter-kimi-k3"


def test_session_model_key_non_project_session():
    assert agent_core._session_model_key("default") == model_registry.DEFAULT_MODEL_KEY


def test_client_for_openrouter_entry(projects_dir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    project = client.post("/api/projects",
                          json={"name": "OR", "model": "openrouter-kimi-k3"}).json()
    client_obj, model_id = agent_core._client_for(project["id"])
    assert model_id == "moonshotai/kimi-k3"
    assert str(client_obj.base_url).rstrip("/") == "https://openrouter.ai/api/v1"


def test_chat_missing_env_key_yields_error(projects_dir, monkeypatch):
    """A project whose model's API key is missing gets an error frame, not a crash."""
    import asyncio
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    project = client.post("/api/projects",
                          json={"name": "OR", "model": "openrouter-kimi-k3"}).json()

    async def main():
        return [event async for event in agent_core.chat("hi", session_id=project["id"])]

    events = asyncio.run(main())
    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "OPENROUTER_API_KEY" in events[0]["content"]
    assert "unavailable" in events[0]["content"]


# -- proactive key verification -----------------------------------------------

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_verify_ok(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "ok"


def test_verify_auth_failed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")

    async def probe_401(url, api_key):
        return 401

    monkeypatch.setattr(model_registry, "_probe", probe_401)
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "auth_failed"


def test_verify_unreachable(monkeypatch):
    import httpx
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")

    async def probe_down(url, api_key):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(model_registry, "_probe", probe_down)
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "unreachable"


def test_verify_unknown_on_404(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "dummy")

    async def probe_404(url, api_key):
        return 404

    monkeypatch.setattr(model_registry, "_probe", probe_404)
    assert _run(model_registry.verify_model("kimi-k3")) == "unknown"


def test_verify_no_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "no_key"


def test_verify_caches_probe(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    calls = []

    async def counting_probe(url, api_key):
        calls.append(url)
        return 200

    monkeypatch.setattr(model_registry, "_probe", counting_probe)
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "ok"
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "ok"
    assert len(calls) == 1  # second call served from the TTL cache


def test_verify_no_key_not_cached(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "no_key"
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")  # key appears later
    assert _run(model_registry.verify_model("openrouter-kimi-k3")) == "ok"


def test_models_endpoint_includes_health():
    response = client.get("/api/models")
    assert response.status_code == 200
    for m in response.json():
        assert m["health"] in ("ok", "auth_failed", "unreachable",
                               "unknown", "no_key", "checking")


def test_health_reports_models_healthy():
    body = client.get("/health").json()
    assert "models_healthy" in body
    assert 0 <= body["models_healthy"] <= body["models_available"]


def test_reasoning_capabilities_are_declared_per_model():
    """The registry records which models think by default and which effort
    levels they accept, so the chat loop can size output headroom and the
    length-stop continuation can lower the effort only where supported."""
    glm = model_registry.get_model("mistral-glm-5.3")
    assert glm["reasoning"] is True
    assert glm["reasoning_efforts"] == ["low", "high", "max"]
    assert "none" not in glm["reasoning_efforts"]  # rejected by the API
    assert model_registry.get_model("kimi-k3")["reasoning"] is True
    assert "reasoning_efforts" not in model_registry.get_model("kimi-k3")
    assert model_registry.get_model("mistral-vibe")["reasoning"] is False
    assert model_registry.get_model("openrouter-kimi-k2.7-code")["reasoning"] is False
    assert model_registry.get_model("openrouter-kimi-k3")["reasoning"] is True


def test_glm_reasoning_effort_default_and_env_override(monkeypatch):
    """GLM's default reasoning effort is low (the platform's serving default
    is high, which exhausts the run budget on small designs); the
    GLM_REASONING_EFFORT env override is read at call time like the other
    provider knobs."""
    monkeypatch.delenv("GLM_REASONING_EFFORT", raising=False)
    assert model_registry.get_model("mistral-glm-5.3")["reasoning_effort_default"] == "low"
    monkeypatch.setenv("GLM_REASONING_EFFORT", "high")
    assert model_registry.get_model("mistral-glm-5.3")["reasoning_effort_default"] == "high"
    monkeypatch.setenv("GLM_REASONING_EFFORT", "max")
    assert model_registry.get_model("mistral-glm-5.3")["reasoning_effort_default"] == "max"
