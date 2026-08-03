from __future__ import annotations

import json
import mimetypes
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .db import Database
from .service import run_once


class DashboardState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_error = ""
        self.last_result: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []

    def add_event(self, event: str, payload: dict[str, Any]) -> None:
        item = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "payload": payload,
        }
        with self.lock:
            self.events.append(item)
            self.events = self.events[-200:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "last_error": self.last_error,
                "last_result": self.last_result,
                "events": list(reversed(self.events[-80:])),
            }


def serve_dashboard(config: AppConfig, host: str = "127.0.0.1", port: int = 8080) -> None:
    state = DashboardState()
    scan_config = _replace_headless(config, False)

    class Handler(DashboardHandler):
        app_config = scan_config
        dashboard_state = state

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}", flush=True)
    server.serve_forever()


class DashboardHandler(BaseHTTPRequestHandler):
    app_config: AppConfig
    dashboard_state: DashboardState

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/status":
            self._send_json(self._status())
            return
        if parsed.path == "/api/posts":
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", ["60"])[0])
            self._send_json({"posts": self._posts(limit)})
            return
        if parsed.path == "/api/images/recent":
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", ["24"])[0])
            self._send_json({"images": self._recent_images(limit)})
            return
        if parsed.path.startswith("/image/"):
            self._send_image(parsed.path.rsplit("/", 1)[-1])
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/scan/start":
            self._start_scan()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _status(self) -> dict[str, Any]:
        db = Database(self.app_config.db_path)
        try:
            summary = db.summary()
        finally:
            db.close()
        snapshot = self.dashboard_state.snapshot()
        snapshot["summary"] = summary
        return snapshot

    def _posts(self, limit: int) -> list[dict[str, Any]]:
        db = Database(self.app_config.db_path)
        try:
            rows = db.list_recent_posts(limit=max(1, min(limit, 200)))
            posts = [_row_to_post(row) for row in rows]
            images_by_post: dict[int, list[dict[str, Any]]] = {}
            for image in db.list_images_for_posts([int(post["id"]) for post in posts]):
                images_by_post.setdefault(int(image["post_id"]), []).append(
                    {
                        "id": image["id"],
                        "image_url": image["image_url"],
                        "width": image["width"],
                        "height": image["height"],
                    }
                )
            for post in posts:
                post["images"] = images_by_post.get(int(post["id"]), [])
            return posts
        finally:
            db.close()

    def _recent_images(self, limit: int) -> list[dict[str, Any]]:
        db = Database(self.app_config.db_path)
        try:
            rows = db.list_recent_images(limit=max(1, min(limit, 100)))
            return [_row_to_image(row) for row in rows]
        finally:
            db.close()

    def _send_image(self, image_id_text: str) -> None:
        try:
            image_id = int(image_id_text)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        db = Database(self.app_config.db_path)
        try:
            row = db.conn.execute("SELECT local_path FROM images WHERE id = ?", (image_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = Path(row["local_path"])
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _start_scan(self) -> None:
        with self.dashboard_state.lock:
            if self.dashboard_state.running:
                self._send_json({"started": False, "message": "scan already running"})
                return
            self.dashboard_state.running = True
            self.dashboard_state.last_error = ""

        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()
        self._send_json({"started": True})

    def _scan_worker(self) -> None:
        def progress(event: str, payload: dict[str, Any]) -> None:
            self.dashboard_state.add_event(event, payload)

        try:
            result = run_once(self.app_config, progress=progress)
            with self.dashboard_state.lock:
                self.dashboard_state.last_result = {
                    "boards": result.stats.board_count,
                    "refs": result.stats.post_refs,
                    "posts": result.stats.post_details,
                    "images": result.stats.images,
                    "assessed": result.assessed,
                    "comments": result.comments,
                }
        except Exception as exc:
            with self.dashboard_state.lock:
                self.dashboard_state.last_error = str(exc)
            self.dashboard_state.add_event("scan_failed", {"error": str(exc)})
        finally:
            with self.dashboard_state.lock:
                self.dashboard_state.running = False

    def _send_json(self, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, value: str) -> None:
        data = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _row_to_post(row: Any) -> dict[str, Any]:
    reasons = _loads(row["reasons_json"])
    sources = _loads(row["candidate_posts_json"])
    return {
        "id": row["id"],
        "board_id": row["board_id"],
        "url": row["url"],
        "title": row["title"],
        "author_name": row["author_name"],
        "author_id": row["author_id"],
        "posted_at": row["posted_at"],
        "content_text": row["content_text"],
        "last_seen_at": row["last_seen_at"],
        "image_count": row["image_count"],
        "score": row["score"] if row["score"] is not None else 0,
        "duplicate_image_count": row["duplicate_image_count"] or 0,
        "duplicate_post_count": row["duplicate_post_count"] or 0,
        "reasons": reasons,
        "sources": sources,
    }


def _row_to_image(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "post_id": row["post_id"],
        "post_url": row["post_url"],
        "title": row["title"],
        "author_name": row["author_name"],
        "image_url": row["image_url"],
        "width": row["width"],
        "height": row["height"],
    }


def _loads(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _replace_headless(config: AppConfig, headless: bool) -> AppConfig:
    return type(config)(
        cafe_url=config.cafe_url,
        login_url=config.login_url,
        data_dir=config.data_dir,
        user_data_dir=config.user_data_dir,
        poll_interval_seconds=config.poll_interval_seconds,
        headless=headless,
        locale=config.locale,
        timezone_id=config.timezone_id,
        user_agent=config.user_agent,
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


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daum Market Guard</title>
  <style>
    :root { color-scheme: light; --line:#d7dee8; --bg:#f6f8fb; --ink:#18212f; --muted:#667085; --brand:#0f766e; --bad:#b42318; --warn:#b54708; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: "Noto Sans CJK KR", "NanumGothic", system-ui, sans-serif; color:var(--ink); background:var(--bg); }
    header { height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 18px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:2; }
    h1 { font-size:18px; margin:0; }
    button { border:1px solid #0b5d56; background:var(--brand); color:#fff; height:34px; padding:0 12px; border-radius:6px; cursor:pointer; font-weight:700; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    main { display:grid; grid-template-columns: 360px 1fr; gap:16px; padding:16px; }
    section { background:#fff; border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    h2 { font-size:14px; margin:0; padding:12px 14px; border-bottom:1px solid var(--line); background:#fbfcfe; }
    .stats { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; padding:12px; }
    .stat { border:1px solid var(--line); border-radius:6px; padding:10px; background:#fff; }
    .stat b { display:block; font-size:22px; }
    .stat span { font-size:12px; color:var(--muted); }
    .events { height:300px; overflow:auto; padding:10px 12px; font-size:12px; line-height:1.45; }
    .event { border-bottom:1px solid #eef2f6; padding:6px 0; }
    .posts { max-height: calc(100vh - 112px); overflow:auto; }
    .post { display:grid; grid-template-columns:70px 1fr 96px; gap:10px; padding:12px 14px; border-bottom:1px solid #eef2f6; }
    .score { height:34px; min-width:52px; padding:7px 8px; border-radius:6px; text-align:center; font-weight:800; background:#e7f5f2; color:#0f766e; }
    .score.warn { background:#fff4e5; color:var(--warn); }
    .score.bad { background:#fee4e2; color:var(--bad); }
    .title { font-weight:800; margin-bottom:4px; }
    .meta, .content, .reasons, .links { color:var(--muted); font-size:12px; line-height:1.45; }
    .content { color:#344054; margin-top:8px; white-space:pre-wrap; max-height:86px; overflow:hidden; }
    .links a { color:#075985; margin-right:8px; }
    .thumbs { display:flex; flex-wrap:wrap; gap:6px; align-content:flex-start; }
    .thumbs img { width:42px; height:42px; object-fit:cover; border-radius:4px; border:1px solid var(--line); background:#f2f4f7; }
    .gallery { display:grid; grid-template-columns:repeat(auto-fill, minmax(100px, 1fr)); gap:8px; padding:12px; }
    .gallery img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:6px; border:1px solid var(--line); }
    @media (max-width: 900px) { main { grid-template-columns:1fr; } .posts { max-height:none; } .post { grid-template-columns:56px 1fr; } .thumbs { grid-column:2; } }
  </style>
</head>
<body>
  <header>
    <h1>Daum Market Guard</h1>
    <button id="scanBtn" onclick="startScan()">Scan</button>
  </header>
  <main>
    <aside>
      <section>
        <h2>상태</h2>
        <div class="stats" id="stats"></div>
      </section>
      <section style="margin-top:16px">
        <h2>진행 로그</h2>
        <div class="events" id="events"></div>
      </section>
      <section style="margin-top:16px">
        <h2>최근 이미지</h2>
        <div class="gallery" id="gallery"></div>
      </section>
    </aside>
    <section>
      <h2>최근 수집 글</h2>
      <div class="posts" id="posts"></div>
    </section>
  </main>
  <script>
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const short = (v, n) => String(v ?? '').length > n ? String(v).slice(0, n) + '...' : String(v ?? '');
    async function getJson(url, options) { const r = await fetch(url, options); return await r.json(); }
    async function startScan() { await getJson('/api/scan/start', {method:'POST'}); refresh(); }
    function scoreClass(score) { return score >= 85 ? 'bad' : score >= 60 ? 'warn' : ''; }
    async function refresh() {
      const status = await getJson('/api/status');
      document.getElementById('scanBtn').disabled = status.running;
      const s = status.summary || {};
      document.getElementById('stats').innerHTML = [
        ['Posts', s.posts || 0], ['Images', s.images || 0],
        ['Suspects', s.suspects || 0], ['Blacklist', s.blacklist || 0]
      ].map(([k,v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
      document.getElementById('events').innerHTML = (status.events || []).map(e => {
        const p = e.payload || {};
        return `<div class="event"><b>${esc(e.event)}</b><br>${esc(p.title || p.board || p.error || JSON.stringify(p))}</div>`;
      }).join('');
      const posts = await getJson('/api/posts?limit=80');
      document.getElementById('posts').innerHTML = (posts.posts || []).map(p => {
        const reasons = (p.reasons || []).map(esc).join(' ');
        const links = (p.sources || []).slice(0,3).map((u,i) => `<a target="_blank" href="${esc(u)}">source ${i+1}</a>`).join('');
        return `<div class="post">
          <div class="score ${scoreClass(p.score)}">${p.score || 0}</div>
          <div>
            <div class="title"><a target="_blank" href="${esc(p.url)}">${esc(p.title || '(no title)')}</a></div>
            <div class="meta">${esc(p.board_id)} | ${esc(p.author_name || '-')} | images ${p.image_count || 0} | dup ${p.duplicate_image_count || 0}/${p.duplicate_post_count || 0}</div>
            <div class="content">${esc(short(p.content_text, 420))}</div>
            <div class="reasons">${reasons}</div>
            <div class="links">${links}</div>
          </div>
          <div class="thumbs" data-post="${p.id}"></div>
        </div>`;
      }).join('');
      document.querySelectorAll('.thumbs').forEach(el => {
        const post = (posts.posts || []).find(p => String(p.id) === String(el.dataset.post));
        const list = post ? (post.images || []) : [];
        el.innerHTML = list.slice(0,4).map(img => `<img src="/image/${img.id}">`).join('');
      });
      const images = await getJson('/api/images/recent?limit=36');
      document.getElementById('gallery').innerHTML = (images.images || []).map(img => `<a target="_blank" href="${esc(img.post_url)}"><img title="${esc(img.title)}" src="/image/${img.id}"></a>`).join('');
    }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""
