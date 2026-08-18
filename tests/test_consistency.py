"""Cross-page contradiction detection (sampling-based consistency check)."""

from llm_wiki import schema, validators
from llm_wiki.store import WikiStore

from conftest import FakeLLM


def _page(path, facts, related):
    return schema.render_page(schema.Page(
        path=path, title=path, summary="s", key_facts=facts,
        related_pages=related, related_sources=[("sources/digests/d", "n")],
    ), "2026-08-04")


def test_contradiction_detected_and_dedup_pairs(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A", ["born 1926"], [("people/B", "x")]))
    store.write("people/B", _page("people/B", ["born 1927"], [("people/A", "x")]))
    store.write("sources/digests/d", "x")
    # A->B and B->A form ONE pair after dedup
    llm = FakeLLM(["CONTRADICTION: birth year: A says 1926, B says 1927"])
    errors = validators.llm_consistency_check(llm, store)
    assert len(llm.seen) == 1  # deduplicated pair -> single LLM call
    assert len(errors) == 1
    assert errors[0].type == validators.CROSS_PAGE_CONTRADICTION
    assert "1926" in errors[0].detail


def test_consistent_pages_yield_no_errors(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A", ["born 1926"], [("people/B", "x")]))
    store.write("people/B", _page("people/B", ["born 1926"], []))
    llm = FakeLLM(["OK"])
    assert validators.llm_consistency_check(llm, store) == []


def test_sampling_caps_pairs(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    for i in range(10):  # 10 pages chained -> 9 pairs, cap at 3
        nxt = [("people/P" + str(i + 1), "next")] if i < 9 else []
        store.write(f"people/P{i}", _page(f"people/P{i}", ["f"], nxt))
    llm = FakeLLM(["OK"] * 10)
    validators.llm_consistency_check(llm, store, max_pairs=3)
    assert len(llm.seen) == 3


def test_targeted_pairs_bypass_sampling(tmp_path):
    """pairs= mode checks exactly the given pairs (no cap, no randomness)."""
    store = WikiStore(tmp_path / "wiki")
    for i in range(10):
        nxt = [("people/P" + str(i + 1), "next")] if i < 9 else []
        store.write(f"people/P{i}", _page(f"people/P{i}", ["f"], nxt))
    llm = FakeLLM(["OK"] * 2)
    errors = validators.llm_consistency_check(
        llm, store, pairs=[("people/P3", "people/P4"), ("people/P7", "people/P8")])
    assert errors == []
    assert len(llm.seen) == 2


def _chain_wiki(tmp_path, n=25):
    """n linked pairs; more pairs than the sweep sample cap (20)."""
    store = WikiStore(tmp_path / "wiki")
    for i in range(n):
        a, b = f"entities/A-{i:02d}", f"entities/B-{i:02d}"
        store.write(a, _page(a, ["f"], [(b, "x")]))
        store.write(b, _page(b, ["f"], [(a, "x")]))
    store.rebuild_all_indices()
    return store


class _Contradict04(FakeLLM):
    def fallback(self, prompt: str) -> str:
        if "PAGE A (entities/A-04)" in prompt and "PAGE B (entities/B-04)" in prompt:
            return "CONTRADICTION: year: A says 1, B says 2"
        return "OK"


def test_verify_and_close_rechecks_open_contradiction_pair(tmp_path):
    """Regression: an open contradiction entry must be re-checked on its own
    pair, never 'closed unseen' because the sweep sample missed it."""
    from llm_wiki.compile import Compiler
    from llm_wiki.error_book import ErrorBook

    store = _chain_wiki(tmp_path)
    book = ErrorBook(tmp_path / "error_book.yaml")
    book.discover([validators.WikiError(
        validators.CROSS_PAGE_CONTRADICTION, "entities/A-04",
        "entities/A-04 vs entities/B-04: year mismatch")], "2026-08-04")
    book.entries[0]["constraint_rule"] = "r"
    book.save()

    c = Compiler(store, _Contradict04(), book)
    for _ in range(5):  # any sampled sweep would miss the pair sooner or later
        assert c.verify_and_close() == []  # contradiction persists -> stays open
    assert len(book.open_entries()) == 1

    resolved = Compiler(store, FakeLLM(["OK"]), book)
    closed = resolved.verify_and_close()
    assert len(closed) == 1 and book.open_entries() == []
