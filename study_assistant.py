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
from session_store import SessionStore
from playwright.sync_api import Error as BrowserError, sync_playwright

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "https://studyvideoh5.zhihuishu.com/stuStudy"
LOG = logging.getLogger("study")
SESSIONS = SessionStore(ROOT / '.session' / 'auth.json')


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


def manual_pause(adapter: CoursePage, reason: str) -> None:
    LOG.warning(reason)
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
        if time.monotonic() - notified > 30:
            LOG.info('等待登录或打开课程视频页，无须回终端按回车')
            notified = time.monotonic()
        try:
            context.pages[-1].wait_for_timeout(1000)
        except BrowserError:
            if not context.pages:
                return None
    return None


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
    stalled_since = time.monotonic()
    last_session_save = 0.0
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
        now = time.monotonic()
        try:
            if now - last_session_save > 30:
                SESSIONS.save(context)
                last_session_save = now
            if (pending_key or adapter.has_answer_feedback()) and adapter.has_dialog() and adapter.continue_after_answer():
                LOG.info('已处理答题反馈，继续下一题或返回视频')
                page.wait_for_timeout(500)
                continue
            # 先记录弹窗状态，避免弹窗在读题结束后刚出现就被误判为无法识别。
            had_dialog = adapter.has_dialog()
            quiz = adapter.read_quiz()
            if quiz:
                key = adapter.submission_key(quiz.question)
                if key in adapter.submitted:
                    if now - pending_since > 15:
                        manual_pause(adapter, "提交后弹题仍未关闭，请检查答题反馈")
                        pending_key = None
                        pending_since = time.monotonic()
                else:
                    LOG.info("识别到%s：%s", "多选题" if quiz.question.multiple else "单选题", quiz.question.text)
                    answers = match_bank(quiz.question, bank)
                    if answers is None:
                        answers = answered_cache.get(quiz.question.fingerprint)
                    if answers is None:
                        answers = ask_ai(quiz.question, config["ai"])
                    adapter.submit(quiz, answers)
                    answered_cache[quiz.question.fingerprint] = answers
                    pending_key, pending_since = key, time.monotonic()
                    LOG.info("已提交选项：%s；等待网页反馈", ", ".join(str(i + 1) for i in answers))
            elif had_dialog:
                if not pending_key or now - pending_since > 15:
                    manual_pause(adapter, "发现未识别的弹窗，请手动处理或提供诊断文件进行适配")
                    pending_key = None
            else:
                pending_key = None
                state = adapter.video_state()
                if state is None:
                    if now - stalled_since > 30:
                        manual_pause(adapter, "尚未检测到视频，请进入课程视频页")
                        stalled_since = time.monotonic()
                elif state["error"]:
                    manual_pause(adapter, f"播放器错误码 {state['error']}，请检查网络或重新加载视频")
                elif state["ended"]:
                    if next_clicked != state["key"]:
                        if not adapter.next_lesson():
                            latest = adapter.video_state()
                            if latest and (latest['key'] != state['key'] or not latest['ended']):
                                continue  # 网站已自动切到下一节，不能误判为播放结束。
                            LOG.info("视频已结束，未找到可用的下一节按钮，停止运行。请核对课程进度。")
                            return
                        next_clicked, next_since = state["key"], now
                        LOG.info("视频自然播放结束，已点击下一节")
                    elif now - next_since > 20:
                        manual_pause(adapter, "点击下一节后视频未切换，请手动检查课程目录")
                        next_since = time.monotonic()
                else:
                    adapter.resume()
                    mark = (state["key"], int(state["time"]))
                    if mark != last_time:
                        last_time, stalled_since = mark, now
                    elif now - stalled_since > 60:
                        manual_pause(adapter, "播放进度 60 秒没有变化，请检查是否有未识别弹题、登录提示或网络问题")
                        stalled_since = time.monotonic()
                    if now - last_status > 30:
                        LOG.info("%s | %.0f / %.0f 秒", adapter.lesson(), state["time"], state["duration"])
                        last_status = now
        except ValueError as exc:
            manual_pause(adapter, str(exc))
            pending_key = None
        except BrowserError:
            if page.is_closed():
                continue
            manual_pause(adapter, "网页控件已变化或操作超时，需要检查当前页面")
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
    raise BrowserError(f"无法启动可用浏览器（{'；'.join(failures)}）")


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
