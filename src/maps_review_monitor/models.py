from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any


@dataclass(slots=True)
class ProfileSummary:
    url: str = ""
    avatar_url: str = ""
    avatar_path: str = ""
    local_guide: bool = False
    review_count: int | None = None
    photo_count: int | None = None


@dataclass(slots=True)
class OwnerReply:
    text: str = ""
    time_text: str = ""


@dataclass(slots=True)
class ReviewSnapshot:
    review_id: str
    shop_key: str
    shop_name: str
    shop_url: str
    author: str
    stars: float | None
    time_text: str
    text: str
    profile: ProfileSummary = field(default_factory=ProfileSummary)
    photo_urls: list[str] = field(default_factory=list)
    photo_paths: list[str] = field(default_factory=list)
    owner_reply: OwnerReply | None = None
    back_calculated_at: str = ""
    estimated_posted_date: str = ""
    time_parse_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewSnapshot":
        value = dict(data)
        value["profile"] = ProfileSummary(**value.get("profile", {}))
        reply = value.get("owner_reply")
        value["owner_reply"] = OwnerReply(**reply) if reply else None
        return cls(**value)

    def content_hash(self) -> str:
        return _hash({
            "author": self.author,
            "stars": self.stars,
            "text": self.text,
            "profile_url": self.profile.url,
            "photo_urls": self.photo_urls,
        })

    def reply_hash(self) -> str:
        return _hash(self.owner_reply.text if self.owner_reply else None)


def _hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
