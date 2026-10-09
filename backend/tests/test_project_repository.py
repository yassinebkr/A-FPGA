"""Durability and concurrency tests for project JSON persistence."""

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from project_repository import CorruptProjectError, ProjectRepository


def test_atomic_save_keeps_previous_snapshot_on_serialization_failure(tmp_path):
    repository = ProjectRepository(tmp_path)
    repository.save("proj-atomic", {"name": "before", "history": []})

    with pytest.raises(TypeError):
        repository.save(
            "proj-atomic",
            {"name": "broken", "history": [], "not_serializable": object()},
        )

    assert repository.load("proj-atomic")["name"] == "before"
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_source_is_in_destination_directory(tmp_path, monkeypatch):
    repository = ProjectRepository(tmp_path)
    replacements = []
    real_replace = __import__("os").replace

    def record_replace(source, destination):
        replacements.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr("project_repository.os.replace", record_replace)
    repository.save("proj-atomic", {"name": "x", "history": []})

    source, destination = replacements[0]
    assert Path(source).parent == tmp_path
    assert Path(destination) == tmp_path / "proj-atomic.json"


def test_concurrent_saves_always_leave_complete_json(tmp_path):
    repository = ProjectRepository(tmp_path)

    def save(index):
        for iteration in range(30):
            repository.save(
                "proj-race",
                {
                    "name": f"worker-{index}",
                    "history": [{"role": "user", "content": f"{index}:{iteration}"}],
                },
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(8)))

    data = repository.load("proj-race")
    assert data["name"].startswith("worker-")
    assert len(data["history"]) == 1
    assert ":" in data["history"][0]["content"]
    assert json.loads((tmp_path / "proj-race.json").read_text()) == data


def test_corrupt_projects_are_reported_and_skipped_from_listing(tmp_path):
    repository = ProjectRepository(tmp_path)
    corrupt = tmp_path / "proj-corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    with pytest.raises(CorruptProjectError):
        repository.load("proj-corrupt")
    assert list(repository.iter_projects()) == []
