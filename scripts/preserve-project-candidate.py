#!/usr/bin/env python3
"""Preserve a project's current RTL as a new, hash-identified candidate.

This is a recovery utility, not revision reconstruction: it never assigns a
historical revision name and never imports prior verification/timing claims.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--output-root", type=Path,
                        default=BACKEND / "data/qualifications/matmul3/candidates")
    args = parser.parse_args(argv)
    project_path = BACKEND / "projects" / f"{args.project_id}.json"
    if not project_path.is_file():
        parser.error(f"project does not exist: {project_path}")
    project = json.loads(project_path.read_text(encoding="utf-8"))
    latest = project.get("latest_verilog") or {}
    source = latest.get("code")
    top = latest.get("top")
    if not isinstance(source, str) or not source.strip() or not isinstance(top, str):
        parser.error("project has no complete latest_verilog source")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    candidate_id = f"recovered-current-{digest[:16]}"
    destination = args.output_root.resolve() / candidate_id
    rtl_path = destination / f"{top}.sv"
    evidence_root = Path(os.environ.get(
        "EVIDENCE_ROOT", BACKEND / "data/evidence")).resolve()
    artifact_path = evidence_root / "artifacts" / digest[:2] / digest
    if artifact_path.is_file() and artifact_path.read_bytes() != source.encode("utf-8"):
        raise RuntimeError("content-addressed artifact collision")
    if not artifact_path.exists():
        _atomic_write(artifact_path, source.encode("utf-8"))
    if artifact_path.read_bytes() != source.encode("utf-8"):
        raise RuntimeError("content-addressed artifact verification failed")
    manifest = {
        "schema": "recovered-project-candidate/v1",
        "candidate_id": candidate_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_project": args.project_id,
        "project_name": project.get("name"),
        "top_module": top,
        "rtl_sha256": digest,
        "rtl_artifact_sha256": digest,
        "rtl": str(rtl_path),
        "historical_revision": None,
        "verification_status": "unverified",
        "warning": "Current project RTL only; this is not recovered r5, r6, or r7.",
    }
    existing = destination / "manifest.json"
    if existing.is_file():
        prior = json.loads(existing.read_text(encoding="utf-8"))
        if prior.get("rtl_sha256") != digest or prior.get("source_project") != args.project_id:
            raise RuntimeError(f"candidate destination conflicts: {destination}")
        # Recovery is idempotent. Never erase qualification fields added by a
        # later independent gate merely because preservation is rerun.
        for key in ("created_at", "qualification_manifest",
                    "qualification_trials", "qualification_vectors_per_trial",
                    "verification_status", "warning"):
            if key in prior:
                manifest[key] = prior[key]
    _atomic_write(rtl_path, source.encode("utf-8"))
    _atomic_write(existing, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
