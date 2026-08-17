"""Document deletion: the inverse of Algorithm 1 (repository extension).

Removes a source document's whole footprint — paragraph digests, archived
articles, and the wiki updates derived from them — then restores every
consistency invariant:

  * structural_validate over the whole wiki reports no errors;
  * every surviving page still cites at least one existing digest, and its
    Key Facts are re-verified against the remaining digests (LLM);
  * pages whose only citations pointed at the deleted document are deleted
    outright, and links to them are pruned from other pages (cascade);
  * indices are rebuilt from disk.

``report["validation"]`` holds whatever structural errors remain at the end;
the CLI turns a non-empty list into a non-zero exit code.

Crash recovery: run ``python -m llm_wiki fix``. Every write here is
individually idempotent (citation strips, ``unlink(missing_ok=True)``, index
rebuilds), but ``delete`` is *not* safely re-runnable once the digests are
gone: ``document_footprint`` would then match nothing and abort. ``fix``
(``code_fix_wiki``) finishes any half-done link/index cleanup instead.
"""

from __future__ import annotations

import re

from . import schema, validators
from .compile import Compiler
from .error_book import ErrorBook
from .llm import LLMClient
from .store import WikiStore


def document_footprint(store: WikiStore, stem: str) -> tuple[list[str], list[str]]:
    """(digests, articles) whose source id equals ``stem`` or ``stem-<digits>``.

    Full-match on the id, not a string prefix, so deleting "notes" can never
    touch "notes-2-001".
    """
    pat = re.compile(rf"^{re.escape(stem)}-\d+$")

    def match(path: str) -> bool:
        sid = path.rsplit("/", 1)[-1]
        return sid == stem or bool(pat.fullmatch(sid))

    digests = [d for d in store.iter_digests() if match(d)]
    articles = [a for a in store.iter_articles() if match(a)]
    return digests, articles


def _citations(store: WikiStore, rel: str) -> set[str]:
    return {t for t, _ in schema.parse_section_links(store.read(rel), "Related Sources")}


def _citation_map(store: WikiStore) -> dict[str, set[str]]:
    """One pass over every page -> its Related Sources citation set."""
    return {rel: _citations(store, rel) for rel in store.iter_pages()}


def affected_pages(cites: dict[str, set[str]], digests: list[str]) -> list[str]:
    """Knowledge pages citing any of the given digests (reverse provenance)."""
    dead = set(digests)
    return [rel for rel, c in cites.items() if c & dead]


def _sole_citation_pages(cites: dict[str, set[str]], pages: list[str],
                         digests: list[str]) -> list[str]:
    """Pages whose citations will all be gone once the digests are deleted.

    A page with no citations at all is never treated as sole-source (its empty
    set is a subset of everything) — only pages that *do* cite, and cite only
    dead digests, are removed.
    """
    dead = set(digests)
    return [rel for rel in pages if cites[rel] and cites[rel] <= dead]


def delete_document(store: WikiStore, llm, book: ErrorBook, stem: str,
                    dry_run: bool = False) -> dict:
    """Delete one ingested document and restore wiki consistency."""
    digests, articles = document_footprint(store, stem)
    if not digests and not articles:
        raise FileNotFoundError(
            f"no ingested document matches source-id prefix {stem!r} in {store.root}")

    cites = _citation_map(store)  # one pass; reused by affected/sole-source below
    affected = affected_pages(cites, digests)
    dead_pages = _sole_citation_pages(cites, affected, digests)
    survivors = sorted(set(affected) - set(dead_pages))

    report = {
        "stem": stem,
        "digests": digests,
        "articles": articles,
        "pages_deleted": dead_pages,
        "pages_reverified": survivors,
        "repaired": [],
        "pruned_links": [],
        "closed_entries": [],
        "validation": [],
    }
    if dry_run:
        return report

    if survivors and isinstance(llm, LLMClient) and not llm.api_key:
        raise RuntimeError(
            "LLM_WIKI_API_KEY is not set: facts of surviving pages cannot be "
            "re-verified; aborting before any change is written")

    dead = set(digests)
    for rel in survivors:  # strip citations of the deleted document
        text = store.read(rel)
        new, _removed = schema.rewrite_section(
            text, "Related Sources", lambda t: t not in dead)
        if new != text:
            store.write(rel, new)

    for rel in dead_pages:  # sole-source pages go entirely
        store.delete(rel)
    report["closed_entries"] = book.close_for_pages(dead_pages)

    for rel in digests + articles:
        store.delete(rel)

    compiler = Compiler(store, llm, book)
    if survivors:  # re-verify facts against the remaining digests (LLM)
        report["repaired"] = compiler.llm_periodic_fix(pages=survivors)
        # close any now-stale open entries on survivors (e.g. an UNSUPPORTED_FACT
        # that pointed at a digest we just removed and the repair resolved)
        report["closed_entries"] += compiler.verify_and_close()

    report["pruned_links"] = store.prune_dangling_links()  # safety net, wiki-wide
    store.rebuild_indices_for(survivors + dead_pages)
    report["validation"] = validators.structural_validate(store)
    return report
