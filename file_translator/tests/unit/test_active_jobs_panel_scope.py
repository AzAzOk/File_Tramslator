"""The translation app's personal active-jobs panel must show only the caller's
own jobs (change: admin-job-monitoring, task 5.3).

The SPA is a standalone HTML file with no JS test harness, so this guards the
scoping at the source level: `restoreActiveJobs()` has to drop jobs owned by
someone else before they can enter the panel. Without it, a `system:manage`
caller — whose `GET /jobs` returns everyone's jobs as a monitoring feed — would
see another person's document as if it were their own.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INDEX = ROOT / "static" / "index.html"


def _restore_active_jobs_body() -> str:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index("async function restoreActiveJobs()")
    end = html.index("function updateJobStatusUI", start)
    return html[start:end]


def test_panel_drops_jobs_not_owned_by_the_caller() -> None:
    body = _restore_active_jobs_body()
    # The filter compares the job's owner against the signed-in user's id...
    assert re.search(r"job\.user_id\s*!==\s*currentUser\.user_id", body), body
    # ...and skips the job (via `continue`) before it reaches the panel.
    assert "continue;" in body
