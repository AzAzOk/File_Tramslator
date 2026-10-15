"""The admin monitoring block is sourced from durable history (job-monitoring-history).

The block lives in `admin_service/static/app.js`. These tests hold its source to
the contract the change specifies: it reads the durable history (so jobs removed
from the live store are still listed), folds the append-only entries to the
latest record per job, renders a bounded window (most recent 100 by default, or
everything since 00:00 UTC) inside a scroll container, and carries no action
controls (task 7.1-7.3).

The pure window helpers are executed under node, because the bounding and the
day filter are behaviour, not a substring.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "admin_service" / "static" / "app.js"
CSS_JS = ROOT / "admin_service" / "static" / "styles.css"
SOURCE = APP_JS.read_text(encoding="utf-8")
CSS = CSS_JS.read_text(encoding="utf-8")


def _run_under_node(driver: str) -> dict:
    """Execute a driver script through a UTF-8 file, not argv (see test_admin_ui_rights)."""
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the block's source logic")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as script:
        script.write(driver)
        script_path = script.name
    try:
        result = subprocess.run(
            ["node", script_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    return json.loads(result.stdout)


def _extract(pattern: str) -> str:
    match = re.search(pattern, SOURCE, re.S | re.M)
    assert match, f"pattern not found in app.js: {pattern}"
    return match.group(1)


def _window_helpers() -> str:
    """The constants and pure helpers the block's window is built from."""
    return "\n".join(
        [
            _extract(r"(const MONITOR_RANGE_RECENT = .*?;)"),
            _extract(r"(const MONITOR_RANGE_TODAY = .*?;)"),
            _extract(r"(const MONITOR_RECENT_LIMIT = .*?;)"),
            _extract(r"(function foldLatestPerJob\(.*?^\})"),
            _extract(r"(function entriesSinceUtcMidnight\(.*?^\})"),
            _extract(r"(function visibleMonitorEntries\(.*?^\})"),
        ]
    )


# --------------------------------------------------------------------- 7.1
def test_block_reads_the_durable_history_not_the_live_feed() -> None:
    # The durable history endpoint is the source; the old live feed is gone.
    assert "api('/api/monitoring/history?limit=500')" in SOURCE
    assert "/api/jobs" not in SOURCE
    assert "visibleMonitorEntries(monitorHistory" in SOURCE


def test_a_completed_then_removed_job_is_still_listed() -> None:
    """A job whose only recorded transition is `completed` survives folding."""
    driver = f"""
    {_window_helpers()}
    const folded = foldLatestPerJob([
      {{ job_id: 'gone', status: 'completed', timestamp: '2026-01-01T10:00:00+00:00' }},
      {{ job_id: 'live', status: 'running', timestamp: '2026-01-01T09:00:00+00:00' }},
      {{ job_id: 'live', status: 'pending', timestamp: '2026-01-01T08:00:00+00:00' }},
    ]);
    console.log(JSON.stringify({{
      ids: folded.map((e) => e.job_id),
      goneStatus: folded.find((e) => e.job_id === 'gone').status,
      liveStatus: folded.find((e) => e.job_id === 'live').status,
    }}));
    """
    out = _run_under_node(driver)
    assert out["ids"] == ["gone", "live"]  # newest-first preserved, one row per job
    assert out["goneStatus"] == "completed"
    assert out["liveStatus"] == "running"  # the latest of the two live transitions


# --------------------------------------------------------------------- 7.2
def test_default_window_is_most_recent_100() -> None:
    assert _extract(r"(const MONITOR_RECENT_LIMIT = .*?;)").endswith("100;")
    driver = f"""
    {_window_helpers()}
    const many = Array.from({{ length: 150 }}, (_, i) => ({{
      job_id: 'j' + String(i).padStart(3, '0'),
      status: 'completed',
      timestamp: '2026-01-02T10:00:00+00:00',
    }}));
    const now = Date.parse('2026-01-02T12:00:00Z');
    const recent = visibleMonitorEntries(many, MONITOR_RANGE_RECENT, now);
    console.log(JSON.stringify({{ count: recent.length, first: recent[0].job_id }}));
    """
    out = _run_under_node(driver)
    assert out["count"] == 100
    assert out["first"] == "j000"  # order preserved; the window is the first 100


def test_today_range_keeps_only_entries_since_utc_midnight() -> None:
    driver = f"""
    {_window_helpers()}
    const now = Date.parse('2026-01-02T12:00:00Z');
    const entries = [
      {{ job_id: 'today', status: 'completed', timestamp: '2026-01-02T05:00:00+00:00' }},
      {{ job_id: 'yesterday', status: 'completed', timestamp: '2026-01-01T23:59:00+00:00' }},
      {{ job_id: 'exactly-midnight', status: 'running', timestamp: '2026-01-02T00:00:00+00:00' }},
    ];
    const today = visibleMonitorEntries(entries, MONITOR_RANGE_TODAY, now);
    console.log(JSON.stringify({{ ids: today.map((e) => e.job_id) }}));
    """
    out = _run_under_node(driver)
    assert out["ids"] == ["today", "exactly-midnight"]  # 00:00 UTC is included


def test_block_is_rendered_inside_a_bounded_scroll_container() -> None:
    assert 'class="table-wrap job-scroll"' in SOURCE
    # The container caps its height and scrolls vertically without page overflow.
    block = re.search(r"\.job-scroll \{[^}]*\}", CSS, re.S)
    assert block, ".job-scroll rule not found in styles.css"
    rule = block.group(0)
    assert "max-height:" in rule
    assert "overflow-y: auto;" in rule
    assert "overflow-x: auto;" in rule


# --------------------------------------------------------------------- 7.3
def test_the_rendered_rows_carry_no_action_controls() -> None:
    """Rows are read-only — no cancel/download/delete affordance anywhere."""
    rows = _extract(r"(function renderJobRows\(.*?^\})")
    for forbidden in ("<button", "cancel", "download", "delete", "/api/"):
        assert forbidden not in rows
    # Exactly the five reporting columns, no extra action column.
    body = _extract(r"(<tbody id=\"jobs-rows\"></tbody>)")
    assert body
    header = _extract(r"(<thead><tr><th>Кто</th>.*?</tr></thead>)")
    assert header.count("<th>") == 5
