"""Unit tests for debug artifact retention (diagnostics group 7).

Covers the pure helpers in ``file_translator/diagnostics/retention.py``:
- ``retention_enabled`` (env flag + per-job ``keep_artifacts`` metadata)
- ``mark_retained`` / ``load_retained_info`` (sidecar marker + job:<uuid> assoc)
- ``collect_retained_dirs``
- ``purge_debug_artifacts`` (cap-based + age-based, on_purge callback)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from file_translator.diagnostics.retention import (
    RETAINED_MARKER,
    collect_retained_dirs,
    env_bool,
    load_retained_info,
    mark_retained,
    purge_debug_artifacts,
    retention_enabled,
)


# ── helpers ────────────────────────────────────────────────

def _mk_dir(root: Path, prefix: str, name: str, extra_file: str | None = None) -> Path:
    d = root / f"{prefix}{name}"
    d.mkdir(parents=True, exist_ok=True)
    if extra_file:
        (d / extra_file).write_text("x", encoding="utf-8")
    return d


def _mark(root: Path, d: Path, job_id: str, retained_at: str | None = None) -> Path:
    payload = {"job_id": job_id}
    if retained_at is not None:
        payload["retained_at"] = retained_at
    (d / RETAINED_MARKER).write_text(json.dumps(payload), encoding="utf-8")
    return d


def _fake_job(keep: bool = False) -> SimpleNamespace:
    return SimpleNamespace(metadata={"keep_artifacts": keep})


# ── env_bool / retention_enabled ───────────────────────────

def test_env_bool_defaults():
    assert env_bool("DEBUG_KEEP_ARTIFACTS") is False
    assert env_bool("DEBUG_KEEP_ARTIFACTS", default=True) is True


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
    (" 1 ", True), ("maybe", False),
])
def test_env_bool_variants(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert env_bool("SOME_FLAG") is expected


def test_retention_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DEBUG_KEEP_ARTIFACTS", raising=False)
    assert retention_enabled() is False
    assert retention_enabled(_fake_job(keep=False)) is False


def test_retention_enabled_by_env(monkeypatch):
    monkeypatch.setenv("DEBUG_KEEP_ARTIFACTS", "1")
    assert retention_enabled() is True
    assert retention_enabled(_fake_job(keep=False)) is True


def test_retention_enabled_by_job_metadata():
    assert retention_enabled(_fake_job(keep=True)) is True


def test_retention_job_without_metadata():
    job = SimpleNamespace(metadata=None)
    assert retention_enabled(job) is False


# ── mark_retained / load_retained_info ─────────────────────

def test_mark_retained_creates_sidecar(tmp_path):
    d = _mk_dir(tmp_path, "translator_", "abc")
    mark_retained(d, "job:deadbeef")
    marker = d / RETAINED_MARKER
    assert marker.is_file()
    info = json.loads(marker.read_text(encoding="utf-8"))
    assert info["job_id"] == "job:deadbeef"
    assert "retained_at" in info


def test_mark_retained_missing_dir_is_noop(tmp_path):
    # Must not raise; mimics already-cleaned dir (cancel race).
    mark_retained(tmp_path / "translator_missing", "job:x")
    assert not (tmp_path / "translator_missing").exists()


def test_load_retained_info_none_for_unmarked(tmp_path):
    d = _mk_dir(tmp_path, "translator_", "plain")
    assert load_retained_info(d) is None


def test_load_retained_info_returns_marker(tmp_path):
    d = _mk_dir(tmp_path, "translator_", "abc")
    _mark(root=tmp_path, d=d, job_id="job:123")
    info = load_retained_info(d)
    assert info is not None
    assert info["job_id"] == "job:123"
    assert info["path"] == str(d)
    assert "retained_at" in info


def test_load_retained_info_falls_back_to_mtime(tmp_path):
    d = _mk_dir(tmp_path, "translator_", "corrupt")
    (d / RETAINED_MARKER).write_text("{not json", encoding="utf-8")
    info = load_retained_info(d)
    assert info is not None
    assert info["job_id"] == ""
    assert "retained_at" in info


# ── collect_retained_dirs ──────────────────────────────────

def test_collect_retained_dirs_finds_marked_only(tmp_path):
    kept = _mark(tmp_path, _mk_dir(tmp_path, "translator_", "keep"), "job:1")
    _mark(tmp_path, _mk_dir(tmp_path, "docx_okapi_", "keep2"), "job:2")
    # Unmarked dir must not appear.
    _mk_dir(tmp_path, "translator_", "orphan")
    infos = collect_retained_dirs(tmp_path)
    paths = {i["path"] for i in infos}
    assert paths == {str(kept), str(kept.with_name("docx_okapi_keep2"))}
    assert {i["job_id"] for i in infos} == {"job:1", "job:2"}


def test_collect_retained_dirs_empty(tmp_path):
    assert collect_retained_dirs(tmp_path) == []


# ── purge_debug_artifacts ──────────────────────────────────

def test_purge_respects_max_dirs(tmp_path):
    for i in range(5):
        _mark(tmp_path, _mk_dir(tmp_path, "translator_", f"job{i}"), f"job:{i}")
    purged = purge_debug_artifacts(max_dirs=3, temp_root=tmp_path)
    assert purged == 2
    assert len(collect_retained_dirs(tmp_path)) == 3


def test_purge_deletes_oldest_beyond_cap(tmp_path):
    # Oldest marker ~10 days ago -> must be deleted first.
    old = datetime.now(timezone.utc) - timedelta(days=10)
    _mark(tmp_path, _mk_dir(tmp_path, "translator_", "old"), "job:old", old.isoformat())
    for i in range(3):
        _mk_dir(tmp_path, "translator_", f"fresh{i}")
        _mark(tmp_path, tmp_path / f"translator_fresh{i}", f"job:fresh{i}")
    purged = purge_debug_artifacts(max_dirs=2, max_age_seconds=7 * 86400, temp_root=tmp_path)
    assert purged == 2
    assert not (tmp_path / "translator_old").exists()
    assert len(collect_retained_dirs(tmp_path)) == 2


def test_purge_age_based_only(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _mark(tmp_path, _mk_dir(tmp_path, "translator_", "ancient"), "job:old", old.isoformat())
    fresh = _mark(tmp_path, _mk_dir(tmp_path, "translator_", "fresh"), "job:fresh")
    purged = purge_debug_artifacts(max_dirs=10, max_age_seconds=7 * 86400, temp_root=tmp_path)
    assert purged == 1
    assert not (tmp_path / "translator_ancient").exists()
    assert (tmp_path / "translator_fresh").exists()
    assert fresh.exists()


def test_purge_calls_on_purge_callback(tmp_path):
    purged_infos: list[dict] = []
    for i in range(4):
        _mark(tmp_path, _mk_dir(tmp_path, "translator_", f"job{i}"), f"job:{i}")
    purge_debug_artifacts(
        max_dirs=2,
        temp_root=tmp_path,
        on_purge=lambda info: purged_infos.append(info),
    )
    assert len(purged_infos) == 2
    # Callback receives job_id association (group-7 requirement).
    assert all(i.get("job_id") for i in purged_infos)


def test_purge_skips_unmarked(tmp_path):
    _mk_dir(tmp_path, "translator_", "orphan")
    assert purge_debug_artifacts(max_dirs=1, temp_root=tmp_path) == 0
    assert (tmp_path / "translator_orphan").exists()


def test_purge_none_retained(tmp_path):
    assert purge_debug_artifacts(temp_root=tmp_path) == 0


def test_purge_legacy_corrupt_marker_by_mtime(tmp_path):
    # Corrupt marker (no readable retained_at) falls back to dir mtime.
    # Force an old mtime by writing the dir long ago is not possible, so we
    # emulate a legacy marker whose retained_at is missing entirely and whose
    # mtime is recent -> must survive (not crash, not spuriously deleted).
    d = _mk_dir(tmp_path, "translator_", "legacy")
    (d / RETAINED_MARKER).write_text("garbage", encoding="utf-8")
    assert purge_debug_artifacts(max_dirs=1, max_age_seconds=3600, temp_root=tmp_path) == 0
    assert d.exists()


def test_purge_env_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DEBUG_KEEP_ARTIFACTS_MAX_DIRS", "1")
    for i in range(2):
        _mark(tmp_path, _mk_dir(tmp_path, "translator_", f"job{i}"), f"job:{i}")
    purged = purge_debug_artifacts(temp_root=tmp_path)
    assert purged == 1
    assert len(collect_retained_dirs(tmp_path)) == 1


def test_marker_survives_with_job_association(tmp_path):
    # End-to-end: mark -> collect -> association intact.
    d = _mk_dir(tmp_path, "tikal_", "wd")
    (d / "out.xliff").write_text("<xliff/>", encoding="utf-8")
    mark_retained(d, "job:uuid-123")
    infos = collect_retained_dirs(tmp_path)
    assert len(infos) == 1
    assert infos[0]["job_id"] == "job:uuid-123"
    assert infos[0]["path"] == str(d)
    # The artifact itself is still there for diagnostics.
    assert (d / "out.xliff").exists()