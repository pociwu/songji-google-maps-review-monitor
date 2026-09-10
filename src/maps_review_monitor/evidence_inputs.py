"""Stable identities and fingerprints used by evidence-model snapshots."""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit
import warnings

from PIL import Image


CONTRIBUTOR_PATH_RE = re.compile(
    r"(?:^|/)contrib/([A-Za-z0-9_-]+)(?:/|$)", re.IGNORECASE
)
GOOGLE_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*google\.(?:com|com\.[a-z]{2}|co\.[a-z]{2}|[a-z]{2})$",
    re.IGNORECASE,
)
ANALYSIS_INPUT_VERSION = "v2"
MAX_EVIDENCE_PHOTO_BYTES = 25 * 1024 * 1024


@lru_cache(maxsize=4096)
def _verified_photo_digest(
    path_value: str,
    device: int,
    inode: int,
    size: int,
    modified_ns: int,
    changed_ns: int,
) -> str | None:
    """Decode and hash one immutable filesystem version of an image."""
    del device, inode, size, modified_ns, changed_ns  # values form the cache key
    target = Path(path_value)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(target) as image:
                image.verify()
            with Image.open(target) as image:
                image.load()
        digest = sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def canonical_contributor_id(url: str) -> str:
    """Return the stable Google Maps contributor id without URL variants."""
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "https" or not GOOGLE_HOST_RE.fullmatch(
            str(parsed.hostname or "")
        ):
            return ""
        path = unquote(parsed.path)
    except ValueError:
        return ""
    match = CONTRIBUTOR_PATH_RE.search(path)
    return match.group(1).casefold() if match else ""


def verified_local_photo_entries(
    review: dict[str, Any],
    *,
    project_root: Path | None,
    media_root: Path | None,
) -> list[dict[str, str]]:
    """Return path/hash pairs only for decoded images in the media allowlist."""
    if project_root is None or media_root is None:
        return []
    try:
        base = project_root.resolve()
        allowed_root = media_root.resolve()
    except OSError:
        return []

    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for stored_path in review.get("photo_paths") or []:
        path = Path(str(stored_path))
        target = path if path.is_absolute() else base / path
        try:
            target = target.resolve()
            if allowed_root != target.parent and allowed_root not in target.parents:
                continue
            if not target.is_file():
                continue
            stat = target.stat()
            if stat.st_size > MAX_EVIDENCE_PHOTO_BYTES:
                continue
            digest = _verified_photo_digest(
                str(target),
                int(stat.st_dev),
                int(stat.st_ino),
                int(stat.st_size),
                int(stat.st_mtime_ns),
                int(stat.st_ctime_ns),
            )
            if digest:
                entry = (str(stored_path), digest)
                if entry not in seen:
                    seen.add(entry)
                    entries.append({"path": entry[0], "hash": entry[1]})
        except (
            OSError,
            SyntaxError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            continue
    return sorted(entries, key=lambda item: (item["hash"], item["path"]))


def verified_local_photo_hashes(
    review: dict[str, Any],
    *,
    project_root: Path | None,
    media_root: Path | None,
) -> list[str]:
    """Hash only fully decoded images contained by the configured media root."""
    return sorted(
        {
            item["hash"]
            for item in verified_local_photo_entries(
                review, project_root=project_root, media_root=media_root
            )
        }
    )


def analysis_input_hash(
    review: dict[str, Any],
    *,
    project_root: Path | None = None,
    media_root: Path | None = None,
) -> str:
    """Hash every persisted value that can change a v2 group or its evidence."""
    profile = review.get("profile") or {}
    contributor_id = str(review.get("contributor_id") or "").strip()
    if not contributor_id:
        contributor_id = canonical_contributor_id(str(profile.get("url", "")))
    raw_stars = review.get("stars")
    try:
        stars = float(raw_stars) if raw_stars is not None else None
    except (TypeError, ValueError):
        stars = str(raw_stars)
    supplied_photo_entries = review.get("photo_hash_entries")
    if isinstance(supplied_photo_entries, list):
        photo_entries = sorted(
            [
                {
                    "path": str(item.get("path") or ""),
                    "hash": str(item.get("hash") or ""),
                }
                for item in supplied_photo_entries
                if isinstance(item, dict) and item.get("hash")
            ],
            key=lambda item: (item["hash"], item["path"]),
        )
    else:
        photo_entries = verified_local_photo_entries(
            review, project_root=project_root, media_root=media_root
        )
    photo_hashes = sorted({item["hash"] for item in photo_entries})
    payload = {
        "author": str(review.get("author", "")),
        "text": str(review.get("text", "")),
        "stars": stars,
        "contributor_id": contributor_id,
        "back_calculated_at": str(review.get("back_calculated_at") or ""),
        "estimated_posted_date": str(review.get("estimated_posted_date") or ""),
        "time_relative_unit": str(review.get("time_relative_unit") or ""),
        "photo_paths": sorted(str(path) for path in (review.get("photo_paths") or [])),
        "photo_hash_entries": photo_entries,
        "photo_hashes": photo_hashes,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{ANALYSIS_INPUT_VERSION}:" + sha256(encoded).hexdigest()


def analysis_input_matches(
    stored_hash: str,
    review: dict[str, Any],
    *,
    project_root: Path | None = None,
    media_root: Path | None = None,
) -> bool:
    """Validate v2 fingerprints while retaining old text-only snapshots."""
    value = str(stored_hash or "")
    if value.startswith(f"{ANALYSIS_INPUT_VERSION}:"):
        return value == analysis_input_hash(
            review, project_root=project_root, media_root=media_root
        )
    legacy_text_hash = sha256(str(review.get("text", "")).encode("utf-8")).hexdigest()
    return value == legacy_text_hash
