# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Python implementation of the paper "Retrieval as Reasoning: Self-Evolving Agent-Native Retrieval via LLM-Wiki" (arXiv:2605.25480). Documents are *compiled* by an LLM into a bidirectionally-linked Wiki; queries are answered by an agent that traverses the Wiki with `wiki_search`/`wiki_read`; compile errors feed a persistent Error Book that drives self-correction.

> **Read `DOCUMENTATION.md` before touching `compile.py` or `error_book.py`.** It maps every module to the paper's Algorithm 1 / §3.3 line by line. `AGENTS.md` is a condensed companion to this file.

## Commands

```bash
# Setup (only pyyaml is a runtime dep; LLM calls use stdlib urllib)
python -m venv .venv
.venv/Scripts/pip install pyyaml pytest      # Windows; POSIX: .venv/bin/pip
# or, with uv: uv venv && uv pip install pyyaml pytest

# Tests (90 cases, FakeLLM-driven, no API key needed)
.venv/Scripts/python -m pytest tests/ -q     # Windows; POSIX: .venv/bin/python
uv run --no-project python -m pytest tests/ -q          # via uv
python -m pytest tests/test_compile.py -q              # single module
python -m pytest tests/test_compile.py::test_name -q   # single test

# Run (real LLM calls; requires LLM_WIKI_API_KEY). --wiki is a GLOBAL option:
# it MUST go BEFORE the subcommand, or argparse rejects it.
python -m llm_wiki --wiki ./wiki ingest notes.txt    # compile a doc into the Wiki
python -m llm_wiki --wiki ./wiki query "..."         # agent traversal + answer
python -m llm_wiki --wiki ./wiki search "director"   # structured-signal page search
python -m llm_wiki --wiki ./wiki read entities/X concepts/Y   # batch-read pages
python -m llm_wiki --wiki ./wiki stats               # page/digest/error-book counts
python -m llm_wiki --wiki ./wiki validate            # 4 checks here (5th, unseen-overwrite, is compile-time)
python -m llm_wiki --wiki ./wiki fix --finalize      # code autofix + 3 rounds code<->LLM repair
python -m llm_wiki --wiki ./wiki delete notes.txt    # remove a doc; --dry-run previews impact
python -m llm_wiki --wiki ./wiki errorbook           # dump the Error Book

# Every read/repair command (ingest/search/read/stats/validate/fix/errorbook) accepts --json.
# The DSH harness plugin (dsh-llm-wiki) drives the wiki through these JSON lines.
# `delete` is deliberately human-only: destructive KB management, NOT exposed to the agent (no --json).
python -m llm_wiki --wiki ./wiki search "director" --json

# Scripted end-to-end demo (compiles the paper itself, runs multi-hop queries), no API key
python examples/demo_paper.py
```

No lint/typecheck configured. CI (`.github/workflows/ci.yml`) runs pytest on ubuntu+windows × Python 3.10/3.13, so **Windows compatibility matters**: use `pathlib`, avoid POSIX-only assumptions.

## Architecture (dependency order)

`schema.py` (page format, `[[dir/Page]]` wikilinks) → `store.py` (filesystem, backlinks, index rebuilds) → `validators.py` (5 deterministic checks + LLM content check) → `error_book.py` (Discover→Attribute→Constrain→Inject→Verify&Close) → `compile.py` (`Compiler.compile_passage` = Algorithm 1; the core) → `delete.py` (document removal = inverse of Algorithm 1; repo extension beyond the paper) → `search.py` / `agent.py` (query side) → `llm.py` / `cli.py` (shell).

## Hard rules / design decisions

- **All LLM calls go through `llm.py`'s `chat(messages) -> str` interface.** Any object with a `chat()` method can substitute — that's how `tests/conftest.py`'s `FakeLLM` works. Never put urllib/json LLM logic anywhere else.
- **Semantic judgment → LLM; mechanical validity → code.** Link existence, format, and set-inclusion checks are deterministic code; fact-checking, attribution, and content repair are LLM. Preserve this split.
- **`_index.md` and `index.md` are derived products** — always rebuild via `rebuild_directory_index`/`rebuild_global_index` (or the incremental `rebuild_indices_for`), never edit by hand.
- **`store.page_meta()` memoizes (frontmatter, summary) by mtime** to keep `search` off a full read+parse of every page. `write`/`delete` invalidate the entry (covers same-second overwrites where mtime may not advance). Any new write path MUST go through `store.write`/`store.delete`, never raw file writes, or the cache goes stale.
- **Backlinks are system-guaranteed**: the LLM only declares A→B; `store.add_backlink` adds B→A. Don't ask the LLM for reverse links.
- **Never trust the LLM's `is_new` flag** — derive it from filesystem state.
- **Error Book constraints are prompt text** injected via `{constraints_block}` in `COMPILE_PAGES_PROMPT`; adding a constraint must never require architecture changes.
- **Agent tool calls: native function calling first, JSON-action fallback.** `agent.py` calls `llm.chat_tools()` (native `tools`) when the client exposes it; falls back to prompt-driven JSON actions (`_parse_action`) either statically (no `chat_tools` method) or at runtime (`ToolsUnsupported` when an endpoint rejects `tools`). Both paths share `_execute()` and enforce `T_max=15`, patience `P=3`, and no `answer` before at least one `wiki_read`. The `chat(messages) -> str` contract is unchanged; `chat_tools` is additive and Agent-only.
- **Deletion must restore every invariant**: `delete.py` matches the document footprint by full id (`notes` never matches `notes-2-001`), strips dead citations from surviving pages (a page with *no* citations is never treated as sole-source), deletes sole-source pages outright, has the LLM re-verify survivors' facts and `verify_and_close` shut any now-stale open entries on them, prunes dangling links wiki-wide, rebuilds only affected-category indices, and ends with `structural_validate == []` (CLI exits non-zero otherwise). Individual writes are idempotent, but `delete` is NOT re-runnable once digests are gone (it then aborts on no-match); `code_fix_wiki` (`fix`) is the crash-recovery path.

## Conventions

- Python ≥3.10; **stdlib + pyyaml only** (HTTP via `urllib`, no requests/httpx). Keep dependencies at zero beyond pyyaml.
- `from __future__ import annotations` at the top of every module.
- Code docstrings/comments in English; README/DOCUMENTATION.md are in Chinese.
- `wiki/` and `error_book.yaml` are runtime artifacts, gitignored — never commit them.
- CJK queries: `search.py` uses bigram tokenization; structured signals (name 8 > alias 6 > tag 4 > summary 2 > body 1) always outrank body-text matches.

## Key hyperparameters (paper §4.4)

`T_max=15` (tool-call budget), `P=3` (empty-search patience), `k=5` (SelectPages cap), LLM periodic fix every 10 articles — all defaults, adjustable in `agent.py` / `compile.py`.

## Config

Env vars (OpenAI-compatible endpoints): `LLM_WIKI_BASE_URL` (default Moonshot), `LLM_WIKI_API_KEY` (required for real calls only), `LLM_WIKI_MODEL` (default `kimi-k2-0711-preview`).
