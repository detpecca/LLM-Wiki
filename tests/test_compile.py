"""End-to-end Algorithm-1 loop under FakeLLM."""

from llm_wiki import schema, validators
from llm_wiki.compile import Compiler
from llm_wiki.error_book import ErrorBook
from llm_wiki.store import WikiStore

from conftest import FakeLLM, compile_reply, make_page


def _page_on_disk(path):
    return schema.render_page(schema.Page(
        path=path, title=path.split("/")[-1], summary="s", key_facts=["f"],
        related_sources=[("sources/digests/d", "n")]), "2026-08-04")


def _setup(tmp_path, llm):
    store = WikiStore(tmp_path / "wiki")
    book = ErrorBook(tmp_path / "error_book.yaml")
    return store, book, Compiler(store, llm, book)


def test_compile_creates_pages_with_backlinks(tmp_path):
    llm = FakeLLM([
        "[]",  # SelectPages: wiki empty
        compile_reply([  # CompileWikiPages
            make_page("people/John-V", "John V", related_pages=[["people/Ernest-I", "father"]]),
            make_page("people/Ernest-I", "Ernest I"),
        ]),
        "OK",  # ContentValidate for page 1
        "OK",  # ContentValidate for page 2
    ])
    store, book, compiler = _setup(tmp_path, llm)
    written = compiler.compile_passage("John V was son of Ernest I ...", "s-001")

    assert set(written) == {"people/John-V", "people/Ernest-I"}
    # bidirectional: Ernest-I got a backlink to John-V
    assert "[[people/John-V]]" in store.read("people/Ernest-I")
    # indices updated
    assert "[[people/John-V]]" in store.read("people/_index")
    # digest archived
    assert store.exists("sources/digests/s-001")
    # clean wiki: no structural errors
    assert validators.structural_validate(store) == []
    assert book.open_entries() == []


def test_select_pages_over_budget_is_recorded(tmp_path):
    """LLM returning more than k valid pages: excess is dropped but logged
    to `skipped`, not silently truncated."""
    import json
    llm = FakeLLM()
    store, _book, compiler = _setup(tmp_path, llm)
    for i in range(compiler.k + 2):  # k+2 real pages on disk
        store.write(f"people/P{i}", _page_on_disk(f"people/P{i}"))
    llm.replies = [json.dumps([f"people/P{i}" for i in range(compiler.k + 2)])]

    selected = compiler.select_pages("passage")

    assert len(selected) == compiler.k               # capped
    reason = next(r for sid, r in compiler.skipped if sid == "select_pages")
    assert "people/P" in reason                       # names the dropped pages


def test_unseen_overwrite_goes_to_error_book(tmp_path):
    llm = FakeLLM([
        "[]",  # SelectPages
        compile_reply([make_page("people/A", "A"), make_page("people/B", "B")]),
        "OK", "OK",  # content validation of the 2 new pages
    ])
    store, book, compiler = _setup(tmp_path, llm)
    compiler.compile_passage("passage about A and B", "s-001")
    assert store.exists("people/B")
    original_b = store.read("people/B")
    # second passage: LLM selects only A but also tries to overwrite existing B
    llm.replies = [
        '["people/A"]',  # SelectPages selects only A
        compile_reply([
            make_page("people/A", "A", is_new=False),
            make_page("people/B", "B HACKED", is_new=False),  # unseen overwrite!
        ], digest_id="s-002"),
        "- id: 1\n  root_cause: over-eager edit\n  constraint_rule: ONLY update selected pages",
        "OK",  # content validation for the one written page (A)
    ]
    compiler.compile_passage("another passage", "s-002")

    assert store.read("people/B") == original_b  # unauthorized update dropped
    open_types = [e["type"] for e in book.open_entries()]
    assert validators.UNSEEN_OVERWRITE in open_types
    # constraint was generated and will be injected next time
    assert any("ONLY update selected pages" in c for c in book.active_constraints())


def test_missing_is_new_flag_still_creates_page(tmp_path):
    """LLM forgets is_new on a brand-new page: disk fact must win (Fable fix)."""
    import json
    page = {"path": "concepts/Brand-New", "title": "New", "aliases": [], "tags": [],
            "summary": "s", "key_facts": ["f"], "related_pages": [],
            "related_sources": [["sources/digests/s-001", "n"]]}  # no is_new!
    llm = FakeLLM(["[]", json.dumps(
        {"digest": {"id": "s-001", "summary": "x"}, "pages": [page]}), "OK"])
    store, book, compiler = _setup(tmp_path, llm)
    written = compiler.compile_passage("passage", "s-001")

    assert written == ["concepts/Brand-New"]     # page created, not discarded
    assert store.exists("concepts/Brand-New")
    assert book.open_entries() == []             # no bogus unseen_overwrite entry


def test_constraint_injected_into_next_compile(tmp_path):
    llm = FakeLLM([
        "[]",
        compile_reply([make_page("people/A", "A")]),
        "OK",
    ])
    store, book, compiler = _setup(tmp_path, llm)
    compiler.compile_passage("p1", "s-001")
    book.discover([validators.WikiError(validators.DANGLING_LINK, "people/A", "x")], store.today())
    for e in book.entries:
        e["constraint_rule"] = "NEVER create dangling links"
    book.save()

    llm.replies = ["[]", compile_reply([make_page("people/C", "C")], digest_id="s-002"), "OK"]
    compiler.compile_passage("p2", "s-002")
    compile_prompt = llm.seen[-2]  # the CompileWikiPages prompt
    assert "NEVER create dangling links" in compile_prompt


def test_dangling_link_in_update_autofixed_without_overwrite(tmp_path):
    """A dangling link in U (no unseen overwrite) must still be caught and
    dropped before hitting disk — P0 fix: autofix runs unconditionally."""
    llm = FakeLLM([
        "[]",  # SelectPages
        compile_reply([make_page(
            "people/A", "A",
            related_pages=[["people/Ghost", "phantom link"]],  # dangling!
            related_sources=[["sources/digests/s-001", "ok"], ["bad/ref", "malformed"]],
        )]),
        "- id: 1\n  root_cause: hallucinated link\n  constraint_rule: NEVER link to non-existent pages\n"
        "- id: 2\n  root_cause: bad ref\n  constraint_rule: ONLY cite sources/digests",
        "OK",  # ContentValidate
    ])
    store, book, compiler = _setup(tmp_path, llm)
    compiler.compile_passage("passage", "s-001")

    text = store.read("people/A")
    assert "people/Ghost" not in text          # dangling link dropped pre-apply
    assert "bad/ref" not in text               # malformed ref dropped pre-apply
    assert validators.structural_validate(store) == []
    types = {e["type"] for e in book.open_entries()}
    assert validators.DANGLING_LINK in types
    assert validators.MALFORMED_REF in types
