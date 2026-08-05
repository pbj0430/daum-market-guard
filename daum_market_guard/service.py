from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import UnidentifiedImageError

from .commenter import process_pending_comments
from .config import AppConfig
from .db import Database
from .detector import assess_post, maybe_blacklist
from .hashing import fingerprint_image
from .models import PostDetail, PostRef
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
            "strategy": config.scan_strategy,
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
                refs = _collect_post_refs(config, db, scraper, board, progress)
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
                    post_number = _post_number(ref)
                    if not config.rescan_existing_posts and db.post_key_exists(ref.post_key):
                        _emit(
                            progress,
                            "post_skipped",
                            {
                                "board": board.name,
                                "index": index,
                                "total": len(refs),
                                "post_key": ref.post_key,
                                "title": ref.title,
                            },
                        )
                        continue
                    if post_number is not None and db.missing_post_exists(ref.board_id, post_number):
                        _emit(
                            progress,
                            "post_skipped",
                            {
                                "board": board.name,
                                "index": index,
                                "total": len(refs),
                                "post_key": ref.post_key,
                                "title": "known missing",
                            },
                        )
                        continue
                    _emit(
                        progress,
                        "post_started",
                        {
                            "board": board.name,
                            "index": index,
                            "total": len(refs),
                            "post_key": ref.post_key,
                            "title": ref.title,
                            "url": ref.url,
                        },
                    )
                    try:
                        post_started_at = time.monotonic()
                        started = time.monotonic()
                        detail = scraper.collect_post_detail(ref, progress=progress)
                        detail_ms = _elapsed_ms(started)
                        _emit_service_timing(progress, ref, "detail", detail_ms)
                    except Exception as exc:
                        if post_number is not None and _looks_missing_error(exc):
                            db.mark_missing_post(ref.board_id, post_number)
                            _emit(
                                progress,
                                "post_missing",
                                {
                                    "board": board.name,
                                    "index": index,
                                    "total": len(refs),
                                    "post_key": ref.post_key,
                                    "url": ref.url,
                                },
                            )
                            continue
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
                    started = time.monotonic()
                    post_id = db.upsert_post(detail)
                    db_ms = _elapsed_ms(started)
                    _emit_service_timing(progress, ref, "db_upsert", db_ms, {"post_id": post_id})
                    started = time.monotonic()
                    stored_images = _store_images(config, db, scraper, post_id, detail, progress)
                    images_ms = _elapsed_ms(started)
                    stats.images += stored_images
                    _emit_service_timing(
                        progress,
                        ref,
                        "images_total",
                        images_ms,
                        {"found": len(detail.images), "stored": stored_images},
                    )
                    started = time.monotonic()
                    assessment = assess_post(db, post_id, config.duplicate_hamming_threshold)
                    assess_ms = _elapsed_ms(started)
                    _emit_service_timing(progress, ref, "assess", assess_ms, {"score": assessment.score})
                    started = time.monotonic()
                    db.save_assessment(assessment)
                    assessment_save_ms = _elapsed_ms(started)
                    _emit_service_timing(progress, ref, "assessment_save", assessment_save_ms)
                    started = time.monotonic()
                    maybe_blacklist(db, assessment, config.blacklist_score_threshold)
                    blacklist_ms = _elapsed_ms(started)
                    _emit_service_timing(progress, ref, "blacklist", blacklist_ms)
                    assessed += 1
                    post_total_ms = _elapsed_ms(post_started_at)
                    _emit_service_timing(
                        progress,
                        ref,
                        "post_total",
                        post_total_ms,
                        {
                            "detail_ms": detail_ms,
                            "db_ms": db_ms,
                            "images_ms": images_ms,
                            "assess_ms": assess_ms,
                            "assessment_save_ms": assessment_save_ms,
                            "blacklist_ms": blacklist_ms,
                        },
                    )
                    _emit(
                        progress,
                        "post_done",
                        {
                            "post_id": post_id,
                            "post_key": detail.ref.post_key,
                            "title": detail.title,
                            "author": detail.author_name,
                            "images": len(detail.images),
                            "stored_images": stored_images,
                            "score": assessment.score,
                            "elapsed_ms": post_total_ms,
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


def _collect_post_refs(
    config: AppConfig,
    db: Database,
    scraper: DaumCafeScraper,
    board,
    progress: ProgressCallback | None,
) -> list[PostRef]:
    if config.scan_strategy != "direct_numbers":
        return scraper.collect_board_posts(board, progress=progress)

    latest_refs = scraper.collect_board_posts(board, progress=progress)
    latest_number = max((_post_number(ref) or 0 for ref in latest_refs), default=0)
    saved_max = db.max_post_number(board.board_id)
    if latest_number <= 0:
        _emit(
            progress,
            "direct_scan_range",
            {"board": board.name, "latest": 0, "saved_max": saved_max, "count": 0},
        )
        return []

    start = latest_number
    stop = max(1, config.direct_scan_min_post_id)
    numbers = list(range(start, stop - 1, -1))
    if config.direct_scan_limit_per_board > 0:
        numbers = numbers[: config.direct_scan_limit_per_board]
    _emit(
        progress,
        "direct_scan_range",
        {
            "board": board.name,
            "latest": latest_number,
            "saved_max": saved_max,
            "stop": stop,
            "count": len(numbers),
            "limit": config.direct_scan_limit_per_board,
        },
    )
    cafe_id = _direct_cafe_id(config, board)
    return [
        PostRef(
            board_id=board.board_id,
            url=_direct_read_url(config, board, number, cafe_id),
            title=f"{board.board_id} #{number}",
            post_key=f"{board.board_id}:{number}",
        )
        for number in numbers
    ]


def _direct_read_url(config: AppConfig, board, number: int, cafe_id: str) -> str:
    grpid = config.cafe_grpid or cafe_id
    return (
        "https://cafe.daum.net/_c21_/bbs_read"
        f"?grpid={grpid}&fldid={board.board_id}&datanum={number}"
    )


def _direct_cafe_id(config: AppConfig, board) -> str:
    for url in (board.url, config.cafe_url):
        path = [part for part in urlparse(url).path.split("/") if part]
        if path and path[0] != "_c21_":
            return path[0]
    return "730418"


def _post_number(ref: PostRef) -> int | None:
    try:
        return int(ref.post_key.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _looks_missing_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "not found" in text
        or "not a readable post" in text
        or "resolved to different article" in text
    )


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
        image_started_at = time.monotonic()
        _emit(
            progress,
            "image_started",
            {
                "post_id": post_id,
                "post_key": detail.ref.post_key,
                "index": index,
                "total": len(detail.images),
                "url": image.url,
            },
        )
        started = time.monotonic()
        if db.image_exists(post_id, image.url):
            _emit_image_done(
                progress,
                detail,
                index,
                len(detail.images),
                image.url,
                "already_exists",
                {"db_check_ms": _elapsed_ms(started), "total_ms": _elapsed_ms(image_started_at)},
            )
            continue
        db_check_ms = _elapsed_ms(started)
        started = time.monotonic()
        image_bytes = scraper.download_image(image.url, detail.ref.url)
        if not image_bytes:
            _emit_image_done(
                progress,
                detail,
                index,
                len(detail.images),
                image.url,
                "download_failed",
                {
                    "db_check_ms": db_check_ms,
                    "download_ms": _elapsed_ms(started),
                    "total_ms": _elapsed_ms(image_started_at),
                },
            )
            continue
        download_ms = _elapsed_ms(started)
        started = time.monotonic()
        try:
            fingerprint = fingerprint_image(image_bytes)
        except (UnidentifiedImageError, OSError):
            _emit_image_done(
                progress,
                detail,
                index,
                len(detail.images),
                image.url,
                "fingerprint_failed",
                {
                    "db_check_ms": db_check_ms,
                    "download_ms": download_ms,
                    "fingerprint_ms": _elapsed_ms(started),
                    "bytes": len(image_bytes),
                    "total_ms": _elapsed_ms(image_started_at),
                },
            )
            continue
        fingerprint_ms = _elapsed_ms(started)
        started = time.monotonic()
        local_path = save_image_bytes(
            config.image_dir,
            detail.ref.post_key,
            image.url,
            image_bytes,
        )
        file_save_ms = _elapsed_ms(started)
        started = time.monotonic()
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
        db_add_ms = _elapsed_ms(started)
        stored += 1
        _emit_image_done(
            progress,
            detail,
            index,
            len(detail.images),
            image.url,
            "stored",
            {
                "db_check_ms": db_check_ms,
                "download_ms": download_ms,
                "fingerprint_ms": fingerprint_ms,
                "file_save_ms": file_save_ms,
                "db_add_ms": db_add_ms,
                "bytes": len(image_bytes),
                "width": fingerprint.width,
                "height": fingerprint.height,
                "total_ms": _elapsed_ms(image_started_at),
            },
        )
    return stored


def _emit(progress: ProgressCallback | None, event: str, payload: dict[str, Any]) -> None:
    if progress is not None:
        progress(event, payload)


def _emit_service_timing(
    progress: ProgressCallback | None,
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


def _emit_image_done(
    progress: ProgressCallback | None,
    detail: PostDetail,
    index: int,
    total: int,
    url: str,
    status: str,
    timings: dict[str, Any],
) -> None:
    payload: dict[str, Any] = {
        "post_key": detail.ref.post_key,
        "title": detail.title,
        "index": index,
        "total": total,
        "url": url,
        "status": status,
    }
    payload.update(timings)
    _emit(progress, "image_done", payload)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _print_progress(event: str, payload: dict[str, Any]) -> None:
    if event == "scan_started":
        print(
            "[scan] started: "
            f"boards={payload['boards']} headless={payload['headless']} "
            f"strategy={payload.get('strategy', 'board_list')} "
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
            f"[scan] post {payload['index']}/{payload['total']}: "
            f"{payload.get('post_key', '-')} {payload['title']}",
            flush=True,
        )
    elif event == "post_timing":
        print(
            "[time] "
            f"{payload.get('post_key', '-')} phase={payload.get('phase')} "
            f"elapsed={payload.get('elapsed_ms')}ms {_timing_extra(payload)}",
            flush=True,
        )
    elif event == "direct_scan_range":
        limit = payload.get("limit")
        limit_text = "all" if not limit else str(limit)
        print(
            "[scan] direct range: "
            f"board={payload['board']} latest={payload['latest']} "
            f"saved_max={payload.get('saved_max', 0)} stop={payload.get('stop', '-')} "
            f"numbers={payload['count']} limit={limit_text}",
            flush=True,
        )
    elif event == "post_skipped":
        if _should_print_skip(payload):
            print(
                f"[scan] skipped {payload['index']}/{payload['total']}: "
                f"{payload['post_key']} {payload['title']}",
                flush=True,
            )
    elif event == "post_missing":
        if _should_print_skip(payload):
            print(
                f"[scan] missing {payload['index']}/{payload['total']}: "
                f"{payload['post_key']} {payload['url']}",
                flush=True,
            )
    elif event == "image_started":
        print(
            "[image] "
            f"{payload.get('post_key', '-')} {payload.get('index')}/{payload.get('total')} "
            f"start {payload.get('url')}",
            flush=True,
        )
    elif event == "image_done":
        print(
            "[image] "
            f"{payload.get('post_key', '-')} {payload.get('index')}/{payload.get('total')} "
            f"status={payload.get('status')} total={payload.get('total_ms')}ms "
            f"download={payload.get('download_ms', '-')}ms "
            f"hash={payload.get('fingerprint_ms', '-')}ms "
            f"file={payload.get('file_save_ms', '-')}ms "
            f"db={payload.get('db_add_ms', '-')}ms "
            f"bytes={payload.get('bytes', '-')}",
            flush=True,
        )
    elif event == "post_done":
        print(
            f"[scan] saved: score={payload['score']} images={payload['stored_images']} "
            f"elapsed={payload.get('elapsed_ms', '-')}ms "
            f"key={payload.get('post_key', '-')} author={payload['author']} title={payload['title']}",
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


def _timing_extra(payload: dict[str, Any]) -> str:
    ignored = {"post_key", "phase", "elapsed_ms"}
    parts = []
    for key in (
        "frames",
        "body_frames",
        "chars",
        "images",
        "found",
        "stored",
        "score",
        "detail_ms",
        "db_ms",
        "images_ms",
        "assess_ms",
        "assessment_save_ms",
        "blacklist_ms",
        "final_url",
        "title",
        "author",
        "posted_at",
    ):
        if key in ignored or key not in payload:
            continue
        value = payload[key]
        if key == "final_url":
            value = _short(value, 140)
        elif key in {"title", "author", "posted_at"}:
            value = _short(value, 60)
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _should_print_skip(payload: dict[str, Any]) -> bool:
    index = int(payload.get("index") or 0)
    total = int(payload.get("total") or 0)
    return index <= 5 or index == total or index % 100 == 0
