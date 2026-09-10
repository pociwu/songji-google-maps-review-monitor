from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from maps_review_monitor.models import ProfileSummary, ReviewSnapshot
from maps_review_monitor.scraper import (
    MapsScraper,
    merge_profile_summaries,
    parse_count,
    parse_star_label,
    profile_identity_status,
    stored_photo_paths_available,
    verified_image_suffix,
    with_locale,
)


def test_star_label_locales():
    assert parse_star_label("5 顆星") == 5
    assert parse_star_label("Rated 4.5 stars") == 4.5
    assert parse_star_label("") is None


def test_profile_counts():
    assert parse_count("Local Guide · 123 則評論 · 45 張照片", r"則評論|評論|reviews?") == 123
    assert parse_count("18 reviews · 3 photos", r"則評論|評論|reviews?") == 18


def test_locale_query_preserves_existing_values():
    url = with_locale("https://www.google.com/maps/place/x?entry=ttu", "zh-TW")
    assert "entry=ttu" in url and "hl=zh-TW" in url


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (3, 2), (20, 40, 60)).save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, content_type: str, content: bytes):
        self.headers = {"content-type": content_type}
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _Http:
    def __init__(self, response: _Response):
        self.response = response

    def get(self, _url: str) -> _Response:
        return self.response


def _downloader(response: _Response) -> MapsScraper:
    scraper = object.__new__(MapsScraper)
    scraper.http = _Http(response)
    return scraper


def _review(review_id: str = "r1") -> ReviewSnapshot:
    return ReviewSnapshot(
        review_id=review_id,
        shop_key="shop",
        shop_name="店家",
        shop_url="https://www.google.com/maps/place/shop",
        author="小明",
        stars=5,
        time_text="1 天前",
        text="很好",
    )


def test_verified_image_suffix_uses_decoded_format_not_header():
    assert verified_image_suffix("image/jpeg; charset=binary", _png_bytes()) == ".png"


def test_download_rejects_html_even_when_body_contains_valid_image(tmp_path: Path):
    scraper = _downloader(_Response("text/html", _png_bytes()))

    assert scraper._download("https://example.test/photo", tmp_path / "photo") is None
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_corrupt_image_without_writing_file(tmp_path: Path):
    scraper = _downloader(_Response("image/jpeg", b"not-an-image"))

    assert scraper._download("https://example.test/photo", tmp_path / "photo") is None
    assert list(tmp_path.iterdir()) == []


def test_download_writes_verified_image_with_actual_suffix(tmp_path: Path):
    payload = _png_bytes()
    scraper = _downloader(_Response("image/jpeg", payload))

    path = scraper._download("https://example.test/photo", tmp_path / "photo")

    assert path == tmp_path / "photo.png"
    assert path.read_bytes() == payload


def test_profile_merge_backfills_url_without_losing_saved_summary():
    existing = ProfileSummary(
        local_guide=True,
        review_count=21,
        photo_count=8,
        avatar_path="data/media/avatar.jpg",
    )
    discovered = ProfileSummary(
        url="https://www.google.com/maps/contrib/123/reviews",
        avatar_url="https://example.test/current-avatar.jpg",
    )

    merged = merge_profile_summaries(existing, discovered)

    assert profile_identity_status(existing, discovered) == "new_verified_id"
    assert merged.url == discovered.url
    assert merged.avatar_url == discovered.avatar_url
    assert merged.avatar_path == existing.avatar_path
    assert merged.local_guide is True
    assert merged.review_count == 21
    assert merged.photo_count == 8


def test_existing_review_enrichment_preserves_old_fields_when_new_page_is_partial(
    tmp_path: Path,
):
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    enriched_urls: list[str] = []
    downloads: list[str] = []

    def enrich(review: ReviewSnapshot) -> None:
        enriched_urls.append(review.profile.url)
        review.profile.review_count = None
        review.profile.photo_count = 13
        review.profile.local_guide = False

    scraper._enrich_profile = enrich
    scraper._download_assets = lambda review: downloads.append(review.review_id)

    existing = _review()
    existing.profile = ProfileSummary(local_guide=True, review_count=21, photo_count=8)
    current = _review()
    current.profile = ProfileSummary(url="https://www.google.com/maps/contrib/123/reviews")

    scraper._merge_existing_review(current, existing)

    assert enriched_urls == [current.profile.url]
    assert downloads == []
    assert current.profile.local_guide is True
    assert current.profile.review_count == 21
    assert current.profile.photo_count == 13


def test_same_remote_photos_are_downloaded_again_when_local_file_is_missing(
    tmp_path: Path,
):
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    scraper._enrich_profile = lambda _review: None
    downloads: list[str] = []
    scraper._download_assets = lambda review: downloads.append(review.review_id)

    existing = _review()
    existing.photo_urls = ["https://example.test/photo.jpg"]
    existing.photo_paths = ["data/media/missing.jpg"]
    current = _review()
    current.photo_urls = list(existing.photo_urls)

    assert not stored_photo_paths_available(
        tmp_path, existing.photo_urls, existing.photo_paths
    )
    scraper._merge_existing_review(current, existing)

    assert downloads == [current.review_id]
    assert current.photo_paths == []


def test_same_remote_photos_reuse_complete_local_files(tmp_path: Path):
    saved = tmp_path / "data" / "media" / "photo.jpg"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(_png_bytes())

    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    scraper._enrich_profile = lambda _review: None
    downloads: list[str] = []
    scraper._download_assets = lambda review: downloads.append(review.review_id)

    existing = _review()
    existing.photo_urls = ["https://example.test/photo.jpg"]
    existing.photo_paths = ["data/media/photo.jpg"]
    current = _review()
    current.photo_urls = list(existing.photo_urls)

    scraper._merge_existing_review(current, existing)

    assert downloads == []
    assert current.photo_paths == existing.photo_paths
    assert current.photo_paths is not existing.photo_paths


def test_same_canonical_profile_variant_preserves_summary_without_enrichment(
    tmp_path: Path,
):
    saved_avatar = tmp_path / "data" / "media" / "old-avatar.jpg"
    saved_avatar.parent.mkdir(parents=True)
    saved_avatar.write_bytes(_png_bytes())
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    enrichment_calls: list[str] = []
    scraper._enrich_profile = lambda review: enrichment_calls.append(review.profile.url)
    scraper._download_assets = lambda _review: None

    existing = _review()
    existing.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/ABC123/reviews?hl=zh-TW",
        avatar_path="data/media/old-avatar.jpg",
        local_guide=True,
        review_count=30,
        photo_count=12,
    )
    current = _review()
    current.profile = ProfileSummary(
        url="https://maps.google.com/maps/contrib/abc123?entry=ttu"
    )

    assert profile_identity_status(existing.profile, current.profile) == "same_verified_id"
    scraper._merge_existing_review(current, existing)

    assert enrichment_calls == []
    assert current.profile.url == "https://maps.google.com/maps/contrib/abc123?entry=ttu"
    assert current.profile.local_guide is True
    assert current.profile.review_count == 30
    assert current.profile.photo_count == 12
    assert current.profile.avatar_path == "data/media/old-avatar.jpg"


@pytest.mark.parametrize("cached_payload", [None, b"legacy-html-error-body"])
def test_same_canonical_profile_repairs_missing_or_invalid_avatar_cache(
    tmp_path: Path, cached_payload: bytes | None
):
    cached = tmp_path / "data" / "media" / "old-avatar.jpg"
    if cached_payload is not None:
        cached.parent.mkdir(parents=True)
        cached.write_bytes(cached_payload)

    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    scraper._enrich_profile = lambda _review: None
    scraper._download_assets = lambda _review: None
    downloads: list[str] = []

    def repair_avatar(review: ReviewSnapshot) -> None:
        downloads.append(review.profile.avatar_url)
        review.profile.avatar_path = "data/media/repaired-avatar.png"

    scraper._download_avatar = repair_avatar
    existing = _review()
    existing.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/ABC123/reviews",
        avatar_url="https://example.test/avatar.png",
        avatar_path="data/media/old-avatar.jpg",
        local_guide=True,
        review_count=30,
    )
    current = _review()
    current.profile = ProfileSummary(
        url="https://maps.google.com/maps/contrib/abc123?entry=ttu"
    )

    scraper._merge_existing_review(current, existing)

    assert downloads == ["https://example.test/avatar.png"]
    assert current.profile.avatar_path == "data/media/repaired-avatar.png"
    assert current.profile.local_guide is True
    assert current.profile.review_count == 30


def test_changed_canonical_profile_drops_old_summary_and_enriches_new_identity(
    tmp_path: Path,
):
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    enrichment_calls: list[str] = []

    def enrich(review: ReviewSnapshot) -> None:
        enrichment_calls.append(review.profile.url)
        review.profile.review_count = 4

    scraper._enrich_profile = enrich
    scraper._download_assets = lambda _review: None
    scraper._download_avatar = lambda _review: None

    existing = _review()
    existing.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/100/reviews",
        avatar_path="data/media/person-100.jpg",
        local_guide=True,
        review_count=99,
        photo_count=88,
    )
    current = _review()
    current.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/200/reviews"
    )

    assert profile_identity_status(existing.profile, current.profile) == "changed_verified_id"
    scraper._merge_existing_review(current, existing)

    assert enrichment_calls == ["https://www.google.com/maps/contrib/200/reviews"]
    assert current.profile.url.endswith("/contrib/200/reviews")
    assert current.profile.local_guide is False
    assert current.profile.review_count == 4
    assert current.profile.photo_count is None
    assert current.profile.avatar_path == ""


def test_untrusted_current_profile_url_does_not_reuse_or_enrich_old_identity(
    tmp_path: Path,
):
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    enrichment_calls: list[str] = []
    scraper._enrich_profile = lambda review: enrichment_calls.append(review.profile.url)
    scraper._download_assets = lambda _review: None

    existing = _review()
    existing.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/100/reviews",
        avatar_path="data/media/person-100.jpg",
        local_guide=True,
        review_count=99,
        photo_count=88,
    )
    current = _review()
    current.profile = ProfileSummary(
        url="https://example.test/maps/contrib/100/reviews",
        avatar_url="https://example.test/avatar.jpg",
    )

    assert profile_identity_status(existing.profile, current.profile) == "untrusted_current_url"
    scraper._merge_existing_review(current, existing)

    assert enrichment_calls == []
    assert current.profile == ProfileSummary()


def test_unparseable_old_url_cannot_donate_summary_to_verified_current_id(
    tmp_path: Path,
):
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    enrichment_calls: list[str] = []
    scraper._enrich_profile = lambda review: enrichment_calls.append(review.profile.url)
    scraper._download_assets = lambda _review: None
    scraper._download_avatar = lambda _review: None

    existing = _review()
    existing.profile = ProfileSummary(
        url="not a contributor URL",
        avatar_path="data/media/unknown.jpg",
        local_guide=True,
        review_count=77,
    )
    current = _review()
    current.profile = ProfileSummary(
        url="https://www.google.com/maps/contrib/300/reviews"
    )

    assert profile_identity_status(existing.profile, current.profile) == "changed_verified_id"
    scraper._merge_existing_review(current, existing)

    assert enrichment_calls == ["https://www.google.com/maps/contrib/300/reviews"]
    assert current.profile.local_guide is False
    assert current.profile.review_count is None
    assert current.profile.avatar_path == ""


def test_enrichment_refuses_non_google_profile_url():
    scraper = object.__new__(MapsScraper)
    current = _review()
    current.profile.url = "https://example.test/maps/contrib/123/reviews"

    # The canonical URL guard returns before browser state is accessed.
    scraper._enrich_profile(current)


def test_same_remote_photos_replace_invalid_legacy_cache(tmp_path: Path):
    saved = tmp_path / "data" / "media" / "photo.jpg"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(b"legacy-html-error-body")

    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(root=tmp_path)
    scraper._enrich_profile = lambda _review: None
    downloads: list[str] = []
    scraper._download_assets = lambda review: downloads.append(review.review_id)

    existing = _review()
    existing.photo_urls = ["https://example.test/photo.jpg"]
    existing.photo_paths = ["data/media/photo.jpg"]
    current = _review()
    current.photo_urls = list(existing.photo_urls)

    scraper._merge_existing_review(current, existing)

    assert downloads == [current.review_id]
