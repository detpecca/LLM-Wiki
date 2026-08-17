"""CLI JSON surface used by the DSH harness plugin.

The plugin (dsh-llm-wiki) drives the wiki through ``python -m llm_wiki … --json``
subprocess calls, so these tests lock the JSON contract for the read/repair
commands and assert that ``delete`` stays human-only (no ``--json``).

Read-only commands (search/read/stats/validate/errorbook) never call the LLM,
so ``cli.main`` can run against a real store with no API key.
"""

from __future__ import annotations

import json

import pytest

from llm_wiki import cli
from llm_wiki.schema import Page, render_page
from llm_wiki.store import WikiStore


def _page(path, sources=(), pages=(), facts=("fact one", "fact two")):
    return render_page(Page(
        path=path, title=path.split("/")[-1], page_type=path.split("/")[0],
        summary=f"summary of {path}", key_facts=list(facts),
        related_pages=list(pages), related_sources=list(sources)), "2026-01-01")


def _wiki(tmp_path):
    """A small consistent wiki: one digest + one page citing it."""
    store = WikiStore(tmp_path / "wiki")
    store.write("sources/digests/doc1-001", "# Digest: doc1-001\n\ndoc1 content")
    store.write("sources/articles/doc1-001", "raw passage one")
    store.write("entities/Foo", _page("entities/Foo", facts=("Foo is a thing",),
        sources=[("sources/digests/doc1-001", "supports")]))
    store.rebuild_all_indices()
    return store


def _run(capsys, *argv):
    """Invoke cli.main; return (exit_code, parsed_json_stdout)."""
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def test_stats_json(tmp_path, capsys):
    _wiki(tmp_path)
    code, data = _run(capsys, "--wiki", str(tmp_path / "wiki"), "stats", "--json")
    assert code == 0
    assert data["pages"] == 1
    assert data["categories"] == {"entities": 1}
    assert data["digests"] == 1
    assert data["errorBookEntries"] == 0


def test_search_json(tmp_path, capsys):
    _wiki(tmp_path)
    code, hits = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                      "search", "Foo", "--json")
    assert code == 0
    assert isinstance(hits, list)
    assert hits and hits[0]["path"] == "entities/Foo"
    # contract fields the plugin's output schema declares:
    for key in ("path", "score", "aliases", "tags", "summary"):
        assert key in hits[0]


def test_search_json_limit(tmp_path, capsys):
    _wiki(tmp_path)
    code, hits = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                      "search", "Foo", "--limit", "1", "--json")
    assert code == 0
    assert len(hits) <= 1


def test_read_json(tmp_path, capsys):
    _wiki(tmp_path)
    code, pages = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                       "read", "entities/Foo", "--json")
    assert code == 0
    assert "entities/Foo" in pages
    assert "Foo is a thing" in pages["entities/Foo"]


def test_read_json_missing_page(tmp_path, capsys):
    _wiki(tmp_path)
    code, pages = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                       "read", "entities/Nope", "--json")
    assert code == 0
    assert "entities/Nope" in pages           # present with a not-found note


def test_validate_json_ok(tmp_path, capsys):
    _wiki(tmp_path)
    code, data = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                      "validate", "--json")
    assert code == 0
    assert data["ok"] is True
    assert data["errors"] == []


def test_validate_json_reports_errors_and_exit_1(tmp_path, capsys):
    store = _wiki(tmp_path)
    # Introduce a dangling link to force a structural error.
    store.write("entities/Bar", _page("entities/Bar",
        sources=[("sources/digests/doc1-001", "supports")],
        pages=[("entities/Ghost", "missing")]))
    store.rebuild_all_indices()
    code, data = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                      "validate", "--json")
    assert code == 1
    assert data["ok"] is False
    assert any(e["type"] for e in data["errors"])


def test_errorbook_json_empty(tmp_path, capsys):
    _wiki(tmp_path)
    code, data = _run(capsys, "--wiki", str(tmp_path / "wiki"),
                      "errorbook", "--json")
    assert code == 0
    assert data == {"entries": []}


def test_delete_has_no_json_flag(tmp_path):
    """Permission boundary: delete is human-only, never machine-driven."""
    with pytest.raises(SystemExit):
        cli.main(["--wiki", str(tmp_path / "wiki"), "delete", "doc1", "--json"])
