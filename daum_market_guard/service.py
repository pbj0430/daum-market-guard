from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

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


ProgressCallback = Callable[[str, dict[str, Any]], None]


def run_once(config: AppConfig, progress: ProgressCallback | None = None) -> RunResult:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.image_dir.mkdir(parents=True, exist_ok=True)
    db = Database(config.db_path)
    stats = ScrapeStats()
    assessed = 0
    _emit(progress, "scan_started", {})
    try:
        with DaumCafeScraper(config) as scraper:
            for board in config.boards:
                stats.board_count += 1
                _emit(progress, "board_started", {"board": board.name, "url": board.url})
                refs = scraper.collect_board_posts(board)
                stats.post_refs += len(refs)
                _emit(
                    progress,
                    "board_posts_found",
                    {"board": board.name, "count": len(refs)},
                )
                for index, ref in enumerate(refs, start=1):
                    _emit(
                        progress,
                        "post_started",
                        {
                            "board": board.name,
                            "index": index,
                            "total": len(refs),
                            "title": ref.title,
                            "url": ref.url,
                        },
                    )
                    try:
                        detail = scraper.collect_post_detail(ref)
                    except Exception as exc:
                        _emit(
                            progress,
                            "post_failed",
                            {
                                "title": ref.title,
                                "url": ref.url,
                                "error": str(exc),
                            },
                        )
                        continue
                    stats.post_details += 1
                    post_id = db.upsert_post(detail)
                    stored_images = _store_images(config, db, scraper, post_id, detail, progress)
                    stats.images += stored_images
                    assessment = assess_post(db, post_id, config.duplicate_hamming_threshold)
                    db.save_assessment(assessment)
                    maybe_blacklist(db, assessment, config.blacklist_score_threshold)
                    assessed += 1
                    _emit(
                        progress,
                        "post_done",
                        {
                            "post_id": post_id,
                            "title": detail.title,
                            "author": detail.author_name,
                            "images": len(detail.images),
                            "stored_images": stored_images,
                            "score": assessment.score,
                        },
                    )
        comments = process_pending_comments(config, db)
        result = RunResult(stats=stats, assessed=assessed, comments=comments)
        _emit(
            progress,
            "scan_done",
            {
                "boards": stats.board_count,
                "refs": stats.post_refs,
                "posts": stats.post_details,
                "images": stats.images,
                "assessed": assessed,
                "comments": comments,
            },
        )
        return result
    except Exception as exc:
        _emit(progress, "scan_failed", {"error": str(exc)})
        raise
    finally:
        db.close()


def run_forever(config: AppConfig) -> None:
    while True:
        started = time.monotonic()
        try:
            result = run_once(config, progress=_print_progress)
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
    progress: ProgressCallback | None = None,
) -> int:
    stored = 0
    for index, image in enumerate(detail.images, start=1):
        _emit(
            progress,
            "image_started",
            {"post_id": post_id, "index": index, "total": len(detail.images), "url": image.url},
        )
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


def _emit(progress: ProgressCallback | None, event: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress(event, payload)


def _print_progress(event: str, payload: dict[str, Any]) -> None:
    if event == "board_started":
        print(f"[scan] board: {payload['board']}", flush=True)
    elif event == "board_posts_found":
        print(f"[scan] posts found: {payload['board']} count={payload['count']}", flush=True)
    elif event == "post_started":
        print(
            f"[scan] post {payload['index']}/{payload['total']}: {payload['title']}",
            flush=True,
        )
    elif event == "post_done":
        print(
            f"[scan] saved: score={payload['score']} images={payload['stored_images']} "
            f"author={payload['author']} title={payload['title']}",
            flush=True,
        )
    elif event == "scan_failed":
        print(f"[scan] failed: {payload['error']}", flush=True)
