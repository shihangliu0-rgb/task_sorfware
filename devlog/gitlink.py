"""Git 关联：读取本地仓库提交，自动把 commit 与任务对应起来。

识别规则（按优先级）：
  1. commit message 里写 #12 / task-12 / DEVLOG-12 → 任务 id 12
  2. 分支名与任务的 branch 字段一致
  3. commit message 与任务标题关键词重合度足够高
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REF_PATTERNS = [
    re.compile(r"#(\d+)"),
    re.compile(r"\btask[-_ ]?(\d+)\b", re.I),
    re.compile(r"\bdevlog[-_ ]?(\d+)\b", re.I),
]

SEP = "\x1f"
REC = "\x1e"


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(repo: str, args: list[str], timeout: int = 15) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git 命令失败").strip()[:400])
    return proc.stdout


def is_repo(path: str) -> bool:
    p = Path(path).expanduser()
    if not p.is_dir():
        return False
    try:
        return _run(str(p), ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except Exception:
        return False


def current_branch(repo: str) -> str:
    try:
        return _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    except Exception:
        return ""


def status_summary(repo: str) -> dict:
    """当前工作区状态：分支、改动文件数、是否干净。"""
    try:
        porcelain = _run(repo, ["status", "--porcelain"])
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    changed = [l for l in porcelain.splitlines() if l.strip()]
    staged = sum(1 for l in changed if l[:1] not in (" ", "?"))
    untracked = sum(1 for l in changed if l.startswith("??"))
    return {
        "ok": True,
        "branch": current_branch(repo),
        "changed": len(changed),
        "staged": staged,
        "untracked": untracked,
        "clean": not changed,
        "files": [l[3:] for l in changed[:20]],
    }


STAT_RE = re.compile(
    r"^\s*(?:(\d+) files? changed)?"
    r"(?:,?\s*(\d+) insertions?\(\+\))?"
    r"(?:,?\s*(\d+) deletions?\(-\))?\s*$",
    re.M,
)


def read_commits(repo: str, limit: int = 60, since: str | None = None) -> list[dict]:
    """读取提交历史（含改动行数统计）。

    注意：--shortstat 的统计行输出在每条记录的**后面**，所以记录分隔符
    必须放在格式串开头，才能让统计行落进本条记录的 chunk 里。
    """
    fmt = REC + SEP.join(["%H", "%an", "%at", "%s", "%b"])
    args = ["log", f"--max-count={limit}", f"--pretty=format:{fmt}", "--shortstat"]
    if since:
        args.append(f"--since={since}")
    raw = _run(repo, args)
    branch = current_branch(repo)
    name = Path(repo).expanduser().resolve().name

    commits: list[dict] = []
    for chunk in raw.split(REC):
        if not chunk.strip():
            continue
        parts = chunk.split(SEP)
        if len(parts) < 5:
            continue
        sha, author, ts = parts[0].strip(), parts[1], parts[2]
        subject = parts[3]
        rest = parts[4]  # body（可能多行）+ 末尾的 shortstat 行

        files = insertions = deletions = 0
        body_lines: list[str] = []
        for line in rest.splitlines():
            m = STAT_RE.match(line)
            if m and any(m.groups()):
                files = int(m.group(1) or 0)
                insertions = int(m.group(2) or 0)
                deletions = int(m.group(3) or 0)
            else:
                body_lines.append(line)

        try:
            ts_int = int(ts)
        except ValueError:
            continue
        commits.append({
            "sha": sha, "repo": name, "author": author, "ts": ts_int,
            "subject": subject.strip(),
            "body": "\n".join(body_lines).strip()[:2000],
            "branch": branch, "files": files,
            "insertions": insertions, "deletions": deletions,
        })
    return commits


STOP = {
    "fix", "add", "update", "remove", "refactor", "chore", "feat", "docs", "test",
    "the", "and", "for", "with", "from", "into", "修复", "新增", "更新", "删除", "重构",
}


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text.lower())
    return {w for w in words if w not in STOP}


def match_task(commit: dict, tasks: list[dict]) -> int | None:
    """把一条 commit 匹配到某个任务上。"""
    text = f"{commit.get('subject', '')} {commit.get('body', '')}"
    ids = {t["id"] for t in tasks}

    for pat in REF_PATTERNS:
        for m in pat.finditer(text):
            tid = int(m.group(1))
            if tid in ids:
                return tid

    branch = (commit.get("branch") or "").strip()
    if branch:
        for t in tasks:
            if t.get("branch") and t["branch"].strip() == branch:
                return t["id"]

    ckw = _keywords(text)
    if not ckw:
        return None
    best, best_score = None, 0.0
    for t in tasks:
        tkw = _keywords(f"{t.get('title', '')} {t.get('tags', '')}")
        if not tkw:
            continue
        overlap = ckw & tkw
        if not overlap:
            continue
        score = len(overlap) / min(len(tkw), 8)
        if score > best_score:
            best, best_score = t["id"], score
    return best if best_score >= 0.5 else None


def sync(store, repo: str, limit: int = 60) -> dict:
    """拉取提交并写库，返回同步结果。"""
    repo = str(Path(repo).expanduser())
    if not git_available():
        return {"ok": False, "error": "系统里没有找到 git 命令"}
    if not is_repo(repo):
        return {"ok": False, "error": f"不是一个 git 仓库：{repo}"}

    tasks = store.list_tasks()
    commits = read_commits(repo, limit=limit)
    known = {c["sha"] for c in store.list_commits(limit=500)}
    linked = added = 0
    for c in commits:
        tid = match_task(c, tasks)
        if tid:
            c["task_id"] = tid
            linked += 1
        store.upsert_commit(c)
        if c["sha"] not in known:
            added += 1
            store.add_event(
                "git",
                f"[{c['repo']}] {c['subject'][:80]}",
                f"{c['author']} · +{c['insertions']}/-{c['deletions']} · {c['sha'][:8]}",
                "commit", tid, {"sha": c["sha"], "branch": c["branch"]}, ts=c["ts"],
            )
    store.set_setting("git_repo", repo)
    return {
        "ok": True, "total": len(commits), "added": added, "linked": linked,
        "branch": current_branch(repo), "status": status_summary(repo),
    }
