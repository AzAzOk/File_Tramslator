"""The admin rights dialog is wired to the deviation model (user-rights-collection-levels, tasks 4.1–4.4, 8.2–8.4, 10.1–10.5).

The dialog lives in `admin_service/static/app.js`, so these tests hold its
source to the contract the API implements: it renders the *effective* set (not
the personal one), submits the desired effective set, turns the returned counts
into a confirmation, and shows the role offer / «Вернуть к роли» only when
there is something to offer or to undo. The collections tab (8.2–8.4) is held
to the per-collection contract: four states in cascade order on the server's
effective level, personal levels saved through the grant API, `default` never
below reading, and the built-in admin shown as unrestricted. Feedback round
10: no per-row source labels anywhere (10.1–10.2), the dialog body is the
locked, contained scroll surface (10.3), and rendering keeps exactly one
checked radio per collection row — executed under node, because that is
behaviour, not a substring (10.4–10.5).
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "admin_service" / "static" / "app.js"
CSS_JS = ROOT / "admin_service" / "static" / "styles.css"
SOURCE = APP_JS.read_text(encoding="utf-8")
CSS = CSS_JS.read_text(encoding="utf-8")


def _run_under_node(driver: str) -> dict:
    """Execute a driver script through a UTF-8 file, not argv.

    On Windows the command line uses the locale codepage, which would hand
    node a double-encoded source and make every Cyrillic label mojibake.
    """
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the dialog's source logic")
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


# --------------------------------------------------------------------- 4.1
def test_dialog_renders_the_effective_set_without_source_labels() -> None:
    assert "permissionGrid(user.effective)" in SOURCE
    # The personal set alone would open a role-derived right unchecked.
    assert "permissionGrid(user.permissions)" not in SOURCE
    # Feedback 10.1–10.2: the dialog carries no per-row source label, and the
    # helpers that built them are gone rather than merely uncalled.
    assert "rightsSources" not in SOURCE
    assert "collectionChip" not in SOURCE
    assert "perm-src" not in SOURCE
    assert ".perm-src" not in CSS


def test_dialog_uses_the_servers_denial_aware_effective_set() -> None:
    """`role ∪ own` still counts a personally denied right as granted."""
    assert "u.effective" in SOURCE


# -------------------------------------------------------------------- 10.3
def test_dialog_scroll_is_locked_reset_and_contained() -> None:
    """The page behind must not move, and a reopen starts at the top."""
    assert "document.body.style.overflow = 'hidden';" in SOURCE
    assert "document.body.style.overflow = '';" in SOURCE
    assert "$('#modal-body').scrollTop = 0;" in SOURCE
    assert "overscroll-behavior: contain;" in CSS
    assert "scrollbar-width: thin;" in CSS
    # A full-screen blur is re-composited every frame and made the scroll feel
    # heavy on weak GPUs; the 55% tint alone keeps the backdrop readable.
    assert "backdrop-filter: blur(2px)" not in CSS


# -------------------------------------------------------------------- 10.4
def test_each_collection_row_keeps_one_checked_radio_of_its_own() -> None:
    """Executed under node: rendering three rows never shares a selection."""
    levels = re.search(r"(const COLLECTION_LEVELS = \[.*?\];)", SOURCE, re.S)
    helper = re.search(r"(function collectionLevels\(name, level.*?^\})", SOURCE, re.S | re.M)
    pane = re.search(r"(function collectionPane\(access\) \{.*?^\})", SOURCE, re.S | re.M)
    assert levels and helper and pane, "collectionPane()/collectionLevels() not found in app.js"
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the dialog's source logic")
    driver = f"""
    const esc = (s) => String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    {levels.group(1)}
    {helper.group(1)}
    {pane.group(1)}
    console.log(JSON.stringify({{
      html: collectionPane({{
        collections: [
          {{ collection: 'dtd', level: 2, source: 'group:DTD' }},
          {{ collection: 'oup', level: 0, source: 'none' }},
          {{ collection: 'default', level: 1, source: 'default' }},
        ],
      }}),
    }}));
    """
    html = _run_under_node(driver)["html"]

    # 10.2: no source chip in the rendered rows.
    assert "perm-src" not in html
    assert 'type="radio"' in html
    # Exactly one checked radio per row, on the level the server reported.
    checked = re.findall(r'name="coll-level-(\d+)" value="(\d+)" checked', html)
    assert checked == [("0", "2"), ("1", "0"), ("2", "1")]
    # Each row is its own radio group: four inputs keyed by the row index
    # alone, so clicking one row can never move another row's selection.
    per_group: dict[str, int] = {}
    for index in re.findall(r'name="coll-level-(\d+)"', html):
        per_group[index] = per_group.get(index, 0) + 1
    assert per_group == {"0": 4, "1": 4, "2": 4}


# --------------------------------------------------------------------- 4.2
def test_save_submits_the_desired_effective_set_and_reports_the_counts() -> None:
    assert "permissions: collectPermissions()" in SOURCE
    assert "res.raised + res.lowered" in SOURCE
    assert "+${res.raised}" in SOURCE
    assert "−${res.lowered}" in SOURCE
    assert "отклонений от роли нет" in SOURCE


# --------------------------------------------------------------------- 4.3
def test_role_offer_follows_the_suggestion_and_clears_deviations() -> None:
    assert "openRoleOffer(user, res)" in SOURCE
    assert "if (res.suggestions && res.suggestions.length)" in SOURCE
    assert "clear_deviations: true" in SOURCE
    # Several roles may fit; the dialog must not pick one silently.
    assert "names.length > 1" in SOURCE
    # The offer replaces the save dialog in place and must not be closed away.
    assert re.search(r"openRoleOffer\(user, res\);\s*\n\s*return false;", SOURCE)


# --------------------------------------------------------------------- 4.4
def test_reset_to_role_is_gated_on_deviations_and_names_both_counts() -> None:
    assert "const deviations = own.length + lowered.length;" in SOURCE
    assert re.search(
        r"deviations\s*\?\s*`<button class=\"btn js-reset-rights\"", SOURCE
    ), "the button must be rendered only while deviations exist"
    assert "/reset-rights" in SOURCE
    assert "повышенных прав — ${raised}, пониженных — ${lowered}" in SOURCE
    # After the reset the counts are zero, so the re-render drops the button.
    assert "renderUsers();" in SOURCE


# --------------------------------------------------------------------- 8.2
def test_dialog_has_a_collections_tab_next_to_the_functions_tab() -> None:
    """Both panes live in one dialog under one Save (design D8)."""
    assert 'data-pane="perms">Функции<' in SOURCE
    assert 'data-pane="colls">Глоссарии<' in SOURCE
    assert (
        'class="dlg-pane hidden" data-pane="colls">${collectionPane(access)}</div>' in SOURCE
    ), "the collections pane must be part of the same modal body"


def test_collections_tab_searches_and_offers_the_four_states() -> None:
    assert "coll-search" in SOURCE
    assert re.search(
        r"COLLECTION_LEVELS = \[\s*\[0, '—'\],\s*\[1, 'смотреть'\],"
        r"\s*\[2, 'добавлять'\],\s*\[3, 'править и удалять'\],\s*\]",
        SOURCE,
    ), "the four states must exist in the cascade order the resolver enforces"
    # The row opens on the level the server reported — the effective one —
    # so the selection shows today's access before any change. The renderer is
    # now shared with the matrix (design D1), so the checked expression lives in
    # `collectionLevels` and `collectionPane` only supplies the current level.
    assert "level === value ? ' checked'" in SOURCE
    assert "collectionLevels(`coll-level-${index}`, c.level, { floor })" in SOURCE
    # The row is keyed by collection so the save can diff against the load.
    assert 'data-collection="${esc(c.collection)}"' in SOURCE


def test_tab_switch_and_search_are_wired_inside_the_modal() -> None:
    assert "#modal-body .dlg-tab" in SOURCE
    assert "#modal-body .coll-search" in SOURCE
    # Switching tabs must not be mistaken for closing or saving.
    assert "pane.dataset.pane !== tab.dataset.pane" in SOURCE


# --------------------------------------------------------------------- 8.3
def test_personal_levels_save_through_the_existing_grant_api() -> None:
    """Only rows whose selection differs from the load are written, per user."""
    assert "'/api/access/grant'" in SOURCE
    assert "subject_type: 'user'" in SOURCE
    assert "subject: user.user_id" in SOURCE
    assert "level: change.level" in SOURCE
    # Diff against the server's own report, never against a client default.
    assert "if (!before || before.level !== level)" in SOURCE
    # Levels are saved before the permissions POST: the role offer compares
    # the resulting per-collection levels and must see them already stored.
    assert SOURCE.index("for (const change of changed)") < SOURCE.index(
        "user.user_id}/permissions"
    )


# --------------------------------------------------------------------- 8.4
def test_default_collection_refuses_the_zero_state() -> None:
    # The floor lives in the shared renderer: `default` is readable by all, so
    # the control disables every level below `смотреть`.
    assert "const floor = c.collection === 'default' ? 1 : 0;" in SOURCE
    assert "value < floor" in SOURCE
    assert "denied ? ' disabled' : ''" in SOURCE
    # The disabled styling lives with the rest of the dialog's CSS.
    css = (ROOT / "admin_service" / "static" / "styles.css").read_text(encoding="utf-8")
    assert ".coll-levels label:has(input:disabled)" in css


def test_admin_role_shows_unrestricted_instead_of_rows() -> None:
    assert "if (access.unrestricted)" in SOURCE
    assert "coll-unrestricted" in SOURCE
    assert "Без ограничений" in SOURCE
    # An empty list would render as "no collections at all" — never that.
    assert "coll-list" in SOURCE


# ------------------------------------------------- access matrix (unification)
def test_matrix_splits_subjects_into_three_views() -> None:
    assert re.search(
        r"MATRIX_VIEWS = \[\s*\['role', 'Роли'\],\s*\['user', 'Пользователи'\],"
        r"\s*\['group', 'Группы'\],\s*\]",
        SOURCE,
    ), "the matrix must offer one view per subject kind"
    assert "row.subject_type === view" in SOURCE


def test_matrix_writes_levels_and_drops_the_read_write_pair() -> None:
    """The whole point of the change: a level in, a level out (proposal)."""
    save = re.search(r"(function saveMatrixLevel\(.*?^\})", SOURCE, re.S | re.M)
    assert save, "saveMatrixLevel() not found in app.js"
    body = save.group(1)
    assert "'/api/access/grant'" in body
    assert "subject_type: subjectType, subject, collection, level" in body
    # The old flag-only write and its R/W checkboxes must be gone for good.
    assert "read, write" not in body
    assert "data-flag" not in SOURCE
    assert "cell-pair" not in SOURCE


def test_matrix_reuses_the_shared_control_in_a_popover() -> None:
    assert "collectionLevels('popover-level'" in SOURCE
    # The `default` floor is applied by the popover too.
    assert "collection === 'default' ? 1 : 0" in SOURCE
    # Built-in admin is reported by the backend and rendered as unrestricted.
    assert "row.unrestricted" in SOURCE


def test_matrix_table_renders_views_indicators_and_filter() -> None:
    """Executed under node: three views, level badges, and a column filter."""
    glyphs = re.search(r"(const MATRIX_LEVEL_GLYPHS = \{.*?\};)", SOURCE, re.S)
    titles = re.search(r"(const MATRIX_LEVEL_TITLES = \{.*?\};)", SOURCE, re.S)
    state = re.search(r"(const matrixState = \{.*?\};)", SOURCE, re.S)
    table = re.search(r"(function matrixTableHtml\(\) \{.*?^\})", SOURCE, re.S | re.M)
    assert glyphs and titles and state and table, "matrix renderer not found in app.js"
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the matrix's source logic")
    driver = f"""
    const esc = (s) => String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    {glyphs.group(1)}
    {titles.group(1)}
    {state.group(1)}
    {table.group(1)}
    matrixState.data = {{
      collections: ['default', 'oup'],
      rows: [
        {{ subject_type: 'role', subject: 'admin', label: 'role: admin', unrestricted: true, cells: {{}} }},
        {{ subject_type: 'role', subject: 'user', label: 'role: user', unrestricted: false,
          cells: {{ default: {{ read: true, write: false, level: 1 }},
                    oup: {{ read: true, write: false, level: 2 }} }} }},
        {{ subject_type: 'user', subject: 'u-1', label: 'user: ivanov', unrestricted: false, cells: {{}} }},
      ],
    }};
    const roleView = matrixTableHtml();
    matrixState.view = 'user';
    const userView = matrixTableHtml();
    matrixState.view = 'role';
    matrixState.filter = 'oup';
    const filtered = matrixTableHtml();
    console.log(JSON.stringify({{ roleView, userView, filtered }}));
    """
    out = _run_under_node(driver)

    # 3.1: each subject kind only appears in its own view.
    assert "admin</td>" in out["roleView"] and "user</td>" in out["roleView"]
    assert "ivanov" not in out["roleView"]
    assert "ivanov" in out["userView"] and "admin</td>" not in out["userView"]

    # 3.2/3.6: compact level badges; level 0 stays a dash; admin is unrestricted.
    assert "cell-level level-1" in out["roleView"]
    assert "cell-level level-2" in out["roleView"]
    assert "R+" in out["roleView"]
    assert "cell-level level-3" not in out["roleView"]
    assert "Без ограничений" in out["roleView"]

    # 3.7: the search narrows the collection columns.
    assert '<th class="rot">oup</th>' in out["filtered"]
    assert '<th class="rot">default</th>' not in out["filtered"]
