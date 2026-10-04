"""智慧树课中选择题助手。运行：python study_assistant.py --help。"""
import argparse
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from answer_engine import ask_ai, match_bank
from browser_adapter import CoursePage, DEFAULT_SELECTORS
from image_text import ImageNotReadyError
from cleanup import validate_retention
from session_store import SessionStore
import run_control
from playwright.sync_api import Error as BrowserError, sync_playwright

ROOT = Path(__file__).resolve().parent
# 仅使用站点通用入口；课程标识由用户在本机 config.json 中填写。
DEFAULT_URL = "https://studyvideoh5.zhihuishu.com/stuStudy"
LOG = logging.getLogger("study")
SESSIONS = SessionStore(ROOT / '.session' / 'auth.json')
# 计时入口做成可替换的，测试可以注入快进时钟，不必真的等 15 秒。
CLOCK = time.monotonic

# 提交后等待网页反馈的时间；超时才判定弹题没有正常关闭。
FEEDBACK_WAIT_SECONDS = 15
RECOVERY_WINDOW_SECONDS = 45
DIALOG_CONTENT_GRACE_SECONDS = 15
NEXT_LESSON_RETRY_SECONDS = 20
MAX_NEXT_LESSON_CLICKS = 3


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    config.setdefault("course_url", DEFAULT_URL)
    config.setdefault("browser_channel", "auto")
    config.setdefault("poll_seconds", 2)
    config.setdefault("ai", {})
    config.setdefault("selectors", {})
    if not isinstance(config["ai"], dict) or not isinstance(config["selectors"], dict):
        raise ValueError("ai 和 selectors 必须是 JSON 对象")
    url = urlsplit(config["course_url"])
    if url.scheme != "https" or url.hostname != "studyvideoh5.zhihuishu.com" or url.username or url.password:
        raise ValueError("course_url 必须是 HTTPS 的 studyvideoh5.zhihuishu.com 课程地址")
    for env, field in (("ZHS_AI_API_KEY", "api_key"), ("ZHS_AI_BASE_URL", "base_url"), ("ZHS_AI_MODEL", "model")):
        if os.environ.get(env):
            config["ai"][field] = os.environ[env]
    ai = config["ai"]
    for field in ("api_key", "model", "base_url"):
        ai.setdefault(field, "")
        if not isinstance(ai[field], str):
            raise ValueError(f"ai.{field} 必须是字符串")
    ai.setdefault("min_confidence", 0.85)
    confidence = ai["min_confidence"]
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("ai.min_confidence 必须为 0 到 1 的数字")
    for name, value, low, high in (("poll_seconds", config["poll_seconds"], 0.5, 30),
                                    ("timeout_seconds", ai.get("timeout_seconds", 45), 1, 120)):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} 必须在 {low} 到 {high} 之间")
    if any(k not in DEFAULT_SELECTORS or not isinstance(v, str) for k, v in config["selectors"].items()):
        raise ValueError("selectors 包含未知名称或非字符串值")
    # 诊断清理的保留策略也在这里校验，坏值会让 --check-config 直接报错。
    config["diagnostics"] = validate_retention(config.get("diagnostics"))
    if config["browser_channel"] not in ("auto", "chrome", "msedge", "chromium"):
        raise ValueError("browser_channel 仅支持 auto、chrome、msedge、chromium")
    return config


def diagnostics(adapter: CoursePage) -> Path:
    """保存可见页面和控件结构，不保存 Cookie、请求头或脚本内状态。"""
    folder = ROOT / "diagnostics" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True, exist_ok=True)
    report = {"frames": []}
    for frame in adapter.page.frames:
        try:
            report["frames"].append(frame.evaluate("""() => {
                const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
                return {text: document.body?.innerText?.slice(0, 20000),
                    controls: Array.from(document.querySelectorAll('button,label,[role],input,video,[class*=dialog],[class*=Dialog],[class*=question],[class*=option],.topic-title,.topic-item,.item-topic,.dialog-footer .btn,#nextBtn,#lessonOrder'))
                    .filter(visible).slice(0, 250).map(el => ({tag: el.tagName, id: el.id, class: el.className,
                        role: el.getAttribute('role'), type: el.getAttribute('type'),
                        checked: el.getAttribute('aria-checked'), text: el.innerText?.slice(0, 1000)}))};
            }"""))
        except BrowserError:
            report["frames"].append({"error": "页面已切换，无法读取此框架"})
    (folder / "page.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        adapter.page.screenshot(path=str(folder / "page.png"))
    except BrowserError:
        LOG.warning("页面截图失败，已保留文本诊断")
    LOG.info("诊断已保存：%s", folder)
    return folder


def mark_waiting() -> None:
    """助手停在终端等回车时也要让控制台知道它还活着，否则页面会显示"无数据"。"""
    state = dict(run_control.read_state(max_age=None) or {})
    state.pop("updated_at", None)
    state["waiting"] = True
    run_control.write_state(state)


def manual_pause(adapter: CoursePage, reason: str) -> None:
    LOG.warning(reason)
    mark_waiting()
    SESSIONS.save(adapter.page.context)
    diagnostics(adapter)
    print("请在浏览器处理当前题目/提示，或调整配置后重启。")
    if input("处理完按回车继续，输入 q 退出：").strip().lower() == "q":
        raise KeyboardInterrupt


def course_page(context):
    pages = [p for p in context.pages if not p.is_closed() and urlsplit(p.url).hostname == "studyvideoh5.zhihuishu.com"]
    # 用户登录过程中可能打开新标签页，选最后一个课程标签。
    return pages[-1] if pages else None


def wait_for_course_ready(context, config: dict):
    """自动等待登录和课程加载，不让用户漏掉终端回车而一直空转。"""
    notified = 0.0
    while context.pages:
        page = course_page(context)
        if page:
            adapter = CoursePage(page, config['selectors'], config.get('ai'))
            if page.locator(adapter.selectors['lesson']).count() and adapter.video() is not None:
                LOG.info('已识别课程页面，自动开始监控：%s', adapter.lesson())
                if SESSIONS.save(context):
                    LOG.info('登录状态已保存；下次启动会自动恢复有效会话')
                return page
        if CLOCK() - notified > 30:
            LOG.info('等待登录或打开课程视频页，无须回终端按回车')
            notified = CLOCK()
        try:
            context.pages[-1].wait_for_timeout(1000)
        except BrowserError:
            if not context.pages:
                return None
    return None


def page_is_transitioning(adapter: CoursePage) -> bool:
    """视频已结束、或连视频状态都读不到时，控件短暂失效通常只是页面在切课。"""
    try:
        state = adapter.video_state()
    except BrowserError:
        return True
    if state is None:
        return True
    return bool(state.get('ended'))


def _error_detail(exc: Exception) -> str:
    """只记录异常首行，限制长度，避免浏览器调用细节写入日志。"""
    lines = str(exc).splitlines()
    return f'{type(exc).__name__}: {(lines[0] if lines else "无详细信息")[:160]}'


def run_loop(context, config: dict, inspect_only: bool = False) -> None:
    page = course_page(context)
    if page is None:
        raise ValueError("未找到课程标签页，请进入具体视频播放页面后重新运行")
    adapter = CoursePage(page, config["selectors"], config.get('ai'))
    if inspect_only:
        diagnostics(adapter)
        return
    bank_path = ROOT / "question_bank.json"
    bank = json.loads(bank_path.read_text(encoding="utf-8-sig")) if bank_path.exists() else []
    if not isinstance(bank, list):
        raise ValueError("question_bank.json 必须是数组")
    answered_cache = {}
    pending_key = None
    pending_since = 0.0
    next_clicked = None
    next_since = 0.0
    last_status = 0.0
    last_time = None
    stalled_since = CLOCK()
    last_session_save = 0.0
    image_retry_since = None
    browser_retry_since = None
    dialog_empty_since = None
    next_attempts = 0
    next_retry_at = 0.0
    next_unavailable_since = None
    manual_paused = False
    # 启动时以当前命令序号为基线：重启助手等于恢复自动播放，不继承上次的暂停状态
    seen_seq = run_control.current_seq()
    while context.pages:
        page = course_page(context)
        if page is None:
            LOG.warning("课程页已关闭或登录已失效，请在浏览器重新进入课程页")
            if wait_for_course_ready(context, config) is None:
                return
            continue
        if page != adapter.page:
            adapter = CoursePage(page, config["selectors"], config.get('ai'))
            pending_key = next_clicked = None
            image_retry_since = browser_retry_since = dialog_empty_since = None
            next_attempts = 0
            next_unavailable_since = None
            stalled_since, last_time = CLOCK(), None
        now = CLOCK()
        try:
            command = run_control.read_command(seen_seq)
            if command is not None:
                seen_seq = command["seq"]
                if command["action"] == "pause":
                    manual_paused = True
                    LOG.info("收到控制台指令：暂停视频（弹题仍会自动处理）")
                elif command["action"] == "resume":
                    if manual_paused:
                        # 暂停期间进度不动，这里不重置的话恢复后第一轮就会误判卡死
                        stalled_since, last_time = CLOCK(), None
                    manual_paused = False
                    LOG.info("收到控制台指令：恢复播放")
                else:
                    # 倍速控制已移除：兼容旧控制台或手工写入的历史命令，但绝不
                    # 通过助手修改播放器倍速，避免主动控制触发平台检测。
                    LOG.warning("已忽略倍速控制指令；请在课程原生播放器菜单手动选择倍速")
            try:
                snapshot = adapter.video_state()
            except BrowserError:
                snapshot = None
            recovery_has_video = snapshot is not None
            run_control.write_state({
                "applied_seq": seen_seq,
                "rate": snapshot.get("rate") if snapshot else None,
                "requested_rate": None,
                "paused": snapshot.get("paused") if snapshot else None,
                "manual_paused": manual_paused,
                "time": snapshot.get("time") if snapshot else None,
                "duration": snapshot.get("duration") if snapshot else None,
                "lesson": adapter.lesson() if snapshot else None,
                "waiting": False,
            })
            if manual_paused:
                adapter.pause_video()
            if now - last_session_save > 30:
                SESSIONS.save(context)
                last_session_save = now
            if (pending_key or adapter.has_answer_feedback()) and adapter.has_dialog() and adapter.continue_after_answer():
                LOG.info('已处理答题反馈，继续下一题或返回视频')
                # 反馈处理完就清掉等待窗，否则同一题再弹出时会拿旧时间戳直接判超时。
                pending_key, pending_since = None, CLOCK()
                stalled_since, last_time = CLOCK(), None
                image_retry_since = None
                if recovery_has_video:
                    browser_retry_since = None
                page.wait_for_timeout(500)
                continue
            # 先记录弹窗状态，避免弹窗在读题结束后刚出现就被误判为无法识别。
            had_dialog = adapter.has_dialog()
            quiz_read_started = CLOCK()
            quiz = adapter.read_quiz()
            # 读图/OCR 耗时不属于视频播放停滞时间。
            stalled_since += CLOCK() - quiz_read_started
            if quiz:
                dialog_empty_since = None
                stalled_since, last_time = CLOCK(), None
                key = adapter.submission_key(quiz.question)
                if key in adapter.submitted:
                    if not pending_key:
                        # 同一题再次弹出（平台重发或弹窗重渲染）：重新开一个等待窗，
                        # 先尝试关闭它，绝不重复提交。
                        pending_key, pending_since = key, now
                        LOG.info('本题已提交过，弹窗再次出现，先尝试关闭，不重复提交')
                    if adapter.continue_after_answer():
                        LOG.info('已关闭重复弹窗')
                        pending_key, pending_since = None, CLOCK()
                        image_retry_since = None
                        if recovery_has_video:
                            browser_retry_since = None
                        page.wait_for_timeout(500)
                        continue
                    if now - pending_since > FEEDBACK_WAIT_SECONDS:
                        manual_pause(adapter, "提交后弹题仍未关闭，请检查答题反馈")
                        pending_key, pending_since = None, CLOCK()
                        stalled_since, last_time = CLOCK(), None
                else:
                    LOG.info("识别到%s：%s", "多选题" if quiz.question.multiple else "单选题", quiz.question.text)
                    answers = match_bank(quiz.question, bank)
                    if answers is None:
                        answers = answered_cache.get(quiz.question.fingerprint)
                    if answers is None:
                        answers = ask_ai(quiz.question, config["ai"])
                    adapter.submit(quiz, answers)
                    answered_cache[quiz.question.fingerprint] = answers
                    pending_key, pending_since = key, CLOCK()
                    stalled_since, last_time = CLOCK(), None
                    LOG.info("已提交选项：%s；等待网页反馈", ", ".join(str(i + 1) for i in answers))
            elif had_dialog:
                stalled_since, last_time = CLOCK(), None
                if dialog_empty_since is None:
                    dialog_empty_since = now
                if (pending_key and now - pending_since > FEEDBACK_WAIT_SECONDS) or (
                        not pending_key and now - dialog_empty_since > DIALOG_CONTENT_GRACE_SECONDS):
                    manual_pause(adapter, "发现未识别的弹窗，请手动处理或提供诊断文件进行适配")
                    pending_key = None
                    dialog_empty_since = None
                    stalled_since, last_time = CLOCK(), None
            else:
                dialog_empty_since = None
                pending_key = None
                state = adapter.video_state()
                recovery_has_video = state is not None
                if state is None:
                    # 控件异常与缺失视频属于同一恢复过程，不能交替重开 45 秒窗口。
                    if browser_retry_since is None:
                        browser_retry_since = CLOCK()
                    if CLOCK() - browser_retry_since > RECOVERY_WINDOW_SECONDS:
                        manual_pause(adapter, "尚未检测到视频，请进入课程视频页")
                    stalled_since, last_time = CLOCK(), None
                elif state["error"]:
                    manual_pause(adapter, f"播放器错误码 {state['error']}，请检查网络或重新加载视频")
                    stalled_since, last_time = CLOCK(), None
                elif state["ended"]:
                    if next_clicked != state["key"]:
                        if not adapter.next_lesson():
                            latest = adapter.video_state()
                            if latest and (latest['key'] != state['key'] or not latest['ended']):
                                continue  # 网站已自动切到下一节，不能误判为播放结束。
                            LOG.info("视频已结束，未找到可用的下一节按钮，停止运行。请核对课程进度。")
                            return
                        next_clicked, next_since = state["key"], CLOCK()
                        next_attempts = 1
                        next_retry_at = next_since + NEXT_LESSON_RETRY_SECONDS
                        next_unavailable_since = None
                        LOG.info("视频自然播放结束，已点击下一节")
                    elif now >= next_retry_at:
                        if next_attempts >= MAX_NEXT_LESSON_CLICKS:
                            manual_pause(adapter, "点击下一节后视频未切换，请手动检查课程目录")
                            next_clicked = None
                            next_attempts = 0
                            stalled_since, last_time = CLOCK(), None
                        elif adapter.next_lesson():
                            next_attempts += 1
                            next_since = CLOCK()
                            next_retry_at = next_since + NEXT_LESSON_RETRY_SECONDS
                            next_unavailable_since = None
                            LOG.info("视频仍停在上一节，重试点击下一节（第 %d 次）", next_attempts)
                        else:
                            if next_unavailable_since is None:
                                next_unavailable_since = now
                            if now - next_unavailable_since > RECOVERY_WINDOW_SECONDS:
                                manual_pause(adapter, "下一节按钮持续不可用，请检查课程目录")
                                next_clicked = None
                                next_attempts = 0
                                next_unavailable_since = None
                                stalled_since, last_time = CLOCK(), None
                            next_retry_at = CLOCK() + config['poll_seconds']
                    stalled_since, last_time = CLOCK(), None
                else:
                    if next_clicked is not None:
                        next_clicked = None
                        next_attempts = 0
                        next_unavailable_since = None
                        stalled_since, last_time = CLOCK(), None
                    if manual_paused:
                        stalled_since, last_time = CLOCK(), None
                    else:
                        adapter.resume()
                        mark = (state["key"], int(state["time"]))
                        if mark != last_time:
                            last_time, stalled_since = mark, now
                        elif now - stalled_since > 60:
                            manual_pause(adapter, "播放进度 60 秒没有变化，请检查是否有未识别弹题、登录提示或网络问题")
                            stalled_since, last_time = CLOCK(), None
                    if now - last_status > 30:
                        LOG.info("%s | %.0f / %.0f 秒", adapter.lesson(), state["time"], state["duration"])
                        last_status = now
            image_retry_since = None
            if recovery_has_video:
                browser_retry_since = None
        except ImageNotReadyError as exc:
            if image_retry_since is None:
                image_retry_since = CLOCK()
            if CLOCK() - image_retry_since <= RECOVERY_WINDOW_SECONDS:
                LOG.warning("图片暂不可用，稍后重试：%s", _error_detail(exc))
                stalled_since, last_time = CLOCK(), None
            else:
                manual_pause(adapter, f"图片等待超时：{_error_detail(exc)}")
                image_retry_since = None
                pending_key = None
                stalled_since, last_time = CLOCK(), None
        except ValueError as exc:
            manual_pause(adapter, str(exc))
            pending_key = None
            stalled_since, last_time = CLOCK(), None
        except BrowserError as exc:
            if page.is_closed():
                continue
            try:
                transitioning = page_is_transitioning(adapter)
            except BrowserError:
                transitioning = True
            if browser_retry_since is None:
                browser_retry_since = CLOCK()
            if transitioning and CLOCK() - browser_retry_since <= RECOVERY_WINDOW_SECONDS:
                LOG.warning("视频已结束或页面正在切换，稍后重试本次检查：%s", _error_detail(exc))
                stalled_since, last_time = CLOCK(), None
            else:
                manual_pause(adapter, f"网页控件已变化或操作超时，需要检查当前页面：{_error_detail(exc)}")
                stalled_since, last_time = CLOCK(), None
        if not page.is_closed():
            page.wait_for_timeout(config["poll_seconds"] * 1000)


def launch_browser_context(playwright, config: dict, user_data_dir: Path):
    """启动本机浏览器；自动模式按 Chrome、Edge 顺序回退。"""
    requested = config["browser_channel"]
    candidates = ("chrome", "msedge") if requested in ("auto", "chrome") else (requested,)
    failures = []
    for channel in candidates:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel=None if channel == "chromium" else channel,
                headless=False,
                no_viewport=True,
                args=["--start-maximized"],
            )
            if channel == "msedge" and requested in ("auto", "chrome"):
                LOG.warning("Chrome 不可用，已自动切换到 Microsoft Edge")
            LOG.info("使用浏览器：%s", channel)
            return context, channel
        except BrowserError as exc:
            failures.append(f"{channel}: {exc}")
            if channel != candidates[-1]:
                LOG.warning("无法启动 %s，尝试下一个浏览器", channel)
    detail = "；".join(failures)
    raise BrowserError(f"无法启动可用浏览器（{detail}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="智慧树视频播放与 AI 课中选择题助手")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="本机 JSON 配置")
    parser.add_argument("--inspect", action="store_true", help="登录后仅保存当前页面诊断，不播放或答题")
    parser.add_argument("--check-config", action="store_true", help="校验配置，不连接浏览器或 AI")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.check_config:
            ready = all(config["ai"].get(k) for k in ("api_key", "model", "base_url"))
            print("配置格式正确。AI 配置" + ("已填写（尚未联网验证）。" if ready else "未填齐，遇到题目时将停止并提示。"))
            return 0 if ready else 2
        (ROOT / "logs").mkdir(exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
            logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "study.log", encoding="utf-8")], force=True)
        LOG.info('启动课程助手：AI 模型 %s', config['ai']['model'])
        if not args.inspect and not all(config["ai"].get(k) for k in ("api_key", "model", "base_url")):
            LOG.warning("AI 配置尚未填齐；播放可以运行，遇到未知题将暂停自动处理")
        with sync_playwright() as p:
            context, _ = launch_browser_context(p, config, ROOT / "browser_profile")
            try:
                if SESSIONS.restore(context):
                    LOG.info('已恢复本机保存的登录会话；若平台已让会话失效，仍需重新登录')
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(config["course_url"], wait_until="domcontentloaded", timeout=45000)
                except BrowserError:
                    LOG.warning("页面加载较慢，请在打开的浏览器中检查页面")
                print("请在新打开的浏览器中进入课程。已有有效登录会自动恢复；需要登录时在浏览器操作即可。")
                if args.inspect:
                    print("请让需要诊断的弹题保持显示。")
                    input("准备好后按回车保存诊断：")
                    run_loop(context, config, True)
                else:
                    print("课程加载后自动开始，无须按回车。停止：Ctrl+C 或关闭脚本浏览器。")
                    if wait_for_course_ready(context, config):
                        run_loop(context, config)
            finally:
                SESSIONS.save(context)
                context.close()
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        return 0
    except FileNotFoundError:
        print("缺少配置文件：请将 config.example.json 复制为 config.json 后填写。", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"配置或运行错误：{exc}", file=sys.stderr)
        return 2
    except BrowserError:
        print("浏览器启动/连接失败：请安装 Chrome 或 Microsoft Edge，并关闭由本脚本打开的旧窗口后重试。", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
