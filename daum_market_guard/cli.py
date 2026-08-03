from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .commenter import process_pending_comments
from .config import load_config
from .db import Database
from .scraper import DaumCafeScraper
from .service import run_forever, run_once


def main(argv: list[str] | None = None) -> None:
    argv = _normalize_global_options(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="daum-market-guard")
    parser.add_argument("--config", default="config.toml", help="config TOML path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-config", help="copy config.example.toml to config.toml")
    subparsers.add_parser("login", help="open browser and save authenticated session")
    subparsers.add_parser("scan", help="run one scan")
    subparsers.add_parser("daemon", help="run scans forever")

    suspects = subparsers.add_parser("suspects", help="list suspicious posts")
    suspects.add_argument("--min-score", type=int, default=70)

    comments = subparsers.add_parser("comments", help="process pending comments")
    comments.add_argument("--min-score", type=int, default=None)

    blacklist = subparsers.add_parser("blacklist", help="manage blacklist")
    blacklist_sub = blacklist.add_subparsers(dest="blacklist_command", required=True)
    blacklist_sub.add_parser("list")
    blacklist_add = blacklist_sub.add_parser("add")
    blacklist_add.add_argument("--author-name", default="")
    blacklist_add.add_argument("--author-id", default="")
    blacklist_add.add_argument("--reason", required=True)
    blacklist_remove = blacklist_sub.add_parser("remove")
    blacklist_remove.add_argument("--author-name", default="")
    blacklist_remove.add_argument("--author-id", default="")

    args = parser.parse_args(argv)
    if args.command == "init-config":
        _init_config(Path(args.config))
        return

    config = load_config(args.config)
    if args.command == "login":
        login_config = _replace_headless(config, False)
        login_config.data_dir.mkdir(parents=True, exist_ok=True)
        with DaumCafeScraper(login_config) as scraper:
            scraper.login_interactive()
        print(f"login session saved under: {login_config.user_data_dir}")
        return
    if args.command == "scan":
        result = run_once(config)
        print(
            f"boards={result.stats.board_count} refs={result.stats.post_refs} "
            f"posts={result.stats.post_details} images={result.stats.images} "
            f"assessed={result.assessed} comments={result.comments}"
        )
        return
    if args.command == "daemon":
        run_forever(config)
        return
    if args.command == "suspects":
        _print_suspects(config, args.min_score)
        return
    if args.command == "comments":
        if args.min_score is not None:
            config = _replace_comment_min_score(config, args.min_score)
        db = Database(config.db_path)
        try:
            count = process_pending_comments(config, db)
        finally:
            db.close()
        print(f"comments processed={count}")
        return
    if args.command == "blacklist":
        _blacklist(config, args)
        return


def _normalize_global_options(argv: list[str]) -> list[str]:
    """Allow global options before or after subcommands."""
    normalized: list[str] = []
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--config":
            normalized.append(item)
            if index + 1 < len(argv):
                normalized.append(argv[index + 1])
                index += 2
            else:
                index += 1
            continue
        if item.startswith("--config="):
            normalized.append(item)
            index += 1
            continue
        remaining.append(item)
        index += 1
    return normalized + remaining


def _init_config(path: Path) -> None:
    source = Path(__file__).resolve().parent.parent / "config.example.toml"
    if path.exists():
        raise SystemExit(f"already exists: {path}")
    shutil.copyfile(source, path)
    print(f"created: {path}")


def _print_suspects(config, min_score: int) -> None:
    db = Database(config.db_path)
    try:
        rows = db.list_suspects(min_score)
    finally:
        db.close()
    if not rows:
        print("no suspects")
        return
    for row in rows:
        reasons = json.loads(row["reasons_json"] or "[]")
        links = json.loads(row["candidate_posts_json"] or "[]")
        print(f"[{row['score']:3d}] {row['title']}")
        print(f"      author={row['author_name'] or row['author_id'] or '-'}")
        print(f"      url={row['url']}")
        print(f"      duplicate_images={row['duplicate_image_count']} duplicate_posts={row['duplicate_post_count']}")
        if reasons:
            print(f"      reason={'; '.join(reasons)}")
        if links:
            print(f"      sources={', '.join(links[:5])}")


def _blacklist(config, args) -> None:
    db = Database(config.db_path)
    try:
        if args.blacklist_command == "list":
            rows = db.list_blacklist()
            if not rows:
                print("blacklist is empty")
                return
            for row in rows:
                author = row["author_id"] or row["author_name"] or "-"
                print(f"{author}: score={row['score']} reason={row['reason']} created={row['created_at']}")
            return
        if args.blacklist_command == "add":
            if not args.author_name and not args.author_id:
                raise SystemExit("author-name or author-id is required")
            db.add_blacklist(args.author_name, args.author_id, args.reason)
            print("blacklist entry added")
            return
        if args.blacklist_command == "remove":
            if not args.author_name and not args.author_id:
                raise SystemExit("author-name or author-id is required")
            count = db.deactivate_blacklist(args.author_name, args.author_id)
            print(f"blacklist entries deactivated={count}")
            return
    finally:
        db.close()


def _replace_headless(config, headless: bool):
    return type(config)(
        cafe_url=config.cafe_url,
        login_url=config.login_url,
        data_dir=config.data_dir,
        user_data_dir=config.user_data_dir,
        poll_interval_seconds=config.poll_interval_seconds,
        headless=headless,
        browser_executable_path=config.browser_executable_path,
        max_pages_per_board=config.max_pages_per_board,
        max_posts_per_board_page=config.max_posts_per_board_page,
        image_timeout_seconds=config.image_timeout_seconds,
        duplicate_hamming_threshold=config.duplicate_hamming_threshold,
        blacklist_score_threshold=config.blacklist_score_threshold,
        boards=config.boards,
        comment=config.comment,
        selectors=config.selectors,
    )


def _replace_comment_min_score(config, min_score: int):
    comment = type(config.comment)(
        enabled=config.comment.enabled,
        mode=config.comment.mode,
        min_score=min_score,
        max_per_run=config.comment.max_per_run,
        template=config.comment.template,
    )
    return type(config)(
        cafe_url=config.cafe_url,
        login_url=config.login_url,
        data_dir=config.data_dir,
        user_data_dir=config.user_data_dir,
        poll_interval_seconds=config.poll_interval_seconds,
        headless=config.headless,
        browser_executable_path=config.browser_executable_path,
        max_pages_per_board=config.max_pages_per_board,
        max_posts_per_board_page=config.max_posts_per_board_page,
        image_timeout_seconds=config.image_timeout_seconds,
        duplicate_hamming_threshold=config.duplicate_hamming_threshold,
        blacklist_score_threshold=config.blacklist_score_threshold,
        boards=config.boards,
        comment=comment,
        selectors=config.selectors,
    )
