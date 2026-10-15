"""The two clients' vendored CSS bundles stay in sync with the classes their
markup uses, and the generated admin assets stay current.

Backs the `ui-css-build` capability: `tools/check_css_coverage.py` fails when a
used class has no rule in the bundle a page loads, or when
`admin_service/static/tailwind.css` / `tokens.css` no longer match a fresh run of
`tools/gen_admin_tailwind.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))

import check_css_coverage as coverage  # noqa: E402


def test_every_used_class_is_defined_and_generated_css_is_current() -> None:
    """The whole tree passes: no undefined class, no stale generated asset."""
    problems = coverage.run_checks()
    assert problems == [], "\n".join(problems)


def test_checker_detects_a_class_missing_from_a_bundle(monkeypatch) -> None:
    """An injected used-but-undefined class is reported, not ignored."""
    real_harvest = coverage.harvest_class_tokens

    def fake_harvest(text: str) -> set[str]:
        return real_harvest(text) | {"definitely-not-a-real-utility"}

    monkeypatch.setattr(coverage, "harvest_class_tokens", fake_harvest)
    main = next(c for c in coverage.CLIENTS if c.name == "main app")
    assert "definitely-not-a-real-utility" in coverage.check_client(main)


def test_checker_detects_stale_generated_assets(tmp_path, monkeypatch) -> None:
    """A committed generated file that differs from a fresh build is reported."""
    (tmp_path / "tailwind.css").write_text("/* stale */", encoding="utf-8")
    (tmp_path / "tokens.css").write_text("/* stale */", encoding="utf-8")
    monkeypatch.setattr(coverage, "ADMIN_STATIC", tmp_path)
    problems = coverage.check_generated_current()
    assert any("stale" in problem for problem in problems)
