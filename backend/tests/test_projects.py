import json

import pytest
from fastapi.testclient import TestClient

import agent_core
from main import app

client = TestClient(app)


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    """Point project persistence at a temp dir and clean up in-memory state."""
    monkeypatch.setattr(agent_core, "PROJECTS_DIR", str(tmp_path))
    yield tmp_path
    for sid in [s for s in agent_core._histories if s.startswith("proj-")]:
        agent_core._histories.pop(sid, None)
        agent_core._project_names.pop(sid, None)


def _create_project(name="Original name"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_rename_project(projects_dir):
    project = _create_project()

    response = client.patch(f"/api/projects/{project['id']}", json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert response.json()["id"] == project["id"]

    data = json.loads((projects_dir / f"{project['id']}.json").read_text())
    assert data["name"] == "Renamed"
    assert data["history"]  # history survives the rename

    names = [p["name"] for p in client.get("/api/projects").json()]
    assert "Renamed" in names


def test_rename_project_strips_whitespace(projects_dir):
    project = _create_project()

    response = client.patch(f"/api/projects/{project['id']}", json={"name": "  Trimmed  "})
    assert response.status_code == 200
    assert response.json()["name"] == "Trimmed"


def test_rename_project_not_found(projects_dir):
    response = client.patch("/api/projects/proj-doesnotexist", json={"name": "X"})
    assert response.status_code == 404


def test_rename_project_empty_name(projects_dir):
    project = _create_project()

    for bad in ("", "   "):
        response = client.patch(f"/api/projects/{project['id']}", json={"name": bad})
        assert response.status_code == 422

    data = json.loads((projects_dir / f"{project['id']}.json").read_text())
    assert data["name"] == "Original name"


def test_rename_project_name_too_long(projects_dir):
    project = _create_project()

    response = client.patch(f"/api/projects/{project['id']}", json={"name": "x" * 121})
    assert response.status_code == 422

    response = client.patch(f"/api/projects/{project['id']}", json={"name": "x" * 120})
    assert response.status_code == 200


def test_projects_have_created_and_updated(projects_dir):
    """Sorting support: list entries carry created (first save) and updated
    (last activity = file mtime)."""
    project = _create_project("Sortable")
    data = json.loads((projects_dir / f"{project['id']}.json").read_text())
    assert data["created"] > 0

    entries = {p["id"]: p for p in client.get("/api/projects").json()}
    entry = entries[project["id"]]
    assert entry["created"] == data["created"]
    # mtime granularity: allow a small slack around the created timestamp
    assert entry["updated"] >= entry["created"] - 1


def test_reasoning_effort_patch_roundtrip(projects_dir):
    """The per-project effort level is stored, listed, cleared, and validated
    against the project's model (only models that declare effort levels)."""
    project = client.post("/api/projects",
                          json={"name": "Effort", "model": "mistral-glm-5.3",
                                "reasoning_effort": "high"}).json()
    assert project["reasoning_effort"] == "high"

    # Absent field leaves the setting unchanged; null clears it.
    assert client.patch(f"/api/projects/{project['id']}",
                        json={"name": "Effort renamed"}).json()["reasoning_effort"] == "high"
    cleared = client.patch(f"/api/projects/{project['id']}",
                           json={"reasoning_effort": None}).json()
    assert cleared["reasoning_effort"] is None

    assert client.patch(f"/api/projects/{project['id']}",
                       json={"reasoning_effort": "max"}).json()["reasoning_effort"] == "max"
    listing = client.get("/api/projects").json()
    entry = next(p for p in listing if p["id"] == project["id"])
    assert entry["reasoning_effort"] == "max"


def test_reasoning_effort_validation(projects_dir):
    project = client.post("/api/projects",
                          json={"name": "Effort val",
                                "model": "mistral-glm-5.3"}).json()
    # Not an effort level GLM accepts.
    invalid = client.patch(f"/api/projects/{project['id']}",
                           json={"reasoning_effort": "none"})
    assert invalid.status_code == 422
    # Not supported by a model without effort levels.
    kimi = client.post("/api/projects",
                       json={"name": "Kimi proj", "model": "kimi-k3"}).json()
    unsupported = client.patch(f"/api/projects/{kimi['id']}",
                               json={"reasoning_effort": "high"})
    assert unsupported.status_code == 422
    # Invalid at creation too.
    assert client.post("/api/projects",
                       json={"name": "Bad create", "model": "mistral-glm-5.3",
                             "reasoning_effort": "ultra"}).status_code == 422


def test_model_change_drops_incompatible_effort(projects_dir):
    """Switching to a model without effort levels clears the stored setting
    instead of sending an unsupported value to the provider later."""
    project = client.post("/api/projects",
                          json={"name": "Switch", "model": "mistral-glm-5.3",
                                "reasoning_effort": "max"}).json()
    switched = client.patch(f"/api/projects/{project['id']}",
                            json={"model": "kimi-k3"}).json()
    assert switched["model"] == "kimi-k3"
    assert switched["reasoning_effort"] is None
