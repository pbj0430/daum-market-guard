from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urljoin, urlparse

from .config import AppConfig, BoardConfig
from .models import ImageRef, PostDetail, PostRef

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
else:
    BrowserContext = Any
    Page = Any


POST_ID_PATTERNS = [
    re.compile(r"/(?P<board>[A-Za-z0-9]+)/(?P<id>\d+)(?:[/?#]|$)"),
    re.compile(r"[?&](?:dataid|articleid|bbsid)=(?P<id>\d+)"),
]


@dataclass
class ScrapeStats:
    board_count: int = 0
    post_refs: int = 0
    post_details: int = 0
    images: int = 0


class DaumCafeScraper:
    def __init__(self, config: AppConfig):
        self.config = config
        self._playwright = None
        self.context: BrowserContext | None = None

    def __enter__(self) -> "DaumCafeScraper":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def start(self) -> None:
        if self.context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Playwright is not installed in the Python environment running this command. "
                "On Raspberry Pi, run `./scripts/bootstrap_pi.sh`, then use "
                "`./scripts/run.sh login --config config.toml` or "
                "`.venv/bin/python -m daum_market_guard login --config config.toml`."
            ) from exc
        self.config.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": self.config.headless,
            "user_data_dir": str(self.config.user_data_dir),
            "viewport": {"width": 1280, "height": 900},
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "args": [
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                f"--lang={self.config.locale}",
            ],
        }
        executable_path = self.config.browser_executable_path or find_system_chromium()
        if executable_path:
            launch_args["executable_path"] = executable_path
        self.context = self._playwright.chromium.launch_persistent_context(**launch_args)

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
            self.context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def page(self) -> Page:
        if self.context is None:
            raise RuntimeError("scraper not started")
        if self.context.pages:
            return self.context.pages[0]
        return self.context.new_page()

    def login_interactive(self) -> None:
        page = self.page()
        page.goto(self.config.cafe_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1000)
        self._click_login_if_available(page)
        print(
            "The cafe page is open. Log in from the browser if needed, "
            "then press Enter here."
        )
        input()
        page.goto(self.config.cafe_url, wait_until="domcontentloaded", timeout=60_000)
        self.context.storage_state(path=str(self.config.data_dir / "storage-state.json"))

    def collect_board_posts(self, board: BoardConfig) -> list[PostRef]:
        refs: dict[str, PostRef] = {}
        for page_no in range(1, self.config.max_pages_per_board + 1):
            url = self._board_page_url(board.url, page_no)
            page = self.page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)
            if self._looks_logged_out(page):
                raise RuntimeError("Login is required. Run the login command first.")
            for link in self._extract_links_from_page_and_frames(page):
                ref = self._link_to_post_ref(board, link)
                if ref is not None:
                    refs.setdefault(ref.post_key, ref)
                if len(refs) >= self.config.max_posts_per_board_page * page_no:
                    break
        return list(refs.values())

    def collect_post_detail(self, ref: PostRef) -> PostDetail:
        page = self.page()
        page.goto(ref.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        title = self._first_text(
            page,
            [
                "h1",
                "h2",
                "h3",
                ".tit_view",
                ".article_subject",
                ".subject",
                "meta[property='og:title']",
            ],
        )
        author = self._first_text(
            page,
            [
                ".txt_writer",
                ".nickname",
                ".writer",
                ".nick_name",
                "[class*='writer']",
                "[class*='nickname']",
            ],
        )
        posted_at = self._first_text(page, ["time", ".date", ".txt_date", "[class*='date']"])
        images = self._extract_images_from_page_and_frames(page)
        return PostDetail(
            ref=ref,
            title=title or ref.title,
            author_name=_clean_text(author),
            author_id="",
            posted_at=_clean_text(posted_at),
            images=images,
        )

    def download_image(self, image_url: str, referer: str) -> bytes | None:
        if self.context is None:
            raise RuntimeError("scraper not started")
        try:
            response = self.context.request.get(
                image_url,
                headers={"Referer": referer},
                timeout=self.config.image_timeout_seconds * 1000,
            )
            if not response.ok:
                return None
            content_type = response.headers.get("content-type", "")
            if "image" not in content_type.lower():
                return None
            return response.body()
        except Exception:
            return None

    def _board_page_url(self, board_url: str, page_no: int) -> str:
        if page_no <= 1:
            return board_url
        sep = "&" if "?" in board_url else "?"
        return f"{board_url}{sep}page={page_no}"

    def _extract_links_from_page_and_frames(self, page: Page) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        script = """
            () => Array.from(document.querySelectorAll('a')).map((a) => ({
                text: (a.innerText || a.textContent || '').trim(),
                href: a.href || ''
            }))
        """
        for frame in page.frames:
            try:
                frame_links = frame.evaluate(script)
            except Exception:
                continue
            links.extend(frame_links)
        return links

    def _extract_images_from_page_and_frames(self, page: Page) -> list[ImageRef]:
        found: dict[str, ImageRef] = {}
        script = """
            () => Array.from(document.images).map((img) => ({
                src: img.currentSrc || img.src || '',
                width: img.naturalWidth || img.width || 0,
                height: img.naturalHeight || img.height || 0
            }))
        """
        for frame in page.frames:
            try:
                images = frame.evaluate(script)
            except Exception:
                continue
            for item in images:
                src = str(item.get("src") or "")
                if not src.startswith("http"):
                    src = urljoin(frame.url, src)
                width = int(item.get("width") or 0)
                height = int(item.get("height") or 0)
                if self._is_content_image(src, width, height):
                    found.setdefault(src, ImageRef(src, width, height))
        return list(found.values())

    def _link_to_post_ref(self, board: BoardConfig, link: dict[str, str]) -> PostRef | None:
        href = str(link.get("href") or "")
        title = _clean_text(str(link.get("text") or ""))
        if not href or len(title) < 2:
            return None
        if "cafe.daum.net" not in href and not href.startswith("/"):
            return None
        href = urljoin(board.url, href)
        parsed = urlparse(href)
        if "cafe.daum.net" not in parsed.netloc:
            return None
        if self.config.selectors.post_link_contains:
            if not any(token in href for token in self.config.selectors.post_link_contains):
                return None
        post_key = self._post_key(board, href)
        if post_key is None:
            return None
        if post_key == board.board_id:
            return None
        return PostRef(board_id=board.board_id, url=href, title=title, post_key=post_key)

    def _post_key(self, board: BoardConfig, href: str) -> str | None:
        parsed = urlparse(href)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        for key in ("dataid", "articleid", "bbsid"):
            if query.get(key):
                return f"{board.board_id}:{query[key][0]}"
        if len(path_parts) >= 3 and path_parts[-2] == board.board_id and path_parts[-1].isdigit():
            return f"{board.board_id}:{path_parts[-1]}"
        for pattern in POST_ID_PATTERNS:
            match = pattern.search(href)
            if match and match.groupdict().get("id"):
                board_id = match.groupdict().get("board") or board.board_id
                return f"{board_id}:{match.group('id')}"
        return None

    def _is_content_image(self, src: str, width: int, height: int) -> bool:
        lower = src.lower()
        if not src.startswith("http"):
            return False
        if any(token in lower for token in ("icon", "profile", "emoticon", "blank.gif", "logo")):
            return False
        if width and height and (width < 180 or height < 120):
            return False
        return True

    def _first_text(self, page: Page, selectors: list[str]) -> str:
        for frame in page.frames:
            for selector in selectors:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    if selector.startswith("meta"):
                        value = locator.get_attribute("content") or ""
                    else:
                        value = locator.inner_text(timeout=1000)
                    value = _clean_text(value)
                    if value:
                        return value
                except Exception:
                    continue
        try:
            title = page.title()
        except Exception:
            title = ""
        return _clean_text(title)

    def _looks_logged_out(self, page: Page) -> bool:
        url = page.url.lower()
        if "accounts.kakao.com" in url or "login" in url:
            return True
        try:
            text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return "로그인" in text and "카카오" in text and "비밀번호" in text

    def _click_login_if_available(self, page: Page) -> None:
        candidates = [
            "a:has-text('로그인')",
            "button:has-text('로그인')",
            "a[href*='login']",
            "a[href*='accounts.kakao.com']",
            "a[href*='logins.daum.net']",
        ]
        for frame in page.frames:
            for selector in candidates:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    locator.click(timeout=2000)
                    page.wait_for_timeout(1000)
                    return
                except Exception:
                    continue


def save_image_bytes(base_dir: Path, post_key: str, image_url: str, image_bytes: bytes) -> Path:
    suffix = _guess_suffix(image_url)
    safe_post_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", post_key)
    digest = __import__("hashlib").sha256(image_bytes).hexdigest()[:16]
    directory = base_dir / safe_post_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(image_bytes)
    return path


def _guess_suffix(image_url: str) -> str:
    suffix = Path(urlparse(image_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return suffix
    return ".jpg"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def find_system_chromium() -> str | None:
    for command in (
        "chromium-browser",
        "chromium",
        "google-chrome-stable",
        "google-chrome",
    ):
        path = shutil.which(command)
        if path:
            return path
    return None
