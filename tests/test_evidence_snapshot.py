from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from PIL import Image

from maps_review_monitor.database import Database
from maps_review_monitor.evidence_inputs import analysis_input_hash
from maps_review_monitor.models import ProfileSummary, ReviewSnapshot


def _snapshot(shop: str, review_id: str, contributor: str) -> ReviewSnapshot:
    return ReviewSnapshot(
        review_id=review_id,
        shop_key=shop,
        shop_name=f"店家 {shop}",
        shop_url="https://www.google.com/maps/place/example",
        author=f"評論者 {contributor}",
        stars=5,
        time_text="1 小時前",
        text="服務親切環境舒適值得再次光臨",
        profile=ProfileSummary(
            url=f"https://www.google.com/maps/contrib/{contributor}/reviews?hl=zh-TW"
        ),
        photo_paths=[f"data/media/{shop}/{review_id}/photo.png"],
    )


def _complete_v2_group(db: Database, *, valid_high: bool = True) -> str:
    current = list(db.iter_reviews())
    run_id = db.start_analysis("evidence-v2-test", {"evidence_model_version": 2}, len(current))
    group_id = "cross-v2-snapshot"
    signals = [
        {
            "key": "reviewer_cohort",
            "family": "reviewer_network",
            "title": "跨店評論者群組",
            "matched": True,
            "status": "matched",
            "independent_for_level": True,
            "observed": {
                "largest_cohort_profile_count": 2,
                "largest_cohort_shop_count": 2,
            },
        }
    ]
    if valid_high:
        signals.append(
            {
                "key": "shared_photo",
                "family": "media",
                "title": "跨帳號共用照片",
                "matched": True,
                "status": "matched",
                "independent_for_level": True,
                "observed": {
                    "shared_photo_count": 1,
                    "largest_distinct_profile_count": 2,
                },
            }
        )
    evidence = {
        "schema_version": 2,
        "level": "suspicious_high",
        "score": 75,
        "signals": signals,
        "metrics": {"review_count": 3, "shop_count": 2},
        "coverage": {
            "profile_known_count": 3,
            "profile_total": 3,
            "distinct_profiles": 3,
        },
    }
    db.complete_analysis(
        run_id,
        [
            {
                "shop_key": item["shop_key"],
                "review_id": item["review_id"],
                "text_hash": analysis_input_hash(
                    item,
                    project_root=db.project_root,
                    media_root=db.media_root,
                ),
            }
            for item in current
        ],
        [
            {
                "group_id": group_id,
                "scope": "cross",
                "fingerprint": "fixture",
                "label": "suspected",
                "direction": "positive",
                "review_count": 3,
                "shop_count": 2,
                "max_lexical": 1,
                "max_semantic": 1,
                "evidence": evidence,
                "members": [
                    {
                        "shop_key": item["shop_key"],
                        "review_id": item["review_id"],
                        "max_lexical": 1,
                        "max_semantic": 1,
                    }
                    for item in current
                ],
            }
        ],
        [],
    )
    return group_id


@pytest.mark.parametrize(
    "changed_input",
    ["author", "profile", "photo_content", "photo_paths", "stars", "time"],
)
def test_v2_group_expires_when_any_evidence_input_changes(
    tmp_path: Path, changed_input: str
):
    project_root = tmp_path / changed_input
    db = Database(
        project_root / "data" / "reviews.sqlite3", project_root=project_root
    )
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        snapshots = (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        )
        for item in snapshots:
            photo = project_root / item.photo_paths[0]
            photo.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (3, 3), (20, 120, 80)).save(photo, format="PNG")
            db.upsert_review(item, notify=False, observed_at="2026-09-06T12:00:00+08:00")
        group_id = _complete_v2_group(db)
        assert db.analysis_groups(scope="cross")[0]["label"] == "suspected"

        changed = db.get_review("a", "1")
        assert changed is not None
        if changed_input == "author":
            changed.author = "更名後的評論者"
            db.upsert_review(changed, notify=False, observed_at="2026-09-06T13:00:00+08:00")
        elif changed_input == "profile":
            changed.profile.url = "https://www.google.com/maps/contrib/999/reviews"
            db.upsert_review(changed, notify=False, observed_at="2026-09-06T13:00:00+08:00")
        elif changed_input == "photo_content":
            photo = project_root / changed.photo_paths[0]
            Image.new("RGB", (3, 3), (180, 20, 40)).save(photo, format="PNG")
        elif changed_input == "photo_paths":
            changed.photo_paths = ["data/media/a/1/replacement.png"]
            db.upsert_review(changed, notify=False, observed_at="2026-09-06T13:00:00+08:00")
        elif changed_input == "stars":
            changed.stars = 4
            db.upsert_review(changed, notify=False, observed_at="2026-09-06T13:00:00+08:00")
        else:
            db.conn.execute(
                """UPDATE reviews SET back_calculated_at=?
                   WHERE shop_key='a' AND review_id='1'""",
                ("2026-09-05T08:00:00+08:00",),
            )
            db.conn.commit()

        assert db.analysis_groups(scope="cross") == []
        assert db.analysis_group(group_id) is None
        remaining = db.review_analysis()
        assert remaining[("b", "2")]["label"] == ""
        assert ("a", "1") not in remaining
    finally:
        db.close()


def test_inconsistent_v2_high_declaration_is_downgraded(tmp_path: Path):
    db = Database(tmp_path / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for item in (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ):
            db.upsert_review(item, notify=False, observed_at="2026-09-06T12:00:00+08:00")
        _complete_v2_group(db, valid_high=False)

        group = db.analysis_groups(scope="cross")[0]
        assert group["label"] == "highly_similar"
        assert group["evidence"]["level"] == "review"
        assert group["evidence"]["decision"]["high_eligible"] is False
        assert group["evidence"]["decision"]["independent_signal_families"] == [
            "reviewer_network"
        ]
        assert "已自動降級" in group["evidence"]["summary"]
    finally:
        db.close()


def test_v2_group_expires_when_one_of_duplicate_photo_paths_disappears(
    tmp_path: Path,
):
    project_root = tmp_path / "duplicate-photo-path"
    db = Database(
        project_root / "data" / "reviews.sqlite3", project_root=project_root
    )
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        snapshots = [
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ]
        snapshots[0].photo_paths.append("data/media/a/1/duplicate.png")
        for item in snapshots:
            first = project_root / item.photo_paths[0]
            first.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (3, 3), (20, 120, 80)).save(first, format="PNG")
            for duplicate in item.photo_paths[1:]:
                target = project_root / duplicate
                target.write_bytes(first.read_bytes())
            db.upsert_review(item, notify=False, observed_at="2026-09-06T12:00:00+08:00")
        group_id = _complete_v2_group(db)
        assert db.analysis_group(group_id) is not None

        (project_root / snapshots[0].photo_paths[1]).unlink()

        assert db.analysis_group(group_id) is None
        assert db.analysis_groups(scope="cross") == []
    finally:
        db.close()


def test_stored_profile_coverage_cannot_fake_v2_high_priority(tmp_path: Path):
    db = Database(tmp_path / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for shop, review_id in (("a", "1"), ("b", "2"), ("b", "3")):
            db.upsert_review(
                _snapshot(shop, review_id, "100"),
                notify=False,
                observed_at="2026-09-06T12:00:00+08:00",
            )
        _complete_v2_group(db)

        group = db.analysis_groups(scope="cross")[0]
        assert group["label"] == "highly_similar"
        assert group["evidence"]["coverage"]["distinct_profiles"] == 1
        assert group["evidence"]["decision"]["high_eligible"] is False
    finally:
        db.close()


@pytest.mark.parametrize("inconsistency", ["unknown_keys", "contradictory_status"])
def test_stored_signal_claims_must_satisfy_v2_contract(
    tmp_path: Path, inconsistency: str
):
    db = Database(tmp_path / inconsistency / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for item in (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ):
            db.upsert_review(item, notify=False)
        _complete_v2_group(db)
        row = db.conn.execute(
            "SELECT evidence_json FROM analysis_groups LIMIT 1"
        ).fetchone()
        evidence = json.loads(row["evidence_json"])
        if inconsistency == "unknown_keys":
            evidence["signals"] = [
                {
                    "key": "text_similarity",
                    "family": "reviewer_network",
                    "matched": True,
                    "status": "matched",
                    "independent_for_level": True,
                    "observed": {
                        "largest_cohort_profile_count": 3,
                        "largest_cohort_shop_count": 2,
                    },
                },
                {
                    "key": "star_direction",
                    "family": "media",
                    "matched": True,
                    "status": "matched",
                    "independent_for_level": True,
                    "observed": {
                        "shared_photo_count": 1,
                        "largest_distinct_profile_count": 3,
                    },
                },
            ]
        else:
            for signal in evidence["signals"]:
                signal["status"] = "not_matched"
        db.conn.execute(
            "UPDATE analysis_groups SET evidence_json=?",
            (json.dumps(evidence, ensure_ascii=False),),
        )
        db.conn.commit()

        group = db.analysis_groups(scope="cross")[0]
        assert group["label"] == "highly_similar"
        assert group["evidence"]["decision"]["high_eligible"] is False
        assert group["evidence"]["decision"]["independent_signal_families"] == []
        assert group["evidence"]["supporting"] == []
        assert group["evidence"]["score"] is None
        assert group["evidence"]["score_breakdown"] == []
    finally:
        db.close()


def test_same_shop_scope_cannot_use_cross_shop_reviewer_signal_for_high(
    tmp_path: Path,
):
    db = Database(tmp_path / "scope" / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for item in (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ):
            db.upsert_review(item, notify=False)
        group_id = _complete_v2_group(db)
        db.conn.execute(
            "UPDATE analysis_groups SET scope='same' WHERE group_id=?", (group_id,)
        )
        db.conn.commit()

        listed = db.analysis_groups(scope="same")[0]
        assert listed["label"] == "highly_similar"
        assert listed["evidence"]["decision"]["independent_signal_families"] == [
            "media"
        ]
        assert all(
            value["label"] == "highly_similar"
            for value in db.review_analysis().values()
        )
        assert all(
            counts["suspected"] == 0
            for counts in db.analysis_shop_counts().values()
        )
        detailed = db.analysis_group(group_id)
        assert detailed is not None
        assert detailed[0]["label"] == "highly_similar"
    finally:
        db.close()


def test_unknown_future_evidence_schema_fails_closed(tmp_path: Path):
    db = Database(tmp_path / "future" / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for item in (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ):
            db.upsert_review(item, notify=False)
        _complete_v2_group(db)
        row = db.conn.execute(
            "SELECT evidence_json FROM analysis_groups LIMIT 1"
        ).fetchone()
        evidence = json.loads(row["evidence_json"])
        evidence["schema_version"] = 999
        evidence["summary"] = "已證實高風險"
        db.conn.execute(
            "UPDATE analysis_groups SET evidence_json=?",
            (json.dumps(evidence, ensure_ascii=False),),
        )
        db.conn.commit()

        group = db.analysis_groups(scope="cross")[0]
        assert group["label"] == "highly_similar"
        assert group["evidence"]["level"] == "review"
        assert group["evidence"]["unsupported_version"] is True
        assert group["evidence"]["version_label"] == "未知版本證據 v999"
        assert "已證實高風險" not in group["evidence"]["summary"]
        assert "已安全降級" in group["evidence"]["summary"]
        assert group["evidence"]["supporting"] == []
        assert group["evidence"]["decision"]["independent_signal_count"] == 0
        assert group["evidence"]["score"] is None
    finally:
        db.close()


def test_v2_evidence_with_legacy_member_fingerprint_disables_all_strong_claims(
    tmp_path: Path,
):
    db = Database(tmp_path / "mixed-fingerprint" / "reviews.sqlite3")
    try:
        for shop in ("a", "b"):
            db.sync_shop(shop, f"店家 {shop}", "https://www.google.com/maps/place/example")
        for item in (
            _snapshot("a", "1", "100"),
            _snapshot("b", "2", "200"),
            _snapshot("b", "3", "300"),
        ):
            db.upsert_review(item, notify=False)
        group_id = _complete_v2_group(db)
        current = db.get_review("a", "1")
        assert current is not None
        db.conn.execute(
            """UPDATE analysis_reviews SET text_hash=?
               WHERE shop_key='a' AND review_id='1'""",
            (sha256(current.text.encode("utf-8")).hexdigest(),),
        )
        db.conn.commit()

        listed = db.analysis_groups(scope="cross")[0]
        assert listed["label"] == "highly_similar"
        assert listed["evidence"]["level"] == "content_similar"
        assert listed["evidence"]["supporting"] == []
        assert listed["evidence"]["score"] is None
        assert listed["evidence"]["decision"]["v2_snapshot_complete"] is False
        assert all(
            counts["suspected"] == 0
            for counts in db.analysis_shop_counts().values()
        )
        assert all(
            item["label"] == "highly_similar"
            for item in db.review_analysis().values()
        )
        detailed = db.analysis_group(group_id)
        assert detailed is not None
        assert detailed[0]["label"] == "highly_similar"
        assert detailed[0]["evidence"]["supporting"] == []

        current.profile.url = "https://www.google.com/maps/contrib/999/reviews"
        db.upsert_review(current, notify=False)

        # The legacy text hash cannot detect this profile mutation, so the
        # group may remain as a text candidate but can never regain v2 claims.
        after = db.analysis_groups(scope="cross")[0]
        assert after["label"] == "highly_similar"
        assert after["evidence"]["decision"]["independent_signal_count"] == 0
        assert after["evidence"]["score"] is None
    finally:
        db.close()
