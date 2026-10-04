"""控制台与助手之间的运行时状态／命令文件。

两个进程谁都不 import 谁，只通过 logs/ 下的两个 JSON 文件握手：

- `run_state.json`：助手写、控制台读（当前视频倍速、是否暂停、进度）。
- `run_control.json`：控制台写、助手读（暂停／开始命令；`rate` 仅为旧版本命令兼容保留，当前助手不会执行）。

命令用单调递增的 `seq` 握手：助手只执行比自己见过的更大的序号，所以同一份文件
被反复读到也只生效一次；序号取自文件本身，控制台重启不会让序号倒退。

`RUN_DIR` 是模块级常量，测试把它 patch 成临时目录，绝不去碰真实的 logs/。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

LOG = logging.getLogger("run_control")

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "logs"
STATE_NAME = "run_state.json"
CONTROL_NAME = "run_control.json"

ACTIONS = ("pause", "resume", "rate")
MIN_RATE = 0.25
MAX_RATE = 4.0
STATE_MAX_AGE = 10.0
WRITE_ATTEMPTS = 3

# 同一进程里的并发写者（控制台是多线程 HTTP 服务）先在这里排队；跨进程的占用
# 靠唯一临时文件名 + 重试兜住。用 RLock 是因为 write_command 会在锁内再调 _write_json。
_WRITE_LOCK = threading.RLock()

# 同一条坏输入只警告一次：坏文件不能把 2 秒一次的轮询变成刷屏机。
_LAST_WARNING: str | None = None


def state_path() -> Path:
    return RUN_DIR / STATE_NAME


def control_path() -> Path:
    return RUN_DIR / CONTROL_NAME


def validate_rate(value) -> float | None:
    """倍速只接受 0.25–4 之间、最多两位小数的数值；None 表示"不干预"，由调用方单独识别。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rate = float(value)
    if not MIN_RATE <= rate <= MAX_RATE:
        return None
    if round(rate, 2) != rate:
        return None
    return rate


def _write_json(path: Path, payload: dict) -> bool:
    """把 JSON 写进 path，尽量让别人"要么读到旧内容、要么读到新内容"。

    Windows 上 `os.replace` 要求目标文件此刻没有被别人以「不共享删除」的方式打开，
    而另一个进程正在读的那一瞬间就可能撞上；所以用唯一临时文件名 + 重试，并把
    同一进程内的并发写者串行化。失败只回报 False，绝不抛异常。
    """
    with _WRITE_LOCK:
        try:
            # 编码也在 try 里：页面文本里可能出现孤立代理项，write_text 会抛
            # UnicodeEncodeError（ValueError 子类，不是 OSError），那样会带着一个
            # 0 字节 .tmp 逃出去，违背"失败只返回 False"。
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            LOG.warning("序列化 %s 失败，已放弃写入：%s", path.name, exc)
            return False
        for attempt in range(WRITE_ATTEMPTS):
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return True
            except OSError as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    LOG.debug("清理 %s 失败：%s", tmp.name, cleanup_error)
                if attempt == WRITE_ATTEMPTS - 1:
                    LOG.warning("写入 %s 失败：%s", path.name, exc)
                    return False
                time.sleep(0.02 * (attempt + 1))
    return False


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(fields: dict, now: float | None = None) -> bool:
    payload = dict(fields)
    payload["updated_at"] = time.time() if now is None else now
    return _write_json(state_path(), payload)


def read_state(max_age: float | None = STATE_MAX_AGE, now: float | None = None) -> dict | None:
    data = _read_json(state_path())
    if data is None:
        return None
    if data.get("waiting") is True:
        return data  # 助手停在终端等回车，不写状态是正常的，不能当成"没数据"
    updated = data.get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        return None
    if max_age is None:
        return data
    current = time.time() if now is None else now
    return None if current - updated > max_age else data


def _warn_once(message: str, *args) -> None:
    """同一条坏输入只警告一次，避免坏文件把 2 秒一次的轮询变成刷屏机。"""
    global _LAST_WARNING
    text = message % args
    if text != _LAST_WARNING:
        _LAST_WARNING = text
        LOG.warning("%s", text)


def _valid_command() -> dict | None:
    """命令文件里当前那条命令；文件坏了或字段非法一律返回 None。"""
    data = _read_json(control_path())
    if data is None:
        return None
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        _warn_once("命令序号非法，已忽略：%r", seq)
        return None
    action = data.get("action")
    if action not in ACTIONS:
        _warn_once("命令内容非法，已忽略：%r", action)
        return None
    value = data.get("value")
    if action != "rate":
        value = None  # 只有倍速带参数；读侧也归一，免得写入侧和读出侧不一致
    elif value is not None:
        rate = validate_rate(value)
        if rate is None:
            _warn_once("倍速取值非法，已忽略：%r", value)  # 打原值，别打校验后的 None
            return None
        value = rate
    return {"seq": seq, "action": action, "value": value}


def current_seq() -> int:
    """命令文件里当前这条**合法**命令的序号；控制台用它判断助手有没有跟上。

    非法文件（序号坏了、action 不认识、倍速越界）一律算 0：否则页面会一直显示
    "等待助手响应…"，而助手其实早就正确地忽略了那条命令。
    """
    command = _valid_command()
    return command["seq"] if command else 0


def _applied_seq() -> int:
    """助手状态里回报的"已经处理到哪一号"；读不到就当 0。"""
    state = read_state(max_age=None)
    applied = (state or {}).get("applied_seq")
    if isinstance(applied, bool) or not isinstance(applied, int):
        return 0
    return applied if applied > 0 else 0


def write_command(action: str, value=None) -> dict | None:
    if action not in ACTIONS:
        return None
    if action == "rate":
        if value is not None:
            value = validate_rate(value)
            if value is None:
                return None
    else:
        value = None  # 只有倍速带参数，其它命令一律不带
    # 取号和写入必须在同一把锁里，否则两个写者可能取到同一个号。序号还必须高于助手
    # 已经见过的号：命令文件被手删或被改坏时序号会倒退，助手按"只认更大的序号"会把
    # 之后的命令全部静默忽略，而页面看不到任何提示。失败只返回 None。
    with _WRITE_LOCK:
        seq = max(current_seq(), _applied_seq()) + 1
        payload = {"seq": seq, "action": action, "value": value, "at": time.time()}
        return payload if _write_json(control_path(), payload) else None


def _normalize_seq(value) -> int | None:
    """把 `seen_seq` 归一成整数；`None` 当 0，其余不可信的一律返回 `None`。

    返回 `None` 表示"进度不可信"：此时调用方只能当作"没有命令"，绝不能退回 0 去把
    一条可能已经执行过的老命令再执行一遍。`bool` 虽然是 `int` 的子类，但把它当序号
    没有意义，同样按不可信处理。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_command(seen_seq: int) -> dict | None:
    """返回比 seen_seq 新的命令；参数或文件有问题一律当"没有命令"。"""
    command = _valid_command()
    seen = _normalize_seq(seen_seq)
    if command is None or seen is None or command["seq"] <= seen:
        return None
    return command


def pending(state: dict | None) -> bool:
    """控制台已经写了命令、助手还没确认应用。

    没有命令时永远为假；有命令但状态里没有可用的 `applied_seq`（助手还没写过状态、
    或字段坏了）也算待办，让页面显示"等待助手响应…"，而不是假装命令已生效。
    """
    seq = current_seq()
    if seq <= 0:
        return False
    applied = (state or {}).get("applied_seq")
    if isinstance(applied, bool) or not isinstance(applied, int):
        applied = 0
    return seq > applied
