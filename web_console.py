"""本机网页控制台：启动/停止课程助手、实时日志、读写 config.json。

运行：python -X utf8 web_console.py [--port 8765] [--no-browser]
只监听 127.0.0.1，只用 Python 标准库，不导入助手模块（助手由子进程运行）。
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import webbrowser

import cleanup
import run_control

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
LOG_PATH = ROOT / "logs" / "study.log"
ASSETS_DIR = ROOT / "web"
CONSOLE = logging.getLogger("web_console")

HISTORY = 200
BUFFER_LIMIT = 500
MAX_BODY = 1024 * 1024
CHECK_TIMEOUT = 15
DEFAULT_PORT = 8765
# 停止时先给助手一个 q（它停在 input() 时这是真正的优雅退出），等不到就强杀进程树。
GRACEFUL_STOP_SECONDS = 4

# 助手日志行的格式（logging basicConfig），这些行已经写进 logs/study.log，
# 子进程的同一份输出不再重复进日志视图。
LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \w+ ")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def is_duplicate_log_line(text: str) -> bool:
    """判断子进程输出是否与 logs/study.log 中已有的一行重复。"""
    return bool(LOG_LINE_RE.match(text))


class LogBuffer:
    """线程安全的日志环形缓冲，供页面拉取和 SSE 阻塞等待。"""

    def __init__(self, limit: int = BUFFER_LIMIT):
        self._items: collections.deque = collections.deque(maxlen=limit)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._seq = 0

    def append(self, text: str, source: str) -> dict:
        text = str(text).rstrip("\r\n")
        with self._changed:
            self._seq += 1
            item = {
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "source": source,
                "text": text,
            }
            self._items.append(item)
            self._changed.notify_all()
            return item

    def snapshot(self, count: int = HISTORY) -> list[dict]:
        with self._lock:
            items = list(self._items)
        if count <= 0:
            return []
        return items[-count:]

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [item for item in self._items if item["seq"] > seq]

    def wait_for(self, since_seq: int, timeout: float) -> list[dict]:
        with self._changed:
            if not any(item["seq"] > since_seq for item in self._items):
                self._changed.wait(timeout)
            return [item for item in self._items if item["seq"] > since_seq]

    def last(self) -> dict | None:
        with self._lock:
            return self._items[-1] if self._items else None

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq


class LogTailer(threading.Thread):
    """按字节偏移增量读取日志文件；文件被清空或轮转时从头重读。"""

    def __init__(self, path: Path, buffer: LogBuffer, interval: float = 0.5):
        super().__init__(daemon=True, name="log-tailer")
        self.path = Path(path)
        self.buffer = buffer
        self.interval = interval
        self._stop = threading.Event()
        self._offset = 0
        self._pending = b""

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_new()
            except OSError as exc:
                CONSOLE.debug("读取日志失败：%s", exc)
            self._stop.wait(self.interval)

    def _reset(self) -> None:
        self._offset = 0
        self._pending = b""

    def _read_new(self) -> None:
        if not self.path.exists():
            if self._offset:
                self._reset()
            return
        size = self.path.stat().st_size
        if size < self._offset:
            self._reset()
        if size == self._offset:
            return
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        lines = (self._pending + chunk).split(b"\n")
        self._pending = lines.pop()
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line.strip():
                self.buffer.append(line, "log")


def python_executable(root: Path) -> Path:
    """助手和配置校验都用项目自带虚拟环境的解释器。"""
    candidate = Path(root) / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def assistant_command(python: Path, config_path: Path) -> list[str]:
    # -u 很重要：子进程的 stdout 是管道，默认块缓冲，助手 print 的提示会迟迟到不了控制台。
    return [str(python), "-u", "-X", "utf8", "study_assistant.py", "--config", str(config_path)]


def _creation_flags() -> int:
    """Windows 上建独立进程组，才能向子进程发 CTRL_BREAK 优雅停止。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class Supervisor:
    """管理助手子进程：启动、停止、发送输入、汇报状态。"""

    def __init__(self, root: Path, config_path: Path, python: Path, buffer: LogBuffer,
                 popen=None, run=None):
        self.root = Path(root)
        self.config_path = Path(config_path)
        self.python = Path(python)
        self.buffer = buffer
        self._popen = popen or subprocess.Popen
        self._run = run or subprocess.run
        self._process = None
        self._started_at = None
        self._started_wall = None
        self._start_seq = 0
        self._exit_code = None
        self._logged_exit = False
        self._lock = threading.RLock()

    # ---- 状态 ----

    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _close_pipes(self, process) -> None:
        """子进程结束后关掉自己这端的管道，避免解释器退出时报未关闭文件。"""
        for stream in (getattr(process, "stdout", None), getattr(process, "stdin", None)):
            if stream is None or getattr(stream, "closed", True):
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _note_exit(self, process) -> None:
        if process is None:
            return
        code = process.poll()
        if code is None:
            return
        self._close_pipes(process)
        self._exit_code = code
        if not self._logged_exit:
            self._logged_exit = True
            self.buffer.append(f"[控制台] 助手已退出，退出码 {code}", "console")

    def status(self) -> dict:
        with self._lock:
            process = self._process
            self._note_exit(process)
            running = process is not None and process.poll() is None
            pid = None
            if running:
                try:
                    pid = int(process.pid)
                except (TypeError, ValueError):
                    pid = None
            uptime = time.monotonic() - self._started_at if running and self._started_at else 0.0
            label, detail = self._describe(running, uptime)
            stale, changed_at = self._config_stale(running)
            return {
                "running": running,
                "pid": pid,
                "uptime": round(uptime, 1),
                "exit_code": process.poll() if process is not None else None,
                "label": label,
                "detail": detail,
                "config_stale": stale,
                "config_changed_at": changed_at,
            }

    def _config_stale(self, running: bool) -> tuple[bool, str]:
        """助手只在启动时读配置：进程启动后配置文件又被改过就得重启才生效。"""
        if not running or self._started_wall is None:
            return False, ""
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return False, ""
        if mtime <= self._started_wall + 0.5:
            return False, ""
        return True, time.strftime("%H:%M", time.localtime(mtime))

    def _describe(self, running: bool, uptime: float) -> tuple[str, str]:
        if not running:
            if self._exit_code is None:
                return "未运行", "尚未启动助手"
            if self._exit_code == 0:
                return "已停止", "助手已正常退出"
            return "异常退出", f"助手异常退出，退出码 {self._exit_code}"
        # 只看本次启动之后产生的日志，避免上一轮的结论残留。
        recent = [item for item in self.buffer.since(self._start_seq) if item["source"] != "console"]
        if recent:
            text = recent[-1]["text"]
            if "请在浏览器处理当前题目" in text:
                return "等待你在浏览器处理", "助手停在浏览器里等你处理：发送回车继续，或发送 q 退出"
            if "已提交选项" in text:
                return "正在答题", text
            if "已识别课程页面" in text:
                return "监控中", text
        if uptime < 5:
            return "启动中", "正在打开浏览器并加载课程"
        return "运行中", "助手正在运行"

    # ---- 控制 ----

    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return False, f"助手已在运行（PID {self._process.pid}），请先停止"
            command = assistant_command(self.python, self.config_path)
            try:
                process = self._popen(
                    command,
                    cwd=str(self.root),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                return False, f"无法启动助手：{exc}"
            self._process = process
            self._started_at = time.monotonic()
            self._started_wall = time.time()
            self._start_seq = self.buffer.latest_seq()
            self._exit_code = None
            self._logged_exit = False
            self.buffer.append(f"[控制台] 已启动助手，PID {process.pid}", "console")
            threading.Thread(target=self._pump, args=(process,), daemon=True,
                             name="assistant-output").start()
            return True, f"助手已启动（PID {process.pid}）"

    def _pump(self, process) -> None:
        """把子进程的 print 提示和崩溃 traceback 收进日志视图。"""
        stream = getattr(process, "stdout", None)
        if stream is None:
            return
        try:
            for line in stream:
                if line.strip() and not is_duplicate_log_line(line):
                    self.buffer.append(line, "app")
        except (ValueError, OSError) as exc:
            CONSOLE.debug("读取助手输出结束：%s", exc)

    def _send_quit(self, process) -> None:
        """助手停在 input() 时，一个 q 才是真正的优雅退出：会保存会话并关掉浏览器。

        实测 CTRL_BREAK 只会让它被控制信号直接杀掉（退出码 3221225786），
        except KeyboardInterrupt 和 finally 里的会话保存、浏览器关闭都不会执行，
        留下的 Chrome 还会变成孤儿进程。
        """
        try:
            process.stdin.write("q\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError, AttributeError):
            pass

    def _kill_tree(self, pid: int) -> bool:
        if os.name == "nt":
            try:
                self._run(["taskkill", "/PID", str(pid), "/T", "/F"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
            except OSError as exc:
                CONSOLE.debug("taskkill 失败：%s", exc)
                return False
            return True
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False

    def stop(self) -> tuple[bool, str]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._note_exit(process)
                return True, "助手未在运行"
            pid = int(process.pid)
            self._send_quit(process)
            try:
                process.wait(timeout=GRACEFUL_STOP_SECONDS)
                code = process.poll()
                self._note_exit(process)
                if code == 0:
                    self.buffer.append(f"[控制台] 助手已优雅退出（PID {pid}）", "console")
                    return True, "助手已优雅停止，登录状态已保存"
                self.buffer.append(
                    f"[控制台] 助手已退出，退出码 {code}（未执行退出前的会话保存）", "console")
                return True, f"助手已结束（退出码 {code}）；它每 30 秒会自动保存登录状态"
            except subprocess.TimeoutExpired:
                pass
            self._kill_tree(pid)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False, f"无法结束助手进程（PID {pid}），请在任务管理器里结束它"
            self._note_exit(process)
            self.buffer.append(f"[控制台] 已强制结束助手进程树（PID {pid}）", "console")
            return True, "助手已强制停止"

    def send(self, text: str) -> tuple[bool, str]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False, "助手未在运行，无法发送输入"
            try:
                process.stdin.write((text or "") + "\n")
                process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError) as exc:
                return False, f"发送失败：{exc}"
            if text:
                self.buffer.append(f"[控制台] 已发送输入：{text}", "console")
                return True, f"已发送：{text}"
            self.buffer.append("[控制台] 已发送回车（继续）", "console")
            return True, "已发送回车"

    def shutdown(self) -> None:
        if self.running():
            self.stop()


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    return "sk-****" + (key[-4:] if len(key) > 4 else "")


class ConfigStore:
    """读写 config.json：密钥不外泄，保存前用助手自己的 --check-config 校验。"""

    KNOWN_FIELDS = ("course_url", "browser_channel", "poll_seconds", "ai", "selectors", "diagnostics")

    def __init__(self, root: Path, config_path: Path, python: Path, run=None):
        self.root = Path(root)
        self.config_path = Path(config_path)
        self.python = Path(python)
        self._run = run or subprocess.run
        self._check_cache: tuple[float, tuple[bool, str]] | None = None

    def read(self) -> tuple[bool, dict | str]:
        try:
            raw = self.config_path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return False, f"找不到配置文件：{self.config_path}"
        except OSError as exc:
            return False, f"读取配置失败：{exc}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            return False, f"配置不是合法 JSON：{exc}"
        if not isinstance(data, dict):
            return False, "配置必须是 JSON 对象"
        return True, data

    def read_masked(self) -> dict:
        ok, data = self.read()
        if not ok:
            return {"ok": False, "error": data, "config": None,
                    "api_key_set": False, "api_key_mask": ""}
        key = ""
        ai = data.get("ai")
        if isinstance(ai, dict) and isinstance(ai.get("api_key"), str):
            key = ai["api_key"]
        safe = json.loads(json.dumps(data))
        if isinstance(safe.get("ai"), dict):
            safe["ai"]["api_key"] = ""
        return {"ok": True, "error": "", "config": safe,
                "api_key_set": bool(key), "api_key_mask": mask_api_key(key)}

    def _validate(self, path: Path) -> tuple[bool, str]:
        command = [str(self.python), "-X", "utf8", "study_assistant.py",
                   "--check-config", "--config", str(path)]
        try:
            result = self._run(command, cwd=str(self.root), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "配置校验超时，请重试。"
        except OSError as exc:
            return False, f"无法运行配置校验：{exc}"
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output

    def check(self, cache_seconds: float = 10.0) -> tuple[bool, str]:
        now = time.monotonic()
        if self._check_cache and now - self._check_cache[0] < cache_seconds:
            return self._check_cache[1]
        result = self._validate(self.config_path)
        self._check_cache = (now, result)
        return result

    def state(self) -> dict:
        valid, message = self.check()
        return {
            "config_ok": "配置格式正确" in message,
            "ai_ready": valid,
            "config_message": message or "配置校验没有输出",
        }

    def save(self, payload: dict) -> tuple[bool, str]:
        if not isinstance(payload, dict):
            return False, "提交内容必须是 JSON 对象"
        ok, current = self.read()
        if not ok:
            return False, str(current)
        candidate = json.loads(json.dumps(current))
        for field in self.KNOWN_FIELDS:
            if field not in payload:
                continue
            value = payload[field]
            if field in ("ai", "selectors", "diagnostics"):
                if not isinstance(value, dict):
                    return False, f"{field} 必须是 JSON 对象"
                base = candidate.get(field)
                if not isinstance(base, dict):
                    base = {}
                merged = dict(base)
                merged.update(value)
                candidate[field] = merged
            else:
                candidate[field] = value
        ai = candidate.get("ai")
        if isinstance(ai, dict):
            current_ai = current.get("ai") if isinstance(current.get("ai"), dict) else {}
            existing = current_ai.get("api_key", "")
            submitted = ai.get("api_key")
            if submitted in (None, "") or submitted == mask_api_key(existing):
                ai["api_key"] = existing
        text = json.dumps(candidate, ensure_ascii=False, indent=2) + "\n"
        try:
            handle, temp_name = tempfile.mkstemp(dir=str(self.root), prefix="config-candidate-",
                                                 suffix=".json")
        except OSError as exc:
            return False, f"无法创建临时文件：{exc}"
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(text, encoding="utf-8")
            valid, message = self._validate(temp_path)
            if not valid:
                return False, message or "配置校验未通过"
            if self.config_path.exists():
                shutil.copyfile(self.config_path,
                                self.config_path.with_name(self.config_path.name + ".bak"))
            staging = self.config_path.with_name(self.config_path.name + ".tmp")
            staging.write_text(text, encoding="utf-8")
            os.replace(staging, self.config_path)
        finally:
            temp_path.unlink(missing_ok=True)
        self._check_cache = None
        return True, message or "配置已保存"


def is_local_host(value: str) -> bool:
    """只接受本机回环地址，防止别的站点驱动这个控制台。"""
    if not value:
        return False
    candidate = value.strip().lower()
    if candidate in ("127.0.0.1", "localhost", "::1", "[::1]"):
        return True
    if candidate.startswith("["):
        return candidate.split("]")[0].lstrip("[") == "::1"
    host, _, port = candidate.rpartition(":")
    if not port.isdigit():
        return False
    return host in ("127.0.0.1", "localhost")


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "StudyConsole/1.0"
    buffer: LogBuffer
    supervisor: Supervisor
    store: ConfigStore
    assets: dict
    diagnostics_dir: Path
    _json_body: dict = {}

    def log_message(self, fmt, *args):
        CONSOLE.debug("%s %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        CONSOLE.debug("请求异常：%s", fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        # 先把请求体读完再分发：否则早退的 403/404 会在这边还在发数据时关连接。
        body, error, status = self._read_body()
        if body is None:
            return self._json(status, {"ok": False, "error": error})
        self._json_body = body
        self._dispatch("POST")

    # ---- 分发 ----

    def _dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        path = path.rstrip("/") or "/"
        try:
            if not self._origin_allowed():
                return self._json(403, {"ok": False, "error": "只允许本机页面访问控制台"})
            if method == "GET" and path in self.assets:
                return self._asset(path)
            if method == "GET" and path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if path == "/api/logs/stream":
                if method != "GET":
                    return self._json(405, {"ok": False, "error": "该路径只支持 GET"})
                return self._stream()
            handler = ROUTES.get((method, path))
            if handler is None:
                return self._json(404, {"ok": False, "error": f"未知路径：{path}"})
            return handler(self)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as exc:  # 控制台自身出错不能让线程静默崩掉
            CONSOLE.warning("处理 %s %s 失败：%s", method, path, exc)
            try:
                self._json(500, {"ok": False, "error": f"控制台内部错误：{exc}"})
            except OSError:
                self.close_connection = True

    def _origin_allowed(self) -> bool:
        if not is_local_host(self.headers.get("Host", "")):
            return False
        origin = self.headers.get("Origin")
        if origin in (None, "", "null"):
            return True
        parsed = urlsplit(origin)
        return parsed.scheme == "http" and is_local_host(parsed.netloc)

    # ---- 响应 ----

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _asset(self, path: str) -> None:
        target = Path(self.assets[path])
        try:
            body = target.read_bytes()
        except OSError:
            return self._json(404, {"ok": False, "error": f"缺少静态文件：{target.name}"})
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> tuple[dict | None, str, int]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None, "缺少 Content-Length", 411
        try:
            size = int(length)
        except ValueError:
            return None, "Content-Length 无效", 400
        if size < 0 or size > MAX_BODY:
            return None, "请求体过大", 413
        raw = self.rfile.read(size)
        if not raw.strip():
            return {}, "", 200
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"请求体不是合法 JSON：{exc}", 400
        if not isinstance(data, dict):
            return None, "请求体必须是 JSON 对象", 400
        return data, "", 200

    # ---- 接口 ----

    def _status_payload(self) -> dict:
        payload = {"ok": True}
        payload.update(self.supervisor.status())
        payload.update(self.store.state())
        payload["log_path"] = str(LOG_PATH)
        video = run_control.read_state()
        payload["video"] = video
        payload["video_pending"] = run_control.pending(video)
        return payload

    def _api_status(self) -> None:
        self._json(200, self._status_payload())

    def _api_config_get(self) -> None:
        data = self.store.read_masked()
        self._json(200 if data.get("ok") else 500, data)

    # ---- 诊断文件 ----

    def _retention(self):
        """保留策略取 config.json 里的 diagnostics 段，缺失或非法就用默认值。"""
        try:
            ok, data = self.store.read()
        except Exception:  # noqa: BLE001 - 读配置失败不该让页面挂掉
            return cleanup.Retention()
        return cleanup.Retention.from_config(data if ok and isinstance(data, dict) else {})

    def _diagnostics_report(self, dry_run: bool) -> dict:
        return cleanup.clean(self.diagnostics_dir, self._retention(), dry_run=dry_run)

    def _api_diagnostics(self) -> None:
        report = self._diagnostics_report(dry_run=True)
        report["ok"] = True   # 预览本身不算失败
        self._json(200, report)

    def _api_diagnostics_clean(self) -> None:
        report = self._diagnostics_report(dry_run=False)
        self._json(200 if report["ok"] else 500, report)

    def _api_config_post(self) -> None:
        ok, message = self.store.save(self._json_body)
        self._json(200 if ok else 400, {"ok": ok, "message": message})

    def _api_start(self) -> None:
        ok, message = self.supervisor.start()
        self._json(200 if ok else 409, {"ok": ok, "message": message,
                                        "status": self._status_payload()})

    def _api_stop(self) -> None:
        ok, message = self.supervisor.stop()
        self._json(200 if ok else 500, {"ok": ok, "message": message,
                                        "status": self._status_payload()})

    def _api_restart(self) -> None:
        stopped, stop_message = self.supervisor.stop()
        if not stopped:
            return self._json(500, {"ok": False, "message": stop_message,
                                    "status": self._status_payload()})
        ok, message = self.supervisor.start()
        self._json(200 if ok else 409, {"ok": ok, "message": message,
                                        "status": self._status_payload()})

    def _api_stdin(self) -> None:
        text = self._json_body.get("text", "")
        if not isinstance(text, str):
            return self._json(400, {"ok": False, "error": "text 必须是字符串"})
        ok, message = self.supervisor.send(text)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _api_video(self) -> None:
        action = self._json_body.get("action")
        value = self._json_body.get("value")
        if action not in run_control.ACTIONS:
            return self._json(400, {"ok": False,
                                    "error": "action 必须是 " + " / ".join(run_control.ACTIONS)})
        if action == "rate" and value is not None and run_control.validate_rate(value) is None:
            return self._json(400, {"ok": False, "error": "倍速必须是 0.25–4 之间、最多两位小数的数值"})
        status = self.supervisor.status()
        if not status.get("running"):
            return self._json(409, {"ok": False, "error": "助手没在运行，先点「启动助手」"})
        # 进程起来了 ≠ 助手的监控循环跑起来了：循环要等浏览器启动 + 进入课程页之后才开始，而循环
        # 开头会把"启动那一刻的命令序号"当基线，只执行之后写下的命令。所以在这段窗口里写下的
        # 命令会被基线吃掉——必须拒绝并说明，不能写下去然后让页面显示"已发送"。
        state = run_control.read_state(max_age=None)
        updated = state.get("updated_at") if state else None
        started_at = time.time() - float(status.get("uptime") or 0)
        if isinstance(updated, bool) or not isinstance(updated, (int, float)) or updated < started_at - 1:
            return self._json(409, {"ok": False,
                                    "error": "助手还在启动浏览器或进入课程页，等它开始监控后再操作"})
        command = run_control.write_command(action, value)
        if command is None:
            return self._json(500, {"ok": False, "error": "写入控制命令失败，请检查运行目录是否可写"})
        if action == "rate":
            # 保留旧命令格式以兼容旧控制台，但当前助手会忽略它，不写入播放器倍速。
            message = "倍速控制已停用，请在课程原生播放器菜单手动选择 1x / 1.25x / 1.5x"
        else:
            message = {"pause": "已发送：暂停视频", "resume": "已发送：恢复播放"}[action]
        video = run_control.read_state()
        self._json(200, {"ok": True, "message": message,
                         "video": video, "video_pending": run_control.pending(video)})

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last = 0
        for item in self.buffer.snapshot(HISTORY):
            self._write_event(item)
            last = item["seq"]
        while True:
            items = self.buffer.wait_for(last, 15)
            if not items:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                continue
            for item in items:
                self._write_event(item)
                last = item["seq"]

    def _write_event(self, item: dict) -> None:
        data = json.dumps(item, ensure_ascii=False)
        self.wfile.write(f"id: {item['seq']}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


ROUTES = {
    ("GET", "/api/status"): ConsoleHandler._api_status,
    ("GET", "/api/config"): ConsoleHandler._api_config_get,
    ("GET", "/api/diagnostics"): ConsoleHandler._api_diagnostics,
    ("POST", "/api/diagnostics/cleanup"): ConsoleHandler._api_diagnostics_clean,
    ("POST", "/api/config"): ConsoleHandler._api_config_post,
    ("POST", "/api/start"): ConsoleHandler._api_start,
    ("POST", "/api/stop"): ConsoleHandler._api_stop,
    ("POST", "/api/restart"): ConsoleHandler._api_restart,
    ("POST", "/api/stdin"): ConsoleHandler._api_stdin,
    ("POST", "/api/video"): ConsoleHandler._api_video,
}


def create_server(host: str, port: int, supervisor, store, buffer: LogBuffer,
                  assets_dir: Path, handler=ConsoleHandler,
                  diagnostics_dir: Path | None = None) -> ThreadingHTTPServer:
    assets_dir = Path(assets_dir)
    assets = {
        "/": assets_dir / "index.html",
        "/app.js": assets_dir / "app.js",
        "/style.css": assets_dir / "style.css",
    }
    bound = type("BoundConsoleHandler", (handler,), {
        "supervisor": supervisor,
        "store": store,
        "buffer": buffer,
        "assets": assets,
        "diagnostics_dir": Path(diagnostics_dir) if diagnostics_dir else ROOT / cleanup.DIAGNOSTICS_DIRNAME,
    })
    # Windows 下 SO_REUSEADDR 允许第二个进程绑同一端口，冲突会静默发生；
    # 关掉它才能像 Linux 一样直接报“端口被占用”。重启不受影响，已实测。
    server_class = type("BoundConsoleServer", (ThreadingHTTPServer,),
                        {"allow_reuse_address": os.name != "nt"})
    server = server_class((host, port), bound)
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="智慧树课程助手网页控制台（只监听本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="助手使用的配置文件")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    buffer = LogBuffer()
    tailer = LogTailer(LOG_PATH, buffer)
    tailer.start()
    python = python_executable(ROOT)
    supervisor = Supervisor(ROOT, args.config, python, buffer)
    store = ConfigStore(ROOT, args.config, python)
    try:
        server = create_server("127.0.0.1", args.port, supervisor, store, buffer, ASSETS_DIR)
    except OSError as exc:
        print(f"无法监听 127.0.0.1:{args.port}：{exc}\n端口可能被占用，请用 --port 指定其他端口。",
              file=sys.stderr)
        tailer.stop()
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"控制台已启动：{url}\n助手解释器：{python}\n关闭这个窗口或按 Ctrl+C 退出控制台。")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出控制台…")
    finally:
        server.server_close()
        tailer.stop()
        supervisor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
