"""本地 HTTP 服务：提供 REST API 与静态界面。只监听 127.0.0.1，不对外暴露。"""
from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import analyzer, gitlink, summarizer
from .db import Store, attachments_dir, now

WEB_DIR = Path(__file__).parent / "web"
MAX_UPLOAD = 12 * 1024 * 1024  # 单张图片上限 12MB
ALLOWED_IMAGE = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/svg+xml"}

store: Store | None = None
TOKEN = secrets.token_urlsafe(16)


def get_store() -> Store:
    global store
    if store is None:
        store = Store()
    return store


class Handler(BaseHTTPRequestHandler):
    server_version = "DevLog/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 工具 ----------------
    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"))

    def fail(self, msg: str, code: int = 400) -> None:
        self.json({"ok": False, "error": msg}, code)

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_UPLOAD + 2 * 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")

    def _check_origin(self) -> bool:
        """只接受本机来源，防止其他网站通过浏览器打到本地服务。"""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = urlparse(origin).hostname
        return host in ("127.0.0.1", "localhost", "::1")

    # ---------------- 路由 ----------------
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self.api_get(path, query)
            if path.startswith("/files/"):
                return self.serve_attachment(path[len("/files/"):])
            return self.serve_static(path)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):  # noqa: N802
        if not self._check_origin():
            return self.fail("跨域请求被拒绝", 403)
        parsed = urlparse(self.path)
        try:
            return self.api_post(parsed.path, self.body_json())
        except ValueError as exc:
            self.fail(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"{type(exc).__name__}: {exc}", 500)

    # ---------------- 静态文件 ----------------
    def serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def serve_attachment(self, name: str) -> None:
        safe = Path(name).name
        target = (attachments_dir() / safe).resolve()
        if not str(target).startswith(str(attachments_dir().resolve())) or not target.is_file():
            return self._send(404, b"Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    # ---------------- GET API ----------------
    def api_get(self, path: str, q: dict) -> None:
        s = get_store()
        one = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731

        if path == "/api/ping":
            return self.json({"ok": True, "version": "1.0", "time": now()})

        if path == "/api/bootstrap":
            repo = s.get_setting("git_repo", "") or ""
            return self.json({
                "ok": True,
                "tasks": s.list_tasks(),
                "issues": s.list_issues(),
                "shortcuts": s.list_shortcuts(),
                "timeline": s.timeline(limit=200),
                "attachments": s.list_attachments(),
                "commits": s.list_commits(limit=60),
                "logs": s.recent_logs(limit=200),
                "settings": {
                    "git_repo": repo,
                    "ai_model": s.get_setting("ai_model", "gpt-4o-mini"),
                    "ai_base_url": s.get_setting("ai_base_url", "https://api.openai.com/v1"),
                    "has_key": bool(s.get_setting("ai_api_key", "")),
                    "active_task": s.get_setting("active_task", ""),
                },
                "git_status": gitlink.status_summary(repo) if repo and gitlink.is_repo(repo) else None,
            })

        if path == "/api/tasks":
            return self.json({"ok": True, "items": s.list_tasks(
                include_archived=one("archived") == "1")})
        if path == "/api/issues":
            return self.json({"ok": True, "items": s.list_issues()})
        if path == "/api/shortcuts":
            return self.json({"ok": True, "items": s.list_shortcuts()})
        if path == "/api/timeline":
            return self.json({"ok": True, "items": s.timeline(
                since=int(one("since", 0)), limit=int(one("limit", 300)))})
        if path == "/api/attachments":
            return self.json({"ok": True, "items": s.list_attachments()})
        if path == "/api/logs":
            return self.json({"ok": True, "items": s.recent_logs(
                limit=int(one("limit", 300)), level=one("level"))})
        if path == "/api/commits":
            return self.json({"ok": True, "items": s.list_commits(limit=int(one("limit", 100)))})

        if path == "/api/log-report":
            logs = s.recent_logs(limit=2000)
            return self.json({"ok": True, "report": analyzer.summarize(logs)})

        if path == "/api/summary":
            days = max(1, min(int(one("days", 1)), 90))
            use_ai = one("ai", "0") == "1"
            fn = summarizer.ai_summary if use_ai else summarizer.offline_summary
            return self.json({"ok": True, "summary": fn(s, days)})

        if path == "/api/stats":
            return self.json({"ok": True, "stats": self._stats(s)})

        if path == "/api/export":
            days = max(1, min(int(one("days", 7)), 365))
            data = summarizer.offline_summary(s, days)
            return self._send(
                200, data["markdown"].encode("utf-8"), "text/markdown; charset=utf-8",
                {"Content-Disposition": 'attachment; filename="devlog-report.md"'},
            )

        if path == "/api/git/status":
            repo = one("repo") or s.get_setting("git_repo", "")
            if not repo:
                return self.fail("尚未设置仓库路径")
            return self.json({"ok": True, "status": gitlink.status_summary(repo)})

        return self.fail("未知接口", 404)

    def _stats(self, s: Store) -> dict:
        tasks = s.list_tasks(include_archived=True)
        active = [t for t in tasks if not t["archived"]]
        issues = s.list_issues()
        logs = s.recent_logs(limit=2000)
        today0 = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        return {
            "tasks_total": len(active),
            "tasks_done": sum(1 for t in tasks if t["status"] == "done"),
            "tasks_doing": sum(1 for t in active if t["status"] == "doing"),
            "tasks_blocked": sum(1 for t in active if t["status"] == "blocked"),
            "tasks_todo": sum(1 for t in active if t["status"] == "todo"),
            "done_today": sum(1 for t in tasks if (t.get("done_at") or 0) >= today0),
            "issues_open": sum(1 for i in issues if i["status"] == "open"),
            "errors_today": sum(1 for l in logs if l["level"] == "error" and l["ts"] >= today0),
            "focus_today": sum(
                e["meta"].get("seconds", 0)
                for e in s.timeline(since=today0, limit=500)
                if e["kind"] == "focus" and isinstance(e.get("meta"), dict)
            ),
            "commits_today": sum(1 for c in s.list_commits(limit=300) if c["ts"] >= today0),
        }

    # ---------------- POST API ----------------
    def api_post(self, path: str, b: dict) -> None:
        s = get_store()

        # ---- 任务 ----
        if path == "/api/tasks/create":
            return self.json({"ok": True, "item": s.create_task(**b)})
        if path == "/api/tasks/update":
            item = s.update_task(int(b.pop("id")), **b)
            return self.json({"ok": bool(item), "item": item})
        if path == "/api/tasks/delete":
            s.delete_task(int(b["id"]))
            return self.json({"ok": True})
        if path == "/api/tasks/activate":
            tid = b.get("id")
            s.set_setting("active_task", tid or "")
            if tid:
                t = s.get_task(int(tid))
                if t:
                    s.add_event("task", f"切换到任务：{t['title']}", ref_type="task", ref_id=t["id"])
            return self.json({"ok": True})
        if path == "/api/tasks/focus":
            tid, secs = int(b["id"]), int(b.get("seconds", 0))
            t = s.get_task(tid)
            if not t:
                return self.fail("任务不存在", 404)
            s.update_task(tid, seconds=t["seconds"] + secs)
            s.add_event("focus", f"专注 {summarizer.fmt_duration(secs)}：{t['title']}",
                        ref_type="task", ref_id=tid, meta={"seconds": secs})
            return self.json({"ok": True, "item": s.get_task(tid)})

        # ---- 问题 ----
        if path == "/api/issues/create":
            return self.json({"ok": True, "item": s.create_issue(**b)})
        if path == "/api/issues/update":
            item = s.update_issue(int(b.pop("id")), **b)
            return self.json({"ok": bool(item), "item": item})
        if path == "/api/issues/delete":
            s.delete_issue(int(b["id"]))
            return self.json({"ok": True})

        # ---- 快捷键 ----
        if path == "/api/shortcuts/create":
            return self.json({"ok": True, "item": s.create_shortcut(**b)})
        if path == "/api/shortcuts/update":
            return self.json({"ok": True, "item": s.update_shortcut(int(b.pop("id")), **b)})
        if path == "/api/shortcuts/hit":
            return self.json({"ok": True, "item": s.hit_shortcut(int(b["id"]))})
        if path == "/api/shortcuts/delete":
            s.delete_shortcut(int(b["id"]))
            return self.json({"ok": True})

        # ---- 笔记 / 时间线 ----
        if path == "/api/notes/create":
            text = (b.get("text") or "").strip()
            if not text:
                return self.fail("内容为空")
            eid = s.add_event("note", text[:120], text, "note", None)
            return self.json({"ok": True, "id": eid})

        # ---- 图片附件 ----
        if path == "/api/attachments/upload":
            return self.upload(s, b)
        if path == "/api/attachments/delete":
            s.delete_attachment(int(b["id"]))
            return self.json({"ok": True})

        # ---- 日志 ----
        if path == "/api/logs/ingest":
            text = b.get("text") or ""
            if not text.strip():
                return self.fail("日志内容为空")
            entries = analyzer.parse_log(text, now(), b.get("source", "paste"))
            batch = uuid.uuid4().hex[:12]
            s.add_log_entries(entries, b.get("source", "paste"), batch,
                              b.get("task_id"))
            report = analyzer.summarize(entries)
            s.add_event("log", f"导入日志 {len(entries)} 行 · {report['error_count']} 条错误",
                        report["headline"], "log", None,
                        {"batch": batch, "errors": report["error_count"]})
            return self.json({"ok": True, "count": len(entries), "report": report})

        if path == "/api/logs/analyze":  # 只分析不入库
            text = b.get("text") or ""
            entries = analyzer.parse_log(text, now())
            return self.json({"ok": True, "count": len(entries),
                              "report": analyzer.summarize(entries)})

        if path == "/api/logs/ingest-file":
            p = Path(b.get("path", "")).expanduser()
            if not p.is_file():
                return self.fail(f"文件不存在：{p}")
            if p.stat().st_size > 20 * 1024 * 1024:
                return self.fail("文件超过 20MB，请先截取片段")
            text = p.read_text("utf-8", errors="replace")
            entries = analyzer.parse_log(text, now(), str(p))
            batch = uuid.uuid4().hex[:12]
            s.add_log_entries(entries, str(p), batch)
            report = analyzer.summarize(entries)
            s.add_event("log", f"读取日志文件 {p.name}（{len(entries)} 行）",
                        report["headline"], "log", None, {"batch": batch})
            return self.json({"ok": True, "count": len(entries), "report": report})

        if path == "/api/logs/clear":
            s.clear_logs()
            return self.json({"ok": True})

        if path == "/api/logs/to-issue":
            title = (b.get("title") or "").strip()[:200]
            if not title:
                return self.fail("标题为空")
            item = s.create_issue(title=title, detail=b.get("detail", ""),
                                  severity=b.get("severity", "high"),
                                  task_id=b.get("task_id"))
            return self.json({"ok": True, "item": item})

        # ---- Git ----
        if path == "/api/git/sync":
            repo = (b.get("repo") or s.get_setting("git_repo", "") or "").strip()
            if not repo:
                return self.fail("请先填写本地仓库路径")
            result = gitlink.sync(s, repo, limit=int(b.get("limit", 60)))
            if not result.get("ok"):
                return self.fail(result.get("error", "同步失败"), 400)
            return self.json(result)
        if path == "/api/git/link":
            s.run("UPDATE commits SET task_id=? WHERE sha=?",
                  (b.get("task_id"), b["sha"]))
            return self.json({"ok": True})

        # ---- AI 总结 ----
        if path == "/api/summary/ai":
            days = max(1, min(int(b.get("days", 1)), 90))
            return self.json({"ok": True, "summary": summarizer.ai_summary(s, days)})

        # ---- 设置 ----
        if path == "/api/settings":
            for k, v in (b or {}).items():
                if k in {"git_repo", "ai_api_key", "ai_model", "ai_base_url"}:
                    s.set_setting(k, v)
            return self.json({"ok": True})

        return self.fail("未知接口", 404)

    def upload(self, s: Store, b: dict) -> None:
        data_url = b.get("data") or ""
        m = re.match(r"^data:([\w.+/-]+);base64,(.*)$", data_url, re.S)
        if not m:
            return self.fail("图片数据格式不正确")
        mime, payload = m.group(1), m.group(2)
        if mime not in ALLOWED_IMAGE:
            return self.fail(f"不支持的图片类型：{mime}")
        try:
            blob = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return self.fail("图片解码失败")
        if len(blob) > MAX_UPLOAD:
            return self.fail("图片超过 12MB")

        ext = mimetypes.guess_extension(mime) or ".png"
        if ext == ".jpe":
            ext = ".jpg"
        stored = f"{int(time.time())}-{uuid.uuid4().hex[:8]}{ext}"
        (attachments_dir() / stored).write_bytes(blob)
        item = s.add_attachment(
            filename=(b.get("filename") or f"截图{ext}")[:200], stored=stored, mime=mime,
            size=len(blob), note=b.get("note", ""),
            task_id=b.get("task_id"), issue_id=b.get("issue_id"),
        )
        return self.json({"ok": True, "item": item})


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    get_store()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def run(host: str = "127.0.0.1", port: int = 8765, block: bool = True):
    httpd = serve(host, port)
    if block:
        httpd.serve_forever()
        return httpd
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
