"""日志解析 + 错误自动识别 + 归并聚类 + 摘要生成（纯标准库，离线可用）。"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Iterable

# ---------------------------------------------------------------- 级别识别
LEVEL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("error", re.compile(
        r"\b(?:FATAL|CRITICAL|ERROR|ERR|SEVERE|PANIC|EXCEPTION|Traceback|"
        r"AssertionError|SegmentationFault|core dumped|failed|failure|"
        r"cannot|can't|unable to|refused|denied|timeout|timed out|"
        r"not found|no such file|undefined is not|NullPointerException|"
        r"OutOfMemory|StackOverflow|Unhandled|uncaught)\b", re.I)),
    ("warn", re.compile(
        r"\b(?:WARN|WARNING|DEPRECAT(?:ED|ION)|retry|retrying|slow|"
        r"fallback|skipped|ignored)\b", re.I)),
    ("debug", re.compile(r"\b(?:DEBUG|TRACE|VERBOSE)\b")),
    ("info", re.compile(r"\b(?:INFO|NOTICE|LOG)\b")),
]

# 显式带级别标记的日志行，优先级最高（如 "2024-01-01 12:00:00 ERROR xxx"）
EXPLICIT_LEVEL = re.compile(
    r"(?:^|[\s\[\(\|:])(FATAL|CRITICAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|NOTICE)(?:[\s\]\)\|:]|$)",
    re.I,
)

LEVEL_ALIAS = {
    "fatal": "error", "critical": "error", "error": "error",
    "warn": "warn", "warning": "warn",
    "info": "info", "notice": "info",
    "debug": "debug", "trace": "debug",
}

# ---------------------------------------------------------------- 时间戳
TS_PATTERNS = [
    (re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"), "%Y-%m-%d %H:%M:%S"),
    (re.compile(r"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})"), "%Y/%m/%d %H:%M:%S"),
    (re.compile(r"(\d{2}:\d{2}:\d{2})"), "%H:%M:%S"),
]

# ---------------------------------------------------------------- 归一化
NORMALIZERS = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TIME>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "<TIME>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    (re.compile(r"\b\d+\.\d+\.\d+\.\d+\b"), "<IP>"),
    (re.compile(r"(?<=[:/])\d+"), "<N>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|MB|KB|GB)\b", re.I), "<SIZE>"),
    (re.compile(r"\b\d+\b"), "<N>"),
    (re.compile(r'"[^"]{0,200}"'), '"<STR>"'),
    (re.compile(r"'[^']{0,200}'"), "'<STR>'"),
    (re.compile(r"(/[\w.\-]+){2,}"), "<PATH>"),
    (re.compile(r"[A-Za-z]:\\(?:[\w.\-]+\\?){1,}"), "<PATH>"),
]

STACK_HINT = re.compile(
    r"^\s+(at\s|File\s\"|\.{3}|from\s|\|\s|Caused by:|\tat\s)", re.I
)
PY_TRACEBACK = re.compile(r"Traceback \(most recent call last\)")

# 常见错误 → 排查建议（离线知识库）
HINTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"ModuleNotFoundError|ImportError|No module named", re.I),
     "缺少 Python 依赖", "确认虚拟环境已激活，然后 pip install 对应包；注意包名与 import 名可能不同。"),
    (re.compile(r"ENOENT|No such file or directory|FileNotFoundError", re.I),
     "文件/路径不存在", "检查路径拼写与相对路径基准目录；确认文件是否被 .gitignore 排除或未生成。"),
    (re.compile(r"EADDRINUSE|Address already in use|端口.*占用", re.I),
     "端口被占用", "换个端口，或用 lsof -i:端口 / netstat -ano 找到进程后结束它。"),
    (re.compile(r"Permission denied|EACCES|Operation not permitted", re.I),
     "权限不足", "检查文件属主与权限位；容器内注意 UID 映射，尽量避免直接用 root 修补。"),
    (re.compile(r"NullPointerException|TypeError.*of (?:null|undefined)|"
                r"Cannot read propert(?:y|ies) of (?:null|undefined)|"
                r"AttributeError.*NoneType", re.I),
     "空值访问", "在使用前加空值判断；定位是哪一步返回了 null/None，通常是上游查询或解析失败。"),
    (re.compile(r"timeout|timed out|ETIMEDOUT|deadline exceeded", re.I),
     "超时", "确认对端可达与耗时分布；适当增大超时并加重试与退避策略。"),
    (re.compile(r"ECONNREFUSED|Connection refused|connect.*refused", re.I),
     "连接被拒绝", "目标服务可能没启动或端口/host 写错；先用 curl / telnet 验证连通性。"),
    (re.compile(r"OutOfMemory|OOM|MemoryError|heap out of memory", re.I),
     "内存不足", "调大堆内存上限，或排查大对象与内存泄漏；注意一次性加载的大文件。"),
    (re.compile(r"SyntaxError|Unexpected token|ParseError|Unexpected end of", re.I),
     "语法/解析错误", "看报错行号前后；常见是括号未闭合、JSON 结尾多逗号、编码不对。"),
    (re.compile(r"IntegrityError|UNIQUE constraint|Duplicate entry|"
                r"foreign key constraint", re.I),
     "数据库约束冲突", "检查唯一键重复或外键指向不存在的记录；插入前先做存在性判断。"),
    (re.compile(r"CORS|Access-Control-Allow-Origin|Cross-Origin", re.I),
     "跨域被拦截", "在服务端补上 Access-Control-Allow-Origin 等响应头，或走同源代理。"),
    (re.compile(r"401|Unauthorized|invalid token|token expired", re.I),
     "认证失败", "确认令牌是否过期、请求头名字是否正确、时钟是否漂移。"),
    (re.compile(r"IndexError|list index out of range|ArrayIndexOutOfBounds|"
                r"index out of bounds", re.I),
     "下标越界", "检查循环边界与空集合情况；访问前先判断长度。"),
    (re.compile(r"segmentation fault|SIGSEGV|core dumped", re.I),
     "段错误", "多为空指针/越界写内存；用 gdb 或 AddressSanitizer 定位。"),
]


def detect_level(line: str) -> str:
    """判断一行日志的级别。显式标记优先，其次关键词启发式。"""
    m = EXPLICIT_LEVEL.search(line)
    if m:
        return LEVEL_ALIAS.get(m.group(1).lower(), "info")
    for level, pat in LEVEL_PATTERNS:
        if pat.search(line):
            return level
    return "info"


def parse_ts(line: str, default: int) -> int:
    for pat, fmt in TS_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        raw = m.group(1).replace("T", " ").replace("/", "-")
        if fmt.startswith("%Y/"):
            fmt = "%Y-%m-%d %H:%M:%S"
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if dt.year == 1900:  # 只有时分秒，套用今天的日期
            today = datetime.now()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return int(dt.replace(tzinfo=timezone.utc).timestamp()) - _tz_offset()
    return default


def _tz_offset() -> int:
    now = datetime.now()
    return int((now.replace(tzinfo=timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


def normalize(message: str) -> str:
    """把变量部分抹掉，让同类错误能聚到一起。"""
    text = message.strip()
    for pat, repl in NORMALIZERS:
        text = pat.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()[:400]


def fingerprint(message: str) -> str:
    return hashlib.sha1(normalize(message).encode("utf-8", "ignore")).hexdigest()[:16]


def parse_log(text: str, default_ts: int, source: str = "paste") -> list[dict]:
    """把整段日志文本切成结构化条目，堆栈行会挂到上一条错误上。"""
    entries: list[dict] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        block = [line]
        # Python traceback：整块吞掉，直到出现真正的异常行
        if PY_TRACEBACK.search(line):
            j = i + 1
            while j < len(lines) and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                block.append(lines[j])
                j += 1
            if j < len(lines):  # 异常摘要行
                block.append(lines[j])
                j += 1
            raw = "\n".join(block)
            msg = block[-1].strip() if len(block) > 1 else line.strip()
            entries.append({
                "ts": parse_ts(line, default_ts), "level": "error", "message": msg[:500],
                "raw": raw[:8000], "fingerprint": fingerprint(msg), "lineno": i + 1,
            })
            i = j
            continue

        # 普通行 + 后续缩进堆栈
        j = i + 1
        while j < len(lines) and STACK_HINT.match(lines[j]):
            block.append(lines[j])
            j += 1
        raw = "\n".join(block)
        level = detect_level(line)
        entries.append({
            "ts": parse_ts(line, default_ts), "level": level, "message": line.strip()[:500],
            "raw": raw[:8000], "fingerprint": fingerprint(line), "lineno": i + 1,
        })
        i = j
    return entries


def cluster(entries: Iterable[dict], top: int = 12) -> list[dict]:
    """按指纹归并同类日志，输出出现次数、时间跨度、样例与建议。"""
    groups: dict[str, dict] = {}
    for e in entries:
        fp = e.get("fingerprint") or fingerprint(e.get("message", ""))
        g = groups.setdefault(fp, {
            "fingerprint": fp, "count": 0, "level": e.get("level", "info"),
            "sample": e.get("message", ""), "raw": e.get("raw", ""),
            "first_ts": e.get("ts", 0), "last_ts": e.get("ts", 0), "lines": [],
        })
        g["count"] += 1
        g["lines"].append(e.get("lineno"))
        g["first_ts"] = min(g["first_ts"], e.get("ts", 0))
        g["last_ts"] = max(g["last_ts"], e.get("ts", 0))
        order = {"error": 0, "warn": 1, "info": 2, "debug": 3}
        if order.get(e.get("level", "info"), 9) < order.get(g["level"], 9):
            g["level"] = e.get("level", "info")
            g["sample"] = e.get("message", "")
            g["raw"] = e.get("raw", "")

    out = list(groups.values())
    for g in out:
        g["lines"] = g["lines"][:20]
        g["hint"] = suggest(g["sample"] + " " + (g["raw"] or ""))
    order = {"error": 0, "warn": 1, "info": 2, "debug": 3}
    out.sort(key=lambda g: (order.get(g["level"], 9), -g["count"]))
    return out[:top]


def suggest(text: str) -> dict | None:
    for pat, name, advice in HINTS:
        if pat.search(text):
            return {"name": name, "advice": advice}
    return None


def summarize(entries: list[dict]) -> dict:
    """生成整段日志的统计摘要。"""
    counts = Counter(e.get("level", "info") for e in entries)
    clusters = cluster(entries)
    errs = [e for e in entries if e.get("level") == "error"]

    # 按小时分布，找出错误爆发时段
    buckets: dict[str, int] = defaultdict(int)
    for e in errs:
        if e.get("ts"):
            buckets[datetime.fromtimestamp(e["ts"]).strftime("%H:00")] += 1
    peak = max(buckets.items(), key=lambda kv: kv[1]) if buckets else None

    hints = []
    seen = set()
    for c in clusters:
        h = c.get("hint")
        if h and h["name"] not in seen:
            seen.add(h["name"])
            hints.append(h)

    return {
        "total": len(entries),
        "counts": dict(counts),
        "error_count": counts.get("error", 0),
        "warn_count": counts.get("warn", 0),
        "clusters": clusters,
        "peak": {"hour": peak[0], "count": peak[1]} if peak else None,
        "hints": hints[:6],
        "headline": _headline(counts, clusters),
    }


def _headline(counts: Counter, clusters: list[dict]) -> str:
    total = sum(counts.values())
    err = counts.get("error", 0)
    warn = counts.get("warn", 0)
    if total == 0:
        return "没有解析到日志内容。"
    if err == 0 and warn == 0:
        return f"共 {total} 行，未发现错误或警告，看起来一切正常。"
    parts = [f"共 {total} 行"]
    if err:
        parts.append(f"{err} 条错误")
    if warn:
        parts.append(f"{warn} 条警告")
    head = "，".join(parts) + "。"
    top = next((c for c in clusters if c["level"] == "error"), None)
    if top:
        head += f" 最突出的是「{top['sample'][:80]}」，出现 {top['count']} 次。"
        if top.get("hint"):
            head += f" 判断为{top['hint']['name']}。"
    return head
