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
    _emit(
        progress,
        "scan_started",
        {
            "boards": len(config.boards),
            "headless": config.headless,
            "mobile_fallback": config.allow_mobile_fallback,
            "data_dir": str(config.data_dir),
            "profile": str(config.user_data_dir),
        },
    )
    try:
        with DaumCafeScraper(config) as scraper:
            for board in config.boards:
                stats.board_count += 1
                _emit(progress, "board_started", {"board": board.name, "url": board.url})
                refs = scraper.collect_board_posts(board, progress=progress)
                stats.post_refs += len(refs)
                _emit(
                    progress,
                    "board_posts_found",
                    {"board": board.name, "count": len(refs)},
                )
                if not refs:
                    try:
                        _emit(progress, "board_debug", scraper.inspect_board(board))
                    except Exception as exc:
                        _emit(
                            progress,
                            "board_debug_failed",
                            {"board": board.name, "error": str(exc)},
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
    if event == "scan_started":
        print(
            "[scan] started: "
            f"boards={payload['boards']} headless={payload['headless']} "
            f"mobile_fallback={payload['mobile_fallback']} "
            f"profile={payload['profile']} data={payload['data_dir']}",
            flush=True,
        )
    elif event == "board_started":
        print(f"[scan] board: {payload['board']} url={payload['url']}", flush=True)
    elif event == "board_page_opening":
        print(
            f"[scan]   opening page {payload['page']}: {payload['url']}",
            flush=True,
        )
    elif event == "board_page_loaded":
        print(
            "[scan]   loaded: "
            f"final={payload['page_url']} frames={payload['frame_count']} "
            f"links={payload['link_count']} accepted={payload['accepted_count']} "
            f"title={_short(payload['title'], 80)}",
            flush=True,
        )
        for sample in payload.get("accepted_samples", [])[:5]:
            print(
                "[scan]     latest candidate: "
                f"{sample.get('post_key')} title={_short(sample.get('title'), 80)} "
                f"url={sample.get('url')}",
                flush=True,
            )
        if not payload.get("accepted_samples"):
            for sample in payload.get("article_link_samples", [])[:5]:
                print(
                    "[scan]     article-like link: "
                    f"text={_short(sample.get('text'), 80)} "
                    f"href={_short(sample.get('href'), 160)} "
                    f"onclick={_short(sample.get('onclick'), 120)}",
                    flush=True,
                )
    elif event == "board_posts_found":
        print(f"[scan] posts found: {payload['board']} count={payload['count']}", flush=True)
    elif event == "board_debug":
        _print_board_debug(payload)
    elif event == "board_debug_failed":
        print(
            f"[scan] debug failed: {payload['board']} error={payload['error']}",
            flush=True,
        )
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


def _print_board_debug(payload: dict[str, Any]) -> None:
    print(
        "[scan] zero-result debug: "
        f"board={payload.get('board')} accepted={payload.get('accepted_count')} "
        f"links={payload.get('link_count')} logged_out={payload.get('logged_out')} "
        f"unsupported={payload.get('unsupported_browser')}",
        flush=True,
    )
    for report in payload.get("url_reports", []):
        print(
            "[scan]   page: "
            f"requested={report.get('requested_url')} final={report.get('page_url')} "
            f"title={_short(report.get('title'), 120)} frames={report.get('frame_count')} "
            f"links={report.get('link_count')} accepted={report.get('accepted_count')} "
            f"logged_out={report.get('logged_out')} unsupported={report.get('unsupported_browser')}",
            flush=True,
        )
        for sample in report.get("accepted_samples", [])[:5]:
            print(
                "[scan]     accepted: "
                f"title={_short(sample.get('title'), 80)} "
                f"key={sample.get('post_key')} url={sample.get('url')}",
                flush=True,
            )
        if not report.get("accepted_samples"):
            for sample in report.get("link_samples", [])[:5]:
                text = _short(sample.get("text"), 80)
                href = sample.get("href") or sample.get("rawHref") or sample.get("dataHref")
                print(
                    f"[scan]     link sample: text={text} href={_short(href, 160)}",
                    flush=True,
                )
        excerpt = _short(report.get("body_excerpt"), 300)
        if excerpt:
            print(f"[scan]     body: {excerpt}", flush=True)


def _short(value: Any, max_length: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."
