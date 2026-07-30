from pathlib import Path
from types import SimpleNamespace

from maps_review_monitor.analysis import lexical_similarity, normalize_text, run_analysis
from maps_review_monitor.database import Database
from maps_review_monitor.models import ProfileSummary, ReviewSnapshot


class FakeEmbeddingProvider:
    version = "fake-e5"

    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


def review(shop_key: str, review_id: str, text: str, profile_url: str = "") -> ReviewSnapshot:
    return ReviewSnapshot(
        review_id=review_id,
        shop_key=shop_key,
        shop_name=f"店家 {shop_key}",
        shop_url="https://www.google.com/maps/place/example",
        author="評論者",
        stars=5,
        time_text="1 天前",
        text=text,
        profile=ProfileSummary(url=profile_url),
    )


def test_normalization_and_lexical_similarity():
    left = normalize_text("服務 很好！會再來。")
    right = normalize_text("服務很好，會再來")
    assert left == right
    assert lexical_similarity(left, right) == 1


def test_analysis_publishes_atomic_current_snapshot(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    db = Database(path)
    try:
        for key in ("a", "b"):
            db.sync_shop(key, f"店家 {key}", "https://www.google.com/maps/place/example")
        text = "服務態度非常親切餐點也非常美味值得再次光臨"
        db.upsert_review(review("a", "1", text, "https://maps.google.com/contrib/1"), False)
        db.upsert_review(review("b", "2", text, "https://maps.google.com/contrib/2"), False)
        db.upsert_review(review("b", "3", text, "https://maps.google.com/contrib/3"), False)
    finally:
        db.close()

    settings = SimpleNamespace(
        database_path=path,
        timezone="Asia/Taipei",
        analysis_model="intfloat/multilingual-e5-small",
        analysis_lexical_threshold=0.85,
        analysis_semantic_threshold=0.92,
        analysis_rules_path=tmp_path / "review-analysis.yaml",
    )
    result = run_analysis(settings, provider=FakeEmbeddingProvider())
    assert result["groups"] >= 2

    db = Database(path)
    try:
        assert db.analysis_status()["status"] == "completed"
        cross = db.analysis_groups(scope="cross")
        assert cross[0]["label"] == "suspected"
        assert cross[0]["review_count"] == 3
        assert db.analysis_shop_counts()["a"]["suspected"] == 1
        db.upsert_review(
            review("a", "1", "這是編輯後完全不同的評論內容不應沿用舊分析結果"), False
        )
        assert db.analysis_groups(scope="cross") == []
        assert db.analysis_shop_counts().get("a", {}).get("suspected", 0) == 0
        assert db.conn.execute("SELECT COUNT(*) FROM review_versions").fetchone()[0] == 1
    finally:
        db.close()


def test_short_text_requires_three_exact_duplicates_in_scope(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        db.sync_shop("b", "店家 b", "https://www.google.com/maps/place/example")
        db.upsert_review(review("a", "1", "好吃"), False)
        db.upsert_review(review("a", "2", "好吃"), False)
        db.upsert_review(review("b", "3", "好吃"), False)
    finally:
        db.close()
    settings = SimpleNamespace(
        database_path=path,
        timezone="Asia/Taipei",
        analysis_model="fake",
        analysis_lexical_threshold=0.85,
        analysis_semantic_threshold=0.92,
        analysis_rules_path=tmp_path / "rules.yaml",
    )
    run_analysis(settings, provider=FakeEmbeddingProvider())
    db = Database(path)
    try:
        assert db.analysis_groups(scope="same") == []
        assert db.analysis_groups(scope="cross")[0]["review_count"] == 3
    finally:
        db.close()
