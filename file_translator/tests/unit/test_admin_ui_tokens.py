"""The admin UI's palette is generated from the main application, so the two can
never drift silently (admin-service-ui task 4.1).

`admin_service/static/tokens.css` is produced by `tools/gen_admin_tailwind.py`
from the token blocks in `static/index.html`. These tests re-derive the tokens
from that source and fail when the committed file is stale, and they enforce the
"no colour literals in the admin UI" rule that keeps a theme switch a pure CSS
change.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

ROOT = Path(__file__).resolve().parents[3]
MAIN_HTML = ROOT / "static" / "index.html"
ADMIN_STATIC = ROOT / "admin_service" / "static"
TOKENS_CSS = ADMIN_STATIC / "tokens.css"
UTIL_CSS = ADMIN_STATIC / "tailwind.css"

TOKEN_RE = re.compile(r"(--c-[A-Za-z0-9-]+)\s*:\s*([^;]+);")
FAVICON_RE = re.compile(r'<link rel="icon"[^>]*>')

# Palette utility prefixes the admin UI must no longer use: every colour is a
# design token, so none of these may reappear in the markup or component CSS.
FORBIDDEN_CLASSES = ("slate", "sky-", "rose", "emerald", "amber", "zinc", "neutral-", "stone")
COLOUR_FUNCTIONS = re.compile(r"\b(?:rgba?|hsla?|oklch|lab|lch)\s*\(")
HEX_COLOUR = re.compile(r"#[0-9A-Fa-f]{3,8}\b")


def _theme_tokens(html: str, selector: str) -> dict[str, str]:
    """Tokens declared by a standalone `selector { ... }` block."""
    for match in re.finditer(re.escape(selector) + r"\s*\{", html):
        end = html.index("}", match.end())
        found = TOKEN_RE.findall(html[match.end() : end])
        if found:
            return {name: value.strip() for name, value in found}
    pytest.fail(f"no {selector} token block found in {MAIN_HTML}")


def _file_tokens(selector: str) -> dict[str, str]:
    css = TOKENS_CSS.read_text(encoding="utf-8")
    for match in re.finditer(re.escape(selector) + r"\s*\{", css):
        end = css.index("}", match.end())
        found = TOKEN_RE.findall(css[match.end() : end])
        if found:
            return {name: value.strip() for name, value in found}
    pytest.fail(f"no {selector} token block found in {TOKENS_CSS}")


# --------------------------------------------------------------- token parity
@pytest.mark.parametrize("selector", [":root", ".dark"])
def test_tokens_css_matches_the_main_application(selector: str) -> None:
    """tokens.css is a verbatim copy of the main app's theme block."""
    expected = _theme_tokens(MAIN_HTML.read_text(encoding="utf-8"), selector)
    actual = _file_tokens(selector)

    divergent = {
        name: (value, actual.get(name, "<missing>"))
        for name, value in expected.items()
        if actual.get(name) != value
    }
    assert not divergent, (
        f"{TOKENS_CSS.name} is stale for {selector} - re-run "
        f"`python tools/gen_admin_tailwind.py`. Divergent tokens (main app -> admin): {divergent}"
    )
    assert set(actual) == set(expected), (
        f"{TOKENS_CSS.name} declares tokens the main app does not: "
        f"{sorted(set(actual) - set(expected))}"
    )


def test_both_themes_declare_the_same_tokens() -> None:
    light = set(_file_tokens(":root"))
    dark = set(_file_tokens(".dark"))
    assert light == dark, f"token sets differ: light-only={sorted(light - dark)} dark-only={sorted(dark - light)}"


def test_accent_is_the_main_applications_primary() -> None:
    """The admin accent must be the main app's primary, not a substitute hue."""
    main_light = _theme_tokens(MAIN_HTML.read_text(encoding="utf-8"), ":root")
    admin_light = _file_tokens(":root")
    assert admin_light["--c-primary"] == main_light["--c-primary"] == "#FDBB30"


# -------------------------------------------------------- generated-CSS sanity
def _strip_css_comments(css: str) -> tuple[str, bool]:
    """Drop `/* ... */` comments. Returns (css, unterminated_comment).

    A regex cannot do this: the token blocks are regular, so an unterminated
    comment in the generated header hides them from every other test here while
    the browser silently applies zero rules.
    """
    out: list[str] = []
    i, start, unterminated = 0, 0, False
    while i < len(css):
        if css.startswith("/*", i):
            out.append(css[start:i])
            end = css.find("*/", i + 2)
            if end == -1:
                return "".join(out), True
            i = end + 2
            start = i
            continue
        i += 1
    out.append(css[start:])
    return "".join(out), unterminated


@pytest.mark.parametrize("name", ["tokens.css", "tailwind.css"])
def test_generated_css_comments_are_balanced(name: str) -> None:
    css = (ADMIN_STATIC / name).read_text(encoding="utf-8")
    _, unterminated = _strip_css_comments(css)
    assert not unterminated, (
        f"{name} has an unterminated /* comment - the browser treats everything "
        f"after it as a comment and applies none of the rules"
    )
    assert css.count("/*") == css.count("*/"), f"{name} has unbalanced comment markers"


@pytest.mark.parametrize("selector", [":root", ".dark"])
def test_theme_blocks_are_not_commented_out(selector: str) -> None:
    """Regression: a missing `*/` shipped tokens.css with 0 parsed rules."""
    css, _ = _strip_css_comments((TOKENS_CSS).read_text(encoding="utf-8"))
    match = re.search(re.escape(selector) + r"\s*\{", css)
    assert match, f"{selector} block vanished once comments are stripped"
    end = css.index("}", match.end())
    assert TOKEN_RE.findall(css[match.end() : end]), f"{selector} block declares no tokens"


# --------------------------------------------------------- no colour literals
# The documentation page (/docs) ships its own markup and script, so it is held
# to the same rule as the admin shell - otherwise the guarantee quietly stops
# applying to a whole screen.
@pytest.mark.parametrize("name", ["index.html", "app.js", "docs.html", "docs.js", "styles.css"])
def test_no_palette_classes_in_admin_sources(name: str) -> None:
    source = (ADMIN_STATIC / name).read_text(encoding="utf-8")
    used = sorted({cls for group in re.findall(r'class="([^"]*)"', source) for cls in group.split()})
    offenders = [cls for cls in used if any(bad in cls for bad in FORBIDDEN_CLASSES)]
    assert not offenders, f"{name} still uses hardcoded palette classes: {offenders}"


@pytest.mark.parametrize("name", ["index.html", "app.js", "docs.html", "docs.js", "styles.css"])
def test_no_colour_literals_in_admin_sources(name: str) -> None:
    # The favicon is a standalone SVG resource: it cannot read the document's
    # custom properties, so its colours are necessarily literal.
    source = FAVICON_RE.sub("", (ADMIN_STATIC / name).read_text(encoding="utf-8"))
    literals = sorted({m.group(0) for m in HEX_COLOUR.finditer(source)})
    assert not literals, f"{name} contains hardcoded colours: {literals}"
    functions = sorted({m.group(0) for m in COLOUR_FUNCTIONS.finditer(source)})
    assert not functions, f"{name} contains hardcoded colour functions: {functions}"


def test_colours_are_token_references() -> None:
    css = (ADMIN_STATIC / "styles.css").read_text(encoding="utf-8")
    colours = re.findall(r"(?:^|[\s:(,])color:\s*([^;]+);", css)
    offenders = [value.strip() for value in colours if "var(--c-" not in value]
    assert not offenders, f"styles.css sets a non-token colour: {offenders}"


def test_text_colours_use_readable_tokens() -> None:
    """Text may only be painted with an `on-*` token.

    The palette's accent and status tokens are mid-tone: #FDBB30 on the light
    surface ramp measures 1.47:1 and #079455 3.02:1, both far below WCAG AA. The
    yellow therefore lives on fills that carry `--c-on-primary-container` text,
    and status is carried by tints and borders rather than by the text itself.
    """
    css = (ADMIN_STATIC / "styles.css").read_text(encoding="utf-8")
    readable = re.compile(r"var\(--c-on-[a-z-]+\)")

    offenders = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        for value in re.findall(r"(?:^|[\s:(,])color:\s*([^;]+);", body):
            value = value.strip()
            if "var(--c-" in value and not readable.search(value):
                offenders.append(f"{selectors.strip()} -> {value}")

    assert not offenders, (
        "text painted with a non-readable token (use an on-* token, or move the "
        f"colour to a border/background): {offenders}"
    )


# ----------------------------------------------------------------- base reset
def test_base_reset_neutralises_native_controls() -> None:
    """Without this the nav painted the user-agent ButtonFace (white)."""
    controls = {"button", "input", "select", "textarea"}
    block = None
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", UTIL_CSS.read_text(encoding="utf-8")):
        if {s.strip() for s in selectors.split(",")} == controls:
            block = body
            break

    assert block, f"no base reset rule covering {sorted(controls)} in {UTIL_CSS.name}"
    for declaration in ("background-color: transparent", "color: inherit", "font: inherit"):
        assert declaration in block, f"base reset must set `{declaration}` (got: {block.strip()})"


def test_hidden_stays_authoritative() -> None:
    """.hidden must outrank component `display` rules such as .modal-backdrop."""
    css = (ADMIN_STATIC / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.hidden\s*\{[^}]*display:\s*none\s*!important", css), (
        ".hidden{display:none!important} must live in styles.css, after the utility layer"
    )


# --------------------------------------------------------------------- themes
def test_root_theme_is_not_hardcoded() -> None:
    html = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
    root = re.search(r"<html[^>]*>", html)
    assert root, "no <html> tag"
    assert not re.search(r'class="[^"]*\bdark\b', root.group(0)), (
        "the initial theme must come from localStorage/prefers-color-scheme, not a hardcoded class"
    )


def test_theme_is_restored_and_persisted() -> None:
    html = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
    assert "localStorage.getItem('theme')" in html
    assert "localStorage.setItem('theme'" in html
    assert "prefers-color-scheme: dark" in html
    assert "classList.toggle('dark'" in html
    # The restore must run in <head> so the class is set before the first paint.
    head, _, _ = html.partition("</head>")
    assert "prefers-color-scheme: dark" in head


def test_theme_toggle_is_present_and_labelled() -> None:
    html = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="theme-toggle"' in html
    assert "icon-sun" in html and "icon-moon" in html
    assert "aria-label" in html
    assert "/tokens.css" in html, "the token layer must be linked"


def test_token_link_precedes_the_utility_layer() -> None:
    html = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index("/tokens.css") < html.index("/tailwind.css")
    assert html.index("/tailwind.css") < html.index("/styles.css")
