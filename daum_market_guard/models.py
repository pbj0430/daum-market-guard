from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class PostRef:
    board_id: str
    url: str
    title: str
    post_key: str


@dataclass(frozen=True)
class ImageRef:
    url: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class PostDetail:
    ref: PostRef
    title: str
    author_name: str = ""
    author_id: str = ""
    posted_at: str = ""
    content_text: str = ""
    images: list[ImageRef] = field(default_factory=list)


@dataclass(frozen=True)
class StoredImage:
    id: int
    post_id: int
    post_key: str
    post_url: str
    title: str
    author_name: str
    author_id: str
    image_url: str
    sha256: str
    ahash: str
    dhash: str


@dataclass(frozen=True)
class Assessment:
    post_id: int
    post_key: str
    score: int
    duplicate_image_count: int
    duplicate_post_count: int
    same_author_duplicate_count: int
    different_author_duplicate_count: int
    source_links: list[str]
    reasons: list[str]
