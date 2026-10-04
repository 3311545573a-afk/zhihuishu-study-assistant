import http.client
import io
import json
import os
from pathlib import Path
import run_control
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from web_console import (
    ASSETS_DIR,
    ConfigStore,
    LogBuffer,
    LogTailer,
    Supervisor,
    assistant_command,
    create_server,
    is_duplicate_log_line,
    is_local_host,
    mask_api_key,
    python_executable,
)

ROOT = Path(__file__).resolve().parent.parent


def wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_diag_folder(root, name, age_days=0.0, size=100):
    """造一个假的诊断文件夹，测试绝不碰真实的 diagnostics/。"""
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    for filename in ("page.json", "page.png"):
        (folder / filename).write_bytes(b"x" * size)
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(folder, (stamp, stamp))
    return folder


class RecordingStdin:
    """替代真实管道：关闭后仍能读到写进去的内容，方便断言。"""

    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, text):
        self.value += text

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    """替代 subprocess.Popen 的假进程，不真的启动任何东西。"""

    def __init__(self, pid=4242, code=None):
        self.pid = pid
        self.code = code
        self.exit_code = 0
        self.stdin = RecordingStdin()
        self.stdout = iter(())
        self.wait_timeouts = []
        self.fail_first_wait = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.fail_first_wait:
            self.fail_first_wait = False
            raise subprocess.TimeoutExpired("python", timeout)
        self.code = self.exit_code
        return self.code


class LogBufferTests(unittest.TestCase):
    def test_duplicate_log_lines_are_detected(self):
        self.assertTrue(is_duplicate_log_line("2026-10-03 06:30:00,123 INFO 已提交选项：2"))
        self.assertFalse(is_duplicate_log_line("请在新打开的浏览器中进入课程。"))
        self.assertFalse(is_duplicate_log_line("[控制台] 助手已退出，退出码 0"))

    def test_buffer_keeps_limit_and_orders_seq(self):
        buffer = LogBuffer(limit=3)
        for i in range(5):
            buffer.append(f"第 {i} 行", "log")
        items = buffer.snapshot()
        self.assertEqual([i["text"] for i in items], ["第 2 行", "第 3 行", "第 4 行"])
        self.assertEqual([i["seq"] for i in items], [3, 4, 5])
        self.assertEqual(buffer.latest_seq(), 5)

    def test_since_returns_only_new_records(self):
        buffer = LogBuffer()
        buffer.append("a", "log")
        buffer.append("b", "log")
        self.assertEqual([i["text"] for i in buffer.since(1)], ["b"])
        self.assertEqual(buffer.since(2), [])

    def test_wait_for_wakes_up_on_append(self):
        buffer = LogBuffer()
        threading.Timer(0.05, lambda: buffer.append("醒来", "log")).start()
        items = buffer.wait_for(0, timeout=2)
        self.assertEqual([i["text"] for i in items], ["醒来"])

    def test_wait_for_times_out_without_new_records(self):
        buffer = LogBuffer()
        buffer.append("旧", "log")
        started = time.monotonic()
        self.assertEqual(buffer.wait_for(1, timeout=0.1), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.05)


class LogTailerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "study.log"
        self.buffer = LogBuffer()
        self.tailer = None

    def start_tailer(self):
        self.tailer = LogTailer(self.path, self.buffer, interval=0.02)
        self.tailer.start()
        self.addCleanup(self.tailer.stop)

    def texts(self):
        return [item["text"] for item in self.buffer.snapshot()]

    def test_tailer_reads_incrementally_and_recovers_after_truncate(self):
        self.path.write_text("第一行\n", encoding="utf-8")
        self.start_tailer()
        self.assertTrue(wait_until(lambda: self.texts() == ["第一行"]))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("第二行\n")
        self.assertTrue(wait_until(lambda: self.texts() == ["第一行", "第二行"]))
        self.path.write_text("重来\n", encoding="utf-8")
        self.assertTrue(wait_until(lambda: self.texts()[-1] == "重来"))
        self.assertEqual(self.texts().count("第一行"), 1)

    def test_tailer_waits_for_missing_file(self):
        self.start_tailer()
        self.assertEqual(self.texts(), [])
        self.path.write_text("后来才有\n", encoding="utf-8")
        self.assertTrue(wait_until(lambda: self.texts() == ["后来才有"]))

    def test_tailer_keeps_utf8_across_chunk_boundary(self):
        self.path.write_text("标题\n", encoding="utf-8")
        self.start_tailer()
        self.assertTrue(wait_until(lambda: self.texts() == ["标题"]))
        with self.path.open("ab") as handle:
            handle.write("数学公式分段函数\n".encode("utf-8"))
        self.assertTrue(wait_until(lambda: self.texts()[-1] == "数学公式分段函数"))


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.python = Path("python.exe")
        self.buffer = LogBuffer()
        self.process = FakeProcess()
        self.popen = Mock(return_value=self.process)
        self.run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.supervisor = Supervisor(self.root, self.config_path, self.python, self.buffer,
                                     popen=self.popen, run=self.run)

    def test_python_executable_prefers_project_venv(self):
        venv = self.root / ".venv" / "Scripts" / "python.exe"
        venv.parent.mkdir(parents=True)
        venv.write_text("", encoding="utf-8")
        self.assertEqual(python_executable(self.root), venv)

    def test_assistant_command_uses_utf8_and_config(self):
        self.assertEqual(
            assistant_command(self.python, self.config_path),
            [str(self.python), "-u", "-X", "utf8", "study_assistant.py", "--config", str(self.config_path)],
        )

    def test_assistant_command_is_unbuffered(self):
        # 子进程 stdout 走管道，不加 -u 助手 print 的提示会卡在缓冲区里。
        self.assertIn("-u", assistant_command(self.python, self.config_path))

    def test_start_uses_utf8_and_config_path(self):
        ok, message = self.supervisor.start()
        self.assertTrue(ok)
        self.assertIn("4242", message)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], assistant_command(self.python, self.config_path))
        self.assertEqual(Path(kwargs["cwd"]), self.root)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertTrue(self.buffer.snapshot()[-1]["text"].startswith("[控制台] 已启动助手"))

    def test_second_start_is_rejected_while_running(self):
        self.supervisor.start()
        ok, message = self.supervisor.start()
        self.assertFalse(ok)
        self.assertIn("已在运行", message)
        self.assertEqual(self.popen.call_count, 1)

    def test_start_reports_oserror(self):
        self.popen.side_effect = OSError("没有权限")
        ok, message = self.supervisor.start()
        self.assertFalse(ok)
        self.assertIn("无法启动助手", message)

    def test_stop_sends_quit_and_reports_graceful_exit(self):
        self.supervisor.start()
        ok, message = self.supervisor.stop()
        self.assertTrue(ok)
        self.assertEqual(self.process.stdin.value, "q\n", "应先给助手一个 q 走优雅退出")
        self.assertIn("优雅停止", message)
        self.assertEqual(self.run.call_count, 0)

    def test_stop_reports_nonzero_exit_honestly(self):
        self.supervisor.start()
        self.process.exit_code = 1
        ok, message = self.supervisor.stop()
        self.assertTrue(ok)
        self.assertIn("退出码 1", message)
        self.assertNotIn("已保存", message, "没走到退出前保存就不能宣称已保存")

    def test_stop_kills_tree_when_child_ignores_quit(self):
        self.process.fail_first_wait = True
        self.supervisor.start()
        ok, message = self.supervisor.stop()
        self.assertTrue(ok)
        self.assertEqual(self.process.stdin.value, "q\n")
        self.assertEqual(self.run.call_count, 1)
        self.assertIn("/T", self.run.call_args.args[0])
        self.assertIn("/PID", self.run.call_args.args[0])
        self.assertIn("强制", message)

    def test_stop_when_not_running_is_not_an_error(self):
        ok, message = self.supervisor.stop()
        self.assertTrue(ok)
        self.assertIn("未在运行", message)

    def test_send_after_exit_reports_failure(self):
        self.supervisor.start()
        self.process.code = 0
        ok, message = self.supervisor.send("")
        self.assertFalse(ok)
        self.assertIn("未在运行", message)

    def test_send_enter_and_quit_go_to_stdin(self):
        self.supervisor.start()
        ok, message = self.supervisor.send("")
        self.assertTrue(ok)
        self.assertEqual(self.process.stdin.value, "\n")
        self.supervisor.send("q")
        self.assertEqual(self.process.stdin.value, "\nq\n")
        self.assertIn("q", self.buffer.snapshot()[-1]["text"])

    def test_status_labels(self):
        self.assertEqual(self.supervisor.status()["label"], "未运行")
        self.supervisor.start()
        self.assertEqual(self.supervisor.status()["label"], "启动中")
        self.buffer.append("2026-10-03 06:30:00,000 INFO 已识别课程页面，自动开始监控：第一章", "log")
        self.assertEqual(self.supervisor.status()["label"], "监控中")
        self.buffer.append("2026-10-03 06:31:00,000 INFO 已提交选项：2；等待网页反馈", "log")
        self.assertEqual(self.supervisor.status()["label"], "正在答题")
        self.buffer.append("请在浏览器处理当前题目/提示，或调整配置后重启。", "app")
        status = self.supervisor.status()
        self.assertEqual(status["label"], "等待你在浏览器处理")
        self.assertIn("回车", status["detail"])
        self.process.code = 3
        self.assertEqual(self.supervisor.status()["label"], "异常退出")

    def test_status_reports_stale_config_after_edit(self):
        self.config_path.write_text("{}", encoding="utf-8")
        self.supervisor.start()
        self.assertFalse(self.supervisor.status()["config_stale"])
        future = time.time() + 5
        os.utime(self.config_path, (future, future))
        status = self.supervisor.status()
        self.assertTrue(status["config_stale"])
        self.assertRegex(status["config_changed_at"], r"^\d{2}:\d{2}$")

    def test_config_stale_is_false_when_not_running(self):
        self.config_path.write_text("{}", encoding="utf-8")
        future = time.time() + 5
        os.utime(self.config_path, (future, future))
        self.assertFalse(self.supervisor.status()["config_stale"])

    def test_status_ignores_console_notes_for_label(self):
        self.buffer.append("2026-10-03 06:31:00,000 INFO 已提交选项：1；等待网页反馈", "log")
        self.supervisor.start()
        self.supervisor.send("")
        self.buffer.append("2026-10-03 06:31:05,000 INFO 已提交选项：2；等待网页反馈", "log")
        self.assertEqual(self.supervisor.status()["label"], "正在答题")


class ConfigStoreTests(unittest.TestCase):
    BASE = {
        "course_url": "https://studyvideoh5.zhihuishu.com/stuStudy?recruitAndCourseId=abc",
        "browser_channel": "auto",
        "poll_seconds": 2,
        "ai": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "api_key": "sk-abcdefgh1234",
            "min_confidence": 0.85,
            "timeout_seconds": 45,
        },
        "selectors": {"video": "video"},
    }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.run = Mock(return_value=subprocess.CompletedProcess(
            [], 0, "配置格式正确。AI 配置已填写（尚未联网验证）。", ""))
        self.store = ConfigStore(self.root, self.config_path, Path("python.exe"), run=self.run)

    def write_config(self, data=None):
        self.config_path.write_text(json.dumps(data if data is not None else self.BASE, ensure_ascii=False),
                                    encoding="utf-8-sig")

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def payload(self):
        return json.loads(json.dumps(self.BASE))

    def test_mask_api_key(self):
        self.assertEqual(mask_api_key(""), "")
        self.assertEqual(mask_api_key("sk-abcdefgh1234"), "sk-****1234")
        self.assertEqual(mask_api_key("abc"), "sk-****")

    def test_read_masked_never_returns_raw_key(self):
        self.write_config()
        data = self.store.read_masked()
        self.assertTrue(data["ok"])
        self.assertEqual(data["api_key_mask"], "sk-****1234")
        self.assertTrue(data["api_key_set"])
        self.assertNotIn("sk-abcdefgh1234", json.dumps(data))
        self.assertEqual(data["config"]["ai"]["api_key"], "")

    def test_read_missing_or_broken_config(self):
        ok, message = self.store.read()
        self.assertFalse(ok)
        self.assertIn("找不到配置文件", message)
        self.config_path.write_text("{不是 JSON", encoding="utf-8")
        ok, message = self.store.read()
        self.assertFalse(ok)
        self.assertIn("合法 JSON", message)
        self.assertFalse(self.store.read_masked()["ok"])

    def test_blank_key_keeps_existing_secret(self):
        self.write_config()
        payload = self.payload()
        payload["ai"]["api_key"] = ""
        ok, message = self.store.save(payload)
        self.assertTrue(ok)
        self.assertEqual(self.read_config()["ai"]["api_key"], "sk-abcdefgh1234")

    def test_masked_key_submission_keeps_existing_secret(self):
        self.write_config()
        payload = self.payload()
        payload["ai"]["api_key"] = "sk-****1234"
        self.assertTrue(self.store.save(payload)[0])
        self.assertEqual(self.read_config()["ai"]["api_key"], "sk-abcdefgh1234")

    def test_new_key_replaces_secret(self):
        self.write_config()
        payload = self.payload()
        payload["ai"]["api_key"] = "sk-brandnew9999"
        self.assertTrue(self.store.save(payload)[0])
        self.assertEqual(self.read_config()["ai"]["api_key"], "sk-brandnew9999")

    def test_invalid_candidate_is_rejected_without_touching_file(self):
        self.write_config()
        before = self.config_path.read_text(encoding="utf-8")
        self.run.return_value = subprocess.CompletedProcess(
            [], 2, "", "配置或运行错误：course_url 必须是 HTTPS 的 studyvideoh5.zhihuishu.com 课程地址")
        ok, message = self.store.save({**self.payload(), "course_url": "http://evil.example/x"})
        self.assertFalse(ok)
        self.assertIn("course_url", message)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        self.assertFalse((self.root / "config.json.bak").exists())

    def test_successful_save_backs_up_and_writes_utf8_json(self):
        self.write_config()
        payload = self.payload()
        payload["poll_seconds"] = 3
        ok, message = self.store.save(payload)
        self.assertTrue(ok)
        self.assertIn("配置格式正确", message)
        self.assertEqual(self.read_config()["poll_seconds"], 3)
        self.assertTrue((self.root / "config.json.bak").exists())
        self.assertTrue(self.config_path.read_text(encoding="utf-8").endswith("\n"))

    def test_validation_uses_check_config_on_a_temp_file(self):
        self.write_config()
        self.assertTrue(self.store.save(self.payload())[0])
        args = self.run.call_args.args[0]
        self.assertIn("--check-config", args)
        self.assertTrue(args[-1].endswith(".json"))
        self.assertNotEqual(args[-1], str(self.config_path))
        self.assertFalse(Path(args[-1]).exists())
        self.assertIs(self.run.call_args.kwargs["capture_output"], True)

    def test_selectors_are_merged_not_replaced(self):
        self.write_config()
        payload = self.payload()
        payload["selectors"] = {"next": "#customNext"}
        self.assertTrue(self.store.save(payload)[0])
        selectors = self.read_config()["selectors"]
        self.assertEqual(selectors["next"], "#customNext")
        self.assertEqual(selectors["video"], "video")

    def test_unknown_payload_fields_are_ignored(self):
        self.write_config()
        payload = self.payload()
        payload["injected"] = "x"
        self.assertTrue(self.store.save(payload)[0])
        self.assertNotIn("injected", self.read_config())

    def test_check_timeout_is_reported(self):
        self.run.side_effect = subprocess.TimeoutExpired("python", 15)
        valid, message = self.store.check()
        self.assertFalse(valid)
        self.assertIn("超时", message)

    def test_check_result_is_cached(self):
        self.write_config()
        self.store.state()
        self.store.state()
        self.assertEqual(self.run.call_count, 1)

    def test_state_reports_config_and_ai_readiness(self):
        self.write_config()
        self.assertEqual(self.store.state(),
                         {"config_ok": True, "ai_ready": True,
                          "config_message": "配置格式正确。AI 配置已填写（尚未联网验证）。"})
        self.run.return_value = subprocess.CompletedProcess(
            [], 2, "配置格式正确。AI 配置未填齐，遇到题目时将停止并提示。", "")
        self.store._check_cache = None
        state = self.store.state()
        self.assertTrue(state["config_ok"])
        self.assertFalse(state["ai_ready"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        assets = Path(self.temp.name)
        (assets / "index.html").write_text("<!doctype html><title>控制台</title>", encoding="utf-8")
        (assets / "app.js").write_text("// app", encoding="utf-8")
        (assets / "style.css").write_text("/* css */", encoding="utf-8")
        self.buffer = LogBuffer()
        self.supervisor = Mock()
        self.supervisor.status.return_value = {
            "running": False, "pid": None, "uptime": 0.0, "exit_code": None,
            "label": "未运行", "detail": "尚未启动助手",
            "config_stale": False, "config_changed_at": ""}
        self.supervisor.start.return_value = (True, "助手已启动（PID 1）")
        self.supervisor.stop.return_value = (True, "助手已停止")
        self.supervisor.send.return_value = (True, "已发送回车")
        self.store = Mock()
        self.store.read_masked.return_value = {
            "ok": True, "error": "", "config": {"ai": {"api_key": ""}},
            "api_key_set": True, "api_key_mask": "sk-****1234"}
        self.store.save.return_value = (True, "配置已保存")
        self.store.state.return_value = {"config_ok": True, "ai_ready": True,
                                         "config_message": "配置格式正确。"}
        self.diag_dir = Path(self.temp.name) / "diagnostics"
        self.diag_dir.mkdir(parents=True, exist_ok=True)
        # 让整套 HttpTests 密闭：/api/status 现在会读运行目录，不能落到真实 logs/
        self.run_dir = Path(self.temp.name) / "run"
        run_dir_patch = patch.object(run_control, "RUN_DIR", self.run_dir)
        run_dir_patch.start()
        self.addCleanup(run_dir_patch.stop)
        self.server = create_server("127.0.0.1", 0, self.supervisor, self.store, self.buffer, assets,
                                    diagnostics_dir=self.diag_dir)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            payload = None
            merged = dict(headers or {})
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                merged.setdefault("Content-Type", "application/json")
                merged.setdefault("Content-Length", str(len(payload)))
            conn.request(method, path, body=payload, headers=merged)
            response = conn.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            conn.close()

    def json_of(self, method, path, body=None, headers=None):
        status, headers, data = self.request(method, path, body, headers)
        return status, headers, json.loads(data.decode("utf-8"))

    def test_status_returns_json_state(self):
        status, headers, payload = self.json_of("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["label"], "未运行")
        self.assertTrue(payload["config_ok"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_is_local_host_rules(self):
        for value in ("127.0.0.1", "127.0.0.1:8765", "localhost:8765", "[::1]:8765", "::1"):
            self.assertTrue(is_local_host(value), value)
        for value in ("", "evil.example", "evil.example:8765", "10.0.0.5:8765", "localhost.evil.example"):
            self.assertFalse(is_local_host(value), value)

    def test_config_endpoint_masks_key(self):
        status, headers, payload = self.json_of("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertNotIn("sk-abcdefgh1234", json.dumps(payload))
        self.assertEqual(payload["api_key_mask"], "sk-****1234")

    def test_foreign_origin_is_rejected(self):
        status, _, _ = self.request("GET", "/api/status", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/start", body={},
                                    headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_foreign_host_header_is_rejected(self):
        status, _, _ = self.request("GET", "/api/status", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_action_endpoints_return_full_status(self):
        for path in ("/api/start", "/api/stop", "/api/restart"):
            with self.subTest(path=path):
                status, _, payload = self.json_of("POST", path, body={})
                self.assertEqual(status, 200)
                self.assertIn("config_ok", payload["status"])
                self.assertIn("label", payload["status"])
                self.assertIn("config_stale", payload["status"])

    def test_start_returns_409_when_already_running(self):
        self.supervisor.start.return_value = (False, "助手已在运行（PID 1）")
        status, _, payload = self.json_of("POST", "/api/start", body={})
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertIn("已在运行", payload["message"])

    def test_stop_and_stdin_endpoints(self):
        status, _, payload = self.json_of("POST", "/api/stop", body={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _, payload = self.json_of("POST", "/api/stdin", body={"text": "q"})
        self.assertEqual(status, 200)
        self.supervisor.send.assert_called_with("q")

    def test_restart_stops_then_starts(self):
        calls = []
        self.supervisor.stop.side_effect = lambda: (calls.append("stop"), (True, "助手已停止"))[1]
        self.supervisor.start.side_effect = lambda: (calls.append("start"), (True, "助手已启动（PID 2）"))[1]
        status, _, payload = self.json_of("POST", "/api/restart", body={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(calls, ["stop", "start"])

    def test_restart_reports_stop_failure(self):
        self.supervisor.stop.return_value = (False, "无法结束助手进程（PID 1），请在任务管理器里结束它")
        status, _, payload = self.json_of("POST", "/api/restart", body={})
        self.assertEqual(status, 500)
        self.assertFalse(payload["ok"])
        self.supervisor.start.assert_not_called()

    def test_stdin_conflict_returns_409(self):
        self.supervisor.send.return_value = (False, "助手未在运行，无法发送输入")
        status, _, payload = self.json_of("POST", "/api/stdin", body={"text": ""})
        self.assertEqual(status, 409)
        self.assertIn("未在运行", payload["message"])

    def test_stdin_rejects_non_string_text(self):
        status, _, payload = self.json_of("POST", "/api/stdin", body={"text": 5})
        self.assertEqual(status, 400)
        self.assertIn("text", payload["error"])

    def test_config_post_returns_validation_message(self):
        self.store.save.return_value = (False, "配置或运行错误：poll_seconds 必须在 0.5 到 30 之间")
        status, _, payload = self.json_of("POST", "/api/config", body={"poll_seconds": 99})
        self.assertEqual(status, 400)
        self.assertIn("poll_seconds", payload["message"])

    def test_diagnostics_endpoint_lists_usage_and_a_plan(self):
        make_diag_folder(self.diag_dir, "20260101-000000-000001", age_days=30)
        make_diag_folder(self.diag_dir, "20261003-000000-000002")
        status, _, payload = self.json_of("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["usage"]["count"], 2)
        self.assertEqual(payload["usage"]["newest"], "20261003-000000-000002")
        self.assertEqual(payload["deleted"], ["20260101-000000-000001"])
        self.assertEqual(payload["retention"]["keep_count"], 50)
        self.assertEqual(len(list(self.diag_dir.iterdir())), 2, "预览不能真的删")

    def test_diagnostics_cleanup_deletes_and_reports(self):
        make_diag_folder(self.diag_dir, "20260101-000000-000001", age_days=30)
        make_diag_folder(self.diag_dir, "20261003-000000-000002")
        status, _, payload = self.json_of("POST", "/api/diagnostics/cleanup", body={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deleted_count"], 1)
        self.assertEqual([p.name for p in self.diag_dir.iterdir()], ["20261003-000000-000002"])

    def test_diagnostics_uses_retention_from_config(self):
        self.store.read.return_value = (True, {"diagnostics": {"keep_days": 3650, "keep_count": 1,
                                                              "keep_mb": 100}})
        make_diag_folder(self.diag_dir, "20261001-000000-000001")
        make_diag_folder(self.diag_dir, "20261003-000000-000002")
        status, _, payload = self.json_of("GET", "/api/diagnostics")
        self.assertEqual(payload["retention"]["keep_count"], 1)
        self.assertEqual(payload["deleted_count"], 1)

    def test_diagnostics_survives_a_broken_config(self):
        self.store.read.return_value = (False, "配置不是合法 JSON")
        status, _, payload = self.json_of("GET", "/api/diagnostics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["retention"], {"keep_days": 7.0, "keep_count": 50, "keep_mb": 200.0})

    def test_invalid_json_body_returns_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/start", body=b"{oops",
                         headers={"Content-Type": "application/json", "Content-Length": "5"})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertIn("JSON", json.loads(response.read().decode("utf-8"))["error"])
        finally:
            conn.close()

    def test_oversized_body_returns_413(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", "/api/start", body=b"",
                         headers={"Content-Type": "application/json",
                                  "Content-Length": str(2 * 1024 * 1024)})
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            conn.close()

    def test_favicon_is_answered_without_noise(self):
        status, headers, _ = self.request("GET", "/favicon.ico")
        self.assertEqual(status, 204)

    def test_second_server_on_same_port_is_rejected(self):
        """Windows 下 SO_REUSEADDR 会让第二个实例静默抢占端口，必须能报错。"""
        with self.assertRaises(OSError):
            create_server("127.0.0.1", self.port, self.supervisor, self.store, self.buffer,
                          Path(self.temp.name), diagnostics_dir=self.diag_dir)

    def test_unknown_path_returns_404_json(self):
        status, headers, payload = self.json_of("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_index_and_assets_are_served(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("控制台", body.decode("utf-8"))
        self.assertEqual(self.request("GET", "/app.js")[0], 200)
        self.assertEqual(self.request("GET", "/style.css")[0], 200)

    def test_stream_sends_history_then_new_lines(self):
        self.buffer.append("历史一", "log")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/logs/stream")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.headers["Content-Type"])
            first = response.fp.readline().decode("utf-8")
            self.assertTrue(first.startswith("id: "), first)
            data = response.fp.readline().decode("utf-8")
            self.assertIn("历史一", data)
            self.buffer.append("新的二", "log")
            self.buffer.append("新的三", "log")
            lines = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not any("新的三" in line for line in lines):
                lines.append(response.fp.readline().decode("utf-8"))
            self.assertTrue(any("新的三" in line for line in lines), lines)
        finally:
            conn.close()

    def test_log_stream_rejects_post(self):
        status, _, payload = self.json_of("POST", "/api/logs/stream", body={})
        self.assertEqual(status, 405)
        self.assertFalse(payload["ok"])

    def _run_dir(self):
        """测试专用的运行目录：绝不写真实 logs/。"""
        return Path(self.temp.name) / "run"

    def test_video_command_writes_control_file(self):
        run_dir = self._run_dir()
        self.supervisor.status.return_value = dict(self.supervisor.status.return_value, running=True)
        with patch.object(run_control, "RUN_DIR", run_dir):
            # /api/video 要求助手「已经进过监控循环」：先写一份状态当作它回报过
            run_control.write_state({"applied_seq": 0, "rate": 1.0, "requested_rate": None,
                                     "paused": False, "manual_paused": False, "time": 1.0,
                                     "duration": 60.0, "lesson": "1.3.1 函数极限", "waiting": False})
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            written = json.loads((run_dir / "run_control.json").read_text(encoding="utf-8"))
            self.assertEqual(written["action"], "pause")
            self.assertEqual(written["seq"], 1)
            self.assertTrue(payload["video_pending"], "还没被助手应用时要标出等待响应")

            # 倍速的成功路径也要走一遍：value 得原样落盘，序号接着递增
            status, _, payload = self.json_of("POST", "/api/video", {"action": "rate", "value": 1.5})
            self.assertEqual(status, 200)
            written = json.loads((run_dir / "run_control.json").read_text(encoding="utf-8"))
            self.assertEqual(written["action"], "rate")
            self.assertEqual(written["value"], 1.5)
            self.assertEqual(written["seq"], 2)

    def test_video_command_rejects_bad_input(self):
        self.supervisor.status.return_value = dict(self.supervisor.status.return_value, running=True)
        with patch.object(run_control, "RUN_DIR", self._run_dir()):
            status, _, payload = self.json_of("POST", "/api/video", {"action": "关机"})
            self.assertEqual(status, 400)
            self.assertFalse(payload["ok"])
            status, _, payload = self.json_of("POST", "/api/video", {"action": "rate", "value": 99})
            self.assertEqual(status, 400)
            self.assertIn("倍速", payload["error"])
            # 不做隐式类型转换：字符串 "1.5" 与布尔 true 都必须被拒
            for bad in ("1.5", True):
                status, _, payload = self.json_of("POST", "/api/video", {"action": "rate", "value": bad})
                self.assertEqual(status, 400, bad)
                self.assertIn("倍速", payload["error"])

    def test_video_command_needs_assistant_ready(self):
        """助手没在跑、或进程起来了但还没进监控循环，都不能接受命令。

        后者如果不拦，命令会被助手的启动基线当成"启动前的旧命令"吃掉，而页面已经
        显示"已发送"——那是谎报成功。
        """
        run_dir = self._run_dir()
        with patch.object(run_control, "RUN_DIR", run_dir):
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 409)
            self.assertIn("助手", payload["error"])

            self.supervisor.status.return_value = dict(self.supervisor.status.return_value, running=True)
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 409)
            self.assertIn("启动", payload["error"])
            self.assertFalse((run_dir / "run_control.json").exists(), "被拒绝的命令不能落盘")

            # 最关键的一种：状态文件是"上一轮"留下的，这一轮还没进监控循环
            self.supervisor.status.return_value = dict(self.supervisor.status.return_value,
                                                       running=True, uptime=60.0)
            state = {"applied_seq": 4, "rate": 1.0, "requested_rate": None, "paused": False,
                     "manual_paused": False, "time": 1.0, "duration": 60.0,
                     "lesson": "1.3.1 函数极限", "waiting": False}
            run_control.write_state(dict(state), now=time.time() - 300)
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 409)
            self.assertIn("启动", payload["error"])
            self.assertFalse((run_dir / "run_control.json").exists(), "被拒绝的命令不能落盘")

            # 反过来：状态是这一轮写的（哪怕已超龄，比如正卡在长 AI 调用里）必须放行
            run_control.write_state(dict(state), now=time.time() - 30)
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 200, "长 AI 调用导致状态陈旧时不能拒绝命令")
            self.assertTrue(payload["ok"])

    def test_status_carries_video_state(self):
        run_dir = self._run_dir()
        with patch.object(run_control, "RUN_DIR", run_dir):
            run_control.write_state({"applied_seq": 0, "rate": 2.0, "requested_rate": 2.0,
                                     "paused": False, "manual_paused": False,
                                     "time": 12.0, "duration": 60.0,
                                     "lesson": "1.3.1 函数极限", "waiting": False})
            status, _, payload = self.json_of("GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertEqual(payload["video"]["rate"], 2.0)
            self.assertEqual(payload["video"]["lesson"], "1.3.1 函数极限")
            self.assertFalse(payload["video_pending"])
            run_control.write_command("pause")
            status, _, payload = self.json_of("GET", "/api/status")
            self.assertTrue(payload["video_pending"], "序号领先于 applied_seq 时要提示等待助手响应")

    def test_video_command_reports_write_failure(self):
        """命令文件写不进去时要如实 500，不能假装成功。"""
        self.supervisor.status.return_value = dict(self.supervisor.status.return_value, running=True)
        with patch.object(run_control, "RUN_DIR", self._run_dir()), \
                patch.object(run_control, "write_command", return_value=None):
            # 先让助手"回报过"（否则会先被"还没进监控循环"挡成 409）
            run_control.write_state({"applied_seq": 0, "rate": 1.0, "requested_rate": None,
                                     "paused": False, "manual_paused": False, "time": 1.0,
                                     "duration": 60.0, "lesson": "1.3.1 函数极限", "waiting": False})
            status, _, payload = self.json_of("POST", "/api/video", {"action": "pause"})
            self.assertEqual(status, 500)
            self.assertFalse(payload["ok"])
            self.assertIn("控制命令", payload["error"])


class AssetTests(unittest.TestCase):
    def test_index_contains_expected_controls(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="start"', 'id="stop"', 'id="restart"', 'id="stdin"', 'id="send-text"',
                       'id="send-enter"', 'id="send-quit"', 'id="log"',
                       'id="config-form"', 'id="status-label"', 'id="status-warning"',
                       'id="autoscroll"', 'id="diag-summary"', 'id="diag-check"',
                       'id="diag-confirm"', 'id="diag-confirm-yes"', 'id="diag-confirm-no"',
                       'id="diag_keep_days"', 'id="diag_keep_count"', 'id="diag_keep_mb"'):
            self.assertIn(marker, html)

    def test_assets_are_utf8_and_reference_each_other(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("app.js", html)
        self.assertIn("style.css", html)
        self.assertIn("发送回车", html)
        self.assertIn("留空表示不修改", html)

    def test_launcher_cmd_targets_web_console(self):
        text = (ROOT / "web_console.cmd").read_text(encoding="utf-8")
        self.assertIn("web_console.py", text)
        self.assertIn("-X utf8", text)


class RealProcessTests(unittest.TestCase):
    """真实子进程验证 Popen 接线：UTF-8、stdin 输入、退出码与状态标签。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "study_assistant.py").write_text(
            "import sys\n"
            "print('假助手已启动', flush=True)\n"
            "for line in sys.stdin:\n"
            "    print('收到：' + line.strip(), flush=True)\n"
            "    if line.strip() == 'q':\n"
            "        break\n",
            encoding="utf-8")
        self.buffer = LogBuffer()
        self.supervisor = Supervisor(self.root, self.root / "config.json",
                                     python_executable(ROOT), self.buffer, run=Mock())

    def texts(self):
        return [item["text"] for item in self.buffer.snapshot()]

    def test_start_send_quit_and_exit_code(self):
        ok, message = self.supervisor.start()
        self.assertTrue(ok, message)
        self.addCleanup(self.supervisor.stop)
        self.assertTrue(wait_until(lambda: any("假助手已启动" in t for t in self.texts())),
                        self.texts())
        self.assertTrue(self.supervisor.status()["running"])
        self.assertTrue(self.supervisor.send("")[0])
        self.assertTrue(wait_until(lambda: any("收到" in t for t in self.texts())), self.texts())
        self.assertTrue(self.supervisor.send("q")[0])
        self.assertTrue(wait_until(lambda: not self.supervisor.status()["running"], timeout=10))
        status = self.supervisor.status()
        self.assertEqual(status["exit_code"], 0)
        self.assertEqual(status["label"], "已停止")

    def test_stop_end_to_end_kills_the_process(self):
        (self.root / "study_assistant.py").write_text(
            "import time\nprint('不理会输入', flush=True)\ntime.sleep(120)\n", encoding="utf-8")
        # 这条测试要真的调用 taskkill，所以不能用 run=Mock()。
        supervisor = Supervisor(self.root, self.root / "config.json",
                                python_executable(ROOT), self.buffer)
        ok, message = supervisor.start()
        self.assertTrue(ok, message)
        self.addCleanup(supervisor.stop)
        self.assertTrue(wait_until(lambda: any("不理会输入" in t for t in self.texts())), self.texts())
        with patch("web_console.GRACEFUL_STOP_SECONDS", 0.3):
            ok, message = supervisor.stop()
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(lambda: not supervisor.status()["running"], timeout=10))

    def test_stop_is_graceful_with_a_real_child_reading_stdin(self):
        ok, message = self.supervisor.start()
        self.assertTrue(ok, message)
        self.addCleanup(self.supervisor.stop)
        self.assertTrue(wait_until(lambda: any("假助手已启动" in t for t in self.texts())), self.texts())
        ok, message = self.supervisor.stop()
        self.assertTrue(ok, message)
        self.assertIn("优雅停止", message)
        self.assertEqual(self.supervisor.status()["exit_code"], 0)


class ConsolePageBrowserTests(unittest.TestCase):
    """真实浏览器里的页面回归：行样式不能撞上容器样式、密钥不回显。"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("未安装 Playwright")
        cls.pw = sync_playwright().start()
        cls.browser = None
        for channel in ("chrome", "msedge"):
            try:
                cls.browser = cls.pw.chromium.launch(channel=channel, headless=True)
                break
            except Exception:  # noqa: BLE001 - 换下一个本机浏览器
                continue
        if cls.browser is None:
            cls.pw.stop()
            raise unittest.SkipTest("本机没有可用的 Chrome 或 Edge")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.buffer = LogBuffer()
        for i in range(20):
            self.buffer.append(f"2026-10-03 06:30:{i:02d},000 INFO 测试日志第 {i} 行", "log")
        self.buffer.append("请在浏览器处理当前题目/提示，或调整配置后重启。", "app")
        self.supervisor = Mock()
        self.supervisor.status.return_value = {
            "running": False, "pid": None, "uptime": 0.0, "exit_code": None,
            "label": "未运行", "detail": "尚未启动助手",
            "config_stale": False, "config_changed_at": ""}
        self.store = Mock()
        self.store.state.return_value = {"config_ok": True, "ai_ready": True,
                                         "config_message": "配置格式正确。"}
        self.store.read_masked.return_value = {
            "ok": True, "error": "", "config": {"course_url": "https://studyvideoh5.zhihuishu.com/stuStudy",
                                                "browser_channel": "auto", "poll_seconds": 2,
                                                "ai": {"base_url": "https://api.deepseek.com",
                                                       "model": "deepseek-flash", "api_key": "",
                                                       "min_confidence": 0.85, "timeout_seconds": 45},
                                                "selectors": {"next": "#nextBtn"}},
            "api_key_set": True, "api_key_mask": "sk-****1234"}
        self.diag_dir = Path(self.temp.name) / "diagnostics"
        self.diag_dir.mkdir(parents=True, exist_ok=True)
        self.server = create_server("127.0.0.1", 0, self.supervisor, self.store, self.buffer, ASSETS_DIR,
                                    diagnostics_dir=self.diag_dir)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.page = self.browser.new_page(viewport={"width": 1180, "height": 900})
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on("pageerror", lambda exc: self.errors.append(str(exc)))
        self.page.on("console", lambda msg: self.errors.append(f"console.{msg.type}: {msg.text}")
                     if msg.type == "error" else None)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.run_dir = Path(self.temp.name) / "run"
        run_dir_patch = patch.object(run_control, "RUN_DIR", self.run_dir)
        run_dir_patch.start()
        self.addCleanup(run_dir_patch.stop)

    def log_height(self):
        return self.page.eval_on_selector("#log", "el => el.getBoundingClientRect().height")

    def test_log_lines_render_one_row_each(self):
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_selector("#log .line", timeout=15000)
        self.page.wait_for_timeout(300)
        heights = self.page.eval_on_selector_all(
            "#log .line", "els => els.map(el => el.getBoundingClientRect().height)")
        self.assertGreater(len(heights), 5, "日志行没有渲染出来")
        self.assertLess(max(heights), 60, f"日志行被撑成了整框高：{heights[:5]}")
        self.assertEqual(self.page.locator("#log .empty").count(), 0, "有日志时不该还显示“暂无日志”")
        self.assertEqual(self.page.inner_text("#status-label").strip(), "未运行")
        self.assertEqual(self.page.inner_text("#status-detail").strip(), "",
                         "“未运行”已说清楚状态，detail 不该再复述“尚未启动助手”")
        self.assertAlmostEqual(self.log_height(), 340, delta=1, msg="有日志时日志框保持原高度")
        self.assertEqual(self.errors, [])

    def test_empty_log_box_collapses_but_not_when_filtered(self):
        """日志为空时收高，别让空框把「配置」卡片挤出首屏；筛选到 0 条不算空。"""
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_selector("#log .line", timeout=15000)
        self.page.click("#clear-view")
        self.page.wait_for_function(
            "() => document.getElementById('log').classList.contains('is-empty')", timeout=10000)
        self.assertAlmostEqual(self.log_height(), 120, delta=1, msg="空态日志框应当收高")

        # 新日志到达必须还原高度：appendLog 不经过 refreshLogView，漏了这句就会一直卡在 120px。
        self.buffer.append("2026-10-03 07:00:00,000 INFO 清空之后新到的一行", "log")
        self.page.wait_for_selector("#log .line", timeout=10000)
        self.page.wait_for_function(
            "() => !document.getElementById('log').classList.contains('is-empty')", timeout=10000)
        self.assertAlmostEqual(self.log_height(), 340, delta=1, msg="有日志时应当还原高度")

        # 筛选到 0 条时保持高度，避免用户边打字边看框高跳动。
        self.page.fill("#filter", "绝对不会匹配的字符串")
        self.page.wait_for_selector("#log .empty", timeout=10000)
        self.assertAlmostEqual(self.log_height(), 340, delta=1, msg="筛选无结果时不该收高")
        self.assertEqual(self.errors, [])

    def test_restart_button_reports_result_without_false_config_warning(self):
        self.supervisor.status.return_value = {
            "running": True, "pid": 12128, "uptime": 400.0, "exit_code": None,
            "label": "运行中", "detail": "助手正在运行",
            "config_stale": True, "config_changed_at": "06:52"}
        self.supervisor.stop.return_value = (True, "助手已停止，登录状态已保存")
        self.supervisor.start.return_value = (True, "助手已启动（PID 999）")
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_selector("#status-warning:not([hidden])", timeout=15000)
        self.assertIn("重启助手", self.page.inner_text("#status-warning"))
        self.assertFalse(self.page.is_disabled("#restart"))
        self.page.click("#restart")
        self.page.wait_for_function(
            "() => document.getElementById('action-message').textContent.includes('PID 999')",
            timeout=10000)
        self.assertNotIn("未通过校验", self.page.inner_text("#action-message"))
        self.assertEqual(self.supervisor.stop.call_count, 1)
        self.assertEqual(self.supervisor.start.call_count, 1)
        self.assertEqual(self.errors, [])

    def test_start_button_is_disabled_when_config_is_invalid(self):
        self.store.state.return_value = {
            "config_ok": False, "ai_ready": False,
            "config_message": "配置或运行错误：course_url 必须是 HTTPS 的 studyvideoh5.zhihuishu.com 课程地址"}
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function("() => document.getElementById('start').disabled", timeout=15000)
        self.assertTrue(self.page.is_disabled("#start"), "配置无效时不该还能点启动")
        self.assertIn("未通过校验", self.page.inner_text("#action-message"))
        self.assertIn("配置无效", self.page.inner_text("#status-meta"))
        self.assertNotIn("AI 未填齐", self.page.inner_text("#status-meta"),
                         "配置本身不合法时不该对 AI 段下结论")

    def test_diagnostics_cleanup_requires_confirmation(self):
        make_diag_folder(self.diag_dir, "20260101-000000-000001", age_days=30)
        make_diag_folder(self.diag_dir, "20261003-000000-000002")
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.getElementById('diag-summary').textContent.includes('2 个文件夹')",
            timeout=15000)
        self.assertFalse(self.page.is_visible("#diag-confirm"))

        self.page.click("#diag-check")
        self.page.wait_for_selector("#diag-confirm:not([hidden])", timeout=10000)
        self.assertIn("将删除 1 个", self.page.inner_text("#diag-confirm-text"))
        self.assertEqual(
            self.page.eval_on_selector("#diag-confirm-yes",
                                       "el => getComputedStyle(el).backgroundColor"),
            "rgb(197, 48, 48)",
            "确认清理是破坏性操作，应当是实心红而不是和白底描边的「取消」同权重")

        self.page.click("#diag-confirm-no")
        self.page.wait_for_timeout(200)
        self.assertEqual(len(list(self.diag_dir.iterdir())), 2, "取消后不能删任何东西")

        self.page.click("#diag-check")
        self.page.wait_for_selector("#diag-confirm:not([hidden])", timeout=10000)
        self.page.click("#diag-confirm-yes")
        self.page.wait_for_function(
            "() => document.getElementById('diag-message').textContent.includes('已删除 1 个')",
            timeout=10000)
        self.assertEqual([p.name for p in self.diag_dir.iterdir()], ["20261003-000000-000002"])
        self.assertEqual(self.errors, [])

    def test_config_panel_hides_secret_and_shows_values(self):
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.click("#config-toggle")
        self.page.wait_for_function(
            "() => document.getElementById('course_url').value.length > 0", timeout=10000)
        self.assertEqual(self.page.input_value("#ai_api_key"), "")
        self.assertIn("不修改", self.page.get_attribute("#ai_api_key", "placeholder"))
        self.assertEqual(self.page.input_value("#sel_next"), "#nextBtn")
        self.assertEqual(self.page.input_value("#browser_channel"), "auto")
        send_disabled = self.page.is_disabled("#send-enter") and self.page.is_disabled("#send-quit")
        self.assertTrue(send_disabled, "助手未运行时发送按钮应当禁用")

    def test_video_row_shows_rate_and_toggles_pause(self):
        self.supervisor.status.return_value = {
            "running": True, "pid": 12128, "uptime": 400.0, "exit_code": None,
            "label": "运行中", "detail": "助手正在运行",
            "config_stale": False, "config_changed_at": ""}
        run_control.write_state({"applied_seq": 0, "rate": 1.5, "requested_rate": None,
                                 "paused": False, "manual_paused": False, "time": 12.0,
                                 "duration": 60.0, "lesson": "1.3.1 函数极限", "waiting": False})
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.getElementById('video-info').textContent.includes('1.5x')",
            timeout=15000)
        info = self.page.inner_text("#video-info")
        self.assertIn("播放中", info)
        self.assertIn("0:12 / 1:00", info)
        self.assertIn("1.3.1 函数极限", info)
        self.assertEqual(self.page.inner_text("#video-pause").strip(), "暂停视频")

        self.page.click("#video-pause")
        self.page.wait_for_function(
            "() => !document.getElementById('video-pending').hidden", timeout=10000)
        written = json.loads((self.run_dir / "run_control.json").read_text(encoding="utf-8"))
        self.assertEqual(written["action"], "pause")

        run_control.write_state({"applied_seq": written["seq"], "rate": 1.5,
                                 "requested_rate": None, "paused": True, "manual_paused": True,
                                 "time": 12.0, "duration": 60.0,
                                 "lesson": "1.3.1 函数极限", "waiting": False})
        self.page.wait_for_function(
            "() => document.getElementById('video-pause').textContent.includes('开始')",
            timeout=10000)
        self.assertIn("已暂停（手动）", self.page.inner_text("#video-info"))
        self.assertEqual(self.errors, [])

    def test_video_rate_menu_omits_unsupported_2x(self):
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.assertFalse(self.page.locator("#video-rate").count())
        self.assertIn("1x / 1.25x / 1.5x", self.page.locator("main").inner_text())

    def test_video_controls_follow_running_state(self):
        """控件必须随助手运行状态变化：在跑时可点（不能靠静态 HTML 的 disabled 混过去），停了就禁用。"""
        self.supervisor.status.return_value = {
            "running": True, "pid": 12128, "uptime": 400.0, "exit_code": None,
            "label": "运行中", "detail": "助手正在运行",
            "config_stale": False, "config_changed_at": ""}
        run_control.write_state({"applied_seq": 0, "rate": 1.5, "requested_rate": None,
                                 "paused": False, "manual_paused": False, "time": 1.0,
                                 "duration": 60.0, "lesson": "1.3.1 函数极限", "waiting": False})
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.getElementById('video-info').textContent.includes('1.5x')",
            timeout=15000)
        self.assertFalse(self.page.is_disabled("#video-pause"), "助手在跑时不该禁用暂停")
        self.assertFalse(self.page.locator("#video-rate").count())

        self.supervisor.status.return_value = {
            "running": False, "pid": None, "uptime": 0.0, "exit_code": None,
            "label": "未运行", "detail": "尚未启动助手",
            "config_stale": False, "config_changed_at": ""}
        self.page.wait_for_function(
            "() => document.getElementById('video-pause').disabled", timeout=15000)
        self.assertTrue(self.page.is_disabled("#video-pause"))
        self.assertFalse(self.page.locator("#video-rate").count())
        # 助手没在跑时，留下的状态文件不算"现状"：行内必须显示无数据，不能显示过期倍速
        self.page.wait_for_function(
            "() => document.getElementById('video-info').textContent === '无数据'",
            timeout=15000)

        # 助手没在跑时，即使命令还没被应用，也不能一直挂着"等待助手响应"
        run_control.write_command("pause")
        self.page.wait_for_function(
            "() => document.getElementById('video-pending').hidden", timeout=15000)
        self.assertEqual(self.errors, [])

    def test_video_controls_keep_resume_when_state_goes_stale(self):
        """手动暂停后状态超龄（例如卡在一次长 AI 调用里）时，按钮必须还能恢复播放。"""
        self.supervisor.status.return_value = {
            "running": True, "pid": 12128, "uptime": 400.0, "exit_code": None,
            "label": "运行中", "detail": "助手正在运行",
            "config_stale": False, "config_changed_at": ""}
        state = {"applied_seq": 1, "rate": 1.0, "requested_rate": None,
                 "paused": True, "manual_paused": True, "time": 12.0,
                 "duration": 60.0, "lesson": "1.3.1 函数极限", "waiting": False}
        run_control.write_state(dict(state))
        self.page.goto(self.url, wait_until="domcontentloaded")
        self.page.wait_for_function(
            "() => document.getElementById('video-pause').textContent.includes('开始')",
            timeout=15000)

        # 改成 60 秒前写的状态：控制台判它超龄（max_age=10），行内文字变成"无数据"，
        # 但按钮文案与动作必须仍按"已手动暂停"来，否则用户既恢复不了也停不下来。
        run_control.write_state(dict(state), now=time.time() - 60)
        self.page.wait_for_function(
            "() => document.getElementById('video-info').textContent.includes('无数据')",
            timeout=15000)
        self.assertEqual(self.page.inner_text("#video-pause").strip(), "开始视频")
        self.page.click("#video-pause")
        self.page.wait_for_timeout(400)
        written = json.loads((self.run_dir / "run_control.json").read_text(encoding="utf-8"))
        self.assertEqual(written["action"], "resume")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
