"""AI 总结：默认离线规则引擎生成日报/周报；配置了 API Key 则调用大模型。

离线模式不需要联网也不需要密钥，保证软件在任何环境都能用。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import analyzer

PRIORITY_LABEL = {1: "高", 2: "中", 3: "低"}
STATUS_LABEL = {"todo": "待办", "doing": "进行中", "blocked": "受阻", "done": "已完成"}
SEVERITY_LABEL = {"blocker": "阻塞", "high": "高", "normal": "普通", "low": "低"}


def day_range(days: int = 1) -> tuple[int, int]:
    end = datetime.now()
    start = (end - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(end.timestamp()) + 60


def fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"


def collect(store, days: int = 1) -> dict:
    """收集时间窗口内的所有素材。"""
    since, until = day_range(days)
    tasks = store.list_tasks(include_archived=True)
    done = [t for t in tasks if t.get("done_at") and since <= t["done_at"] <= until]
    doing = [t for t in tasks if t["status"] == "doing" and not t["archived"]]
    blocked = [t for t in tasks if t["status"] == "blocked" and not t["archived"]]
    todo = [t for t in tasks if t["status"] == "todo" and not t["archived"]]

    issues = store.list_issues()
    open_issues = [i for i in issues if i["status"] == "open"]
    resolved = [i for i in issues if i.get("resolved_at") and since <= i["resolved_at"] <= until]

    commits = [c for c in store.list_commits(limit=300) if since <= c["ts"] <= until]
    logs = [l for l in store.recent_logs(limit=1000) if since <= l["ts"] <= until]
    events = store.timeline(since=since, until=until, limit=500)
    focus = sum(
        e["meta"].get("seconds", 0) for e in events
        if e["kind"] == "focus" and isinstance(e.get("meta"), dict)
    )

    return {
        "since": since, "until": until, "days": days,
        "tasks": tasks, "done": done, "doing": doing, "blocked": blocked, "todo": todo,
        "issues": issues, "open_issues": open_issues, "resolved_issues": resolved,
        "commits": commits, "logs": logs, "events": events, "focus_seconds": focus,
    }


def offline_summary(store, days: int = 1) -> dict:
    """规则引擎生成的结构化报告。"""
    d = collect(store, days)
    label = "今日" if days == 1 else f"近 {days} 天"

    lines: list[str] = []
    highlights: list[str] = []

    # 1. 完成了什么
    if d["done"]:
        lines.append(f"## ✅ {label}完成（{len(d['done'])} 项）")
        for t in d["done"][:15]:
            extra = f" · 专注 {fmt_duration(t['seconds'])}" if t["seconds"] else ""
            lines.append(f"- {t['title']}{extra}")
        highlights.append(f"完成 {len(d['done'])} 个任务")
    else:
        lines.append(f"## ✅ {label}完成\n- （暂无已完成任务）")

    # 2. 正在做 / 没做完
    unfinished = d["doing"] + d["blocked"] + d["todo"]
    if unfinished:
        lines.append(f"\n## 🚧 未完成（{len(unfinished)} 项）")
        for t in d["doing"][:10]:
            lines.append(f"- [进行中] {t['title']}（优先级{PRIORITY_LABEL.get(t['priority'], '中')}）")
        for t in d["blocked"][:10]:
            lines.append(f"- [受阻] {t['title']} ⚠️")
        for t in d["todo"][:10]:
            lines.append(f"- [待办] {t['title']}")
        if d["blocked"]:
            highlights.append(f"{len(d['blocked'])} 个任务受阻")

    # 3. 问题
    if d["open_issues"]:
        lines.append(f"\n## 🐛 待解决问题（{len(d['open_issues'])} 个）")
        for i in d["open_issues"][:10]:
            sev = SEVERITY_LABEL.get(i["severity"], i["severity"])
            ref = f"（关联：{i['task_title']}）" if i.get("task_title") else ""
            lines.append(f"- [{sev}] {i['title']}{ref}")
        highlights.append(f"{len(d['open_issues'])} 个未解决问题")
    if d["resolved_issues"]:
        lines.append(f"\n## 🎯 {label}解决的问题（{len(d['resolved_issues'])} 个）")
        for i in d["resolved_issues"][:10]:
            fix = f" → {i['resolution'][:60]}" if i.get("resolution") else ""
            lines.append(f"- {i['title']}{fix}")

    # 4. 代码提交
    if d["commits"]:
        ins = sum(c["insertions"] for c in d["commits"])
        dele = sum(c["deletions"] for c in d["commits"])
        lines.append(f"\n## 📦 代码提交（{len(d['commits'])} 次，+{ins}/-{dele}）")
        for c in d["commits"][:10]:
            link = f" → {c['task_title']}" if c.get("task_title") else ""
            lines.append(f"- `{c['sha'][:8]}` {c['subject'][:70]}{link}")
        highlights.append(f"{len(d['commits'])} 次提交")

    # 5. 日志分析
    if d["logs"]:
        summary = analyzer.summarize(d["logs"])
        lines.append(f"\n## 📊 日志分析")
        lines.append(f"- {summary['headline']}")
        if summary["peak"]:
            lines.append(f"- 错误高峰出现在 {summary['peak']['hour']}（{summary['peak']['count']} 条）")
        for c in summary["clusters"][:5]:
            if c["level"] in ("error", "warn"):
                lines.append(f"- ×{c['count']} [{c['level']}] {c['sample'][:80]}")
        if summary["hints"]:
            lines.append("\n### 🔧 排查建议")
            for h in summary["hints"]:
                lines.append(f"- **{h['name']}**：{h['advice']}")
        if summary["error_count"]:
            highlights.append(f"{summary['error_count']} 条错误日志")

    # 6. 专注时长
    if d["focus_seconds"]:
        lines.append(f"\n## ⏱ 专注时长\n- 累计 {fmt_duration(d['focus_seconds'])}")

    # 7. 明天建议
    nexts = []
    for t in d["blocked"][:3]:
        nexts.append(f"优先解开受阻任务「{t['title']}」")
    for i in d["open_issues"][:2]:
        if i["severity"] in ("blocker", "high"):
            nexts.append(f"处理{SEVERITY_LABEL.get(i['severity'])}级问题「{i['title']}」")
    for t in d["doing"][:2]:
        nexts.append(f"继续推进「{t['title']}」")
    for t in sorted(d["todo"], key=lambda x: x["priority"])[:2]:
        nexts.append(f"开始「{t['title']}」")
    if nexts:
        lines.append("\n## 👉 下一步建议")
        for n in nexts[:5]:
            lines.append(f"- {n}")

    headline = "、".join(highlights) if highlights else "今天还没有记录活动"
    return {
        "mode": "offline",
        "headline": headline,
        "markdown": "\n".join(lines),
        "stats": {
            "done": len(d["done"]), "doing": len(d["doing"]), "blocked": len(d["blocked"]),
            "todo": len(d["todo"]), "open_issues": len(d["open_issues"]),
            "commits": len(d["commits"]), "logs": len(d["logs"]),
            "focus_seconds": d["focus_seconds"],
        },
    }


# ------------------------------------------------------------------ 在线模式
def build_prompt(store, days: int = 1) -> str:
    d = collect(store, days)
    label = "今天" if days == 1 else f"最近 {days} 天"
    buf = [f"以下是我{label}的开发记录，请用中文写一份简洁的工作总结。", ""]

    if d["done"]:
        buf.append("【已完成】")
        buf += [f"- {t['title']}" for t in d["done"][:20]]
    if d["doing"] or d["blocked"] or d["todo"]:
        buf.append("\n【未完成】")
        buf += [f"- [{STATUS_LABEL.get(t['status'], t['status'])}] {t['title']}"
                for t in (d["doing"] + d["blocked"] + d["todo"])[:20]]
    if d["open_issues"]:
        buf.append("\n【待解决问题】")
        buf += [f"- [{SEVERITY_LABEL.get(i['severity'], '')}] {i['title']}：{i['detail'][:100]}"
                for i in d["open_issues"][:15]]
    if d["commits"]:
        buf.append("\n【代码提交】")
        buf += [f"- {c['subject'][:80]} (+{c['insertions']}/-{c['deletions']})"
                for c in d["commits"][:20]]
    if d["logs"]:
        s = analyzer.summarize(d["logs"])
        buf.append("\n【日志摘要】")
        buf.append(s["headline"])
        buf += [f"- ×{c['count']} [{c['level']}] {c['sample'][:120]}"
                for c in s["clusters"][:8]]
    if d["focus_seconds"]:
        buf.append(f"\n【专注时长】{fmt_duration(d['focus_seconds'])}")

    buf.append(
        "\n请输出 Markdown，包含：1) 一句话概括今天的进展；2) 完成了什么；"
        "3) 还有什么没做完、卡在哪；4) 日志里暴露的技术风险与排查方向；"
        "5) 下一步最该做的 3 件事。语气务实，不要空话。"
    )
    return "\n".join(buf)


def _post_json(url: str, payload: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ai_summary(store, days: int = 1) -> dict:
    """有 API Key 就用大模型，否则退回离线摘要（永远不会失败）。"""
    key = (store.get_setting("ai_api_key", "") or os.environ.get("DEVLOG_AI_KEY", "")).strip()
    base = (store.get_setting("ai_base_url", "") or "https://api.openai.com/v1").strip()
    model = (store.get_setting("ai_model", "") or "gpt-4o-mini").strip()
    if not key:
        out = offline_summary(store, days)
        out["note"] = "当前为离线摘要。在「设置」里填入 API Key 可切换为大模型总结。"
        return out

    prompt = build_prompt(store, days)
    try:
        data = _post_json(
            f"{base.rstrip('/')}/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一位资深工程师，擅长把零散的开发记录整理成清晰的工作总结。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
            },
            {"Authorization": f"Bearer {key}"},
        )
        text = data["choices"][0]["message"]["content"]
        stats = offline_summary(store, days)["stats"]
        return {"mode": "ai", "model": model, "headline": "AI 总结已生成",
                "markdown": text, "stats": stats}
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError, OSError) as exc:
        out = offline_summary(store, days)
        out["note"] = f"调用大模型失败（{type(exc).__name__}），已回退到离线摘要。"
        return out
