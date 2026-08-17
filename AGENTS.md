# AGENTS.md — LLM-Wiki

Python implementation of the paper "Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki" (arXiv:2605.25480). Documents are *compiled* by an LLM into a bidirectionally-linked Wiki; queries are answered by an agent traversing with `wiki_search`/`wiki_read`; compile errors feed a persistent Error Book.

**Read `DOCUMENTATION.md` before touching `compile.py` or `error_book.py`** — it maps every module to the paper's Algorithm 1 / §3.3 line by line.

## Commands

```bash
# Tests (73 cases, FakeLLM-driven, no API key needed)
.venv/Scripts/python -m pytest tests/ -q        # Windows; on POSIX: .venv/bin/python
python -m pytest tests/test_compile.py -q       # single module

# Run (real LLM calls; requires LLM_WIKI_API_KEY). --wiki is a GLOBAL option:
# it goes BEFORE the subcommand, or argparse rejects it.
python -m llm_wiki --wiki ./wiki ingest notes.txt
python -m llm_wiki --wiki ./wiki query "..."
python -m llm_wiki --wiki ./wiki validate
python -m llm_wiki --wiki ./wiki fix --finalize
python -m llm_wiki --wiki ./wiki delete notes.txt   # --dry-run previews impact

# Scripted end-to-end demo, no API key
python examples/demo_paper.py
```

No lint/typecheck configured. CI (`.github/workflows/ci.yml`) runs pytest on ubuntu+windows × Python 3.10/3.13. Windows compatibility matters: use `pathlib`, no POSIX-only assumptions.

## Architecture (dependency order)

`schema.py` (page format, `[[dir/Page]]` wikilinks) → `store.py` (filesystem, backlinks, index rebuilds) → `validators.py` (5 deterministic checks + LLM content check) → `error_book.py` (Discover→Attribute→Constrain→Inject→Verify&Close) → `compile.py` (`Compiler.compile_passage` = Algorithm 1; the core) → `delete.py` (document removal = inverse of Algorithm 1; repo extension beyond the paper) → `search.py` / `agent.py` (query side) → `llm.py` / `cli.py` (shell).

## Hard rules / design decisions

- **All LLM calls go through `llm.py`'s `chat(messages) -> str` interface.** Any object with a `chat()` method can substitute — that's how `tests/conftest.py`'s `FakeLLM` works. Never import urllib/json LLM logic elsewhere.
- **Semantic judgment → LLM; mechanical validity → code.** Link existence, format, set-inclusion checks are deterministic code; fact-checking, attribution, content repair are LLM. Keep this split.
- **`_index.md` and `index.md` are derived products** — always rebuild via `rebuild_directory_index`/`rebuild_global_index`, never edit by hand.
- **Backlinks are system-guaranteed**: LLM only declares A→B; `store.add_backlink` adds B→A. Don't ask the LLM for reverse links.
- **Never trust the LLM's `is_new` flag** — derive it from filesystem state (see commit 6ecf76c).
- **Error Book constraints are prompt text** injected via `{constraints_block}` in `COMPILE_PAGES_PROMPT` — adding constraints must not require architecture changes.
- **Agent tool calls: native function calling first, JSON-action fallback.** `agent.py` uses `llm.chat_tools()` (native `tools`) when the client exposes it, and falls back to prompt-driven JSON actions either statically (no `chat_tools` method — e.g. plain `FakeLLM`) or at runtime (`ToolsUnsupported` when an endpoint rejects `tools`). Both paths share `_execute()` and enforce: t_max=15, patience=3, no `answer` before at least one `wiki_read`. The `chat(messages) -> str` contract is unchanged; `chat_tools` is purely additive (only the Agent uses it).
- **Deletion must restore every invariant**: `delete.py` matches the document footprint (full id match — `notes` never matches `notes-2-001`), strips dead citations from surviving pages (a page with *no* citations is never treated as sole-source), deletes sole-source pages outright, LLM re-verifies survivors' facts and `verify_and_close` shuts any now-stale open entries on them, prunes dangling links wiki-wide, rebuilds only affected-category indices, and ends with `structural_validate == []` (CLI exits non-zero otherwise). Individual writes are idempotent, but `delete` is NOT re-runnable once digests are gone (it then aborts on no-match); `code_fix_wiki` (`fix`) is the crash-recovery path.

## Conventions

- Python ≥3.10; **stdlib + pyyaml only** (HTTP via `urllib`, no requests/httpx). Keep dependencies at zero beyond pyyaml.
- Code docstrings/comments in English; README/DOCUMENTATION.md in Chinese.
- `from __future__ import annotations` at the top of every module.
- `wiki/` and `error_book.yaml` are runtime artifacts, gitignored — never commit them.
- CJK queries: `search.py` uses bigram tokenization; structured signals (name 8 > alias 6 > tag 4 > summary 2 > body 1) always outrank body-text matches.

## Config

Env vars (OpenAI-compatible endpoints): `LLM_WIKI_BASE_URL` (default Moonshot), `LLM_WIKI_API_KEY` (required for real calls only), `LLM_WIKI_MODEL` (default `kimi-k2-0711-preview`).
