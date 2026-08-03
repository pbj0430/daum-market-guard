from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import tomllib


DEFAULT_BOARDS = [
    ("중고장터 C3Zg", "https://cafe.daum.net/730418/C3Zg"),
    ("중고장터 C3Zi", "https://cafe.daum.net/730418/C3Zi"),
    ("중고장터 TyYz", "https://cafe.daum.net/730418/TyYz"),
    ("중고장터 KykD", "https://cafe.daum.net/730418/KykD"),
]


@dataclass(frozen=True)
class BoardConfig:
    name: str
    url: str

    @property
    def board_id(self) -> str:
        path = urlparse(self.url).path.strip("/").split("/")
        return path[-1] if path else self.name


@dataclass(frozen=True)
class CommentConfig:
    enabled: bool = False
    mode: str = "dry_run"
    min_score: int = 80
    max_per_run: int = 2
    template: str = (
        "자동 탐지 알림: 이 글의 이미지 {duplicate_image_count}장이 과거 게시글 "
        "{duplicate_post_count}개와 유사합니다.\n"
        "위험도 추정: {score}%.\n"
        "참고 원글: {source_links}\n"
        "사기 확정이 아닌 이미지 재사용 주의 신호입니다."
    )


@dataclass(frozen=True)
class SelectorConfig:
    post_link_contains: list[str] = field(default_factory=list)
    comment_textarea: str = "textarea, [contenteditable='true']"
    comment_submit: str = "button:has-text('등록'), button:has-text('댓글'), input[type='submit']"


@dataclass(frozen=True)
class AppConfig:
    cafe_url: str = "https://cafe.daum.net/730418"
    login_url: str = "https://accounts.kakao.com/login/"
    data_dir: Path = Path("data")
    user_data_dir: Path = Path("browser-profile")
    poll_interval_seconds: int = 300
    headless: bool = True
    browser_executable_path: str | None = None
    max_pages_per_board: int = 1
    max_posts_per_board_page: int = 40
    image_timeout_seconds: int = 20
    duplicate_hamming_threshold: int = 8
    blacklist_score_threshold: int = 90
    boards: list[BoardConfig] = field(
        default_factory=lambda: [BoardConfig(name, url) for name, url in DEFAULT_BOARDS]
    )
    comment: CommentConfig = field(default_factory=CommentConfig)
    selectors: SelectorConfig = field(default_factory=SelectorConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "market_guard.sqlite3"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"


def _path(value: str | Path, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _optional_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    base_dir = config_path.parent
    data: dict[str, Any] = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    boards_raw = data.get("boards") or [
        {"name": name, "url": url} for name, url in DEFAULT_BOARDS
    ]
    boards = [BoardConfig(str(item["name"]), str(item["url"])) for item in boards_raw]

    comment_raw = data.get("comment", {})
    comment = CommentConfig(
        enabled=bool(comment_raw.get("enabled", False)),
        mode=str(comment_raw.get("mode", "dry_run")),
        min_score=int(comment_raw.get("min_score", 80)),
        max_per_run=int(comment_raw.get("max_per_run", 2)),
        template=str(comment_raw.get("template", CommentConfig.template)),
    )

    selectors_raw = data.get("selectors", {})
    selectors = SelectorConfig(
        post_link_contains=list(selectors_raw.get("post_link_contains", [])),
        comment_textarea=str(
            selectors_raw.get("comment_textarea", SelectorConfig.comment_textarea)
        ),
        comment_submit=str(
            selectors_raw.get("comment_submit", SelectorConfig.comment_submit)
        ),
    )

    return AppConfig(
        cafe_url=str(data.get("cafe_url", "https://cafe.daum.net/730418")),
        login_url=str(data.get("login_url", "https://accounts.kakao.com/login/")),
        data_dir=_path(data.get("data_dir", "data"), base_dir),
        user_data_dir=_path(data.get("user_data_dir", "browser-profile"), base_dir),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 300)),
        headless=bool(data.get("headless", True)),
        browser_executable_path=_optional_str(data.get("browser_executable_path")),
        max_pages_per_board=int(data.get("max_pages_per_board", 1)),
        max_posts_per_board_page=int(data.get("max_posts_per_board_page", 40)),
        image_timeout_seconds=int(data.get("image_timeout_seconds", 20)),
        duplicate_hamming_threshold=int(data.get("duplicate_hamming_threshold", 8)),
        blacklist_score_threshold=int(data.get("blacklist_score_threshold", 90)),
        boards=boards,
        comment=comment,
        selectors=selectors,
    )
