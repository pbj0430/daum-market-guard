from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .config import AppConfig
from .db import Database
from .scraper import DaumCafeScraper

if TYPE_CHECKING:
    from playwright.sync_api import Page
else:
    Page = object


def render_comment(template: str, row) -> str:
    links = json.loads(row["candidate_posts_json"] or "[]")
    source_links = ", ".join(links[:3]) if links else "없음"
    return template.format(
        score=row["score"],
        duplicate_image_count=row["duplicate_image_count"],
        duplicate_post_count=row["duplicate_post_count"],
        source_links=source_links,
        title=row["title"],
        url=row["url"],
        author_name=row["author_name"],
        author_id=row["author_id"],
    ).strip()


def process_pending_comments(config: AppConfig, db: Database) -> int:
    if not config.comment.enabled:
        return 0
    pending = db.pending_comments(config.comment.min_score, config.comment.max_per_run)
    if not pending:
        return 0

    posted = 0
    if config.comment.mode == "dry_run":
        for row in pending:
            body = render_comment(config.comment.template, row)
            db.save_comment(row["assessment_id"], row["post_id"], body, "dry_run", "dry_run")
            posted += 1
        return posted

    with DaumCafeScraper(config) as scraper:
        for row in pending:
            body = render_comment(config.comment.template, row)
            try:
                page = scraper.page()
                page.goto(row["url"], wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1500)
                _submit_comment(page, config, body)
                db.save_comment(row["assessment_id"], row["post_id"], body, "post", "posted")
                posted += 1
            except Exception as exc:
                db.save_comment(
                    row["assessment_id"],
                    row["post_id"],
                    body,
                    "post",
                    "failed",
                    str(exc),
                )
    return posted


def _submit_comment(page: Page, config: AppConfig, body: str) -> None:
    last_error: Exception | None = None
    for frame in page.frames:
        try:
            textarea = frame.locator(config.selectors.comment_textarea).last
            textarea.wait_for(state="visible", timeout=3000)
            textarea.fill(body)
            submit = frame.locator(config.selectors.comment_submit).last
            submit.click(timeout=5000)
            page.wait_for_timeout(1000)
            return
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"댓글 입력 영역을 찾지 못했습니다: {last_error}")
