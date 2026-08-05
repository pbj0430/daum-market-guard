from __future__ import annotations

import json
import mimetypes
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig
from .db import Database
from .scraper import DaumCafeScraper
from .service import run_once


class DashboardState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_error = ""
        self.last_result: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.login_scraper: DaumCafeScraper | None = None

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
                "login_browser_open": self.login_scraper is not None,
                "events": list(reversed(self.events[-80:])),
            }


def serve_dashboard(config: AppConfig, host: str = "127.0.0.1", port: int = 8080) -> None:
    state = DashboardState()
    scan_config = _replace_headless(config, False)

    class Handler(DashboardHandler):
        app_config = scan_config
        dashboard_state = state

    server = HTTPServer((host, port), Handler)
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
        if parsed.path == "/api/login/open":
            self._open_login_browser()
            return
        if parsed.path == "/api/login/close":
            self._close_login_browser()
            return
        if parsed.path == "/api/debug/boards":
            self._debug_boards()
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
        snapshot["config"] = {
            "scan_strategy": self.app_config.scan_strategy,
            "direct_scan_min_post_id": self.app_config.direct_scan_min_post_id,
            "direct_scan_limit_per_board": self.app_config.direct_scan_limit_per_board,
            "rescan_existing_posts": self.app_config.rescan_existing_posts,
            "max_posts_per_board_page": self.app_config.max_posts_per_board_page,
        }
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
            if self.dashboard_state.login_scraper is not None:
                self._send_json(
                    {
                        "started": False,
                        "message": "close the login browser before scanning",
                    }
                )
                self.dashboard_state.add_event(
                    "scan_blocked",
                    {"message": "Close Login Browser first, then scan."},
                )
                return
            self.dashboard_state.running = True
            self.dashboard_state.last_error = ""

        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()
        self._send_json({"started": True})

    def _open_login_browser(self) -> None:
        with self.dashboard_state.lock:
            if self.dashboard_state.login_scraper is not None:
                self._send_json({"opened": False, "message": "login browser already open"})
                return
            scraper = DaumCafeScraper(_replace_headless(self.app_config, False))
            self.dashboard_state.login_scraper = scraper
        try:
            scraper.start()
            page = scraper.page()
            page.goto(self.app_config.cafe_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(1000)
            self.dashboard_state.add_event(
                "login_browser_opened",
                {"url": page.url, "message": "Log in there, then click Close Login Browser."},
            )
            self._send_json({"opened": True, "url": page.url})
        except Exception as exc:
            with self.dashboard_state.lock:
                self.dashboard_state.login_scraper = None
            try:
                scraper.close()
            except Exception:
                pass
            self.dashboard_state.add_event("login_browser_failed", {"error": str(exc)})
            self._send_json({"opened": False, "error": str(exc)})

    def _close_login_browser(self) -> None:
        with self.dashboard_state.lock:
            scraper = self.dashboard_state.login_scraper
            self.dashboard_state.login_scraper = None
        if scraper is not None:
            try:
                scraper.close()
            except Exception as exc:
                self.dashboard_state.add_event("login_browser_close_failed", {"error": str(exc)})
                self._send_json({"closed": False, "error": str(exc)})
                return
        self.dashboard_state.add_event("login_browser_closed", {})
        self._send_json({"closed": True})

    def _debug_boards(self) -> None:
        if self.dashboard_state.running:
            self._send_json({"started": False, "message": "scan already running"})
            return
        with self.dashboard_state.lock:
            if self.dashboard_state.login_scraper is not None:
                self._send_json(
                    {
                        "started": False,
                        "message": "close the login browser before debugging",
                    }
                )
                self.dashboard_state.add_event(
                    "debug_blocked",
                    {"message": "Close Login Browser first, then debug."},
                )
                return

        def worker() -> None:
            self.dashboard_state.add_event("debug_started", {})
            try:
                with DaumCafeScraper(_replace_headless(self.app_config, False)) as scraper:
                    for board in self.app_config.boards:
                        info = scraper.inspect_board(board)
                        self.dashboard_state.add_event("debug_board", info)
            except Exception as exc:
                self.dashboard_state.add_event("debug_failed", {"error": str(exc)})

        threading.Thread(target=worker, daemon=True).start()
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
        "post_key": row["post_key"],
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
        cafe_grpid=config.cafe_grpid,
        login_url=config.login_url,
        data_dir=config.data_dir,
        user_data_dir=config.user_data_dir,
        poll_interval_seconds=config.poll_interval_seconds,
        headless=headless,
        locale=config.locale,
        timezone_id=config.timezone_id,
        user_agent=config.user_agent,
        allow_mobile_fallback=config.allow_mobile_fallback,
        browser_executable_path=config.browser_executable_path,
        scan_strategy=config.scan_strategy,
        max_pages_per_board=config.max_pages_per_board,
        max_posts_per_board_page=config.max_posts_per_board_page,
        direct_scan_min_post_id=config.direct_scan_min_post_id,
        direct_scan_limit_per_board=config.direct_scan_limit_per_board,
        rescan_existing_posts=config.rescan_existing_posts,
        image_timeout_seconds=config.image_timeout_seconds,
        duplicate_hamming_threshold=config.duplicate_hamming_threshold,
        blacklist_score_threshold=config.blacklist_score_threshold,
        boards=config.boards,
        comment=config.comment,
        login=config.login,
        selectors=config.selectors,
    )


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daum Market Guard</title>
  <style>
    :root { color-scheme: light; --line:#d8dee8; --bg:#f4f6f8; --panel:#fff; --ink:#17202c; --muted:#697586; --brand:#126b5f; --brand2:#174ea6; --bad:#b42318; --warn:#b54708; --ok:#0f766e; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:"Noto Sans CJK KR","NanumGothic",system-ui,sans-serif; color:var(--ink); background:var(--bg); }
    header { height:56px; display:flex; align-items:center; justify-content:space-between; padding:0 16px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:3; }
    h1 { font-size:17px; margin:0; letter-spacing:0; }
    button { border:1px solid var(--brand); background:var(--brand); color:#fff; height:34px; padding:0 12px; border-radius:6px; cursor:pointer; font-weight:700; }
    button:disabled { opacity:.5; cursor:not-allowed; }
    .ghost { background:#fff; color:var(--brand); }
    .actions { display:flex; gap:8px; align-items:center; }
    .layout { display:grid; grid-template-columns:360px minmax(0,1fr); gap:14px; padding:14px; }
    .stack { display:flex; flex-direction:column; gap:14px; min-width:0; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
    h2 { font-size:13px; margin:0; padding:10px 12px; border-bottom:1px solid var(--line); background:#fbfcfe; }
    .statusbar { display:flex; gap:8px; align-items:center; padding:10px 12px; border-bottom:1px solid var(--line); background:#fff; }
    .dot { width:8px; height:8px; border-radius:99px; background:#94a3b8; flex:0 0 auto; }
    .dot.run { background:var(--ok); }
    .status-text { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; padding:10px; }
    .stat { border:1px solid var(--line); border-radius:6px; padding:8px; background:#fff; min-width:0; }
    .stat b { display:block; font-size:20px; line-height:1.1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .stat span { display:block; margin-top:4px; font-size:11px; color:var(--muted); }
    .config { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; padding:10px; border-top:1px solid var(--line); }
    .kv { font-size:12px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .kv b { color:var(--ink); font-weight:700; }
    .events { height:430px; overflow:auto; padding:0; font-size:12px; line-height:1.45; }
    .event { display:grid; grid-template-columns:92px 1fr; gap:8px; padding:8px 10px; border-bottom:1px solid #eef2f6; }
    .event-type { font-weight:800; color:var(--brand2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .event-time { color:var(--muted); font-size:11px; margin-top:2px; }
    .event-body { min-width:0; overflow-wrap:anywhere; }
    .event.bad .event-type { color:var(--bad); }
    .event.warn .event-type { color:var(--warn); }
    .event.ok .event-type { color:var(--ok); }
    .posts { max-height:calc(100vh - 112px); overflow:auto; }
    .post { display:grid; grid-template-columns:64px minmax(0,1fr) 116px; gap:10px; padding:11px 12px; border-bottom:1px solid #eef2f6; }
    .score { height:32px; min-width:52px; padding:6px 8px; border-radius:6px; text-align:center; font-weight:800; background:#e7f5f2; color:var(--ok); }
    .score.warn { background:#fff4e5; color:var(--warn); }
    .score.bad { background:#fee4e2; color:var(--bad); }
    .title { font-weight:800; margin-bottom:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .title a { color:#123048; text-decoration:none; }
    .key { display:inline-block; margin-right:6px; padding:2px 6px; border-radius:6px; background:#e8f4f2; color:#13746d; font-size:12px; font-weight:800; }
    .meta, .content, .reasons, .links { color:var(--muted); font-size:12px; line-height:1.45; }
    .content { color:#344054; margin-top:7px; white-space:pre-wrap; max-height:66px; overflow:hidden; }
    .links a { color:#075985; margin-right:8px; }
    .thumbs { display:grid; grid-template-columns:repeat(2,42px); grid-auto-rows:42px; gap:6px; align-content:start; }
    .thumbs img { width:42px; height:42px; object-fit:cover; border-radius:4px; border:1px solid var(--line); background:#f2f4f7; }
    .gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(72px,1fr)); gap:8px; padding:10px; max-height:340px; overflow:auto; }
    .gallery img { width:100%; aspect-ratio:1; object-fit:cover; border-radius:6px; border:1px solid var(--line); background:#f2f4f7; }
    .empty { padding:14px; color:var(--muted); font-size:12px; }
    @media (max-width:900px) { .layout { grid-template-columns:1fr; } .posts { max-height:none; } .post { grid-template-columns:54px minmax(0,1fr); } .thumbs { grid-column:2; grid-template-columns:repeat(4,42px); } .stats { grid-template-columns:repeat(2,1fr); } }
  </style>
</head>
<body>
  <header>
    <h1>Daum Market Guard</h1>
    <div class="actions">
      <button class="ghost" onclick="openLogin()">Login</button>
      <button class="ghost" onclick="closeLogin()">Close</button>
      <button class="ghost" onclick="debugBoards()">Debug</button>
      <button id="scanBtn" onclick="startScan()">Scan</button>
    </div>
  </header>
  <main class="layout">
    <aside class="stack">
      <section>
        <div class="statusbar">
          <span id="runDot" class="dot"></span>
          <div id="runText" class="status-text">Idle</div>
        </div>
        <div class="stats" id="stats"></div>
        <div class="config" id="config"></div>
      </section>
      <section>
        <h2>Scan Events</h2>
        <div class="events" id="events"></div>
      </section>
      <section>
        <h2>Recent Images</h2>
        <div class="gallery" id="gallery"></div>
      </section>
    </aside>
    <section>
      <h2>Recent Posts</h2>
      <div class="posts" id="posts"></div>
    </section>
  </main>
  <script>
    const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const short = (v, n) => String(v ?? '').length > n ? String(v).slice(0, n) + '...' : String(v ?? '');
    async function getJson(url, options) { const r = await fetch(url, options); return await r.json(); }
    async function startScan() { await getJson('/api/scan/start', {method:'POST'}); refresh(); }
    async function openLogin() { await getJson('/api/login/open', {method:'POST'}); refresh(); }
    async function closeLogin() { await getJson('/api/login/close', {method:'POST'}); refresh(); }
    async function debugBoards() { await getJson('/api/debug/boards', {method:'POST'}); refresh(); }
    function scoreClass(score) { return score >= 85 ? 'bad' : score >= 60 ? 'warn' : ''; }
    function eventClass(name) {
      if (name.includes('failed') || name === 'scan_failed') return 'bad';
      if (name.includes('missing') || name.includes('blocked')) return 'warn';
      if (name.includes('done') || name.includes('saved')) return 'ok';
      return '';
    }
    function eventBody(e) {
      const p = e.payload || {};
      if (e.event === 'scan_started') return `boards ${p.boards}, strategy ${p.strategy || '-'}, profile ${p.profile}`;
      if (e.event === 'direct_scan_range') return `${p.board}: latest ${p.latest}, saved max ${p.saved_max || 0}, stop ${p.stop || '-'}, numbers ${p.count}, limit ${p.limit || 'all'}`;
      if (e.event === 'board_started') return `${p.board} | ${p.url}`;
      if (e.event === 'board_page_opening') return `page ${p.page}: ${p.url}`;
      if (e.event === 'board_page_loaded') return `accepted ${p.accepted_count}/${p.link_count}, frames ${p.frame_count}, final ${p.page_url}`;
      if (e.event === 'board_posts_found') return `${p.board}: ${p.count} candidate numbers`;
      if (e.event === 'post_started') return `${p.index}/${p.total} ${p.post_key || '-'} ${p.title} | ${p.url}`;
      if (e.event === 'post_skipped') return `${p.index}/${p.total} ${p.post_key} ${p.title}`;
      if (e.event === 'post_missing') return `${p.index}/${p.total} ${p.post_key}`;
      if (e.event === 'post_done') return `${p.post_key || '-'} score ${p.score}, images ${p.stored_images}, author ${p.author || '-'}, ${p.title || ''}`;
      if (e.event === 'post_failed') return `${p.title || ''} | ${p.error || ''} | ${p.url || ''}`;
      if (e.event === 'scan_done') return `boards ${p.boards}, refs ${p.refs}, posts ${p.posts}, images ${p.images}, assessed ${p.assessed}`;
      if (e.event === 'debug_board' || e.event === 'board_debug') {
        const reports = (p.url_reports || []).map(r => `${r.requested_url} => accepted ${r.accepted_count}/${r.link_count}, logged_out=${r.logged_out}`).join(' | ');
        return `${p.board}: accepted ${p.accepted_count}/${p.link_count}, logged_out=${p.logged_out}, unsupported=${p.unsupported_browser} ${reports}`;
      }
      return p.title || p.board || p.error || p.message || JSON.stringify(p);
    }
    function currentLine(events, status) {
      if (status.running) {
        const active = events.find(e => ['post_started','post_done','post_skipped','post_missing','direct_scan_range','board_started'].includes(e.event));
        return active ? `${active.event}: ${short(eventBody(active), 160)}` : 'Running';
      }
      if (status.last_error) return `Error: ${status.last_error}`;
      const result = status.last_result;
      if (result) return `Last run: posts ${result.posts}, images ${result.images}, assessed ${result.assessed}`;
      return status.login_browser_open ? 'Login browser open' : 'Idle';
    }
    async function refresh() {
      const status = await getJson('/api/status');
      const events = status.events || [];
      document.getElementById('scanBtn').disabled = status.running;
      document.getElementById('runDot').className = `dot ${status.running ? 'run' : ''}`;
      document.getElementById('runText').textContent = currentLine(events, status);
      const s = status.summary || {};
      document.getElementById('stats').innerHTML = [
        ['Posts', s.posts || 0], ['Images', s.images || 0], ['Missing', s.missing || 0],
        ['Suspects', s.suspects || 0], ['Assessments', s.assessments || 0], ['Blacklist', s.blacklist || 0]
      ].map(([k,v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join('');
      const c = status.config || {};
      document.getElementById('config').innerHTML = [
        ['Strategy', c.scan_strategy || '-'],
        ['Limit', c.direct_scan_limit_per_board || 'all'],
        ['Min ID', c.direct_scan_min_post_id || 1],
        ['Rescan', c.rescan_existing_posts ? 'yes' : 'no']
      ].map(([k,v]) => `<div class="kv">${esc(k)} <b>${esc(v)}</b></div>`).join('');
      document.getElementById('events').innerHTML = events.length ? events.map(e => {
        const time = String(e.time || '').replace('T',' ').replace('+00:00','Z');
        return `<div class="event ${eventClass(e.event)}"><div><div class="event-type">${esc(e.event)}</div><div class="event-time">${esc(time)}</div></div><div class="event-body">${esc(short(eventBody(e), 360))}</div></div>`;
      }).join('') : '<div class="empty">No events yet.</div>';
      const posts = await getJson('/api/posts?limit=100');
      document.getElementById('posts').innerHTML = (posts.posts || []).length ? (posts.posts || []).map(p => {
        const reasons = (p.reasons || []).map(esc).join(' ');
        const links = (p.sources || []).slice(0,3).map((u,i) => `<a target="_blank" href="${esc(u)}">source ${i+1}</a>`).join('');
        return `<div class="post">
          <div class="score ${scoreClass(p.score)}">${p.score || 0}</div>
          <div>
            <div class="title"><span class="key">${esc(p.post_key || p.board_id || '-')}</span> <a target="_blank" href="${esc(p.url)}">${esc(p.title || '(no title)')}</a></div>
            <div class="meta">${esc(p.author_name || '-')} | images ${p.image_count || 0} | dup ${p.duplicate_image_count || 0}/${p.duplicate_post_count || 0}</div>
            <div class="content">${esc(short(p.content_text, 360))}</div>
            <div class="reasons">${reasons}</div>
            <div class="links">${links}</div>
          </div>
          <div class="thumbs" data-post="${p.id}"></div>
        </div>`;
      }).join('') : '<div class="empty">No posts stored yet.</div>';
      document.querySelectorAll('.thumbs').forEach(el => {
        const post = (posts.posts || []).find(p => String(p.id) === String(el.dataset.post));
        const list = post ? (post.images || []) : [];
        el.innerHTML = list.slice(0,4).map(img => `<img src="/image/${img.id}">`).join('');
      });
      const images = await getJson('/api/images/recent?limit=48');
      document.getElementById('gallery').innerHTML = (images.images || []).length ? (images.images || []).map(img => `<a target="_blank" href="${esc(img.post_url)}"><img title="${esc(img.title)}" src="/image/${img.id}"></a>`).join('') : '<div class="empty">No images.</div>';
    }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""
