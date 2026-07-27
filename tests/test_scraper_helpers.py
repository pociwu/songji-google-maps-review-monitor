from maps_review_monitor.scraper import parse_count, parse_star_label, with_locale


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

