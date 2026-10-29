"""Tests del almacén de metadatos de review (título del PR)."""

from agent_review.review_meta_store import (
    InMemoryReviewMetaStore,
    ReviewMeta,
)


def test_review_meta_display_label_con_titulo() -> None:
    meta = ReviewMeta(review_key="owner/repo:42:abc:ci", pr_id=42, pr_title="Mi PR")
    assert meta.display_label == "#42 - Mi PR"


def test_review_meta_display_label_sin_titulo() -> None:
    meta = ReviewMeta(review_key="owner/repo:42:abc:ci", pr_id=42, pr_title="")
    assert meta.display_label == "#42"


def test_in_memory_store_save_and_get() -> None:
    store = InMemoryReviewMetaStore()
    store.save("owner/repo:42:abc:ci", pr_id=42, pr_title="Título del PR")
    meta = store.get("owner/repo:42:abc:ci")
    assert meta is not None
    assert meta.pr_id == 42
    assert meta.pr_title == "Título del PR"
    assert meta.display_label == "#42 - Título del PR"


def test_in_memory_store_get_inexistente() -> None:
    store = InMemoryReviewMetaStore()
    assert store.get("no-existe") is None


def test_in_memory_store_get_many() -> None:
    store = InMemoryReviewMetaStore()
    store.save("k1", pr_id=1, pr_title="PR 1")
    store.save("k2", pr_id=2, pr_title="PR 2")
    metas = store.get_many(["k1", "k2", "k3"])
    assert len(metas) == 2
    assert metas["k1"].pr_title == "PR 1"
    assert metas["k2"].pr_title == "PR 2"
    assert "k3" not in metas


def test_in_memory_store_get_many_vacio() -> None:
    store = InMemoryReviewMetaStore()
    assert store.get_many([]) == {}
