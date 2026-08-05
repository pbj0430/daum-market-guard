from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urljoin, urlparse

from .config import AppConfig, BoardConfig
from .models import ImageRef, PostDetail, PostRef

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
else:
    BrowserContext = Any
    Page = Any


POST_ID_PATTERNS = [
    re.compile(r"/(?P<board>[A-Za-z0-9]+)/(?P<id>\d+)(?:[/?#]|$)"),
    re.compile(r"[?&](?:dataid|datanum|articleid|bbsid)=(?P<id>\d+)"),
]
POST_ID_QUERY_KEYS = ("dataid", "datanum", "articleid", "bbsid")
ARTICLE_FUNC_PATTERNS = [
    re.compile(r"['\"](?P<board>[A-Za-z0-9]+)['\"]\s*,\s*['\"]?(?P<id>\d+)['\"]?"),
    re.compile(r"(?P<board>[A-Za-z0-9]+)\D{0,20}(?P<id>\d{2,})"),
]
STRUCTURED_POST_KEY_PATTERNS = [
    re.compile(
        r"(?:fldid|board|folderid)['\"]?\s*[:=]\s*['\"]?(?P<board>[A-Za-z0-9]+)"
        r".{0,300}?"
        r"(?:dataid|datanum|articleid|bbsid)['\"]?\s*[:=]\s*['\"]?(?P<id>\d+)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"(?:dataid|datanum|articleid|bbsid)['\"]?\s*[:=]\s*['\"]?(?P<id>\d+)"
        r".{0,300}?"
        r"(?:fldid|board|folderid)['\"]?\s*[:=]\s*['\"]?(?P<board>[A-Za-z0-9]+)",
        re.IGNORECASE | re.DOTALL,
    ),
]
BODY_SELECTORS = [
    "#bbs_contents #user_contents",
    "#bbs_contents .board_post.tx-content-container",
    "#user_contents",
    ".board_post.tx-content-container",
    ".tx-content-container",
]
ARTICLE_TITLE_SELECTORS = [
    ".tit_info .article_title",
    "strong.tit_info span.article_title",
    ".article_subject",
    ".tit_subject",
    ".tit_info",
    ".tit_item",
    "#articleTitle",
    "h1",
    "h2",
    "h3",
    ".tit_view",
    ".subject",
    "meta[property='og:title']",
]
ARTICLE_AUTHOR_SELECTORS = [
    ".txt_writer",
    ".txt_name",
    ".nickname",
    ".writer",
    ".nick_name",
    ".author",
    ".info_author",
    "[class*='writer']",
    "[class*='nickname']",
    "[class*='author']",
]
ARTICLE_DATE_SELECTORS = ["time", ".date", ".txt_date", "[class*='date']"]
ARTICLE_DATE_PATTERN = re.compile(r"\d{2,4}\.\d{1,2}\.\d{1,2}(?:\s+\d{1,2}:\d{2})?")


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
            "screen": {"width": 1280, "height": 900},
            "is_mobile": False,
            "has_touch": False,
            "locale": self.config.locale,
            "timezone_id": self.config.timezone_id,
            "user_agent": self.config.user_agent,
            "args": [
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--window-size=1280,900",
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
        page.goto(self._desktop_url(self.config.cafe_url), wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1000)
        if self._looks_logged_out(page):
            if self.config.login.has_credentials:
                self._login_with_credentials(page, self.config.cafe_url)
            else:
                self._click_login_if_available(page)
                print(
                    "The cafe page is open. Log in from the browser if needed, "
                    "then press Enter here."
                )
                input()
        page.goto(self._desktop_url(self.config.cafe_url), wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1000)
        if self._looks_logged_out(page):
            raise RuntimeError("Login did not complete. Check config [login] or log in manually.")
        self.context.storage_state(path=str(self.config.data_dir / "storage-state.json"))

    def collect_board_posts(self, board: BoardConfig, progress: Any | None = None) -> list[PostRef]:
        refs: dict[str, PostRef] = {}
        for page_no in range(1, self.config.max_pages_per_board + 1):
            for url in self._board_page_urls(board, page_no):
                page = self.page()
                _emit(progress, "board_page_opening", {"board": board.name, "page": page_no, "url": url})
                self._goto_with_login(page, url)
                if self._looks_unsupported_browser(page):
                    raise RuntimeError(
                        "Daum returned the unsupported-browser page. "
                        "Run scan from VNC GUI or update Chromium."
                    )
                links = self._extract_links_from_page_and_frames(page)
                page_refs = []
                for link in links:
                    ref = self._link_to_post_ref(board, link)
                    if ref is not None:
                        page_refs.append(ref)
                _emit(
                    progress,
                    "board_page_loaded",
                    {
                        "board": board.name,
                        "page": page_no,
                        "requested_url": url,
                        "page_url": page.url,
                        "title": _safe_title(page),
                        "frame_count": len(page.frames),
                        "link_count": len(links),
                        "accepted_count": len(page_refs),
                        "accepted_samples": [
                            {"title": ref.title, "url": ref.url, "post_key": ref.post_key}
                            for ref in page_refs[:5]
                        ],
                        "article_link_samples": _article_link_samples(links),
                    },
                )
                for ref in page_refs:
                    refs.setdefault(ref.post_key, ref)
                    if len(refs) >= self.config.max_posts_per_board_page * page_no:
                        break
                if refs:
                    break
        return list(refs.values())

    def inspect_board(self, board: BoardConfig) -> dict[str, Any]:
        reports = []
        for url in self._board_page_urls(board, 1):
            page = self.page()
            self._goto_with_login(page, url)
            links = self._extract_links_from_page_and_frames(page)
            accepted = [self._link_to_post_ref(board, link) for link in links]
            accepted = [ref for ref in accepted if ref is not None]
            try:
                body_text = _clean_text(page.locator("body").inner_text(timeout=1000))
            except Exception:
                body_text = ""
            reports.append(
                {
                    "requested_url": url,
                    "page_url": page.url,
                    "title": _safe_title(page),
                    "frame_count": len(page.frames),
                    "link_count": len(links),
                    "accepted_count": len(accepted),
                    "logged_out": self._looks_logged_out(page),
                    "unsupported_browser": self._looks_unsupported_browser(page),
                    "body_excerpt": body_text[:700],
                    "accepted_samples": [
                        {"title": ref.title, "url": ref.url, "post_key": ref.post_key}
                        for ref in accepted[:10]
                    ],
                    "link_samples": links[:20],
                }
            )
        return {
            "board": board.name,
            "board_url": board.url,
            "url_reports": reports,
            "link_count": sum(report["link_count"] for report in reports),
            "accepted_count": sum(report["accepted_count"] for report in reports),
            "logged_out": any(report["logged_out"] for report in reports),
            "unsupported_browser": any(report["unsupported_browser"] for report in reports),
            "body_excerpt": reports[0]["body_excerpt"] if reports else "",
        }

    def collect_post_detail(self, ref: PostRef, progress: Any | None = None) -> PostDetail:
        total_started = time.monotonic()
        timings: dict[str, int] = {}
        page = self.page()
        started = time.monotonic()
        self._goto_with_login(page, ref.url)
        timings["open_ms"] = _elapsed_ms(started)
        _emit_detail_timing(progress, ref, "open", timings["open_ms"], {"final_url": page.url})
        started = time.monotonic()
        body_frames = self._body_frames(page)
        timings["find_body_ms"] = _elapsed_ms(started)
        has_post_content = bool(body_frames)
        _emit_detail_timing(
            progress,
            ref,
            "find_body",
            timings["find_body_ms"],
            {"frames": len(page.frames), "body_frames": len(body_frames)},
        )
        started = time.monotonic()
        content_text = self._content_text(page)
        timings["content_ms"] = _elapsed_ms(started)
        _emit_detail_timing(
            progress,
            ref,
            "content",
            timings["content_ms"],
            {"chars": len(content_text)},
        )
        started = time.monotonic()
        images = self._extract_images_from_page_and_frames(page)
        timings["image_refs_ms"] = _elapsed_ms(started)
        _emit_detail_timing(
            progress,
            ref,
            "image_refs",
            timings["image_refs_ms"],
            {"images": len(images)},
        )
        if not has_post_content:
            raise RuntimeError(f"Post not found or not a readable post: {ref.url}")
        started = time.monotonic()
        self._validate_loaded_post_identity(ref, page, body_frames)
        timings["identity_ms"] = _elapsed_ms(started)
        _emit_detail_timing(progress, ref, "identity", timings["identity_ms"])
        started = time.monotonic()
        metadata = self._article_metadata_from_frames(body_frames)
        timings["metadata_ms"] = _elapsed_ms(started)
        _emit_detail_timing(progress, ref, "metadata", timings["metadata_ms"], metadata)
        started = time.monotonic()
        article_sections = self._article_text_sections(body_frames)
        timings["header_parse_ms"] = _elapsed_ms(started)
        title = (
            metadata.get("title", "")
            or self._first_text(body_frames, ARTICLE_TITLE_SELECTORS)
            or _title_from_article_sections(article_sections)
        )
        author = metadata.get("author", "") or _author_from_article_sections(article_sections)
        posted_at = (
            metadata.get("posted_at", "")
            or _posted_at_from_article_sections(article_sections)
            or self._first_text(body_frames, ARTICLE_DATE_SELECTORS)
        )
        if _looks_cafe_page_title(title) or _looks_non_article_title(title):
            title = ref.title
        if title == ref.title:
            title = _title_from_content_text(content_text) or ref.title
        timings["total_ms"] = _elapsed_ms(total_started)
        _emit_detail_timing(
            progress,
            ref,
            "detail_total",
            timings["total_ms"],
            {
                "title": title,
                "author": author,
                "posted_at": posted_at,
                "images": len(images),
            },
        )
        return PostDetail(
            ref=ref,
            title=title or ref.title,
            author_name=_clean_text(author),
            author_id="",
            posted_at=_clean_text(posted_at),
            content_text=content_text,
            images=images,
        )

    def _article_metadata_from_frames(self, frames: list[Any]) -> dict[str, str]:
        for frame in frames:
            try:
                metadata = _metadata_from_article_markup(frame.content())
            except Exception:
                metadata = {}
            if all(metadata.get(key) for key in ("title", "author", "posted_at")):
                return metadata
            try:
                dom_metadata = frame.evaluate(
                    """
                    () => {
                        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
                        const titleEl = document.querySelector(
                            'strong.tit_info span.article_title, .tit_info .article_title, .bbs_read_tit .article_title'
                        );
                        const authorEl = document.querySelector(
                            '.bbs_read_tit a.link_item[data-nickname], a.link_item[data-nickname]'
                        );
                        const dateItems = Array.from(
                            document.querySelectorAll('.bbs_read_tit span.txt_item, span.txt_item, time, .txt_date')
                        ).map((el) => clean(el.innerText || el.textContent));
                        const postedAt = dateItems.find((text) =>
                            /^\\d{2,4}\\.\\d{1,2}\\.\\d{1,2}(?:\\s+\\d{1,2}:\\d{2})?/.test(text)
                        ) || '';
                        return {
                            title: clean(titleEl ? (titleEl.innerText || titleEl.textContent) : ''),
                            author: clean(authorEl ? (authorEl.getAttribute('data-nickname') || authorEl.innerText || authorEl.textContent) : ''),
                            posted_at: postedAt
                        };
                    }
                    """
                )
            except Exception:
                dom_metadata = {}
            metadata = {
                key: _clean_text(str(metadata.get(key) or dom_metadata.get(key) or ""))
                for key in ("title", "author", "posted_at")
            }
            if any(metadata.values()):
                return metadata
        return {"title": "", "author": "", "posted_at": ""}

    def _body_frames(self, page: Page) -> list[Any]:
        frames = []
        for frame in page.frames:
            for selector in BODY_SELECTORS:
                try:
                    if frame.locator(selector).count() > 0:
                        frames.append(frame)
                        break
                except Exception:
                    continue
        return frames

    def _article_text_sections(self, frames: list[Any]) -> list[tuple[list[str], list[str]]]:
        sections: list[tuple[list[str], list[str]]] = []
        for frame in frames:
            try:
                body_text = frame.locator("body").inner_text(timeout=1000)
            except Exception:
                body_text = ""
            content_text = ""
            for selector in BODY_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    content_text = locator.inner_text(timeout=1000)
                    break
                except Exception:
                    continue
            sections.append((_text_lines(body_text), _text_lines(content_text)))
        return sections

    def _validate_loaded_post_identity(self, ref: PostRef, page: Page, frames: list[Any]) -> None:
        expected_key = ref.post_key
        if ":" not in expected_key:
            return
        observed = self._observed_post_keys(ref.board_id, page, frames, ref.url)
        if observed and expected_key not in observed:
            raise RuntimeError(
                "Post resolved to different article: "
                f"expected {expected_key}, observed {', '.join(sorted(observed))}"
            )

    def _observed_post_keys(
        self,
        board_id: str,
        page: Page,
        frames: list[Any],
        requested_url: str,
    ) -> set[str]:
        observed: set[str] = set()
        urls: list[str] = []
        for frame in frames:
            urls.append(str(getattr(frame, "url", "") or ""))
            urls.extend(self._identity_urls_from_frame(frame))
            try:
                observed.update(_post_keys_from_text(board_id, frame.content()))
            except Exception:
                pass
        if _clean_url(page.url) != _clean_url(requested_url):
            urls.append(page.url)
        for url in urls:
            key = _post_key_from_url(board_id, url)
            if key:
                observed.add(key)
        return observed

    def _identity_urls_from_frame(self, frame: Any) -> list[str]:
        script = """
            () => [
                ...Array.from(document.querySelectorAll('link[rel="canonical"]')).map((el) => el.href || ''),
                ...Array.from(document.querySelectorAll('meta[property="og:url"]')).map((el) => el.content || '')
            ].filter(Boolean)
        """
        try:
            return [str(url) for url in frame.evaluate(script)]
        except Exception:
            return []

    def _goto_with_login(self, page: Page, url: str) -> None:
        target_url = self._desktop_url(url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        if not self._looks_logged_out(page):
            return
        if not self.config.login.has_credentials:
            raise RuntimeError(
                "Login is required. Add [login] username/password to config.toml "
                "or run the login command manually."
            )
        self._login_with_credentials(page, target_url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(1500)
        if self._looks_logged_out(page):
            raise RuntimeError("Automatic login failed. Check the saved credentials or selectors.")

    def _login_with_credentials(self, page: Page, return_url: str) -> None:
        if self._looks_logged_out(page):
            self._click_login_if_available(page)
            page.wait_for_timeout(1000)
        if not self._is_login_page(page):
            page.goto(self._login_url(return_url), wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1500)
        self._fill_first_visible(
            page,
            self.config.login.username_selector,
            self.config.login.username,
        )
        self._fill_first_visible(
            page,
            self.config.login.password_selector,
            self.config.login.password,
        )
        self._click_first_visible(page, self.config.login.submit_selector)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

    def _login_url(self, return_url: str) -> str:
        if "continue=" in self.config.login_url:
            return self.config.login_url
        separator = "&" if "?" in self.config.login_url else "?"
        return (
            f"{self.config.login_url}{separator}"
            f"continue={quote(self._desktop_url(return_url), safe='')}"
        )

    def _fill_first_visible(self, page: Page, selector: str, value: str) -> None:
        last_error: Exception | None = None
        for frame in page.frames:
            try:
                locator = frame.locator(selector).first
                locator.wait_for(state="visible", timeout=5000)
                locator.fill(value)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not find login field for selector: {selector}. {last_error}")

    def _click_first_visible(self, page: Page, selector: str) -> None:
        last_error: Exception | None = None
        for frame in page.frames:
            try:
                locator = frame.locator(selector).first
                locator.wait_for(state="visible", timeout=5000)
                locator.click(timeout=5000)
                return
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Could not find login submit button for selector: {selector}. {last_error}")

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

    def _board_page_urls(self, board: BoardConfig, page_no: int) -> list[str]:
        urls = [self._pc_board_page_url(board, page_no)]
        if not self.config.allow_mobile_fallback:
            return urls
        mobile = f"https://m.cafe.daum.net/{board.cafe_id}/{board.board_id}"
        if page_no > 1:
            mobile = f"{mobile}?page={page_no}"
        if mobile not in urls:
            urls.append(mobile)
        return urls

    def _pc_board_page_url(self, board: BoardConfig, page_no: int) -> str:
        parsed = urlparse(board.url)
        query = parse_qs(parsed.query)
        grpid = _first_query(query, "grpid") or self.config.cafe_grpid
        fldid = _first_query(query, "fldid") or board.board_id
        if grpid and fldid:
            url = f"https://cafe.daum.net/_c21_/bbs_list?grpid={grpid}&fldid={fldid}"
            if page_no > 1:
                url = f"{url}&page={page_no}"
            return url
        return self._board_page_url(self._desktop_url(board.url), page_no)

    def _desktop_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.netloc != "m.cafe.daum.net":
            return url
        query = f"?{parsed.query}" if parsed.query else ""
        fragment = f"#{parsed.fragment}" if parsed.fragment else ""
        return f"https://cafe.daum.net{parsed.path}{query}{fragment}"

    def _extract_links_from_page_and_frames(self, page: Page) -> list[dict[str, str]]:
        links: list[dict[str, str]] = []
        script = """
            () => Array.from(document.querySelectorAll('a')).map((a) => ({
                text: (a.innerText || a.textContent || '').trim(),
                href: a.href || '',
                rawHref: a.getAttribute('href') || '',
                onclick: a.getAttribute('onclick') || '',
                dataHref: a.dataset ? (a.dataset.href || a.dataset.url || a.dataset.link || '') : ''
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
            (selectors) => {
                const roots = selectors.flatMap((selector) =>
                    Array.from(document.querySelectorAll(selector))
                );
                return roots.flatMap((root) =>
                    Array.from(root.querySelectorAll('img')).map((img) => ({
                        src: img.currentSrc || img.src || '',
                        dataSrc: img.getAttribute('data-img-src') || '',
                        className: img.className || '',
                        alt: img.getAttribute('alt') || '',
                        width: img.naturalWidth || img.width || 0,
                        height: img.naturalHeight || img.height || 0
                    }))
                );
            }
        """
        for frame in page.frames:
            try:
                images = frame.evaluate(script, BODY_SELECTORS)
            except Exception:
                continue
            for item in images:
                src = str(item.get("dataSrc") or item.get("src") or "")
                if not src.startswith("http"):
                    src = urljoin(frame.url, src)
                width = int(item.get("width") or 0)
                height = int(item.get("height") or 0)
                class_name = str(item.get("className") or "")
                alt = str(item.get("alt") or "")
                if self._is_content_image(src, width, height, class_name, alt):
                    found.setdefault(src, ImageRef(src, width, height))
        return list(found.values())

    def _link_to_post_ref(self, board: BoardConfig, link: dict[str, str]) -> PostRef | None:
        href = self._normalize_link_href(board, link)
        title = _clean_text(str(link.get("text") or ""))
        if not href or len(title) < 2:
            return None
        if _is_notice_title(title):
            return None
        if "cafe.daum.net" not in href and not href.startswith("/"):
            return None
        href = urljoin(board.url, href)
        parsed = urlparse(href)
        if "cafe.daum.net" not in parsed.netloc:
            return None
        if self._is_non_market_link(board, href):
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

    def _normalize_link_href(self, board: BoardConfig, link: dict[str, str]) -> str:
        href = str(link.get("href") or link.get("rawHref") or link.get("dataHref") or "")
        raw = " ".join(
            str(link.get(key) or "")
            for key in ("href", "rawHref", "dataHref", "onclick")
        )
        if href and not href.lower().startswith("javascript:"):
            return href
        query = parse_qs(raw.replace("&amp;", "&"))
        dataid = _first_query(query, *POST_ID_QUERY_KEYS)
        fldid = _first_query(query, "fldid", "board", "folderid")
        if dataid and (fldid in (None, board.board_id)):
            return f"https://cafe.daum.net/_c21_/bbs_read?fldid={board.board_id}&dataid={dataid}"
        for pattern in ARTICLE_FUNC_PATTERNS:
            match = pattern.search(raw)
            if not match:
                continue
            if match.groupdict().get("board") != board.board_id:
                continue
            return (
                "https://cafe.daum.net/_c21_/bbs_read"
                f"?fldid={board.board_id}&dataid={match.group('id')}"
            )
        return href

    def _post_key(self, board: BoardConfig, href: str) -> str | None:
        parsed = urlparse(href)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        fldid = _first_query(query, "fldid", "board", "folderid")
        if fldid and fldid != board.board_id:
            return None
        for key in POST_ID_QUERY_KEYS:
            if query.get(key):
                return f"{board.board_id}:{query[key][0]}"
        if (
            len(path_parts) >= 3
            and path_parts[0] == board.cafe_id
            and path_parts[-2] == board.board_id
            and path_parts[-1].isdigit()
        ):
            return f"{board.board_id}:{path_parts[-1]}"
        for pattern in POST_ID_PATTERNS:
            match = pattern.search(href)
            if match and match.groupdict().get("id"):
                board_id = match.groupdict().get("board") or board.board_id
                return f"{board_id}:{match.group('id')}"
        return None

    def _is_non_market_link(self, board: BoardConfig, href: str) -> bool:
        parsed = urlparse(href)
        path_parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        fldid = _first_query(query, "fldid", "board", "folderid")
        if fldid is not None:
            return fldid != board.board_id
        if path_parts and path_parts[0] == board.cafe_id:
            return board.board_id not in path_parts
        return True

    def _is_content_image(
        self,
        src: str,
        width: int,
        height: int,
        class_name: str = "",
        alt: str = "",
    ) -> bool:
        lower = " ".join((src, class_name, alt)).lower()
        if not src.startswith("http"):
            return False
        if any(
            token in lower
            for token in (
                "icon",
                "profile",
                "emoticon",
                "blank.gif",
                "logo",
                "avatar",
                "thumb_profile",
                "default_profile",
                "user_profile",
                "img_profile",
                "ico_",
            )
        ):
            return False
        if width and height and (width < 180 or height < 120):
            return False
        return True

    def _first_text(self, frames: list[Any], selectors: list[str]) -> str:
        for frame in frames:
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
        return ""

    def _content_text(self, page: Page) -> str:
        for frame in page.frames:
            for selector in BODY_SELECTORS:
                try:
                    locator = frame.locator(selector).first
                    if locator.count() == 0:
                        continue
                    text = _clean_text(locator.inner_text(timeout=1000))
                    if len(text) >= 20:
                        return text[:10000]
                except Exception:
                    continue
        return ""

    def _looks_logged_out(self, page: Page) -> bool:
        url = page.url.lower()
        if "accounts.kakao.com" in url or "login" in url:
            return True
        loginout_state = self._loginout_state(page)
        if loginout_state == "logged_in":
            return False
        if loginout_state == "logged_out":
            return True
        try:
            if page.locator(self.config.login.password_selector).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
        try:
            text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        try:
            loginout = page.locator("#loginout").first.inner_text(timeout=500)
        except Exception:
            loginout = ""
        if "로그아웃" in loginout:
            return False
        return "로그인" in text and "카카오" in text and "비밀번호" in text

    def _loginout_state(self, page: Page) -> str:
        script = """
            () => {
                const el = document.querySelector('#loginout');
                if (!el) return '';
                const link = el.closest('a');
                const onclick = link ? (link.getAttribute('onclick') || '') : '';
                const text = el.innerText || el.textContent || '';
                return `${onclick} ${text}`;
            }
        """
        for frame in page.frames:
            try:
                signal = str(frame.evaluate(script) or "").lower()
            except Exception:
                continue
            if "logout(" in signal:
                return "logged_in"
            if "login(" in signal:
                return "logged_out"
            if "로그아웃" in signal:
                return "logged_in"
            if "로그인" in signal:
                return "logged_out"
        return ""

    def _is_login_page(self, page: Page) -> bool:
        url = page.url.lower()
        if "accounts.kakao.com" in url or "logins.daum.net" in url:
            return True
        try:
            return page.locator(self.config.login.password_selector).first.is_visible(timeout=1000)
        except Exception:
            return False

    def _looks_unsupported_browser(self, page: Page) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=1000)
        except Exception:
            return False
        return (
            "Internet Explorer 10" in text
            or "브라우저 지원이 종료" in text
            or "브라우저 업데이트" in text
        )

    def _click_login_if_available(self, page: Page) -> None:
        candidates = [
            "a:has-text('로그인')",
            "button:has-text('로그인')",
            "a[onclick*='login']",
            "button[onclick*='login']",
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


def _text_lines(value: str) -> list[str]:
    return [line for line in (_clean_text(line) for line in (value or "").splitlines()) if line]


def _metadata_from_article_markup(value: str) -> dict[str, str]:
    parser = _DaumArticleMetadataParser()
    parser.feed(value or "")
    return {
        "title": _clean_text(parser.title),
        "author": _clean_text(parser.author),
        "posted_at": _clean_text(parser.posted_at),
    }


class _DaumArticleMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str]]] = []
        self.title = ""
        self.author = ""
        self.posted_at = ""
        self._title_depth = 0
        self._title_parts: list[str] = []
        self._author_depth = 0
        self._author_parts: list[str] = []
        self._txt_depth = 0
        self._txt_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        next_depth = len(self.stack) + 1
        if tag == "span" and "article_title" in classes and self._has_ancestor_class("tit_info"):
            self._title_depth = next_depth
            self._title_parts = []
        if tag == "a" and "link_item" in classes and attrs_dict.get("data-nickname") and not self.author:
            self.author = attrs_dict["data-nickname"]
            self._author_depth = next_depth
            self._author_parts = []
        if tag == "span" and "txt_item" in classes:
            self._txt_depth = next_depth
            self._txt_parts = []
        self.stack.append((tag, attrs_dict))

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self._title_parts.append(data)
        if self._author_depth and not self.author:
            self._author_parts.append(data)
        if self._txt_depth:
            self._txt_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        depth = len(self.stack)
        if self._title_depth == depth:
            self.title = self.title or _clean_text("".join(self._title_parts))
            self._title_depth = 0
            self._title_parts = []
        if self._author_depth == depth:
            self.author = self.author or _clean_text("".join(self._author_parts))
            self._author_depth = 0
            self._author_parts = []
        if self._txt_depth == depth:
            text = _clean_text("".join(self._txt_parts))
            if not self.posted_at:
                match = ARTICLE_DATE_PATTERN.search(text)
                if match:
                    self.posted_at = match.group(0)
            self._txt_depth = 0
            self._txt_parts = []
        if self.stack:
            self.stack.pop()

    def _has_ancestor_class(self, class_name: str) -> bool:
        return any(class_name in attrs.get("class", "").split() for _, attrs in self.stack)


def _title_from_article_sections(sections: list[tuple[list[str], list[str]]]) -> str:
    for body_lines, content_lines in sections:
        title = _title_from_header_lines(_header_lines(body_lines, content_lines))
        if title:
            return title
    return ""


def _author_from_article_sections(sections: list[tuple[list[str], list[str]]]) -> str:
    for body_lines, content_lines in sections:
        author = _author_from_header_lines(_header_lines(body_lines, content_lines))
        if author:
            return author
    return ""


def _posted_at_from_article_sections(sections: list[tuple[list[str], list[str]]]) -> str:
    for body_lines, content_lines in sections:
        for line in _header_lines(body_lines, content_lines):
            match = ARTICLE_DATE_PATTERN.search(line)
            if match:
                return match.group(0)
    return ""


def _title_from_content_text(value: str) -> str:
    lines = _text_lines(value)
    if not lines:
        return ""
    first = _strip_contact_info(lines[0])
    first = re.split(
        r"\s+(?:\uc0ac\uc774\uc988|\uc5f0\ub77d|\uc804\ud654|\uac00\uaca9|\ud310\ub9e4\uae08\uc561|\ud310\ub9e4\ud76c\ub9dd\uac00|010[-\s]?\d)",
        first,
        maxsplit=1,
    )[0]
    first = re.split(r"[.!?。]", first, maxsplit=1)[0]
    first = _clean_text(first)
    if len(first) < 2 or _looks_non_article_title(first):
        return ""
    return first[:80]


def _strip_contact_info(value: str) -> str:
    text = re.sub(r"010[-\s]?\d{3,4}[-\s]?\d{4}", "", value)
    text = re.sub(r"010[-\s]?[가-힣]{2,}", "", text)
    return _clean_text(text)


def _header_lines(body_lines: list[str], content_lines: list[str]) -> list[str]:
    first_content_line = next((line for line in content_lines if line), "")
    if not first_content_line:
        return body_lines[:20]
    for index, line in enumerate(body_lines):
        if _same_or_contained_line(line, first_content_line):
            return body_lines[:index]
    return body_lines[:20]


def _same_or_contained_line(left: str, right: str) -> bool:
    left = _clean_text(left)
    right = _clean_text(right)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 12:
        return False
    return left in right or right in left


def _title_from_header_lines(lines: list[str]) -> str:
    for index, line in enumerate(lines):
        if _looks_author_meta_line(line):
            for candidate in reversed(lines[:index]):
                if not _is_article_header_noise(candidate):
                    return candidate
    candidates = [line for line in lines if not _is_article_header_noise(line)]
    if len(candidates) >= 2 and _looks_category_line(candidates[0]):
        return candidates[1]
    return candidates[0] if candidates else ""


def _author_from_header_lines(lines: list[str]) -> str:
    for line in lines:
        if not _looks_author_meta_line(line):
            continue
        author = line
        for token in ("추천", "조회", "댓글"):
            position = author.find(token)
            if position > 0:
                author = author[:position]
                break
        author = re.sub(r"^(작성자|글쓴이)\s*", "", author.strip())
        author = _clean_text(author)
        if author and not ARTICLE_DATE_PATTERN.search(author):
            return author
    return ""


def _looks_author_meta_line(line: str) -> bool:
    return bool(ARTICLE_DATE_PATTERN.search(line)) or ("추천" in line and "조회" in line)


def _is_article_header_noise(line: str) -> bool:
    text = _clean_text(line)
    if len(text) < 2:
        return True
    if _looks_author_meta_line(text):
        return True
    if text in {"Daum", "카페", "메일", "댓글", "스크랩"}:
        return True
    if "Daum" in text and ("카페" in text or "Cafe" in text):
        return True
    if text.startswith(("http://", "https://")):
        return True
    return False


def _looks_category_line(line: str) -> bool:
    text = _clean_text(line)
    return (
        ("관련 중고" in text or text.startswith(("패러관련", "장비관련", "기타관련")))
        and text.endswith(("삽니다", "팝니다", "판매"))
        and len(text) <= 30
    )


def _is_notice_title(title: str) -> bool:
    normalized = title.strip()
    if not normalized:
        return True
    notice_prefixes = (
        "공지",
        "[공지",
        "필독",
        "[필독",
        "운영",
        "[운영",
        # Mojibake variants seen from old Daum mobile pages when text is decoded badly.
        "怨듭",
        "[怨듭",
        "????",
    )
    if normalized.startswith(notice_prefixes):
        return True
    notice_fragments = (
        "인터넷 익스플로러",
        "Internet Explorer",
        "브라우저 지원",
        "카페 이용을 위해",
        "Daum카페 라운지",
    )
    return any(fragment in normalized for fragment in notice_fragments)


def _first_query(query: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query.get(key)
        if values:
            return values[0]
    return None


def _post_key_from_url(default_board_id: str, href: str) -> str | None:
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    board_id = _first_query(query, "fldid", "board", "folderid") or default_board_id
    for key in POST_ID_QUERY_KEYS:
        if query.get(key):
            return f"{board_id}:{query[key][0]}"
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 3 and path_parts[-2].isalnum() and path_parts[-1].isdigit():
        return f"{path_parts[-2]}:{path_parts[-1]}"
    for pattern in POST_ID_PATTERNS:
        match = pattern.search(href)
        if match and match.groupdict().get("id"):
            return f"{match.groupdict().get('board') or board_id}:{match.group('id')}"
    return None


def _post_keys_from_text(default_board_id: str, value: str) -> set[str]:
    keys: set[str] = set()
    for chunk in _identity_text_chunks(value):
        for pattern in STRUCTURED_POST_KEY_PATTERNS:
            for match in pattern.finditer(chunk):
                board_id = match.groupdict().get("board") or default_board_id
                post_id = match.groupdict().get("id")
                if board_id and post_id:
                    keys.add(f"{board_id}:{post_id}")
    return keys


def _identity_text_chunks(value: str) -> list[str]:
    text = value or ""
    chunks = re.findall(r"\{[^{}]{0,500}\}", text)
    chunks.extend(
        match.group(1)
        for match in re.finditer(
            r"['\"]([^'\"]{0,500}(?:fldid|board|folderid|dataid|datanum|articleid|bbsid)[^'\"]{0,500})['\"]",
            text,
            re.IGNORECASE,
        )
    )
    return chunks


def _clean_url(value: str) -> str:
    parsed = urlparse(value)
    return parsed._replace(fragment="").geturl()


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


def _safe_title(page: Page) -> str:
    try:
        return page.title()
    except Exception:
        return ""


def _looks_cafe_page_title(value: str) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    if "Daum" not in text:
        return False
    return len(text) <= 120


def _looks_non_article_title(value: str) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    meta_prefixes = (
        "\uc870\ud68c",
        "\uc870\ud68c\uc218",
        "\ucd94\ucc9c",
        "\ub313\uae00",
        "\uc2a4\ud06c\ub7a9",
    )
    return any(text == prefix or text.startswith(f"{prefix} ") for prefix in meta_prefixes)


def _emit(progress: Any | None, event: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress(event, payload)


def _emit_detail_timing(
    progress: Any | None,
    ref: PostRef,
    phase: str,
    elapsed_ms: int,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "post_key": ref.post_key,
        "title": ref.title,
        "phase": phase,
        "elapsed_ms": elapsed_ms,
    }
    if extra:
        payload.update(extra)
    _emit(progress, "post_timing", payload)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _article_link_samples(links: list[dict[str, str]]) -> list[dict[str, str]]:
    samples = []
    for link in links:
        href = str(link.get("href") or link.get("rawHref") or link.get("dataHref") or "")
        onclick = str(link.get("onclick") or "")
        raw = f"{href} {onclick}".lower()
        if not any(
            token in raw
            for token in ("bbs_read", "dataid", "datanum", "articleid", "bbsid", "goarticle")
        ):
            continue
        samples.append(
            {
                "text": _clean_text(str(link.get("text") or "")),
                "href": href,
                "onclick": onclick,
            }
        )
        if len(samples) >= 10:
            break
    return samples
