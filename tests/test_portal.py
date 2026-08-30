from pathlib import Path

import pytest
from fastapi.testclient import TestClient

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
    assert "顯示疑似協同評論" in home.text
    assert "hide-highly-similar" in home.text
    assert "hide-suspected" in home.text
    assert "版本 0.5.0" in home.text
    assert 'href="/reviewers"' in home.text
    assert 'id="global-search-input"' in home.text
    css = client.get("/static/portal.css")
    assert css.status_code == 200
    assert ".hide-highly-similar .similar-only" in css.text
    assert ".hide-suspected .suspected-only" in css.text
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
    search = client.get("/search?q=測試")
    assert search.status_code == 200
    assert "搜尋" in search.text


def test_same_name_groups_are_cross_shop_and_do_not_assume_identity():
    reviews = [
        {
            "author": "王 小明",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "1",
            "first_seen_at": "2026-07-01",
            "profile": {"url": "https://example.com/profile/1"},
        },
        {
            "author": "王　小明",
            "shop_key": "b",
            "shop_name": "乙店",
            "review_id": "2",
            "first_seen_at": "2026-07-02",
            "profile": {"url": "https://example.com/profile/2"},
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
