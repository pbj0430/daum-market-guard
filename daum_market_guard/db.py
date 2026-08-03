from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Assessment, PostDetail, StoredImage, utc_now


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board_id TEXT NOT NULL,
                post_key TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                author_name TEXT NOT NULL DEFAULT '',
                author_id TEXT NOT NULL DEFAULT '',
                posted_at TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                image_url TEXT NOT NULL,
                local_path TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL,
                ahash TEXT NOT NULL,
                dhash TEXT NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(post_id, image_url)
            );

            CREATE TABLE IF NOT EXISTS assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                score INTEGER NOT NULL,
                duplicate_image_count INTEGER NOT NULL,
                duplicate_post_count INTEGER NOT NULL,
                same_author_duplicate_count INTEGER NOT NULL,
                different_author_duplicate_count INTEGER NOT NULL,
                reasons_json TEXT NOT NULL,
                candidate_posts_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                commented_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_name TEXT NOT NULL DEFAULT '',
                author_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                source_post_id INTEGER REFERENCES posts(id) ON DELETE SET NULL,
                score INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessment_id INTEGER NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                body TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);
            CREATE INDEX IF NOT EXISTS idx_images_dhash ON images(dhash);
            CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author_name, author_id);
            CREATE INDEX IF NOT EXISTS idx_assessments_score ON assessments(score);
            """
        )
        self.conn.commit()

    def upsert_post(self, detail: PostDetail) -> int:
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO posts (
                board_id, post_key, url, title, author_name, author_id,
                posted_at, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_key) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                author_name = excluded.author_name,
                author_id = excluded.author_id,
                posted_at = excluded.posted_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                detail.ref.board_id,
                detail.ref.post_key,
                detail.ref.url,
                detail.title,
                detail.author_name,
                detail.author_id,
                detail.posted_at,
                now,
                now,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM posts WHERE post_key = ?", (detail.ref.post_key,)
        ).fetchone()
        return int(row["id"])

    def image_exists(self, post_id: int, image_url: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM images WHERE post_id = ? AND image_url = ?",
            (post_id, image_url),
        ).fetchone()
        return row is not None

    def add_image(
        self,
        post_id: int,
        image_url: str,
        local_path: str,
        sha256: str,
        ahash: str,
        dhash: str,
        width: int,
        height: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO images (
                post_id, image_url, local_path, sha256, ahash, dhash,
                width, height, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (post_id, image_url, local_path, sha256, ahash, dhash, width, height, utc_now()),
        )
        self.conn.commit()

    def get_post(self, post_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None:
            raise KeyError(f"post not found: {post_id}")
        return row

    def get_post_images(self, post_id: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM images WHERE post_id = ? ORDER BY id", (post_id,)
        ).fetchall()
        return list(rows)

    def iter_prior_images(self, post_id: int) -> Iterable[StoredImage]:
        rows = self.conn.execute(
            """
            SELECT
                images.id, images.post_id, posts.post_key, posts.url AS post_url,
                posts.title, posts.author_name, posts.author_id, images.image_url,
                images.sha256, images.ahash, images.dhash
            FROM images
            JOIN posts ON posts.id = images.post_id
            WHERE images.post_id != ?
            ORDER BY images.id DESC
            """,
            (post_id,),
        )
        for row in rows:
            yield StoredImage(
                id=int(row["id"]),
                post_id=int(row["post_id"]),
                post_key=str(row["post_key"]),
                post_url=str(row["post_url"]),
                title=str(row["title"]),
                author_name=str(row["author_name"]),
                author_id=str(row["author_id"]),
                image_url=str(row["image_url"]),
                sha256=str(row["sha256"]),
                ahash=str(row["ahash"]),
                dhash=str(row["dhash"]),
            )

    def latest_assessment_for_post(self, post_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM assessments
            WHERE post_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (post_id,),
        ).fetchone()

    def save_assessment(self, assessment: Assessment) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO assessments (
                post_id, score, duplicate_image_count, duplicate_post_count,
                same_author_duplicate_count, different_author_duplicate_count,
                reasons_json, candidate_posts_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assessment.post_id,
                assessment.score,
                assessment.duplicate_image_count,
                assessment.duplicate_post_count,
                assessment.same_author_duplicate_count,
                assessment.different_author_duplicate_count,
                json.dumps(assessment.reasons, ensure_ascii=False),
                json.dumps(assessment.source_links, ensure_ascii=False),
                utc_now(),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def author_is_blacklisted(self, author_name: str, author_id: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM blacklist
            WHERE active = 1
              AND ((author_id != '' AND author_id = ?) OR (author_name != '' AND author_name = ?))
            LIMIT 1
            """,
            (author_id, author_name),
        ).fetchone()
        return row is not None

    def add_blacklist(
        self,
        author_name: str,
        author_id: str,
        reason: str,
        source_post_id: int | None = None,
        score: int = 0,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO blacklist (
                author_name, author_id, reason, source_post_id, score, active, created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (author_name, author_id, reason, source_post_id, score, utc_now()),
        )
        self.conn.commit()

    def deactivate_blacklist(self, author_name: str, author_id: str) -> int:
        cursor = self.conn.execute(
            """
            UPDATE blacklist
            SET active = 0
            WHERE active = 1
              AND ((? != '' AND author_id = ?) OR (? != '' AND author_name = ?))
            """,
            (author_id, author_id, author_name, author_name),
        )
        self.conn.commit()
        return int(cursor.rowcount)

    def list_blacklist(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM blacklist
                WHERE active = 1
                ORDER BY created_at DESC
                """
            ).fetchall()
        )

    def list_suspects(self, min_score: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    assessments.id AS assessment_id,
                    assessments.score,
                    assessments.duplicate_image_count,
                    assessments.duplicate_post_count,
                    assessments.reasons_json,
                    assessments.candidate_posts_json,
                    assessments.created_at,
                    posts.url,
                    posts.title,
                    posts.author_name,
                    posts.author_id
                FROM assessments
                JOIN posts ON posts.id = assessments.post_id
                WHERE assessments.score >= ?
                ORDER BY assessments.score DESC, assessments.created_at DESC
                """,
                (min_score,),
            ).fetchall()
        )

    def pending_comments(self, min_score: int, limit: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT
                    assessments.id AS assessment_id,
                    assessments.*,
                    posts.url,
                    posts.title,
                    posts.author_name,
                    posts.author_id
                FROM assessments
                JOIN posts ON posts.id = assessments.post_id
                WHERE assessments.score >= ?
                  AND assessments.commented_at = ''
                  AND NOT EXISTS (
                    SELECT 1 FROM comments
                    WHERE comments.assessment_id = assessments.id
                      AND comments.status IN ('posted', 'dry_run')
                  )
                ORDER BY assessments.score DESC, assessments.id ASC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        )

    def save_comment(
        self,
        assessment_id: int,
        post_id: int,
        body: str,
        mode: str,
        status: str,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO comments (
                assessment_id, post_id, body, mode, status, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (assessment_id, post_id, body, mode, status, error, utc_now()),
        )
        if status in {"posted", "dry_run"}:
            self.conn.execute(
                "UPDATE assessments SET commented_at = ? WHERE id = ?",
                (utc_now(), assessment_id),
            )
        self.conn.commit()
