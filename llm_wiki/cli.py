"""Command-line interface: ingest / query / validate / fix / delete / errorbook.

Examples (--wiki is a global option and comes BEFORE the subcommand):
    python -m llm_wiki --wiki ./wiki ingest notes.txt
    python -m llm_wiki --wiki ./wiki query "Which film has the older director?"
    python -m llm_wiki --wiki ./wiki validate
    python -m llm_wiki --wiki ./wiki fix --finalize
    python -m llm_wiki --wiki ./wiki delete notes.txt    # --dry-run previews impact
    python -m llm_wiki --wiki ./wiki errorbook
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import agent, validators
from .compile import Compiler
from .delete import delete_document
from .error_book import ErrorBook
from .llm import LLMClient
from .schema import slugify
from .store import WikiStore


def _split_passages(text: str) -> list[str]:
    """Split a source file into passages (blank-line separated paragraphs,
    merged to a reasonable size)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras


def _components(args):
    store = WikiStore(args.wiki)
    book = ErrorBook(Path(args.wiki).parent / "error_book.yaml")
    llm = LLMClient()
    return store, book, llm


def cmd_ingest(args) -> int:
    store, book, llm = _components(args)
    compiler = Compiler(store, llm, book)
    text = Path(args.file).read_text(encoding="utf-8")
    passages = _split_passages(text)
    stem = slugify(Path(args.file).stem) or "source"
    batch = [(f"{stem}-{i:03d}", p) for i, p in enumerate(passages, 1)]
    print(f"ingesting {len(batch)} passages from {args.file} ...")
    written = compiler.compile_batch(batch)
    compiler.finalize()
    print(f"done: {len(written)} page updates written; wiki has "
          f"{len(store.iter_pages())} pages, {len(book.open_entries())} open error entries")
    if compiler.skipped:
        print(f"WARNING: {len(compiler.skipped)} passage(s) skipped due to failures:")
        for sid, reason in compiler.skipped:
            print(f"  - {sid}: {reason}")
    return 0


def cmd_query(args) -> int:
    store, _book, llm = _components(args)
    result = agent.run_agent(store, llm, args.question)
    for step in result["trace"]:
        print(f"  trace: {step}")
    print(f"\nANSWER: {result['answer']}")
    if result["evidence"]:
        print(f"EVIDENCE: {', '.join(result['evidence'])}")
    return 0


def cmd_validate(args) -> int:
    store, _book, _llm = _components(args)
    errors = validators.structural_validate(store)
    if not errors:
        print("OK: no structural errors")
        return 0
    for e in errors:
        print(e)
    print(f"{len(errors)} structural error(s)")
    return 1


def cmd_fix(args) -> int:
    store, book, llm = _components(args)
    compiler = Compiler(store, llm, book)
    fixes = compiler.code_fix_wiki()
    print(f"code fixes: {fixes or 'none needed'}")
    if args.finalize:
        compiler.finalize()
        print("finalization complete (3 rounds code-fix <-> LLM-fix)")
    return 0


def cmd_delete(args) -> int:
    store, book, llm = _components(args)
    stem = args.source
    if Path(args.source).is_file():  # original file path: derive the stem
        stem = slugify(Path(args.source).stem) or "source"
    try:
        report = delete_document(store, llm, book, stem, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}")
        return 1

    print(f"document {report['stem']!r}: {len(report['digests'])} digest(s), "
          f"{len(report['articles'])} archived article(s)")
    print(f"pages deleted (sole source): {report['pages_deleted'] or 'none'}")
    print(f"pages re-verified: {report['pages_reverified'] or 'none'}")
    if report.get("repaired"):
        print(f"LLM repairs: {', '.join(report['repaired'])}")
    if report.get("pruned_links"):
        print(f"dangling links pruned: {len(report['pruned_links'])}")
    if args.dry_run:
        print("(dry run: nothing was written)")
        return 0
    errors = report["validation"]
    if errors:
        for e in errors:
            print(e)
        print(f"{len(errors)} structural error(s) remain after deletion")
        return 1
    print("OK: wiki is consistent")
    return 0


def cmd_errorbook(args) -> int:
    _store, book, _llm = _components(args)
    for e in book.entries:
        print(f"#{e['id']} [{e['status']}] {e['type']} on {e['page']} "
              f"(x{e.get('occurrences', 1)})\n    rule: {e.get('constraint_rule', '-')}")
    if not book.entries:
        print("(error book is empty)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to GBK; make Chinese output safe everywhere
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="llm_wiki", description=__doc__)
    ap.add_argument("--wiki", default="./wiki", help="wiki root directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="compile a source file into the wiki")
    p.add_argument("file")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("query", help="answer a question via traversal")
    p.add_argument("question")
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("validate", help="run structural validation")
    p.set_defaults(fn=cmd_validate)

    p = sub.add_parser("fix", help="code autofix; --finalize adds LLM repair rounds")
    p.add_argument("--finalize", action="store_true")
    p.set_defaults(fn=cmd_fix)

    p = sub.add_parser("delete",
                       help="remove an ingested document and restore consistency")
    p.add_argument("source", help="original source file path or source-id prefix")
    p.add_argument("--dry-run", action="store_true",
                   help="show the impact (pages deleted / re-verified), write nothing")
    p.set_defaults(fn=cmd_delete)

    p = sub.add_parser("errorbook", help="show error book entries")
    p.set_defaults(fn=cmd_errorbook)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
