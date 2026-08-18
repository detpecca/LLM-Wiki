"""Error validators (paper Appendix F, Table 6).

Structural errors are detected deterministically; content-level errors
require LLM verification (see llm_content_validate).
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from . import schema
from .store import WikiStore

# --- error type identifiers (Table 6) ---
DANGLING_LINK = "dangling_link"
INCOMPLETE_PAGE = "incomplete_page"
MALFORMED_REF = "malformed_reference"
UNSEEN_OVERWRITE = "unseen_overwrite"
INDEX_INCONSISTENCY = "index_inconsistency"
UNSUPPORTED_FACT = "unsupported_fact"
CROSS_PAGE_CONTRADICTION = "cross_page_contradiction"

STRUCTURAL_TYPES = (
    DANGLING_LINK,
    INCOMPLETE_PAGE,
    MALFORMED_REF,
    UNSEEN_OVERWRITE,
    INDEX_INCONSISTENCY,
)
CONTENT_TYPES = (UNSUPPORTED_FACT, CROSS_PAGE_CONTRADICTION)


@dataclass
class WikiError:
    type: str
    page: str  # page the error was found on ("" if global)
    detail: str

    def __str__(self) -> str:
        return f"[{self.type}] {self.page}: {self.detail}"


# ------------------------------------------------------------- structural
def check_dangling_links(store: WikiStore, pages: list[str] | None = None) -> list[WikiError]:
    """Inter-page links targeting non-existent pages (cross-validated with FS)."""
    errors = []
    for rel in pages or store.iter_pages():
        for link in schema.extract_links(store.read(rel)):
            target = link.removesuffix(".md")
            if not store.exists(target):
                errors.append(WikiError(DANGLING_LINK, rel, f"link to missing page [[{link}]]"))
    return errors


def check_incomplete_pages(store: WikiStore, pages: list[str] | None = None) -> list[WikiError]:
    """Required sections missing (template completeness check)."""
    errors = []
    for rel in pages or store.iter_pages():
        text = store.read(rel)
        for sec in schema.REQUIRED_SECTIONS:
            if sec not in text:
                errors.append(WikiError(INCOMPLETE_PAGE, rel, f"missing section '{sec}'"))
        if not text.startswith("---"):
            errors.append(WikiError(INCOMPLETE_PAGE, rel, "missing YAML frontmatter"))
    return errors


def check_malformed_refs(store: WikiStore, pages: list[str] | None = None) -> list[WikiError]:
    """Source citations violating the format specification (regex validation).

    Every entry under '## Related Sources' must be '[[sources/digests/<slug>]]'.
    """
    errors = []
    for rel in pages or store.iter_pages():
        text = store.read(rel)
        m = re.search(r"^## Related Sources\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
        if not m:
            continue
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("-") or "(none)" in line:
                continue
            lm = re.match(r"-\s*(\[\[[^\[\]]+\]\])", line)
            if not lm or not schema.SOURCE_REF_RE.match(lm.group(1)):
                errors.append(WikiError(MALFORMED_REF, rel, f"bad source ref: '{line[:60]}'"))
    return errors


def check_index_consistency(store: WikiStore) -> list[WikiError]:
    """Index <-> filesystem mismatch (bidirectional diff)."""
    errors = []
    on_disk = set(store.iter_pages())
    listed: set[str] = set()
    for cat in store.categories():
        idx = f"{cat}/_index"
        if not store.exists(idx):
            errors.append(WikiError(INDEX_INCONSISTENCY, "", f"missing directory index {idx}"))
            continue
        for link in schema.extract_links(store.read(idx)):
            listed.add(link)
    for p in sorted(on_disk - listed):
        errors.append(WikiError(INDEX_INCONSISTENCY, p, "page exists on disk but not in _index.md"))
    for p in sorted(listed - on_disk):
        errors.append(WikiError(INDEX_INCONSISTENCY, p, "page listed in _index.md but missing on disk"))
    return errors


def check_unseen_overwrite(updated_paths: set[str], selected: set[str], new_pages: set[str]) -> list[WikiError]:
    """LLM modified pages not selected by SelectPages (set comparison)."""
    errors = []
    for p in sorted(updated_paths - selected - new_pages):
        errors.append(WikiError(UNSEEN_OVERWRITE, p, "page updated but was not in SelectPages output"))
    return errors


def check_update(update: dict, store: WikiStore) -> list[WikiError]:
    """Structural checks on a proposed (not yet applied) update U.

    Validates every link inside U against the known universe: pages already
    on disk plus pages created by this same update. Source refs are checked
    for format only — their digests are created by this update's apply step,
    so existence cannot be checked yet.
    """
    errors: list[WikiError] = []
    new_paths = {p["path"] for p in update.get("pages", []) if p.get("is_new")}
    known = new_paths | set(store.iter_pages())
    for p in update.get("pages", []):
        for target, _note in p.get("related_pages", []):
            if target not in known:
                errors.append(WikiError(DANGLING_LINK, p["path"],
                                        f"link to missing page [[{target}]]"))
        for target, _note in p.get("related_sources", []):
            if not schema.SOURCE_REF_RE.match(f"[[{target}]]"):
                errors.append(WikiError(MALFORMED_REF, p["path"],
                                        f"bad source ref: [[{target}]]"))
    return errors


def structural_validate(store: WikiStore, pages: list[str] | None = None) -> list[WikiError]:
    """StructuralValidate(U, W) in Algorithm 1: all deterministic checks."""
    errors: list[WikiError] = []
    errors += check_dangling_links(store, pages)
    errors += check_incomplete_pages(store, pages)
    errors += check_malformed_refs(store, pages)
    if pages is None:  # index consistency is a global property
        errors += check_index_consistency(store)
    return errors


# ---------------------------------------------------------------- content
FACT_CHECK_PROMPT = """You are verifying a Wiki page against its cited source digest.

PAGE ({page}):
{page_text}

CITED SOURCE DIGESTS:
{digests}

For each bullet under "## Key Facts", decide whether it is SUPPORTED by the
digests. Reply with one line per unsupported fact, format:
UNSUPPORTED: <fact>
If every fact is supported, reply exactly: OK"""


def _key_facts(text: str) -> list[str]:
    """Bullets under '## Key Facts', excluding the '(none)' placeholder."""
    m = re.search(r"^## Key Facts\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    return [line.strip()[2:].strip() for line in m.group(1).splitlines()
            if line.strip().startswith("-") and "(none)" not in line]


def llm_content_validate(llm, store: WikiStore, pages: list[str]) -> list[WikiError]:
    """ContentValidate(U, W, A): source-grounded LLM verification.

    A page with facts but no existing digest is reported WITHOUT an LLM
    call: its facts are unverifiable, and silently skipping it would let a
    repair that stripped citations make an unsupported fact invisible.
    """
    errors: list[WikiError] = []
    for rel in pages:
        text = store.read(rel)
        digests = []
        for target, _note in schema.parse_section_links(text, "Related Sources"):
            if store.exists(target):
                digests.append(f"--- {target} ---\n{store.read(target)}")
        if not digests:
            if _key_facts(text):
                errors.append(WikiError(UNSUPPORTED_FACT, rel,
                                        "no cited digests; facts are unverifiable"))
            continue
        reply = llm.chat([{"role": "user", "content": FACT_CHECK_PROMPT.format(
            page=rel, page_text=text, digests="\n\n".join(digests))}])
        for line in reply.splitlines():
            if line.strip().upper().startswith("UNSUPPORTED:"):
                fact = line.split(":", 1)[1].strip()
                errors.append(WikiError(UNSUPPORTED_FACT, rel, fact))
    return errors


CONSISTENCY_PROMPT = """Two related Wiki pages describe overlapping entities. Check whether
they CONTRADICT each other on any shared attribute (dates, names, relations,
numbers).

PAGE A ({page_a}):
{text_a}

PAGE B ({page_b}):
{text_b}

Reply with one line per contradiction, format:
CONTRADICTION: <attribute>: A says X, B says Y
If the pages are consistent, reply exactly: OK"""


def linked_pairs(store: WikiStore) -> list[tuple[str, str]]:
    """All deduplicated page<->page wikilink pairs, sorted."""
    pairs: set[tuple[str, str]] = set()
    for rel in store.iter_pages():
        for link in schema.extract_links(store.read(rel)):
            if link.startswith("sources/") or not store.exists(link):
                continue
            pairs.add(tuple(sorted((rel, link))))
    return sorted(pairs)


def _check_pair(llm, store: WikiStore, a: str, b: str) -> list[WikiError]:
    """LLM contradiction check for one page pair."""
    reply = llm.chat([{"role": "user", "content": CONSISTENCY_PROMPT.format(
        page_a=a, text_a=store.read(a), page_b=b, text_b=store.read(b))}])
    errors: list[WikiError] = []
    for line in reply.splitlines():
        if line.strip().upper().startswith("CONTRADICTION:"):
            detail = line.split(":", 1)[1].strip()
            errors.append(WikiError(CROSS_PAGE_CONTRADICTION, a, f"{a} vs {b}: {detail}"))
    return errors


def llm_consistency_check(llm, store: WikiStore, max_pairs: int = 20,
                          pairs: list[tuple[str, str]] | None = None) -> list[WikiError]:
    """Cross-page contradiction check (paper Table 6, row 7).

    Two modes:
    - sweep (``pairs=None``): all linked page pairs, randomly sampled down to
      ``max_pairs`` when there are more. Sampling is deliberately NOT
      deterministic — a fixed stride would re-check the same subset forever
      on a static wiki, leaving the rest permanently unexamined.
    - targeted (``pairs`` given): exactly those pairs, no sampling. Used by
      Verify & Close so a known open contradiction entry is re-checked
      directly instead of relying on the luck of the sweep sample.
    """
    if pairs is None:
        pairs = linked_pairs(store)
        if len(pairs) > max_pairs:
            pairs = random.sample(pairs, max_pairs)
    errors: list[WikiError] = []
    for a, b in pairs:
        errors += _check_pair(llm, store, a, b)
    return errors
