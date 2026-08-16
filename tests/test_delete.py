"""Document deletion: footprint, cascade cleanup, consistency restore."""

import pytest

from llm_wiki import delete, validators
from llm_wiki.error_book import ErrorBook
from llm_wiki.schema import Page, render_page
from llm_wiki.store import WikiStore

from conftest import FakeLLM


def _page(path, sources=(), pages=(), facts=("fact one", "fact two")):
    return render_page(Page(
        path=path, title=path.split("/")[-1], page_type=path.split("/")[0],
        summary=f"summary of {path}", key_facts=list(facts),
        related_pages=list(pages), related_sources=list(sources)), "2026-01-01")


def _setup(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    book = ErrorBook(tmp_path / "error_book.yaml")
    return store, book


def _snapshot(store):
    return {f.relative_to(store.root).as_posix(): f.read_text(encoding="utf-8")
            for f in store.root.rglob("*.md")}


def _mixed_wiki(store):
    """A page citing two documents, plus both digests/articles on disk."""
    store.write("sources/digests/doc1-001", "# Digest: doc1-001\n\none")
    store.write("sources/articles/doc1-001", "passage one")
    store.write("sources/digests/doc2-001", "# Digest: doc2-001\n\ntwo")
    store.write("concepts/Mix", _page("concepts/Mix", sources=[
        ("sources/digests/doc1-001", "a"), ("sources/digests/doc2-001", "b")]))
    store.rebuild_all_indices()


def test_delete_sole_source_page_cascades(tmp_path):
    store, book = _setup(tmp_path)
    store.write("sources/digests/doc1-001", "# Digest: doc1-001\n\ndoc1 content")
    store.write("sources/articles/doc1-001", "raw passage one")
    store.write("entities/Foo", _page("entities/Foo",
        sources=[("sources/digests/doc1-001", "supports")]))
    store.write("sources/digests/other-001", "# Digest: other-001\n\nother content")
    store.write("entities/Bar", _page("entities/Bar",
        sources=[("sources/digests/other-001", "supports")],
        pages=[("entities/Foo", "friend")]))
    store.rebuild_all_indices()

    report = delete.delete_document(store, FakeLLM(), book, "doc1")

    assert report["pages_deleted"] == ["entities/Foo"]
    assert report["pages_reverified"] == []
    assert not store.exists("entities/Foo")
    assert not store.exists("sources/digests/doc1-001")
    assert not store.exists("sources/articles/doc1-001")
    assert store.exists("entities/Bar")                       # independent page survives
    assert "entities/Foo" not in store.read("entities/Bar")   # cascade link cleanup
    assert "[[sources/digests/other-001]]" in store.read("entities/Bar")
    assert report["validation"] == []


def test_survivor_keeps_remaining_citation(tmp_path):
    store, book = _setup(tmp_path)
    _mixed_wiki(store)

    report = delete.delete_document(store, FakeLLM(), book, "doc1")  # "OK": facts supported

    assert report["pages_deleted"] == []
    assert report["pages_reverified"] == ["concepts/Mix"]
    assert report["repaired"] == []
    text = store.read("concepts/Mix")
    assert "doc1-001" not in text
    assert "[[sources/digests/doc2-001]]" in text
    assert "- fact one" in text                  # facts untouched
    assert report["validation"] == []


def test_unsupported_fact_pruned_by_llm(tmp_path):
    store, book = _setup(tmp_path)
    _mixed_wiki(store)
    repaired = _page("concepts/Mix",
                     sources=[("sources/digests/doc2-001", "b")], facts=["fact two"])
    llm = FakeLLM(["UNSUPPORTED: fact one", repaired])

    report = delete.delete_document(store, llm, book, "doc1")

    text = store.read("concepts/Mix")
    assert "fact one" not in text
    assert "- fact two" in text
    assert "[[sources/digests/doc2-001]]" in text
    assert report["repaired"] == ["concepts/Mix"]
    assert report["validation"] == []


def test_repair_that_strips_all_citations_rejected(tmp_path):
    store, book = _setup(tmp_path)
    _mixed_wiki(store)
    llm = FakeLLM(["UNSUPPORTED: fact one", _page("concepts/Mix", sources=[], facts=[])])

    report = delete.delete_document(store, llm, book, "doc1")

    text = store.read("concepts/Mix")
    assert report["repaired"] == []             # rejected: page keeps stripped version
    assert "[[sources/digests/doc2-001]]" in text
    assert "- fact one" in text                 # original facts stay (conservative)


def test_prefix_collision_full_match_only(tmp_path):
    store, book = _setup(tmp_path)
    for sid in ("notes-001", "notes-2-001"):
        store.write(f"sources/digests/{sid}", f"# {sid}\n")
        store.write(f"sources/articles/{sid}", "p")
    store.write("topics/Notes", _page("topics/Notes",
        sources=[("sources/digests/notes-001", "s")]))
    store.rebuild_all_indices()

    delete.delete_document(store, FakeLLM(), book, "notes")

    assert not store.exists("sources/digests/notes-001")
    assert store.exists("sources/digests/notes-2-001")   # different document
    assert store.exists("sources/articles/notes-2-001")


def test_dry_run_writes_nothing(tmp_path):
    store, book = _setup(tmp_path)
    store.write("sources/digests/doc1-001", "# d\n")
    store.write("sources/articles/doc1-001", "p")
    store.write("entities/Foo", _page("entities/Foo",
        sources=[("sources/digests/doc1-001", "s")]))
    store.rebuild_all_indices()
    before = _snapshot(store)

    report = delete.delete_document(store, FakeLLM(), book, "doc1", dry_run=True)

    assert report["pages_deleted"] == ["entities/Foo"]    # predicted impact
    assert _snapshot(store) == before


def test_unknown_document_raises(tmp_path):
    store, book = _setup(tmp_path)
    with pytest.raises(FileNotFoundError):
        delete.delete_document(store, FakeLLM(), book, "ghost")


def test_error_book_entries_for_deleted_pages_closed(tmp_path):
    store, book = _setup(tmp_path)
    store.write("sources/digests/doc1-001", "# d\n")
    store.write("entities/Foo", _page("entities/Foo",
        sources=[("sources/digests/doc1-001", "s")]))
    store.rebuild_all_indices()
    book.discover([validators.WikiError(validators.DANGLING_LINK, "entities/Foo", "boom")],
                  "2026-01-01")
    assert book.open_entries()

    report = delete.delete_document(store, FakeLLM(), book, "doc1")

    assert report["closed_entries"]
    assert book.open_entries() == []


def test_second_run_is_a_clean_error(tmp_path):
    store, book = _setup(tmp_path)
    store.write("sources/digests/doc1-001", "# d\n")
    store.write("entities/Foo", _page("entities/Foo",
        sources=[("sources/digests/doc1-001", "s")]))
    store.rebuild_all_indices()
    delete.delete_document(store, FakeLLM(), book, "doc1")
    after = _snapshot(store)

    with pytest.raises(FileNotFoundError):
        delete.delete_document(store, FakeLLM(), book, "doc1")
    assert _snapshot(store) == after


def test_aborts_without_api_key_when_survivors_exist(tmp_path, monkeypatch):
    from llm_wiki.llm import LLMClient
    monkeypatch.delenv("LLM_WIKI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_WIKI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_WIKI_MODEL", raising=False)
    store, book = _setup(tmp_path)
    _mixed_wiki(store)
    before = _snapshot(store)

    with pytest.raises(RuntimeError):
        delete.delete_document(store, LLMClient(), book, "doc1")
    assert _snapshot(store) == before     # aborted before any change was written
