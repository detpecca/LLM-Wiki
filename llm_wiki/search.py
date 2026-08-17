"""wiki_search implementation (paper §3.2 Tool Interface).

Prioritizes structured signals — page names, aliases, tags, summaries —
before falling back to page content. Pure Python scoring; no embeddings.
"""

from __future__ import annotations

import re

from .store import WikiStore

WEIGHTS = {"name": 8, "alias": 6, "tag": 4, "summary": 2, "content": 1}


def _tokens(query: str) -> list[str]:
    """Tokenize a query: alphanumeric words plus CJK bigrams.

    Chinese text has no whitespace separators, so CJK runs are broken into
    overlapping bigrams (e.g. "导演年龄" -> ["导演", "演年", "年龄"]);
    1-2 char runs are kept whole.
    """
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9]+", query) if len(t) > 1]
    for run in re.findall("[一-鿿]+", query):
        if len(run) <= 2:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def search(store: WikiStore, query: str, limit: int = 10) -> list[dict]:
    """Return candidate pages with metadata, best first."""
    tokens = _tokens(query)
    if not tokens:
        return []
    scored = []
    for rel in store.iter_pages():
        meta = store.page_meta(rel)          # cached (frontmatter, summary)
        if meta is None:
            continue
        fm, summary_raw = meta
        name = rel.split("/")[-1].replace("-", " ").lower()
        aliases = [a.lower() for a in fm.get("aliases", [])]
        tags = [t.lower() for t in fm.get("tags", [])]
        summary = summary_raw.lower()

        # Content fallback needs the body, but only for tokens that miss the
        # summary. Read (and lowercase) it at most once, and only on demand.
        content = None

        score, matched = 0, set()
        for tok in tokens:
            if tok in name:
                score += WEIGHTS["name"]; matched.add(tok)
            if any(tok in a for a in aliases):
                score += WEIGHTS["alias"]; matched.add(tok)
            if any(tok in t for t in tags):
                score += WEIGHTS["tag"]; matched.add(tok)
            if tok in summary:
                score += WEIGHTS["summary"]; matched.add(tok)
            else:  # content fallback only if no summary hit
                if content is None:
                    content = store.read(rel).lower()
                if tok in content:
                    score += WEIGHTS["content"]; matched.add(tok)
        if score:
            scored.append((score, len(matched), rel, fm, summary_raw))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [
        {
            "path": rel,
            "score": score,
            "aliases": fm.get("aliases", []),
            "tags": fm.get("tags", []),
            "summary": summary_raw,
        }
        for score, _n, rel, fm, summary_raw in scored[:limit]
    ]
