from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from maps_review_monitor.config import ShopConfig
from maps_review_monitor.database import Database
from maps_review_monitor.evidence_inputs import analysis_input_hash
from maps_review_monitor.models import ProfileSummary, ReviewSnapshot
from maps_review_monitor.portal import (
    build_same_name_groups,
    build_search_results,
    create_app,
    load_content_assets,
    search_similar_reviewer_names,
)


def test_load_content_assets_sorts_newest_first(tmp_path: Path):
    path = tmp_path / "content.yaml"
    path.write_text(
        """content:
  - id: older
    shop_key: shop-a
    published_at: '2026-07-01T09:00:00+08:00'
    title: Older
    body: First
  - id: newer
    shop_key: shop-a
    published_at: '2026-07-02T09:00:00+08:00'
    title: Newer
    body: Second
    photos: [data/portal/shop-a/photo.jpg]
""",
        encoding="utf-8",
    )
    assets = load_content_assets(path)
    assert [asset["id"] for asset in assets] == ["newer", "older"]
    assert assets[0]["photos"] == ["data/portal/shop-a/photo.jpg"]


def test_load_content_assets_rejects_missing_required_field(tmp_path: Path):
    path = tmp_path / "content.yaml"
    path.write_text("content:\n  - id: incomplete\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必要欄位"):
        load_content_assets(path)


def test_portal_analysis_pages_load_without_completed_run(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        """data_dir = "data"
analysis_enabled = false
[[shops]]
name = "測試店家"
url = "https://www.google.com/maps/place/example"
enabled = true
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(str(config)))
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="similar-review-toggle"' in home.text
    assert 'id="suspected-review-toggle"' in home.text
    assert "顯示高度相似評論" in home.text
    assert "顯示高優先複核" in home.text
    assert "hide-highly-similar" in home.text
    assert "hide-suspected" in home.text
    assert "版本 0.8.0" in home.text
    assert 'href="/reviewers"' in home.text
    assert 'href="/review-times"' in home.text
    assert 'id="global-search-input"' in home.text
    css = client.get("/static/portal.css")
    assert css.status_code == 200
    assert ".hide-highly-similar .similar-only" in css.text
    assert ".hide-suspected .suspected-only" in css.text
    assert ".time-heatmap" in css.text
    assert ".two-hour-chart" in css.text
    response = client.get("/analysis")
    assert response.status_code == 200
    assert "全部店家分析" in response.text
    status = client.get("/api/analysis-status").json()
    assert status["status"] == "none"
    assert client.get("/reviewers").status_code == 200
    name_search = client.get("/reviewers?q=王小明")
    assert name_search.status_code == 200
    assert 'id="reviewer-name-query"' in name_search.text
    assert "王小明" in name_search.text
    time_analysis = client.get("/review-times")
    assert time_analysis.status_code == 200
    assert "評論發文時間分析" in time_analysis.text
    assert "資料不足，尚無法判定規律" in time_analysis.text
    assert "星期 × 2 小時發文熱度圖" in time_analysis.text
    assert "時段 ＼ 星期" in time_analysis.text
    assert "週一" in time_analysis.text and "週日" in time_analysis.text
    assert "00–01" in time_analysis.text and "22–23" in time_analysis.text
    assert "heat-cell heat-level-0" in time_analysis.text
    assert "分佈圖" in time_analysis.text
    assert "每 2 小時分布" in time_analysis.text
    assert "00:00–01:59" in time_analysis.text
    assert "22:00–23:59" in time_analysis.text
    assert "0 筆可明確落在單一兩小時區段" in time_analysis.text
    assert "三小時詳細分布" in time_analysis.text
    assert "<figure" in time_analysis.text
    assert "發文時間明細" in time_analysis.text
    assert client.get("/review-times?window=bad").status_code == 404
    assert client.get("/review-times?shop=missing").status_code == 404
    search = client.get("/search?q=測試")
    assert search.status_code == 200
    assert "搜尋" in search.text


def test_media_route_only_serves_public_image_and_video_roots(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        """data_dir = "data"
analysis_enabled = false
[[shops]]
name = "測試店家"
url = "https://www.google.com/maps/place/example"
enabled = true
""",
        encoding="utf-8",
    )
    media_photo = tmp_path / "data" / "media" / "shop-a" / "photo.JPG"
    portal_video = tmp_path / "data" / "portal" / "shop-a" / "intro.mp4"
    media_photo.parent.mkdir(parents=True)
    portal_video.parent.mkdir(parents=True)
    media_photo.write_bytes(b"public-photo")
    portal_video.write_bytes(b"public-video")

    client = TestClient(create_app(str(config)))

    photo = client.get("/media/data/media/shop-a/photo.JPG")
    video = client.get("/media/data/portal/shop-a/intro.mp4")
    assert photo.status_code == 200
    assert photo.content == b"public-photo"
    assert photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["x-content-type-options"] == "nosniff"
    assert video.status_code == 200
    assert video.content == b"public-video"
    assert video.headers["content-type"] == "video/mp4"


def test_media_route_rejects_sensitive_hidden_non_media_and_traversal_paths(
    tmp_path: Path,
):
    config = tmp_path / "config.toml"
    config.write_text(
        """data_dir = "data"
analysis_enabled = false
[[shops]]
name = "測試店家"
url = "https://www.google.com/maps/place/example"
enabled = true
""",
        encoding="utf-8",
    )
    files = {
        tmp_path / ".env": "TELEGRAM_BOT_TOKEN=secret",
        tmp_path / "outside.jpg": "outside public roots",
        tmp_path / "data" / "reviews.sqlite3": "private database",
        tmp_path / "data" / "private" / "photo.jpg": "wrong data subtree",
        tmp_path / "data" / "portal" / "content.yaml": "private metadata",
        tmp_path / "data" / "portal" / ".hidden.jpg": "hidden media",
        tmp_path / "data" / "portal" / ".private" / "photo.jpg": "hidden directory",
        tmp_path / "data" / "media" / "notes.txt": "not media",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    client = TestClient(create_app(str(config)))
    rejected_urls = [
        "/media/.env",
        "/media/outside.jpg",
        "/media/config.toml",
        "/media/data/reviews.sqlite3",
        "/media/data/private/photo.jpg",
        "/media/data/portal/content.yaml",
        "/media/data/portal/.hidden.jpg",
        "/media/data/portal/.private/photo.jpg",
        "/media/data/media/notes.txt",
        "/media/data/media/..%2F..%2Foutside.jpg",
    ]

    for url in rejected_urls:
        assert client.get(url).status_code == 404, url


def test_review_time_page_renders_known_review_details(tmp_path: Path):
    shop_url = "https://www.google.com/maps/place/example"
    shop = ShopConfig(name="測試店家", url=shop_url)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''data_dir = "data"
analysis_enabled = false
[[shops]]
name = "{shop.name}"
url = "{shop.url}"
enabled = true
''',
        encoding="utf-8",
    )
    database_path = tmp_path / "data" / "reviews.sqlite3"
    database = Database(database_path, "Asia/Taipei")
    database.sync_shop(shop.key, shop.name, shop.url)
    snapshot = ReviewSnapshot(
        review_id="known-review",
        shop_key=shop.key,
        shop_name=shop.name,
        shop_url=shop.url,
        author="時間測試評論者",
        stars=5,
        time_text="1 小時前",
        text="時間分析頁面測試",
        profile=ProfileSummary(),
    )
    local_timezone = ZoneInfo("Asia/Taipei")
    observed_local = datetime.combine(
        datetime.now(local_timezone).date(), time(12, 30), tzinfo=local_timezone
    )
    database.upsert_review(
        snapshot,
        notify=False,
        observed_at=observed_local.astimezone(timezone.utc).isoformat(),
    )
    database.close()

    response = TestClient(create_app(str(config))).get(
        f"/review-times?shop={shop.key}&window=30d"
    )

    assert response.status_code == 200
    assert "時間測試評論者" in response.text
    assert "有時分回推" in response.text
    assert "Google 相對時間回推" in response.text
    weekday_label = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")[
        observed_local.weekday()
    ]
    assert f'aria-label="{weekday_label} 10–11：1 筆"' in response.text
    assert 'aria-label="10:00–11:59：1 筆，占 100.0%；秒或分鐘級 0 筆，小時級 1 筆"' in response.text


def test_same_name_groups_are_cross_shop_and_do_not_assume_identity():
    reviews = [
        {
            "author": "王 小明",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "1",
            "first_seen_at": "2026-07-01",
            "profile": {"url": "https://www.google.com/maps/contrib/1/reviews?hl=zh-TW"},
        },
        {
            "author": "王　小明",
            "shop_key": "b",
            "shop_name": "乙店",
            "review_id": "2",
            "first_seen_at": "2026-07-02",
            "profile": {"url": "https://maps.google.com/contrib/2?entry=ttu"},
        },
        {
            "author": "只有單店",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "3",
            "first_seen_at": "2026-07-03",
            "profile": {"url": ""},
        },
    ]
    groups = build_same_name_groups(reviews)
    assert len(groups) == 1
    assert groups[0]["shop_count"] == 2
    assert groups[0]["identity_status"] == "different_profiles"
    assert "未確認為同一人" in groups[0]["identity_text"]


def test_same_name_groups_canonicalize_google_profile_url_variants():
    reviews = [
        {
            "author": "王小明",
            "shop_key": "a",
            "review_id": "1",
            "first_seen_at": "2026-08-01",
            "profile": {
                "url": "https://www.google.com/maps/contrib/123456/reviews?hl=zh-TW"
            },
        },
        {
            "author": "王小明",
            "shop_key": "b",
            "review_id": "2",
            "first_seen_at": "2026-08-02",
            "profile": {
                "url": "https://maps.google.com/contrib/123456?entry=ttu"
            },
        },
    ]

    groups = build_same_name_groups(reviews)

    assert groups[0]["profile_count"] == 1
    assert groups[0]["missing_profiles"] == 0
    assert groups[0]["identity_status"] == "same_profile"


def test_similar_reviewer_name_search_handles_spacing_and_one_character_difference():
    reviews = [
        {
            "author": "郭美岑",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "1",
            "first_seen_at": "2026-08-03",
            "profile": {"url": "https://example.com/profile/1"},
        },
        {
            "author": "郭 美岑",
            "shop_key": "b",
            "shop_name": "乙店",
            "review_id": "2",
            "first_seen_at": "2026-08-02",
            "profile": {"url": "https://example.com/profile/1"},
        },
        {
            "author": "郭美芩",
            "shop_key": "c",
            "shop_name": "丙店",
            "review_id": "3",
            "first_seen_at": "2026-08-01",
            "profile": {"url": "https://example.com/profile/3"},
        },
        {
            "author": "陳大文",
            "shop_key": "d",
            "shop_name": "丁店",
            "review_id": "4",
            "first_seen_at": "2026-07-31",
            "profile": {"url": "https://example.com/profile/4"},
        },
    ]

    groups = search_similar_reviewer_names("郭美岑", reviews)

    assert [group["match_type"] for group in groups] == ["exact", "similar"]
    assert groups[0]["shop_count"] == 2
    assert groups[0]["similarity_percent"] == 100
    assert groups[0]["name_variants"] == ["郭 美岑", "郭美岑"]
    assert groups[1]["author"] == "郭美芩"
    assert groups[1]["similarity_percent"] == 67


def test_single_character_name_query_does_not_expand_to_partial_names():
    reviews = [
        {"author": "王", "shop_key": "a", "first_seen_at": "2026-08-01", "profile": {}},
        {"author": "王小明", "shop_key": "b", "first_seen_at": "2026-08-01", "profile": {}},
        {"author": "李小明", "shop_key": "c", "first_seen_at": "2026-08-01", "profile": {}},
    ]

    groups = search_similar_reviewer_names("王", reviews)

    assert [group["author"] for group in groups] == ["王"]
    assert groups[0]["match_type"] == "exact"


def test_similar_name_search_normalizes_full_width_latin_and_skips_anonymous():
    reviews = [
        {"author": "ＡＬＩＣＥ", "shop_key": "a", "first_seen_at": "2026-08-01", "profile": {}},
        {"author": "匿名評論者", "shop_key": "b", "first_seen_at": "2026-08-01", "profile": {}},
        {"author": "!!!", "shop_key": "c", "first_seen_at": "2026-08-01", "profile": {}},
    ]

    groups = search_similar_reviewer_names("alice", reviews)

    assert [group["author"] for group in groups] == ["ＡＬＩＣＥ"]
    assert groups[0]["similarity_percent"] == 100


def test_search_results_cover_shops_reviews_replies_and_assets():
    shops = [{"shop_key": "a", "name": "松肌虎尾店", "url": "https://google.com/a"}]
    reviews = [
        {
            "shop_key": "a",
            "shop_name": "松肌虎尾店",
            "author": "Alice",
            "text": "老師很親切，讓人放鬆",
            "owner_reply": {"text": "感謝您的推薦"},
        }
    ]
    assets = [
        {
            "id": "post",
            "shop_key": "a",
            "title": "八月公告",
            "body": "營業時間調整",
        }
    ]

    assert build_search_results("虎尾", shops, reviews, assets)["shop_results"]
    assert build_search_results("ALICE 放鬆", shops, reviews, assets)["review_results"]
    assert build_search_results("感謝 推薦", shops, reviews, assets)["review_results"]
    assert build_search_results("八月 營業", shops, reviews, assets)["asset_results"]
    assert build_search_results("不存在", shops, reviews, assets)["total"] == 0


def test_evidence_v2_pages_explain_metrics_and_drop_stale_group_links(tmp_path: Path):
    first_shop = ShopConfig(
        name="甲店", url="https://www.google.com/maps/place/evidence-a"
    )
    second_shop = ShopConfig(
        name="乙店", url="https://www.google.com/maps/place/evidence-b"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'''data_dir = "data"
analysis_enabled = false
[[shops]]
name = "{first_shop.name}"
url = "{first_shop.url}"
enabled = true
[[shops]]
name = "{second_shop.name}"
url = "{second_shop.url}"
enabled = true
''',
        encoding="utf-8",
    )
    snapshots = []
    for index, (shop, review_id) in enumerate(
        ((first_shop, "a1"), (second_shop, "b1"), (second_shop, "b2")), start=1
    ):
        photo = tmp_path / "data" / "media" / shop.key / review_id / "shared.png"
        unique_photo = (
            tmp_path / "data" / "media" / shop.key / review_id / "unique.png"
        )
        photo.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (3, 3), (40, 120, 80)).save(photo, format="PNG")
        Image.new("RGB", (3, 3), (index * 40, 20, 180)).save(
            unique_photo, format="PNG"
        )
        snapshots.append(ReviewSnapshot(
            review_id=review_id,
            shop_key=shop.key,
            shop_name=shop.name,
            shop_url=shop.url,
            author=f"評論者 {index}",
            stars=5,
            time_text="1 小時前",
            text=f"相似評論內容 {index}",
            profile=ProfileSummary(url=f"https://maps.google.com/contrib/{index}"),
            photo_paths=[
                str(photo.relative_to(tmp_path)),
                str(unique_photo.relative_to(tmp_path)),
            ],
        ))
    database = Database(tmp_path / "data" / "reviews.sqlite3")
    for shop in (first_shop, second_shop):
        database.sync_shop(shop.key, shop.name, shop.url)
    for snapshot in snapshots:
        database.upsert_review(snapshot, notify=False)
    current_reviews = {
        (item["shop_key"], item["review_id"]): item
        for item in database.iter_reviews()
    }
    run_id = database.start_analysis("evidence-test", {}, len(snapshots))
    group_id = "cross-evidence-v2"
    reviews = [
        {
            "shop_key": item.shop_key,
            "review_id": item.review_id,
            "text_hash": analysis_input_hash(
                current_reviews[(item.shop_key, item.review_id)],
                project_root=tmp_path,
                media_root=tmp_path / "data" / "media",
            ),
            "label": "suspected",
            "direction": "positive",
            "max_lexical": 0.96,
            "max_semantic": 0.98,
        }
        for item in snapshots
    ]
    evidence = {
        "schema_version": 2,
        "level": "suspicious_high",
        "score": 75,
        "score_breakdown": [
            {"key": "content_candidate", "title": "內容相似候選", "points": 20, "max_points": 20},
            {"key": "group_cohesion", "title": "群組相似門檻餘裕", "points": 10, "max_points": 10},
            {"key": "reviewer_cohort", "title": "跨店評論者群組", "points": 25, "max_points": 25},
            {"key": "shared_photo", "title": "跨帳號共用照片", "points": 20, "max_points": 20},
        ],
        "summary": ["命中兩類獨立訊號。", "建議優先人工複核。"],
        "signals": [
            {
                "key": "reviewer_cohort",
                "family": "reviewer_network",
                "title": "跨店評論者群組",
                "matched": True,
                "status": "matched",
                "independent_for_level": True,
                "detail": "三個可確認個人檔案共同出現在相同店家組合。",
                "observed": {
                    "largest_cohort_profile_count": 3,
                    "largest_cohort_shop_count": 2,
                },
            },
            {
                "key": "shared_photo",
                "family": "media",
                "title": "跨帳號共用照片",
                "matched": True,
                "status": "matched",
                "independent_for_level": True,
                "detail": "三個不同個人檔案共用相同照片。",
                "observed": {
                    "shared_photo_count": 1,
                    "largest_distinct_profile_count": 3,
                    "shared_matches": [
                        {
                            "fingerprint": "abc123def456",
                            "distinct_profile_count": 3,
                            "members": [
                                {
                                    "author": item.author,
                                    "shop_name": item.shop_name,
                                    "shop_key": item.shop_key,
                                    "review_id": item.review_id,
                                    "photo_paths": [item.photo_paths[0]],
                                }
                                for item in snapshots
                            ],
                        }
                    ]
                },
            },
        ],
        "metrics": {
            "min_lexical": 0.9,
            "median_lexical": 0.93,
            "max_lexical": 0.96,
            "min_semantic": 0.95,
            "median_semantic": 0.965,
            "max_semantic": 0.98,
            "direct_pairs": 2,
            "possible_pairs": 2,
            "pair_density": 1.0,
            "distinct_profiles": 3,
            "date_known_count": 3,
            "date_precise_count": 3,
            "date_total": 3,
            "review_count": 3,
        },
        "coverage": {
            "profiles": {"known": 3, "total": 3, "percent": 100},
            "dates": {"known": 3, "total": 3, "percent": 100},
        },
    }
    members = [
        {
            "shop_key": item.shop_key,
            "review_id": item.review_id,
            "max_lexical": 0.96,
            "max_semantic": 0.98,
        }
        for item in snapshots
    ]
    pairs = [
        {
            "scope": "cross",
            "left_shop_key": first_shop.key,
            "left_review_id": "a1",
            "right_shop_key": second_shop.key,
            "right_review_id": review_id,
            "lexical": lexical,
            "semantic": semantic,
        }
        for review_id, lexical, semantic in (("b1", 0.9, 0.95), ("b2", 0.96, 0.98))
    ]
    database.complete_analysis(
        run_id,
        reviews,
        [
            {
                "group_id": group_id,
                "scope": "cross",
                "fingerprint": "evidence-v2",
                "label": "suspected",
                "direction": "positive",
                "review_count": 3,
                "shop_count": 2,
                "max_lexical": 0.96,
                "max_semantic": 0.98,
                "latest_at": None,
                "evidence": evidence,
                "members": members,
            }
        ],
        pairs,
    )
    database.close()

    client = TestClient(create_app(str(config)))
    listing = client.get("/analysis?scope=cross")
    assert listing.status_code == 200
    assert "高優先複核" in listing.text
    assert "命中兩類獨立訊號；建議優先人工複核。" in listing.text
    detail = client.get(f"/analysis/groups/{group_id}")
    assert detail.status_code == 200
    assert "證據模型 v2" in detail.text
    assert "群組密度" in detail.text
    assert "2/2" in detail.text
    assert "最低" in detail.text and "中位" in detail.text and "最高" in detail.text
    assert "跨店評論者群組" in detail.text
    assert "跨帳號共用照片" in detail.text
    assert "照片指紋 abc123def456" in detail.text
    evidence_start = detail.text.index('<div class="shared-photo-evidence"')
    evidence_end = detail.text.index("</div>", evidence_start)
    matched_photo_html = detail.text[evidence_start:evidence_end]
    for item in snapshots:
        assert item.photo_paths[0].replace("\\", "/") in matched_photo_html
        assert item.photo_paths[1].replace("\\", "/") not in matched_photo_html
    assert "不是造假機率" in detail.text
    assert "查看證據分數組成" in detail.text
    assert "內容相似候選" in detail.text
    assert "推定發文時間" in detail.text
    assert "Google 相對時間回推" in detail.text
    assert "可點擊放大" in detail.text
    assert detail.text.count("evidence-review-media") == 3

    # Editing one member invalidates the complete snapshot. No sibling may retain
    # the old badge or a link that now returns 404.
    changed = snapshots[0]
    changed.text = "此評論編輯後已經完全不同"
    database = Database(tmp_path / "data" / "reviews.sqlite3")
    database.upsert_review(changed, notify=False)
    remaining = database.review_analysis(second_shop.key)
    assert remaining[(second_shop.key, "b1")]["label"] == ""
    database.close()
    shop_page = client.get(f"/shops/{second_shop.key}")
    assert shop_page.status_code == 200
    assert f'/analysis/groups/{group_id}' not in shop_page.text
