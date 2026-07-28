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


def normalize_repo_path(path: str) -> str:
    """把用户填的各种路径纠正成仓库根目录。

    常见填法都要能兼容：
      D:\\code\\myapp\\.git   → 多填了一层 .git（最常见）
      D:\\code\\myapp\\src    → 填了子目录
      "D:\\code\\myapp"       → 从资源管理器复制带引号
    """
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return raw
    p = Path(raw).expanduser()

    # 多填了一层 .git
    if p.name == ".git":
        p = p.parent

    if not p.exists():
        return str(p)

    # 填的是文件就取所在目录
    if p.is_file():
        p = p.parent

    # 填了子目录：交给 git 自己找根目录
    try:
        top = _run(str(p), ["rev-parse", "--show-toplevel"]).strip()
        if top:
            return str(Path(top))
    except Exception:  # noqa: BLE001
        pass
    return str(p)


def is_repo(path: str) -> bool:
    p = Path(normalize_repo_path(path)).expanduser()
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
    repo = normalize_repo_path(repo)
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


def read_commits(repo: str, limit: int = 60, since: str | None = None,
                 all_branches: bool = True) -> list[dict]:
    """读取提交历史（含改动行数统计）。

    注意 1：--shortstat 的统计行输出在每条记录的**后面**，所以记录分隔符
    必须放在格式串开头，才能让统计行落进本条记录的 chunk 里。

    注意 2：默认带 --all 读取所有分支。否则停在 main 分支时，
    功能分支上的提交会全部读不到（这才是日常最需要记录的部分）。
    每条提交单独回查它所属的分支，而不是统一用 HEAD 的分支名。
    """
    fmt = REC + SEP.join(["%H", "%an", "%at", "%s", "%D", "%b"])
    args = ["log", f"--max-count={limit}", f"--pretty=format:{fmt}", "--shortstat"]
    if all_branches:
        args.append("--all")
    if since:
        args.append(f"--since={since}")
    raw = _run(repo, args)
    head_branch = current_branch(repo)
    name = Path(repo).expanduser().resolve().name

    commits: list[dict] = []
    for chunk in raw.split(REC):
        if not chunk.strip():
            continue
        parts = chunk.split(SEP)
        if len(parts) < 6:
            continue
        sha, author, ts = parts[0].strip(), parts[1], parts[2]
        subject = parts[3]
        refs = parts[4]          # %D：HEAD -> main, origin/main, feat/xxx
        rest = parts[5]          # body（可能多行）+ 末尾的 shortstat 行

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
            "branch": _pick_branch(refs, head_branch), "files": files,
            "insertions": insertions, "deletions": deletions,
        })
    return commits


def _pick_branch(refs: str, fallback: str) -> str:
    """从 %D 的 ref 列表里挑出最合适的分支名。

    形如 "HEAD -> main, origin/main, tag: v1.0"，优先本地分支，
    跳过 tag 和 remote 前缀；没有 ref 的中间提交回退到 HEAD 分支。
    """
    for raw in (refs or "").split(","):
        ref = raw.strip()
        if not ref or ref.startswith("tag:"):
            continue
        if "->" in ref:                      # "HEAD -> main"
            return ref.split("->")[-1].strip()
        if ref == "HEAD" or ref.startswith("origin/"):
            continue
        return ref
    return fallback


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


def explain_bad_path(raw: str) -> str:
    """路径不对时，给一句能直接照做的提示，而不是干巴巴报错。"""
    p = Path((raw or "").strip().strip('"').strip("'")).expanduser()
    if not raw.strip():
        return "请先填写项目文件夹路径"
    if not p.exists():
        return f"这个路径不存在：{p}　请检查是否拼写有误"
    if p.is_file():
        return f"这是一个文件，不是文件夹。请填它所在的项目目录：{p.parent}"

    # 往上找找，是不是填了某个仓库的子目录
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return (f"这个目录本身不是仓库根目录。"
                    f"请改填：{parent}")
    return (f"{p} 不是一个 git 仓库（里面没有 .git 文件夹）。"
            f"请填你项目的根目录，也就是能看到 .git 的那一层")


def sync(store, repo: str, limit: int = 60) -> dict:
    """拉取提交并写库，返回同步结果。"""
    if not git_available():
        return {"ok": False, "error": "系统里没有找到 git 命令，请先安装 Git"}
    original = repo
    repo = normalize_repo_path(repo)
    if not is_repo(repo):
        return {"ok": False, "error": explain_bad_path(original)}

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
