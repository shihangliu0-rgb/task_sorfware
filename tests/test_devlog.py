"""DevLog 测试套件（标准库 unittest，无需额外依赖）。

运行： python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="devlog-test-")
os.environ["DEVLOG_HOME"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devlog import analyzer, gitlink, summarizer  # noqa: E402
from devlog.db import Store  # noqa: E402
from devlog.server import serve  # noqa: E402


class TestAnalyzer(unittest.TestCase):
    def test_explicit_level_wins(self):
        self.assertEqual(analyzer.detect_level("2024-01-01 ERROR boom"), "error")
        self.assertEqual(analyzer.detect_level("[WARN] disk almost full"), "warn")
        self.assertEqual(analyzer.detect_level("DEBUG cache hit"), "debug")
        self.assertEqual(analyzer.detect_level("INFO started"), "info")

    def test_heuristic_level(self):
        self.assertEqual(analyzer.detect_level("Connection refused by peer"), "error")
        self.assertEqual(analyzer.detect_level("Traceback (most recent call last):"), "error")
        self.assertEqual(analyzer.detect_level("deprecated api used"), "warn")

    def test_info_not_false_positive(self):
        """普通信息行不应被误判成错误。"""
        self.assertEqual(analyzer.detect_level("Server listening on 0.0.0.0:8080"), "info")
        self.assertEqual(analyzer.detect_level("Loaded 42 records"), "info")

    def test_fingerprint_groups_variants(self):
        a = analyzer.fingerprint("Connection refused to db:5432")
        b = analyzer.fingerprint("Connection refused to db:5999")
        self.assertEqual(a, b, "端口不同的同类错误应归为一类")
        c = analyzer.fingerprint("Disk full on /dev/sda1")
        self.assertNotEqual(a, c)

    def test_fingerprint_normalizes_noise(self):
        a = analyzer.fingerprint("req 0xAB12 failed after 300ms at 10:00:01")
        b = analyzer.fingerprint("req 0xFF99 failed after 920ms at 22:41:07")
        self.assertEqual(a, b)

    def test_python_traceback_is_one_entry(self):
        text = (
            "Traceback (most recent call last):\n"
            '  File "/app/main.py", line 42, in handler\n'
            "    result = payload['user']\n"
            "KeyError: 'user'\n"
        )
        entries = analyzer.parse_log(text, 1000)
        self.assertEqual(len(entries), 1, "整个 traceback 应该合成一条")
        self.assertEqual(entries[0]["level"], "error")
        self.assertIn("KeyError", entries[0]["message"])
        self.assertIn("main.py", entries[0]["raw"])

    def test_stack_lines_attach_to_parent(self):
        text = (
            "ERROR something broke\n"
            "    at foo (/app/a.js:1:2)\n"
            "    at bar (/app/b.js:3:4)\n"
            "INFO recovered\n"
        )
        entries = analyzer.parse_log(text, 1000)
        self.assertEqual(len(entries), 2)
        self.assertIn("at foo", entries[0]["raw"])

    def test_blank_lines_skipped(self):
        entries = analyzer.parse_log("\n\n\nERROR x\n\n\n", 1000)
        self.assertEqual(len(entries), 1)

    def test_cluster_counts_and_order(self):
        text = "\n".join(["ERROR fail id=1", "ERROR fail id=2", "ERROR fail id=3", "INFO ok"])
        report = analyzer.summarize(analyzer.parse_log(text, 1000))
        self.assertEqual(report["error_count"], 3)
        self.assertEqual(report["clusters"][0]["count"], 3)
        self.assertEqual(report["clusters"][0]["level"], "error")

    def test_hints_match_known_errors(self):
        cases = {
            "ModuleNotFoundError: No module named 'x'": "缺少 Python 依赖",
            "listen EADDRINUSE :::3000": "端口被占用",
            "Error: connect ECONNREFUSED 127.0.0.1:5432": "连接被拒绝",
            "java.lang.OutOfMemoryError: Java heap space": "内存不足",
            "PermissionError: [Errno 13] Permission denied": "权限不足",
        }
        for text, expect in cases.items():
            with self.subTest(text=text):
                hint = analyzer.suggest(text)
                self.assertIsNotNone(hint, f"应该识别出：{text}")
                self.assertEqual(hint["name"], expect)

    def test_empty_input(self):
        self.assertEqual(analyzer.parse_log("", 1000), [])
        self.assertEqual(analyzer.summarize([])["total"], 0)

    def test_headline_no_errors(self):
        report = analyzer.summarize(analyzer.parse_log("INFO all good\nINFO done", 1000))
        self.assertIn("一切正常", report["headline"])


class TestStore(unittest.TestCase):
    def setUp(self):
        self.db = Store(":memory:")

    def tearDown(self):
        self.db.close()

    def test_task_lifecycle(self):
        t = self.db.create_task(title="写测试", priority=1)
        self.assertEqual(t["status"], "todo")
        self.assertIsNone(t["done_at"])

        done = self.db.update_task(t["id"], status="done")
        self.assertEqual(done["status"], "done")
        self.assertIsNotNone(done["done_at"], "完成时应记录时间")

        self.db.delete_task(t["id"])
        self.assertIsNone(self.db.get_task(t["id"]))

    def test_task_events_recorded(self):
        t = self.db.create_task(title="事件测试")
        self.db.update_task(t["id"], status="done")
        kinds = [e["title"] for e in self.db.timeline()]
        self.assertTrue(any("新建任务" in k for k in kinds))
        self.assertTrue(any("完成任务" in k for k in kinds))

    def test_task_ordering(self):
        self.db.create_task(title="低优先待办", priority=3)
        self.db.create_task(title="进行中", priority=2, status="doing")
        self.db.create_task(title="高优先待办", priority=1)
        titles = [t["title"] for t in self.db.list_tasks()]
        self.assertEqual(titles[0], "进行中", "进行中的任务应排最前")

    def test_empty_title_fallback(self):
        t = self.db.create_task(title="   ")
        self.assertEqual(t["title"], "未命名任务")

    def test_issue_resolution(self):
        i = self.db.create_issue(title="崩溃", severity="blocker")
        self.assertEqual(i["status"], "open")
        r = self.db.update_issue(i["id"], status="resolved", resolution="加了空判断")
        self.assertIsNotNone(r["resolved_at"])
        self.assertEqual(r["resolution"], "加了空判断")

    def test_issue_task_link(self):
        t = self.db.create_task(title="父任务")
        self.db.create_issue(title="子问题", task_id=t["id"])
        found = self.db.list_issues()[0]
        self.assertEqual(found["task_title"], "父任务")

    def test_shortcut_hits(self):
        s = self.db.create_shortcut(keys="Ctrl+P", description="快速打开")
        self.assertEqual(s["hits"], 0)
        self.db.hit_shortcut(s["id"])
        self.db.hit_shortcut(s["id"])
        self.assertEqual(self.db.list_shortcuts()[0]["hits"], 2)

    def test_log_batch_insert(self):
        entries = analyzer.parse_log("ERROR a\nWARN b\nINFO c", 1000)
        n = self.db.add_log_entries(entries, "test", "batch1")
        self.assertEqual(n, 3)
        self.assertEqual(len(self.db.recent_logs(level="error")), 1)

    def test_settings_roundtrip(self):
        self.db.set_setting("k", "v")
        self.assertEqual(self.db.get_setting("k"), "v")
        self.db.set_setting("k", "v2")
        self.assertEqual(self.db.get_setting("k"), "v2", "重复写入应覆盖")
        self.assertEqual(self.db.get_setting("missing", "dft"), "dft")

    def test_commit_upsert_idempotent(self):
        c = {"sha": "abc123", "subject": "first", "ts": 1000}
        self.db.upsert_commit(c)
        self.db.upsert_commit({**c, "subject": "updated"})
        rows = self.db.list_commits()
        self.assertEqual(len(rows), 1, "同一 sha 不应重复插入")
        self.assertEqual(rows[0]["subject"], "updated")

    def test_timeline_time_filter(self):
        self.db.add_event("note", "旧事件", ts=1000)
        self.db.add_event("note", "新事件", ts=int(time.time()))
        recent = self.db.timeline(since=int(time.time()) - 60)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["title"], "新事件")


class TestGitLink(unittest.TestCase):
    def test_match_by_hash_ref(self):
        tasks = [{"id": 12, "title": "登录功能", "tags": "", "branch": ""}]
        c = {"subject": "fix login bug #12", "body": "", "branch": ""}
        self.assertEqual(gitlink.match_task(c, tasks), 12)

    def test_match_by_branch(self):
        tasks = [{"id": 5, "title": "支付", "tags": "", "branch": "feat/pay"}]
        c = {"subject": "wip", "body": "", "branch": "feat/pay"}
        self.assertEqual(gitlink.match_task(c, tasks), 5)

    def test_match_by_keywords(self):
        tasks = [{"id": 7, "title": "重构 payment gateway", "tags": "", "branch": ""}]
        c = {"subject": "refactor payment gateway timeout", "body": "", "branch": ""}
        self.assertEqual(gitlink.match_task(c, tasks), 7)

    def test_no_false_match(self):
        tasks = [{"id": 1, "title": "登录功能", "tags": "", "branch": "main"}]
        c = {"subject": "update readme typo", "body": "", "branch": "other"}
        self.assertIsNone(gitlink.match_task(c, tasks))

    def test_nonexistent_id_ignored(self):
        tasks = [{"id": 1, "title": "x", "tags": "", "branch": ""}]
        c = {"subject": "fix #999", "body": "", "branch": ""}
        self.assertIsNone(gitlink.match_task(c, tasks), "引用不存在的任务 id 不应匹配")

    @unittest.skipUnless(gitlink.git_available(), "系统无 git")
    def test_read_real_repo(self):
        repo = tempfile.mkdtemp(prefix="devlog-git-")
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        subprocess.run(["git", "init", "-q", repo], check=True, env=env)
        Path(repo, "a.txt").write_text("hello\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "feat: 初始提交 #1"],
                       check=True, env=env)

        self.assertTrue(gitlink.is_repo(repo))
        commits = gitlink.read_commits(repo)
        self.assertEqual(len(commits), 1)
        self.assertIn("初始提交", commits[0]["subject"])
        self.assertEqual(commits[0]["insertions"], 1)

        st = gitlink.status_summary(repo)
        self.assertTrue(st["ok"])
        self.assertTrue(st["clean"])

    def test_invalid_repo_path(self):
        self.assertFalse(gitlink.is_repo("/definitely/not/a/repo/xyz"))


class TestSummarizer(unittest.TestCase):
    def setUp(self):
        self.db = Store(":memory:")

    def tearDown(self):
        self.db.close()

    def test_offline_summary_structure(self):
        t = self.db.create_task(title="做完的事")
        self.db.update_task(t["id"], status="done")
        self.db.create_task(title="没做完的事", status="doing")
        self.db.create_issue(title="待解决问题", severity="high")
        out = summarizer.offline_summary(self.db, 1)
        self.assertEqual(out["mode"], "offline")
        self.assertIn("做完的事", out["markdown"])
        self.assertIn("没做完的事", out["markdown"])
        self.assertIn("待解决问题", out["markdown"])
        self.assertEqual(out["stats"]["done"], 1)
        self.assertEqual(out["stats"]["doing"], 1)

    def test_summary_with_no_data(self):
        out = summarizer.offline_summary(self.db, 1)
        self.assertIn("暂无已完成任务", out["markdown"])

    def test_summary_includes_log_hints(self):
        entries = analyzer.parse_log("ERROR ModuleNotFoundError: No module named 'foo'", int(time.time()))
        self.db.add_log_entries(entries, "test", "b1")
        out = summarizer.offline_summary(self.db, 1)
        self.assertIn("缺少 Python 依赖", out["markdown"])

    def test_ai_falls_back_without_key(self):
        out = summarizer.ai_summary(self.db, 1)
        self.assertEqual(out["mode"], "offline", "没有 key 时必须优雅降级")
        self.assertIn("note", out)

    def test_fmt_duration(self):
        self.assertEqual(summarizer.fmt_duration(30), "30 秒")
        self.assertEqual(summarizer.fmt_duration(120), "2 分钟")
        self.assertEqual(summarizer.fmt_duration(3700), "1 小时 1 分")


class TestServer(unittest.TestCase):
    """针对真实 HTTP 服务的端到端测试。"""

    @classmethod
    def setUpClass(cls):
        import threading
        cls.httpd = serve(port=8912)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:8912"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/api/ping", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("服务启动失败")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, path, body=None, headers=None):
        url = self.base + path
        if body is None:
            req = urllib.request.Request(url)
        else:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_ping(self):
        self.assertTrue(self.call("/api/ping")["ok"])

    def test_static_files_served(self):
        for path, needle in [("/", b"DevLog"), ("/style.css", b"--accent"), ("/app.js", b"function")]:
            with self.subTest(path=path):
                with urllib.request.urlopen(self.base + path, timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn(needle, r.read())

    def test_full_task_flow(self):
        t = self.call("/api/tasks/create", {"title": "端到端任务", "priority": 1})["item"]
        self.assertIn("id", t)
        upd = self.call("/api/tasks/update", {"id": t["id"], "status": "doing"})["item"]
        self.assertEqual(upd["status"], "doing")
        self.call("/api/tasks/focus", {"id": t["id"], "seconds": 600})
        after = self.call("/api/tasks/focus", {"id": t["id"], "seconds": 300})["item"]
        self.assertEqual(after["seconds"], 900, "专注时间应累加")
        self.assertTrue(self.call("/api/tasks/delete", {"id": t["id"]})["ok"])

    def test_log_ingest_and_report(self):
        text = "ERROR db down\nERROR db down\nWARN slow"
        d = self.call("/api/logs/ingest", {"text": text})
        self.assertEqual(d["count"], 3)
        self.assertEqual(d["report"]["error_count"], 2)
        report = self.call("/api/log-report")["report"]
        self.assertGreaterEqual(report["total"], 3)

    def test_log_to_issue(self):
        r = self.call("/api/logs/to-issue", {"title": "从日志建的问题", "detail": "堆栈"})
        self.assertEqual(r["item"]["title"], "从日志建的问题")

    def test_bootstrap_has_all_sections(self):
        d = self.call("/api/bootstrap")
        for key in ["tasks", "issues", "shortcuts", "timeline", "attachments",
                    "commits", "logs", "settings"]:
            self.assertIn(key, d, f"bootstrap 缺少 {key}")

    def test_attachment_upload_and_serve(self):
        png = ("data:image/png;base64,"
               "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        item = self.call("/api/attachments/upload",
                         {"data": png, "filename": "t.png", "note": "测试图"})["item"]
        with urllib.request.urlopen(f"{self.base}/files/{item['stored']}", timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "image/png")
        self.assertTrue(self.call("/api/attachments/delete", {"id": item["id"]})["ok"])

    def test_reject_non_image_upload(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.call("/api/attachments/upload",
                      {"data": "data:text/html;base64,PHNjcmlwdD4="})
        self.assertEqual(cm.exception.code, 400)

    def test_reject_cross_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.call("/api/tasks/create", {"title": "x"}, {"Origin": "https://evil.com"})
        self.assertEqual(cm.exception.code, 403, "跨站请求必须被拒绝")

    def test_path_traversal_blocked(self):
        for bad in ["/files/../../../etc/passwd", "/../../../etc/passwd", "/files/....//etc/passwd"]:
            with self.subTest(path=bad):
                try:
                    with urllib.request.urlopen(self.base + bad, timeout=5) as r:
                        self.assertNotIn(b"root:", r.read())
                except urllib.error.HTTPError as e:
                    self.assertIn(e.code, (400, 404))

    def test_unknown_endpoint_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.call("/api/does-not-exist")
        self.assertEqual(cm.exception.code, 404)

    def test_bad_json_handled(self):
        req = urllib.request.Request(
            self.base + "/api/tasks/create", data=b"{not json",
            headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400, "非法 JSON 应返回 400 而不是 500")

    def test_summary_endpoint(self):
        s = self.call("/api/summary?days=1")["summary"]
        self.assertIn("markdown", s)
        self.assertIn("stats", s)

    def test_export_markdown(self):
        with urllib.request.urlopen(self.base + "/api/export?days=7", timeout=10) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/markdown", r.headers["Content-Type"])
            self.assertIn("attachment", r.headers["Content-Disposition"])

    def test_stats_endpoint(self):
        st = self.call("/api/stats")["stats"]
        for k in ["tasks_total", "issues_open", "errors_today", "focus_today"]:
            self.assertIn(k, st)

    def test_git_sync_bad_path(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.call("/api/git/sync", {"repo": "/nope/not/here"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestRegressions(unittest.TestCase):
    """回归测试：修过的真实 bug 不能再犯。"""

    def setUp(self):
        self.db = Store(":memory:")

    def tearDown(self):
        self.db.close()

    def test_task_created_as_done_counts_today(self):
        """直接建成 done 的任务也要计入今日完成。"""
        t = self.db.create_task(title="直接完成", status="done")
        self.assertIsNotNone(t["done_at"], "创建时即为 done 应记录 done_at")
        out = summarizer.offline_summary(self.db, 1)
        self.assertIn("直接完成", out["markdown"])
        self.assertEqual(out["stats"]["done"], 1)

    def test_done_at_not_overwritten(self):
        """已完成任务再改别的字段，不应刷新 done_at。"""
        t = self.db.create_task(title="x", status="done")
        first = t["done_at"]
        time.sleep(1.05)
        again = self.db.update_task(t["id"], status="done", title="y")
        self.assertEqual(again["done_at"], first)

    @unittest.skipUnless(gitlink.git_available(), "系统无 git")
    def test_shortstat_attaches_to_right_commit(self):
        """--shortstat 输出在记录之后，统计不能错位到上一条。"""
        repo = tempfile.mkdtemp(prefix="devlog-stat-")
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        subprocess.run(["git", "init", "-q", repo], check=True, env=env)
        Path(repo, "a.txt").write_text("1\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "one line"], check=True, env=env)
        Path(repo, "b.txt").write_text("1\n2\n3\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "three lines"], check=True, env=env)

        commits = {c["subject"]: c for c in gitlink.read_commits(repo)}
        self.assertEqual(commits["three lines"]["insertions"], 3)
        self.assertEqual(commits["one line"]["insertions"], 1)

    @unittest.skipUnless(gitlink.git_available(), "系统无 git")
    def test_multiline_commit_body_parsed(self):
        """提交信息有多行正文时不能解析错乱。"""
        repo = tempfile.mkdtemp(prefix="devlog-body-")
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        subprocess.run(["git", "init", "-q", repo], check=True, env=env)
        Path(repo, "a.txt").write_text("x\n")
        subprocess.run(["git", "-C", repo, "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "标题行",
                        "-m", "详细说明第一行\n详细说明第二行"], check=True, env=env)
        c = gitlink.read_commits(repo)[0]
        self.assertEqual(c["subject"], "标题行")
        self.assertIn("详细说明第二行", c["body"])
        self.assertNotIn("insertion", c["body"], "统计行不应混进 body")
        self.assertEqual(c["insertions"], 1)

    @unittest.skipUnless(gitlink.git_available(), "系统无 git")
    def test_reads_commits_from_all_branches(self):
        """停在 main 分支时，功能分支上的提交也必须能读到。"""
        repo = tempfile.mkdtemp(prefix="devlog-branch-")
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com"}
        g = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, env=env,
                                      capture_output=True)
        subprocess.run(["git", "init", "-q", repo], check=True, env=env)
        Path(repo, "a.txt").write_text("a\n")
        g("add", "."); g("commit", "-q", "-m", "主干提交")
        g("checkout", "-q", "-b", "feat/x")
        Path(repo, "b.txt").write_text("b\n")
        g("add", "."); g("commit", "-q", "-m", "功能分支提交")
        g("checkout", "-q", "-")   # 回到主干

        subjects = [c["subject"] for c in gitlink.read_commits(repo)]
        self.assertIn("功能分支提交", subjects, "必须能读到其他分支的提交")
        self.assertIn("主干提交", subjects)

        byname = {c["subject"]: c for c in gitlink.read_commits(repo)}
        self.assertEqual(byname["功能分支提交"]["branch"], "feat/x",
                         "分支名应逐条判断，而不是统一用 HEAD")

    def test_pick_branch_from_refs(self):
        self.assertEqual(gitlink._pick_branch("HEAD -> main, origin/main", "x"), "main")
        self.assertEqual(gitlink._pick_branch("feat/pay", "x"), "feat/pay")
        self.assertEqual(gitlink._pick_branch("tag: v1.0", "fallback"), "fallback")
        self.assertEqual(gitlink._pick_branch("", "fallback"), "fallback")
        self.assertEqual(gitlink._pick_branch("origin/main", "fallback"), "fallback")
