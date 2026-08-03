from __future__ import annotations

import time
from dataclasses import dataclass

from PIL import UnidentifiedImageError

from .commenter import process_pending_comments
from .config import AppConfig
from .db import Database
from .detector import assess_post, maybe_blacklist
from .hashing import fingerprint_image
from .models import PostDetail
from .scraper import DaumCafeScraper, ScrapeStats, save_image_bytes


@dataclass
class RunResult:
    stats: ScrapeStats
    assessed: int
    comments: int


def run_once(config: AppConfig) -> RunResult:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.image_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    stats = ScrapeStats()
    assessed = 0
    try:
        with DaumCafeScraper(config) as scraper:
            for board in config.boards:
                stats.board_count += 1
                refs = scraper.collect_board_posts(board)
                stats.post_refs += len(refs)
                for ref in refs:
                    detail = scraper.collect_post_detail(ref)
                    stats.post_details += 1
                    post_id = db.upsert_post(detail)
                    stats.images += _store_images(config, db, scraper, post_id, detail)
                    assessment = assess_post(db, post_id, config.duplicate_hamming_threshold)
                    db.save_assessment(assessment)
                    maybe_blacklist(db, assessment, config.blacklist_score_threshold)
                    assessed += 1
        comments = process_pending_comments(config, db)
        return RunResult(stats=stats, assessed=assessed, comments=comments)
    finally:
        db.close()


def run_forever(config: AppConfig) -> None:
    while True:
        started = time.monotonic()
        try:
            result = run_once(config)
            print(
                "scan complete: "
                f"boards={result.stats.board_count} "
                f"refs={result.stats.post_refs} "
                f"posts={result.stats.post_details} "
                f"images={result.stats.images} "
                f"assessed={result.assessed} "
                f"comments={result.comments}",
                flush=True,
            )
        except Exception as exc:
            print(f"scan failed: {exc}", flush=True)
        elapsed = time.monotonic() - started
        sleep_for = max(10, config.poll_interval_seconds - elapsed)
        time.sleep(sleep_for)


def _store_images(
    config: AppConfig,
    db: Database,
    scraper: DaumCafeScraper,
    post_id: int,
    detail: PostDetail,
) -> int:
    stored = 0
    for image in detail.images:
        if db.image_exists(post_id, image.url):
            continue
        image_bytes = scraper.download_image(image.url, detail.ref.url)
        if not image_bytes:
            continue
        try:
            fingerprint = fingerprint_image(image_bytes)
        except (UnidentifiedImageError, OSError):
            continue
        local_path = save_image_bytes(
            config.image_dir,
            detail.ref.post_key,
            image.url,
            image_bytes,
        )
        db.add_image(
            post_id=post_id,
            image_url=image.url,
            local_path=str(local_path),
            sha256=fingerprint.sha256,
            ahash=fingerprint.ahash,
            dhash=fingerprint.dhash,
            width=fingerprint.width,
            height=fingerprint.height,
        )
        stored += 1
    return stored
