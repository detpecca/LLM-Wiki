"""wiki_search: structured-signal scoring, including CJK queries."""

from llm_wiki import schema
from llm_wiki.search import _tokens, search
from llm_wiki.store import WikiStore


def _store_with_pages(tmp_path):
    store = WikiStore(tmp_path / "wiki")
    store.write("systems/LLM-Wiki", schema.render_page(schema.Page(
        path="systems/LLM-Wiki", title="LLM-Wiki",
        aliases=["LLM Wiki"], tags=["RAG"],
        summary="自我纠错的智能体检索系统",
        key_facts=["f"], related_sources=[("sources/digests/d", "n")],
    ), "2026-08-04"))
    store.write("concepts/Error-Book", schema.render_page(schema.Page(
        path="concepts/Error-Book", title="Error Book",
        aliases=["错误记录本"], tags=["self-correction"],
        summary="persistent self-correction",
        key_facts=["f"], related_sources=[("sources/digests/d", "n")],
    ), "2026-08-04"))
    store.write("sources/digests/d", "x")
    return store


def test_tokens_english_unchanged():
    assert _tokens("Which film is older?") == ["which", "film", "is", "older"]


def test_tokens_cjk_bigrams():
    assert _tokens("导演年龄") == ["导演", "演年", "年龄"]
    assert _tokens("导演") == ["导演"]          # 2-char run kept whole
    assert "wiki" in _tokens("LLM-Wiki 的自我纠错机制")  # mixed query


def test_search_chinese_query_hits_alias(tmp_path):
    store = _store_with_pages(tmp_path)
    results = search(store, "错误记录本")
    assert results and results[0]["path"] == "concepts/Error-Book"


def test_search_chinese_query_hits_summary(tmp_path):
    store = _store_with_pages(tmp_path)
    results = search(store, "自我纠错")
    assert results and results[0]["path"] == "systems/LLM-Wiki"


def test_search_structured_beats_content(tmp_path):
    store = _store_with_pages(tmp_path)
    results = search(store, "LLM-Wiki")
    assert results[0]["path"] == "systems/LLM-Wiki"


def test_search_reflects_rewrite_via_cache_invalidation(tmp_path):
    """Rewriting a page must be visible to search (page_meta cache is
    invalidated on write), even for a same-second overwrite."""
    store = _store_with_pages(tmp_path)
    assert search(store, "自我纠错")[0]["path"] == "systems/LLM-Wiki"
    # rewrite the summary to no longer match the query
    store.write("systems/LLM-Wiki", schema.render_page(schema.Page(
        path="systems/LLM-Wiki", title="LLM-Wiki", aliases=["LLM Wiki"],
        tags=["RAG"], summary="完全不同的主题",
        key_facts=["f"], related_sources=[("sources/digests/d", "n")],
    ), "2026-08-04"))
    hits = [r["path"] for r in search(store, "自我纠错")]
    assert "systems/LLM-Wiki" not in hits  # stale cache would still match
