from llm_wiki import schema
from llm_wiki.store import WikiStore


def _page(path, related=None):
    return schema.render_page(schema.Page(
        path=path, title=path, summary="s", key_facts=["f"],
        related_pages=related or [],
        related_sources=[("sources/digests/d-1", "n")],
    ), "2026-08-04")


def test_store_roundtrip_and_iter(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A"))
    store.write("sources/digests/d-1", "digest")
    assert store.iter_pages() == ["people/A"]
    assert store.exists("people/A") and not store.exists("people/B")
    assert store.read("people/A").startswith("---")


def test_bidirectional_sync(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A", related=[("people/B", "links to B")]))
    store.write("people/B", _page("people/B"))
    fixes = store.sync_bidirectional_links()
    assert fixes == ["people/B <- people/A"]
    assert "[[people/A]]" in store.read("people/B")
    # idempotent: second run adds nothing
    assert store.sync_bidirectional_links() == []


def test_rebuild_indices(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A"))
    store.write("people/B", _page("people/B"))
    store.rebuild_all_indices()
    idx = store.read("people/_index")
    assert "[[people/A]]" in idx and "[[people/B]]" in idx
    assert "people/ (2 pages)" in store.read("index")


def test_rebuild_indices_for_only_touches_affected(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A"))
    store.write("systems/B", _page("systems/B"))
    store.rebuild_all_indices()
    # 手动污染 systems 索引，然后只对 people 做增量重建
    store.write("systems/_index", "# corrupted\n")
    store.rebuild_indices_for(["people/A"])
    assert "[[people/A]]" in store.read("people/_index")   # people 已重建
    assert store.read("systems/_index") == "# corrupted\n"  # systems 未被触碰
    assert "people/" in store.read("index")                # 全局索引已更新


def test_page_meta_cached_and_invalidated_on_write(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    page = schema.render_page(schema.Page(
        path="people/A", title="A", summary="first summary",
        aliases=["Alpha"], key_facts=["f"]), "2026-08-04")
    store.write("people/A", page)
    fm, summary = store.page_meta("people/A")
    assert summary == "first summary"
    assert fm.get("aliases") == ["Alpha"]
    # second call hits the cache (same mtime) but must return the same data
    assert store.page_meta("people/A") == (fm, summary)
    # a write must invalidate: new summary is visible even within the same second
    page2 = schema.render_page(schema.Page(
        path="people/A", title="A", summary="second summary",
        aliases=["Beta"], key_facts=["f"]), "2026-08-04")
    store.write("people/A", page2)
    fm2, summary2 = store.page_meta("people/A")
    assert summary2 == "second summary"
    assert fm2.get("aliases") == ["Beta"]


def test_page_meta_none_after_delete(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("people/A", _page("people/A"))
    assert store.page_meta("people/A") is not None
    store.delete("people/A")
    assert store.page_meta("people/A") is None
