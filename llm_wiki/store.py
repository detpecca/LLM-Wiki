"""Wiki store: file-tree layout, page IO, indices, bidirectional links.

Layout (paper Appendix E):

    wiki/
      index.md               global index: overview + directory catalog
      <category>/_index.md   directory index: one line per page
      <category>/<Page>.md   knowledge pages
      sources/digests/*.md   paragraph-level digests
      sources/articles/*.md  full source archives
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

from . import schema

CATEGORIES = ("concepts", "entities", "events", "systems", "benchmarks", "topics")


class WikiStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sources" / "digests").mkdir(parents=True, exist_ok=True)
        (self.root / "sources" / "articles").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def page_file(self, rel: str) -> Path:
        """Map a logical wiki path to a filesystem path (traversal-safe)."""
        p = Path(rel)
        if (p.is_absolute() or ".." in p.parts or "\\" in rel
                or rel.startswith("/") or ":" in rel):
            raise ValueError(f"unsafe wiki path: {rel!r}")
        return self.root / (rel + ".md")

    def exists(self, rel: str) -> bool:
        return self.page_file(rel).is_file()

    def today(self) -> str:
        return datetime.date.today().isoformat()

    # ------------------------------------------------------------------ reads
    def read(self, rel: str) -> str:
        return self.page_file(rel).read_text(encoding="utf-8")

    def read_many(self, rels: list[str]) -> dict[str, str]:
        """Batch read (wiki_read primitive); bad/missing paths get an error note."""
        out = {}
        for rel in rels:
            rel = rel.removesuffix(".md")
            try:
                out[rel] = self.read(rel) if self.exists(rel) else "(page not found)"
            except (ValueError, OSError):
                out[rel] = "(invalid or unreadable path)"
        return out

    def iter_pages(self) -> list[str]:
        """All knowledge page paths (excluding indices and sources)."""
        pages = []
        for f in sorted(self.root.rglob("*.md")):
            rel = f.relative_to(self.root).with_suffix("").as_posix()
            if rel == "index" or rel.endswith("/_index") or rel.startswith("sources/"):
                continue
            pages.append(rel)
        return pages

    def categories(self) -> list[str]:
        cats = {p.split("/")[0] for p in self.iter_pages()}
        return sorted(cats)

    def iter_digests(self) -> list[str]:
        d = self.root / "sources" / "digests"
        return sorted(f"sources/digests/{f.stem}" for f in d.glob("*.md"))

    def iter_articles(self) -> list[str]:
        d = self.root / "sources" / "articles"
        return sorted(f"sources/articles/{f.stem}" for f in d.glob("*.md"))

    # ----------------------------------------------------------------- writes
    def write(self, rel: str, text: str) -> None:
        f = self.page_file(rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def delete(self, rel: str) -> None:
        self.page_file(rel).unlink(missing_ok=True)

    # -------------------------------------------------------------- backlinks
    def add_backlink(self, target: str, source: str, note: str = "") -> bool:
        """Ensure ``target`` page links back to ``source`` (bidirectional).

        Returns True if a backlink was added.
        """
        if not self.exists(target):
            return False
        text = self.read(target)
        if f"[[{source}]]" in text:
            return False
        entry = f"- [[{source}]]" + (f" -- {note}" if note else "")
        if "## Related Pages" not in text:
            text = text.rstrip() + "\n\n## Related Pages\n\n" + entry + "\n"
        else:
            text = re.sub(
                r"(## Related Pages\s*\n)",
                lambda m: m.group(1) + "\n" + entry + "\n",
                text,
                count=1,
            )
        # drop placeholder "- (none)" if real entries now exist
        text = re.sub(r"(## Related Pages\s*\n(?:\n- \[\[.*\n)+)\n?- \(none\)\n", r"\1", text)
        self.write(target, text)
        return True

    def sync_bidirectional_links(self) -> list[str]:
        """Scan all pages; add missing reverse links. Returns list of fixes."""
        fixes = []
        for rel in self.iter_pages():
            for link in schema.extract_links(self.read(rel)):
                if link.startswith("sources/"):
                    continue
                if self.exists(link) and self.add_backlink(link, rel):
                    fixes.append(f"{link} <- {rel}")
        return fixes

    def prune_dangling_links(self) -> list[str]:
        """Scan all pages; drop bullets linking to non-existent targets
        (pages or digests). The inverse of sync_bidirectional_links."""
        fixes = []
        for rel in self.iter_pages():
            text = self.read(rel)
            new, removed = schema.rewrite_section(text, "Related Pages", self.exists)
            new, removed_src = schema.rewrite_section(new, "Related Sources", self.exists)
            if removed or removed_src:
                self.write(rel, new)
                fixes += [f"{rel}: pruned [[{t}]]" for t, _ in removed + removed_src]
        return fixes

    # ---------------------------------------------------------------- indices
    def rebuild_directory_index(self, category: str) -> str:
        """Regenerate <category>/_index.md from the pages on disk."""
        pages = [p for p in self.iter_pages() if p.startswith(category + "/")]
        lines = [f"# {category}", f"> {len(pages)} pages", ""]
        for rel in pages:
            text = self.read(rel)
            fm = schema.parse_frontmatter(text)
            name = rel.split("/")[-1]
            aliases = fm.get("aliases") or []
            tags = fm.get("tags") or []
            summary = ""
            m = re.search(r"^>\s*(.+)$", text, re.M)
            if m:
                summary = m.group(1).strip()
            entry = f"- [[{rel}]]"
            if aliases:
                entry += f" ({', '.join(aliases)})"
            if summary:
                entry += f" -- {summary}"
            if tags:
                entry += " " + " ".join(f"#{t.replace(' ', '-')}" for t in tags)
            lines.append(entry)
        content = "\n".join(lines) + "\n"
        self.write(f"{category}/_index", content)
        return content

    def rebuild_global_index(self) -> str:
        """Regenerate the root index.md directory catalog (single FS scan)."""
        lines = ["# Wiki Directory Overview", "", "## Directory Catalog", ""]
        desc = {
            "concepts": "theories, methods, abstract ideas",
            "entities": "people, places, organizations, works",
            "events": "events, periods, milestones",
            "systems": "systems, models, software, methods",
            "benchmarks": "datasets and evaluation benchmarks",
            "topics": "thematic overviews",
            "sources": "paragraph digests and original archives",
        }
        counts: dict[str, int] = {}
        for p in self.iter_pages():
            cat = p.split("/")[0]
            counts[cat] = counts.get(cat, 0) + 1
        for cat in sorted(counts):
            lines.append(f"- {cat}/ ({counts[cat]} pages) -- {desc.get(cat, 'knowledge pages')}")
        n_src = len(self.iter_digests())
        lines.append(f"- sources/ ({n_src} digests) -- {desc['sources']}")
        content = "\n".join(lines) + "\n"
        self.write("index", content)
        return content

    def rebuild_all_indices(self) -> None:
        for cat in self.categories():
            self.rebuild_directory_index(cat)
        self.rebuild_global_index()

    def rebuild_indices_for(self, page_paths: list[str]) -> None:
        """Incrementally rebuild only the indices affected by the given pages."""
        for cat in sorted({p.split("/")[0] for p in page_paths if "/" in p}):
            self.rebuild_directory_index(cat)
        self.rebuild_global_index()

    # ------------------------------------------------------------ index reads
    def directory_index_listing(self) -> str:
        """Concatenated directory indices — what SelectPages sees (I in Alg.1)."""
        parts = []
        for cat in self.categories():
            idx = f"{cat}/_index"
            if self.exists(idx):
                parts.append(self.read(idx))
        return "\n\n".join(parts) or "(wiki is empty)"
