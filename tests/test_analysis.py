from math import cos, radians, sin
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from maps_review_monitor.analysis import (
    canonical_contributor_id,
    lexical_similarity,
    normalize_text,
    run_analysis,
)
from maps_review_monitor.database import Database
from maps_review_monitor.models import ProfileSummary, ReviewSnapshot


class FakeEmbeddingProvider:
    version = "fake-e5"

    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


class MappingEmbeddingProvider:
    version = "controlled-e5"

    def __init__(self, vectors):
        self.vectors = {normalize_text(text): vector for text, vector in vectors.items()}

    def encode(self, texts):
        return [self.vectors[text] for text in texts]


def review(
    shop_key: str,
    review_id: str,
    text: str,
    profile_url: str = "",
    *,
    time_text: str = "2 個月前",
    photo_paths: list[str] | None = None,
) -> ReviewSnapshot:
    return ReviewSnapshot(
        review_id=review_id,
        shop_key=shop_key,
        shop_name=f"店家 {shop_key}",
        shop_url="https://www.google.com/maps/place/example",
        author="評論者",
        stars=5,
        time_text=time_text,
        text=text,
        profile=ProfileSummary(url=profile_url),
        photo_paths=photo_paths or [],
    )


def settings_for(tmp_path: Path, database_path: Path):
    return SimpleNamespace(
        root=tmp_path,
        data_dir=tmp_path / "data",
        database_path=database_path,
        timezone="Asia/Taipei",
        analysis_model="intfloat/multilingual-e5-small",
        analysis_lexical_threshold=0.85,
        analysis_semantic_threshold=0.92,
        analysis_rules_path=tmp_path / "review-analysis.yaml",
    )


def write_test_image(path: Path, color: tuple[int, int, int] = (20, 120, 80)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 3), color).save(path, format="PNG")


def test_normalization_and_lexical_similarity():
    left = normalize_text("服務 很好！會再來。")
    right = normalize_text("服務很好，會再來")
    assert left == right
    assert lexical_similarity(left, right) == 1


def test_canonical_contributor_id_ignores_google_url_variants():
    assert canonical_contributor_id(
        "https://www.google.com/maps/contrib/117888788252015619692/reviews?hl=zh-TW"
    ) == "117888788252015619692"
    assert canonical_contributor_id(
        "https://maps.google.com/contrib/ABC_123/?entry=ttu"
    ) == "abc_123"
    assert canonical_contributor_id("https://example.com/profile/123") == ""
    assert canonical_contributor_id("https://evil.example/maps/contrib/123") == ""
    assert canonical_contributor_id("http://www.google.com/maps/contrib/123") == ""


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

    settings = settings_for(tmp_path, path)
    result = run_analysis(settings, provider=FakeEmbeddingProvider())
    assert result["groups"] >= 2

    db = Database(path)
    try:
        assert db.analysis_status()["status"] == "completed"
        cross = db.analysis_groups(scope="cross")
        assert cross[0]["label"] == "highly_similar"
        assert cross[0]["review_count"] == 3
        assert cross[0]["evidence"]["schema_version"] == 2
        assert cross[0]["evidence"]["level"] == "content_similar"
        assert cross[0]["evidence"]["score"] == 30
        assert cross[0]["evidence"]["decision"]["score_not_probability"] is True
        assert cross[0]["evidence"]["decision"][
            "cross_shop_group_size_counts_as_independent_evidence"
        ] is False
        assert db.analysis_shop_counts()["a"] == {"similar": 1, "suspected": 0}
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
    settings = settings_for(tmp_path, path)
    run_analysis(settings, provider=FakeEmbeddingProvider())
    db = Database(path)
    try:
        assert db.analysis_groups(scope="same") == []
        assert db.analysis_groups(scope="cross")[0]["review_count"] == 3
    finally:
        db.close()


def test_unrelated_reviews_do_not_form_a_group(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    texts = {
        "紅燒牛肉湯頭濃郁麵條彈牙份量充足": [1.0, 0.0],
        "店員細心介紹按摩流程空間整潔安靜": [0.0, 1.0],
    }
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        for index, text in enumerate(texts):
            db.upsert_review(review("a", str(index), text), False)
    finally:
        db.close()

    result = run_analysis(settings_for(tmp_path, path), MappingEmbeddingProvider(texts))
    assert result["groups"] == 0


def test_complete_linkage_does_not_join_a_similarity_chain(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    text_a = "紅燒牛肉湯頭濃郁麵條彈牙份量充足"
    text_b = "店員細心介紹按摩流程空間整潔安靜"
    text_c = "甜點造型精緻咖啡香氣飽滿座位舒適"
    vectors = {
        text_a: [cos(radians(0)), sin(radians(0))],
        text_b: [cos(radians(20)), sin(radians(20))],
        text_c: [cos(radians(40)), sin(radians(40))],
    }
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        for index, text in enumerate(vectors):
            db.upsert_review(review("a", str(index), text), False)
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), MappingEmbeddingProvider(vectors))
    db = Database(path)
    try:
        same_groups = db.analysis_groups(scope="same")
        assert len(same_groups) == 1
        assert same_groups[0]["review_count"] == 2
        assert same_groups[0]["evidence"]["metrics"]["pair_density"] == 1.0
    finally:
        db.close()


def test_cross_shop_similar_text_and_coarse_dates_are_content_only(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    db = Database(path)
    try:
        for key in ("a", "b", "c"):
            db.sync_shop(key, f"店家 {key}", "https://www.google.com/maps/place/example")
            db.upsert_review(
                review(key, key, text, f"https://google.com/maps/contrib/{key}123"),
                False,
            )
        db.conn.execute("UPDATE reviews SET estimated_posted_date='2026-06-01'")
        db.conn.commit()
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        group = db.analysis_groups(scope="cross")[0]
        evidence = group["evidence"]
        assert group["label"] == "highly_similar"
        assert evidence["level"] == "content_similar"
        assert evidence["metrics"]["date_coarse_count"] == 3
        assert evidence["metrics"]["date_known_count"] == 3
        assert next(
            signal
            for signal in evidence["signals"]
            if signal["family"] == "temporal_context"
        )["matched"] is False
    finally:
        db.close()


def test_one_reviewer_cohort_signal_stays_at_manual_review(tmp_path: Path):
    path = tmp_path / "reviews.sqlite3"
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    db = Database(path)
    try:
        for key in ("a", "b", "c"):
            db.sync_shop(key, f"店家 {key}", "https://www.google.com/maps/place/example")
        shops_by_profile = {"100": ("a", "b", "c"), "200": ("a", "b")}
        for profile, shops in shops_by_profile.items():
            for shop in shops:
                db.upsert_review(
                    review(
                        shop,
                        f"{profile}-{shop}",
                        text,
                        f"https://www.google.com/maps/contrib/{profile}/reviews?hl=zh-TW",
                    ),
                    False,
                )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        group = db.analysis_groups(scope="cross")[0]
        assert group["label"] == "highly_similar"
        assert group["evidence"]["level"] == "review"
        assert group["evidence"]["decision"]["independent_signal_families"] == [
            "reviewer_network"
        ]
        assert group["evidence"]["score"] == 55
        cohort = next(
            signal
            for signal in group["evidence"]["signals"]
            if signal["key"] == "reviewer_cohort"
        )
        assert cohort["observed"]["largest_cohort_profile_count"] == 2
        assert cohort["observed"]["largest_cohort_shop_count"] == 2
    finally:
        db.close()


def test_time_proximity_does_not_combine_with_one_signal_to_reach_high_review(
    tmp_path: Path,
):
    path = tmp_path / "reviews.sqlite3"
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    observed_at = "2026-07-23T15:00:00+08:00"
    db = Database(path)
    try:
        for key in ("a", "b"):
            db.sync_shop(key, f"店家 {key}", "https://www.google.com/maps/place/example")
        for profile in ("100", "200", "300"):
            for shop in ("a", "b"):
                variant = (
                    f"https://www.google.com/maps/contrib/{profile}/reviews?hl=zh-TW"
                    if shop == "a"
                    else f"https://maps.google.com/contrib/{profile}?entry=ttu"
                )
                db.upsert_review(
                    review(
                        shop,
                        f"{profile}-{shop}",
                        text,
                        variant,
                        time_text="1 小時前",
                    ),
                    False,
                    observed_at=observed_at,
                )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        group = db.analysis_groups(scope="cross")[0]
        evidence = group["evidence"]
        assert group["label"] == "highly_similar"
        assert evidence["level"] == "review"
        assert evidence["decision"]["independent_signal_families"] == [
            "reviewer_network"
        ]
        assert evidence["decision"]["supporting_context_families"] == [
            "temporal_context"
        ]
        assert evidence["score"] == 65
        assert evidence["metrics"] == {
            **evidence["metrics"],
            "distinct_profiles": 3,
            "shop_count": 2,
            "review_count": 6,
            "direct_pairs": 9,
            "possible_pairs": 9,
            "pair_density": 1.0,
        }
    finally:
        db.close()


def test_same_photo_bytes_across_profiles_is_independent_media_signal(tmp_path: Path):
    path = tmp_path / "data" / "reviews.sqlite3"
    media = tmp_path / "data" / "media"
    first = media / "a" / "1" / "shared.png"
    second = media / "a" / "2" / "shared.png"
    first_unique = media / "a" / "1" / "unique.png"
    second_unique = media / "a" / "2" / "unique.png"
    write_test_image(first)
    second.parent.mkdir(parents=True)
    second.write_bytes(first.read_bytes())
    write_test_image(first_unique, (160, 30, 20))
    write_test_image(second_unique, (20, 30, 160))
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        db.upsert_review(
            review(
                "a",
                "1",
                text,
                "https://google.com/maps/contrib/100",
                photo_paths=[
                    str(first.relative_to(tmp_path)),
                    str(first_unique.relative_to(tmp_path)),
                ],
            ),
            False,
        )
        db.upsert_review(
            review(
                "a",
                "2",
                text,
                "https://google.com/maps/contrib/200",
                photo_paths=[
                    str(second.relative_to(tmp_path)),
                    str(second_unique.relative_to(tmp_path)),
                ],
            ),
            False,
        )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        group = db.analysis_groups(scope="same")[0]
        evidence = group["evidence"]
        assert evidence["level"] == "review"
        assert evidence["decision"]["independent_signal_families"] == ["media"]
        media_signal = next(
            signal for signal in evidence["signals"] if signal["family"] == "media"
        )
        assert media_signal["observed"]["shared_matches"][0][
            "distinct_profile_count"
        ] == 2
        assert {
            member["review_id"]
            for member in media_signal["observed"]["shared_matches"][0]["members"]
        } == {"1", "2"}
        matched_paths = {
            path
            for member in media_signal["observed"]["shared_matches"][0]["members"]
            for path in member["photo_paths"]
        }
        assert matched_paths == {
            str(first.relative_to(tmp_path)),
            str(second.relative_to(tmp_path)),
        }
        assert str(first_unique.relative_to(tmp_path)) not in matched_paths
        assert str(second_unique.relative_to(tmp_path)) not in matched_paths
    finally:
        db.close()


def test_invalid_photo_bytes_never_become_media_evidence(tmp_path: Path):
    path = tmp_path / "data" / "reviews.sqlite3"
    media = tmp_path / "data" / "media"
    first = media / "a" / "1" / "photo.jpg"
    second = media / "a" / "2" / "photo.jpg"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"not-an-image")
    second.write_bytes(b"not-an-image")
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        for review_id, photo, profile in (
            ("1", first, "100"),
            ("2", second, "200"),
        ):
            db.upsert_review(
                review(
                    "a",
                    review_id,
                    text,
                    f"https://google.com/maps/contrib/{profile}",
                    photo_paths=[str(photo.relative_to(tmp_path))],
                ),
                False,
            )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        evidence = db.analysis_groups(scope="same")[0]["evidence"]
        media_signal = next(
            signal for signal in evidence["signals"] if signal["family"] == "media"
        )
        assert evidence["level"] == "content_similar"
        assert media_signal["matched"] is False
        assert media_signal["observed"]["hashed_photo_count"] == 0
    finally:
        db.close()


def test_time_proximity_is_context_only_and_prefers_shortest_equal_window(
    tmp_path: Path,
):
    path = tmp_path / "reviews.sqlite3"
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    observed_times = (
        "2026-07-01T15:00:00+08:00",
        "2026-07-11T15:00:00+08:00",
        "2026-07-12T15:00:00+08:00",
        "2026-07-16T15:00:00+08:00",
    )
    db = Database(path)
    try:
        db.sync_shop("a", "店家 a", "https://www.google.com/maps/place/example")
        for index, observed_at in enumerate(observed_times):
            db.upsert_review(
                review(
                    "a",
                    str(index),
                    text,
                    f"https://google.com/maps/contrib/{index + 100}",
                    time_text="1 小時前",
                ),
                False,
                observed_at=observed_at,
            )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        evidence = db.analysis_groups(scope="same")[0]["evidence"]
        time_signal = next(
            signal
            for signal in evidence["signals"]
            if signal["key"] == "time_proximity"
        )
        assert evidence["level"] == "content_similar"
        assert evidence["decision"]["independent_signal_families"] == []
        assert evidence["decision"]["supporting_context_families"] == [
            "temporal_context"
        ]
        assert time_signal["matched"] is True
        # Two three-review windows exist; the later one is only five days plus
        # the conservative one-hour uncertainty on each side.
        assert time_signal["observed"]["cluster_review_count"] == 3
        assert time_signal["observed"]["guaranteed_span_hours"] == 122.0
    finally:
        db.close()


def test_reviewer_cohort_and_verified_photo_reach_high_with_three_profiles(
    tmp_path: Path,
):
    path = tmp_path / "data" / "reviews.sqlite3"
    media = tmp_path / "data" / "media"
    source_photo = media / "shared.png"
    write_test_image(source_photo)
    text = "服務態度非常親切餐點也非常美味值得再次光臨"
    db = Database(path)
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for profile in ("100", "200", "300"):
            copied_photo = media / "a" / profile / "photo.png"
            copied_photo.parent.mkdir(parents=True)
            copied_photo.write_bytes(source_photo.read_bytes())
            for shop in ("a", "b"):
                db.upsert_review(
                    review(
                        shop,
                        f"{profile}-{shop}",
                        text,
                        f"https://google.com/maps/contrib/{profile}",
                        photo_paths=(
                            [str(copied_photo.relative_to(tmp_path))]
                            if shop == "a"
                            else []
                        ),
                    ),
                    False,
                )
    finally:
        db.close()

    run_analysis(settings_for(tmp_path, path), FakeEmbeddingProvider())
    db = Database(path)
    try:
        group = db.analysis_groups(scope="cross")[0]
        evidence = group["evidence"]
        assert group["label"] == "suspected"
        assert evidence["level"] == "suspicious_high"
        assert evidence["decision"]["independent_signal_families"] == [
            "media",
            "reviewer_network",
        ]
        assert evidence["decision"]["supporting_context_families"] == []
        assert evidence["score"] == 75
    finally:
        db.close()
