"""Wiki page schema (paper Appendix E) and wikilink utilities.

A knowledge page is a Markdown file with a fixed layout:

    ---
    type: <category>
    created: YYYY-MM-DD
    updated: YYYY-MM-DD
    aliases: [A, B]
    tags: [t1, t2]
    ---
    # <Title>
    > one-line summary
    ## Key Facts
    - fact ...
    ## Related Pages
    - [[dir/Page]] -- relation note
    ## Related Sources
    - [[sources/digests/xxx]] -- note

Wikilinks use the ``[[path]]`` or ``[[path|label]]`` syntax. Paths are
always relative to the wiki root, without the ``.md`` extension.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")

REQUIRED_SECTIONS = ("## Key Facts", "## Related Pages", "## Related Sources")

SOURCE_REF_RE = re.compile(r"^\[\[sources/digests/[A-Za-z0-9_.\-]+\]\]$")


def extract_links(text: str) -> list[str]:
    """Return all wikilink targets in order of appearance (deduplicated)."""
    seen: dict[str, None] = {}
    for m in WIKILINK_RE.finditer(text):
        seen.setdefault(m.group(1).strip())
    return list(seen)


def slugify(title: str) -> str:
    """Turn a page title into a file-safe Wiki page name.

    "John V, Prince of Anhalt-Zerbst" -> "John-V-Prince-of-Anhalt-Zerbst"
    """
    s = re.sub(r"[^A-Za-z0-9\s\-]", "", title)
    s = re.sub(r"[\s\-]+", "-", s.strip())
    return s.strip("-")


@dataclass
class Page:
    """In-memory representation of a knowledge page update."""

    path: str  # e.g. "people/John-V-Prince-of-Anhalt-Zerbst" (no .md)
    title: str
    page_type: str = ""
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    summary: str = ""
    key_facts: list[str] = field(default_factory=list)
    # (target, note) pairs; target is a wikilink path without .md
    related_pages: list[tuple[str, str]] = field(default_factory=list)
    related_sources: list[tuple[str, str]] = field(default_factory=list)
    created: str = ""
    updated: str = ""

    @property
    def category(self) -> str:
        return self.path.split("/")[0] if "/" in self.path else ""


def _fmt_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def render_page(page: Page, today: str) -> str:
    """Render a Page to Markdown text in the Appendix-E schema."""
    created = page.created or today
    updated = page.updated or today
    lines = [
        "---",
        f"type: {page.page_type or page.category}",
        f"created: {created}",
        f"updated: {updated}",
        f"aliases: {_fmt_list(page.aliases)}",
        f"tags: {_fmt_list(page.tags)}",
        "---",
        f"# {page.title}",
        "",
        f"> {page.summary}",
        "",
        "## Key Facts",
        "",
    ]
    lines += [f"- {f}" for f in page.key_facts] or ["- (none)"]
    lines += ["", "## Related Pages", ""]
    lines += [f"- [[{t}]] -- {n}" if n else f"- [[{t}]]" for t, n in page.related_pages] or ["- (none)"]
    lines += ["", "## Related Sources", ""]
    lines += [f"- [[{t}]] -- {n}" if n else f"- [[{t}]]" for t, n in page.related_sources] or ["- (none)"]
    return "\n".join(lines) + "\n"


def parse_frontmatter(text: str) -> dict:
    """Lenient frontmatter parser (simple ``key: value`` / ``key: [a, b]``)."""
    fm: dict = {"aliases": [], "tags": []}
    if not text.startswith("---"):
        return fm
    end = text.find("\n---", 3)
    if end == -1:
        return fm
    for line in text[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val.startswith("[") and val.endswith("]"):
            fm[key] = [x.strip() for x in val[1:-1].split(",") if x.strip()]
        else:
            fm[key] = val
    return fm


def parse_section_links(text: str, section: str) -> list[tuple[str, str]]:
    """Parse ``- [[target]] -- note`` bullets of a given ``## section``."""
    m = re.search(rf"^## {re.escape(section)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        lm = re.match(r"\s*-\s*\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]\s*(?:--\s*(.*))?$", line)
        if lm:
            out.append((lm.group(1).strip(), (lm.group(2) or "").strip()))
    return out


def rewrite_section(text: str, section: str, keep) -> tuple[str, list[tuple[str, str]]]:
    """Filter the bullets of a ``## section`` block via ``keep(target)``.

    Surgical edit — everything outside the section is left byte-identical.
    The section body is regenerated from its parsed bullets (canonical
    ``- [[target]] -- note`` form); bullets whose target fails ``keep`` are
    dropped, and an emptied section gets the ``- (none)`` placeholder, matching
    render_page. Returns (new_text, removed (target, note) pairs); returns
    the original text and [] when nothing was removed or the section is absent.
    """
    entries = parse_section_links(text, section)
    removed = [(t, n) for t, n in entries if not keep(t)]
    if not removed:
        return text, []
    kept = [(t, n) for t, n in entries if keep(t)]
    bullets = [f"- [[{t}]] -- {n}" if n else f"- [[{t}]]" for t, n in kept] or ["- (none)"]
    block = f"## {section}\n\n" + "\n".join(bullets) + "\n"
    new = re.sub(rf"^## {re.escape(section)}\s*\n.*?(?=^## |\Z)",
                 lambda _m: block, text, count=1, flags=re.M | re.S)
    return new, removed
