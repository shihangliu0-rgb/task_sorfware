"""SQLite 数据层：所有数据都存在本机，不联网。

数据目录默认 ~/.devlog ，可用环境变量 DEVLOG_HOME 覆盖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3


def data_home() -> Path:
    env = os.environ.get("DEVLOG_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".devlog"
    root.mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(exist_ok=True)
    return root


def db_path() -> Path:
    return data_home() / "devlog.db"


def attachments_dir() -> Path:
    return data_home() / "attachments"


def now() -> int:
    return int(time.time())


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'todo',   -- todo | doing | blocked | done
    priority    INTEGER NOT NULL DEFAULT 2,     -- 1 高 2 中 3 低
    project     TEXT DEFAULT '',
    tags        TEXT DEFAULT '',                -- 逗号分隔
    branch      TEXT DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    done_at     INTEGER,
    due_at      INTEGER,
    seconds     INTEGER NOT NULL DEFAULT 0,     -- 累计专注秒数
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    title       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    severity    TEXT NOT NULL DEFAULT 'normal', -- blocker | high | normal | low
    status      TEXT NOT NULL DEFAULT 'open',   -- open | resolved | wontfix
    resolution  TEXT DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    resolved_at INTEGER,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS shortcuts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keys        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    app         TEXT DEFAULT '',
    tags        TEXT DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,   -- task/issue/log/git/note/attachment/focus/shortcut
    title       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    ref_type    TEXT DEFAULT '',
    ref_id      INTEGER,
    meta        TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    stored      TEXT NOT NULL,
    mime        TEXT DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    note        TEXT DEFAULT '',
    task_id     INTEGER,
    issue_id    INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS log_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    source      TEXT DEFAULT 'paste',
    level       TEXT NOT NULL DEFAULT 'info',   -- error | warn | info | debug
    message     TEXT NOT NULL,
    raw         TEXT DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    lineno      INTEGER,
    task_id     INTEGER,
    batch       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS commits (
    sha         TEXT PRIMARY KEY,
    repo        TEXT DEFAULT '',
    author      TEXT DEFAULT '',
    subject     TEXT DEFAULT '',
    body        TEXT DEFAULT '',
    branch      TEXT DEFAULT '',
    ts          INTEGER NOT NULL,
    task_id     INTEGER,
    files       INTEGER DEFAULT 0,
    insertions  INTEGER DEFAULT 0,
    deletions   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts);
CREATE INDEX IF NOT EXISTS idx_logs_ts         ON log_entries(ts);
CREATE INDEX IF NOT EXISTS idx_logs_fp         ON log_entries(fingerprint);
CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_issues_status   ON issues(status);
CREATE INDEX IF NOT EXISTS idx_commits_ts      ON commits(ts);
"""


class Store:
    """薄薄一层 SQLite 封装，返回 dict，方便直接 JSON 序列化。"""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or db_path())
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # ---------- 基础 ----------
    def _migrate(self) -> None:
        cur = self.conn.execute("SELECT value FROM settings WHERE key='schema_version'")
        row = cur.fetchone()
        version = int(row["value"]) if row else 0
        if version < SCHEMA_VERSION:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        self.conn.close()

    def q(self, sql: str, args: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def q1(self, sql: str, args: Iterable = ()) -> dict | None:
        row = self.conn.execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def run(self, sql: str, args: Iterable = ()) -> int:
        cur = self.conn.execute(sql, tuple(args))
        self.conn.commit()
        return cur.lastrowid or 0

    # ---------- 设置 ----------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )

    # ---------- 时间线 ----------
    def add_event(
        self,
        kind: str,
        title: str,
        detail: str = "",
        ref_type: str = "",
        ref_id: int | None = None,
        meta: dict | None = None,
        ts: int | None = None,
    ) -> int:
        return self.run(
            "INSERT INTO events(ts,kind,title,detail,ref_type,ref_id,meta) VALUES(?,?,?,?,?,?,?)",
            (ts or now(), kind, title, detail, ref_type, ref_id, json.dumps(meta or {}, ensure_ascii=False)),
        )

    def timeline(self, since: int = 0, until: int | None = None, limit: int = 400) -> list[dict]:
        until = until if until is not None else now() + 86400
        rows = self.q(
            "SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT ?",
            (since, until, limit),
        )
        for r in rows:
            try:
                r["meta"] = json.loads(r.get("meta") or "{}")
            except json.JSONDecodeError:
                r["meta"] = {}
        return rows

    # ---------- 任务 ----------
    def create_task(self, **kw) -> dict:
        ts = now()
        status = kw.get("status", "todo")
        tid = self.run(
            """INSERT INTO tasks(title, body, status, priority, project, tags, branch,
                                 created_at, updated_at, due_at, done_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                kw.get("title", "未命名任务").strip() or "未命名任务",
                kw.get("body", ""),
                status,
                int(kw.get("priority", 2)),
                kw.get("project", ""),
                kw.get("tags", ""),
                kw.get("branch", ""),
                ts,
                ts,
                kw.get("due_at"),
                ts if status == "done" else None,  # 直接建成已完成时也要记时间
            ),
        )
        self.add_event("task", f"新建任务：{kw.get('title', '')}", ref_type="task", ref_id=tid)
        return self.get_task(tid)  # type: ignore[return-value]

    def get_task(self, tid: int) -> dict | None:
        return self.q1("SELECT * FROM tasks WHERE id=?", (tid,))

    def update_task(self, tid: int, **kw) -> dict | None:
        task = self.get_task(tid)
        if not task:
            return None
        fields = {
            k: v
            for k, v in kw.items()
            if k in {"title", "body", "status", "priority", "project", "tags", "branch",
                     "due_at", "seconds", "archived"}
        }
        if not fields:
            return task
        if fields.get("status") == "done" and task["status"] != "done":
            fields["done_at"] = now()
            self.add_event("task", f"完成任务：{task['title']}", ref_type="task", ref_id=tid)
        elif "status" in fields and fields["status"] != task["status"]:
            self.add_event(
                "task",
                f"任务状态 {task['status']} → {fields['status']}：{task['title']}",
                ref_type="task",
                ref_id=tid,
            )
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), tid))
        return self.get_task(tid)

    def delete_task(self, tid: int) -> None:
        task = self.get_task(tid)
        self.run("DELETE FROM tasks WHERE id=?", (tid,))
        if task:
            self.add_event("task", f"删除任务：{task['title']}", ref_type="task", ref_id=tid)

    def list_tasks(self, include_archived: bool = False) -> list[dict]:
        sql = "SELECT * FROM tasks"
        if not include_archived:
            sql += " WHERE archived=0"
        sql += """ ORDER BY
                     CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1
                                 WHEN 'todo' THEN 2 ELSE 3 END,
                     priority ASC, updated_at DESC"""
        return self.q(sql)

    # ---------- 问题 ----------
    def create_issue(self, **kw) -> dict:
        ts = now()
        iid = self.run(
            """INSERT INTO issues(task_id,title,detail,severity,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                kw.get("task_id"),
                kw.get("title", "未命名问题").strip() or "未命名问题",
                kw.get("detail", ""),
                kw.get("severity", "normal"),
                kw.get("status", "open"),
                ts,
                ts,
            ),
        )
        self.add_event("issue", f"记录问题：{kw.get('title', '')}", ref_type="issue", ref_id=iid)
        return self.q1("SELECT * FROM issues WHERE id=?", (iid,))  # type: ignore[return-value]

    def update_issue(self, iid: int, **kw) -> dict | None:
        issue = self.q1("SELECT * FROM issues WHERE id=?", (iid,))
        if not issue:
            return None
        fields = {
            k: v for k, v in kw.items()
            if k in {"task_id", "title", "detail", "severity", "status", "resolution"}
        }
        if not fields:
            return issue
        if fields.get("status") in {"resolved", "wontfix"} and issue["status"] == "open":
            fields["resolved_at"] = now()
            self.add_event("issue", f"解决问题：{issue['title']}", ref_type="issue", ref_id=iid)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE issues SET {sets} WHERE id=?", (*fields.values(), iid))
        return self.q1("SELECT * FROM issues WHERE id=?", (iid,))

    def delete_issue(self, iid: int) -> None:
        self.run("DELETE FROM issues WHERE id=?", (iid,))

    def list_issues(self) -> list[dict]:
        return self.q(
            """SELECT i.*, t.title AS task_title FROM issues i
               LEFT JOIN tasks t ON t.id = i.task_id
               ORDER BY CASE i.status WHEN 'open' THEN 0 ELSE 1 END,
                        CASE i.severity WHEN 'blocker' THEN 0 WHEN 'high' THEN 1
                                        WHEN 'normal' THEN 2 ELSE 3 END,
                        i.updated_at DESC"""
        )

    # ---------- 快捷键 ----------
    def create_shortcut(self, **kw) -> dict:
        sid = self.run(
            "INSERT INTO shortcuts(keys,description,app,tags,created_at) VALUES(?,?,?,?,?)",
            (
                kw.get("keys", "").strip(),
                kw.get("description", ""),
                kw.get("app", ""),
                kw.get("tags", ""),
                now(),
            ),
        )
        return self.q1("SELECT * FROM shortcuts WHERE id=?", (sid,))  # type: ignore[return-value]

    def update_shortcut(self, sid: int, **kw) -> dict | None:
        fields = {k: v for k, v in kw.items() if k in {"keys", "description", "app", "tags"}}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.run(f"UPDATE shortcuts SET {sets} WHERE id=?", (*fields.values(), sid))
        return self.q1("SELECT * FROM shortcuts WHERE id=?", (sid,))

    def hit_shortcut(self, sid: int) -> dict | None:
        self.run("UPDATE shortcuts SET hits=hits+1, used_at=? WHERE id=?", (now(), sid))
        return self.q1("SELECT * FROM shortcuts WHERE id=?", (sid,))

    def delete_shortcut(self, sid: int) -> None:
        self.run("DELETE FROM shortcuts WHERE id=?", (sid,))

    def list_shortcuts(self) -> list[dict]:
        return self.q("SELECT * FROM shortcuts ORDER BY hits DESC, created_at DESC")

    # ---------- 附件 ----------
    def add_attachment(self, filename: str, stored: str, mime: str, size: int,
                       note: str = "", task_id: int | None = None,
                       issue_id: int | None = None) -> dict:
        aid = self.run(
            """INSERT INTO attachments(filename,stored,mime,size,note,task_id,issue_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (filename, stored, mime, size, note, task_id, issue_id, now()),
        )
        self.add_event("attachment", f"添加图片：{filename}", note, "attachment", aid)
        return self.q1("SELECT * FROM attachments WHERE id=?", (aid,))  # type: ignore[return-value]

    def list_attachments(self) -> list[dict]:
        return self.q("SELECT * FROM attachments ORDER BY created_at DESC LIMIT 300")

    def delete_attachment(self, aid: int) -> None:
        row = self.q1("SELECT * FROM attachments WHERE id=?", (aid,))
        if row:
            f = attachments_dir() / row["stored"]
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass
        self.run("DELETE FROM attachments WHERE id=?", (aid,))

    # ---------- 日志 ----------
    def add_log_entries(self, entries: list[dict], source: str, batch: str,
                        task_id: int | None = None) -> int:
        ts = now()
        rows = [
            (
                e.get("ts", ts), source, e.get("level", "info"), e.get("message", ""),
                e.get("raw", ""), e.get("fingerprint", ""), e.get("lineno"), task_id, batch,
            )
            for e in entries
        ]
        self.conn.executemany(
            """INSERT INTO log_entries(ts,source,level,message,raw,fingerprint,lineno,task_id,batch)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def recent_logs(self, limit: int = 300, level: str | None = None) -> list[dict]:
        if level:
            return self.q(
                "SELECT * FROM log_entries WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit)
            )
        return self.q("SELECT * FROM log_entries ORDER BY id DESC LIMIT ?", (limit,))

    def clear_logs(self) -> None:
        self.run("DELETE FROM log_entries")

    # ---------- Git ----------
    def upsert_commit(self, c: dict) -> None:
        self.run(
            """INSERT INTO commits(sha,repo,author,subject,body,branch,ts,task_id,files,insertions,deletions)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sha) DO UPDATE SET
                 subject=excluded.subject, body=excluded.body, branch=excluded.branch,
                 task_id=COALESCE(commits.task_id, excluded.task_id)""",
            (
                c["sha"], c.get("repo", ""), c.get("author", ""), c.get("subject", ""),
                c.get("body", ""), c.get("branch", ""), c.get("ts", now()), c.get("task_id"),
                c.get("files", 0), c.get("insertions", 0), c.get("deletions", 0),
            ),
        )

    def list_commits(self, limit: int = 100) -> list[dict]:
        return self.q(
            """SELECT c.*, t.title AS task_title FROM commits c
               LEFT JOIN tasks t ON t.id=c.task_id
               ORDER BY c.ts DESC LIMIT ?""",
            (limit,),
        )
