"""Shared class-token harvesting for the CSS build and the coverage check.

Both clients derive their class list from HTML and JavaScript sources. A
``class="..."`` value may interpolate a value (``class="badge ${cls}"``) and the
interpolation may itself contain quotes (``${cond ? 'a' : 'b'}``), which a naive
regular expression mis-parses - it reads the JavaScript as if it were more
classes, or drops the literal classes around it.

This scanner reads an attribute value only *outside* ``${ ... }``, so it keeps
the literal class tokens and skips the JavaScript. A second pass collects
arbitrary-value utilities (``bg-[var(--c-error)]``) that appear anywhere in the
source, since those can be produced inside an interpolation too.

Used by ``tools/gen_admin_tailwind.py`` (to decide which utilities to emit) and
``tools/check_css_coverage.py`` (to assert every used class has a rule), so the
two never disagree about what the markup uses.
"""
import re

# Classes that are relationship/state markers, not utilities: they carry no rule
# of their own but change how variant utilities resolve.
MARKER_CLASSES = {"dark", "light", "group", "peer"}

# JS-only selector hooks: the client styles these elements through other classes.
JS_HOOK_PREFIX = "js-"

# Tailwind arbitrary-value groups: `prefix-[value]`. Restricting the bracket scan
# to these keeps JavaScript array/object indexing (`activeJobs[jobId]`) out.
_ARBITRARY_GROUPS = {
    "w", "min-w", "max-w", "h", "min-h", "max-h", "z", "top", "right", "bottom",
    "left", "inset", "translate-x", "translate-y", "bg", "text", "border",
    "rounded", "p", "px", "py", "pt", "pr", "pb", "pl", "m", "mx", "my", "mt",
    "mr", "mb", "ml", "gap", "grid-cols", "grid-rows", "col-span", "row-span",
    "order", "basis", "leading", "tracking", "font", "shadow", "opacity",
    "duration", "delay", "ring", "outline", "fill", "stroke", "scale", "rotate",
    "skew-x", "skew-y", "aspect", "space-x", "space-y", "indent", "columns",
    "blur", "brightness", "contrast", "saturate", "hue-rotate", "content",
}

# A literal class token: plain utility (optionally a variant chain and/or a
# fraction) or an arbitrary-value utility.
_CLASS_TOKEN_RE = re.compile(
    r"^(?:"
    r"-?[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9.]+|:[A-Za-z0-9.\-]+)*(?:/[0-9.]+)?"
    r"|"
    r"-?[A-Za-z][A-Za-z0-9:.\-]*\[[^\]]*\](?:/[0-9.]+)?"
    r")$"
)

_BRACKET_RE = re.compile(r"[A-Za-z0-9_:\-]*\[[^\]\s\"'`,;]*\][A-Za-z0-9_/\-\.%#]*")


def _class_attribute_values(text: str):
    """Yield the literal (non-interpolated) text of every class attribute."""
    i, n = 0, len(text)
    opener = re.compile(r"""class(?:Name)?\s*=\s*(["'])""")
    while i < n:
        m = opener.search(text, i)
        if not m:
            return
        quote = m.group(1)
        j = m.end()
        depth = 0
        buf: list[str] = []
        while j < n:
            if text[j] == "$" and j + 1 < n and text[j + 1] == "{":
                depth += 1
                j += 2
                continue
            c = text[j]
            if depth > 0:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
                continue
            if c == quote:
                break
            buf.append(c)
            j += 1
        yield "".join(buf)
        i = j + 1


def _classlist_values(text: str):
    """Yield the quoted class tokens passed to classList.add/remove/toggle/replace."""
    for m in re.finditer(r"classList\.(?:add|remove|toggle|replace)\s*\(([^)]*)\)", text):
        yield from re.findall(r"""["']([^"']*)["']""", m.group(1))


def _is_token(t: str) -> bool:
    return bool(
        t
        and not t.endswith("-")
        and not t.startswith(JS_HOOK_PREFIX)
        and _CLASS_TOKEN_RE.match(t)
    )


def harvest_class_tokens(text: str) -> set[str]:
    """Return the class tokens a source file relies on."""
    tokens: set[str] = set()
    for value in _class_attribute_values(text):
        tokens.update(t for t in value.split() if _is_token(t))
    for value in _classlist_values(text):
        tokens.update(t for t in value.split() if _is_token(t))
    # Arbitrary-value utilities, anywhere in the file (also inside interpolations).
    for m in _BRACKET_RE.finditer(text):
        tok = m.group(0)
        group = tok.split("[", 1)[0].split(":")[-1].rstrip("-")
        if group in _ARBITRARY_GROUPS and _is_token(tok):
            tokens.add(tok)
    return tokens


def css_escape(name: str) -> str:
    """Escape a class name for use in a CSS selector."""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "\\" + ch for ch in name)
