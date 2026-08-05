from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import tomllib


DEFAULT_BOARDS = [
    ("Market C3Zg", "https://cafe.daum.net/730418/C3Zg"),
    ("Market C3Zi", "https://cafe.daum.net/730418/C3Zi"),
    ("Market TyYz", "https://cafe.daum.net/730418/TyYz"),
    ("Market KykD", "https://cafe.daum.net/730418/KykD"),
]


@dataclass(frozen=True)
class BoardConfig:
    name: str
    url: str

    @property
    def cafe_id(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.strip("/").split("/")
        if path[:1] == ["_c21_"]:
            return parse_qs(parsed.query).get("grpid", [""])[0]
        return path[0] if path else ""

    @property
    def board_id(self) -> str:
        parsed = urlparse(self.url)
        fldid = parse_qs(parsed.query).get("fldid", [""])[0]
        if fldid:
            return fldid
        path = parsed.path.strip("/").split("/")
        return path[-1] if path else self.name


@dataclass(frozen=True)
class CommentConfig:
    enabled: bool = False
    mode: str = "dry_run"
    min_score: int = 80
    max_per_run: int = 2
    template: str = (
        "\uc790\ub3d9 \ud0d0\uc9c0 \uc54c\ub9bc: \uc774 \uae00\uc758 "
        "\uc774\ubbf8\uc9c0 {duplicate_image_count}\uc7a5\uc774 \uacfc\uac70 "
        "\uac8c\uc2dc\uae00 {duplicate_post_count}\uac1c\uc640 \uc720\uc0ac\ud569\ub2c8\ub2e4.\n"
        "\uc704\ud5d8\ub3c4 \ucd94\uc815: {score}%.\n"
        "\ucc38\uace0 \uc6d0\uae00: {source_links}\n"
        "\uc0ac\uae30 \ud655\uc815\uc774 \uc544\ub2cc \uc774\ubbf8\uc9c0 "
        "\uc7ac\uc0ac\uc6a9 \uc8fc\uc758 \uc2e0\ud638\uc785\ub2c8\ub2e4."
    )


@dataclass(frozen=True)
class LoginConfig:
    username: str = ""
    password: str = ""
    username_selector: str = (
        "input[name='loginId'], input[name='email'], input[type='email'], "
        "input[name='username'], input#loginId"
    )
    password_selector: str = "input[name='password'], input[type='password']"
    submit_selector: str = (
        "button[type='submit'], input[type='submit'], "
        "button:has-text('로그인'), a:has-text('로그인')"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


@dataclass(frozen=True)
class SelectorConfig:
    post_link_contains: list[str] = field(default_factory=list)
    comment_textarea: str = "textarea, [contenteditable='true']"
    comment_submit: str = (
        "button:has-text('\ub4f1\ub85d'), button:has-text('\ub313\uae00'), "
        "input[type='submit']"
    )


@dataclass(frozen=True)
class AppConfig:
    cafe_url: str = "https://cafe.daum.net/730418"
    cafe_grpid: str = "1R9cj"
    login_url: str = "https://accounts.kakao.com/login/"
    data_dir: Path = Path("data")
    user_data_dir: Path = Path("browser-profile")
    poll_interval_seconds: int = 300
    headless: bool = True
    locale: str = "ko-KR"
    timezone_id: str = "Asia/Seoul"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    allow_mobile_fallback: bool = False
    browser_executable_path: str | None = None
    scan_strategy: str = "direct_numbers"
    max_pages_per_board: int = 1
    max_posts_per_board_page: int = 40
    direct_scan_min_post_id: int = 1
    direct_scan_limit_per_board: int = 0
    rescan_existing_posts: bool = False
    image_timeout_seconds: int = 8
    duplicate_hamming_threshold: int = 4
    blacklist_score_threshold: int = 90
    boards: list[BoardConfig] = field(
        default_factory=lambda: [BoardConfig(name, url) for name, url in DEFAULT_BOARDS]
    )
    comment: CommentConfig = field(default_factory=CommentConfig)
    login: LoginConfig = field(default_factory=LoginConfig)
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

    login_raw = data.get("login", {})
    login = LoginConfig(
        username=str(login_raw.get("username", "")),
        password=str(login_raw.get("password", "")),
        username_selector=str(
            login_raw.get("username_selector", LoginConfig.username_selector)
        ),
        password_selector=str(
            login_raw.get("password_selector", LoginConfig.password_selector)
        ),
        submit_selector=str(login_raw.get("submit_selector", LoginConfig.submit_selector)),
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
        cafe_grpid=str(data.get("cafe_grpid", "1R9cj")),
        login_url=str(data.get("login_url", "https://accounts.kakao.com/login/")),
        data_dir=_path(data.get("data_dir", "data"), base_dir),
        user_data_dir=_path(data.get("user_data_dir", "browser-profile"), base_dir),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 300)),
        headless=bool(data.get("headless", True)),
        locale=str(data.get("locale", "ko-KR")),
        timezone_id=str(data.get("timezone_id", "Asia/Seoul")),
        user_agent=str(data.get("user_agent", AppConfig.user_agent)),
        allow_mobile_fallback=bool(data.get("allow_mobile_fallback", False)),
        browser_executable_path=_optional_str(data.get("browser_executable_path")),
        scan_strategy=str(data.get("scan_strategy", "direct_numbers")),
        max_pages_per_board=int(data.get("max_pages_per_board", 1)),
        max_posts_per_board_page=int(data.get("max_posts_per_board_page", 40)),
        direct_scan_min_post_id=int(data.get("direct_scan_min_post_id", 1)),
        direct_scan_limit_per_board=int(data.get("direct_scan_limit_per_board", 0)),
        rescan_existing_posts=bool(data.get("rescan_existing_posts", False)),
        image_timeout_seconds=int(data.get("image_timeout_seconds", 8)),
        duplicate_hamming_threshold=int(data.get("duplicate_hamming_threshold", 4)),
        blacklist_score_threshold=int(data.get("blacklist_score_threshold", 90)),
        boards=boards,
        comment=comment,
        login=login,
        selectors=selectors,
    )
