"""网页适配层。播放器 ID 来自用户页面；弹题 CSS 可在配置中覆盖。"""
from dataclasses import dataclass
import hashlib
import re
from playwright.sync_api import Locator, Page, TimeoutError as BrowserTimeout
from answer_engine import Question
from image_text import ImageTextReader


DEFAULT_SELECTORS = {
    "dialog": '[role="dialog"], .el-dialog, .ant-modal, .layui-layer',
    "question": "",
    "option": "",
    "selected": ".is-checked, .checked, .selected, .active",
    "submit": "",
    "continue": "",
    "video": "video",
    "player": "#vjs_container",
    "next": "#nextBtn",
    "lesson": "#lessonOrder",
}


@dataclass
class Quiz:
    question: Question
    root: Locator
    options: list[Locator]


class CoursePage:
    def __init__(self, page: Page, selectors: dict, ai_config: dict | None = None):
        self.page = page
        self.selectors = DEFAULT_SELECTORS | {k: v for k, v in selectors.items() if v}
        self.submitted: set[str] = set()
        self.image_text = ImageTextReader(ai_config)

    def dialogs(self) -> list[Locator]:
        result = []
        for frame in self.page.frames:
            for root in frame.locator(self.selectors["dialog"]).all():
                if root.is_visible():
                    result.append(root)
        return result

    def has_dialog(self) -> bool:
        return bool(self.dialogs())

    def lesson(self) -> str:
        loc = self.page.locator(self.selectors["lesson"])
        return loc.first.inner_text().strip() if loc.count() else "未知章节"

    def submission_key(self, question: Question) -> str:
        return self.lesson() + ":" + question.fingerprint

    def read_quiz(self) -> Quiz | None:
        for root in self.dialogs():
            names = root.locator('input[type="radio"]').evaluate_all("els => [...new Set(els.map(el => el.name))]")
            if len(names) > 1 or root.locator('[role="radiogroup"]').count() > 1:
                # 多题共用一个弹窗时，不把不同题目的选项拼成一道题。
                continue
            families = [self.selectors["option"]] if self.selectors["option"] else [
                'label:has(input[type="radio"]), label:has(input[type="checkbox"])',
                '.el-radio, .el-checkbox',
                '[role="radio"], [role="checkbox"]',
                '.topic-item',
            ]
            for selector in families:
                options = [loc for loc in root.locator(selector).all() if loc.is_visible()]
                if not 2 <= len(options) <= 20:
                    continue
                texts = tuple(self._option_text(loc) for loc in options)
                if any(not text for text in texts):
                    continue
                q_selector = self.selectors["question"]
                if q_selector:
                    qloc = root.locator(q_selector)
                    if qloc.count() != 1:
                        continue
                    text = self.image_text.read(qloc)
                elif root.locator('.topic-title').count():
                    text = self.image_text.read(root.locator('.topic-title').first, '.right, .error')
                else:
                    # 只取可见 DOM 的题干；不读取页面脚本、Cookie 或隐藏应用数据。
                    text = self.image_text.read(root, '.topic-option-item')
                    for option in sorted(texts, key=len, reverse=True):
                        text = text.replace(option, "", 1)
                    text = "\n".join(line.strip() for line in text.splitlines()
                                     if line.strip() and line.strip() not in {
                                         "提交", "提交答案", "确定", "确认", "继续学习", "继续播放", "关闭"})
                if not text or len(text) > 8000 or any(len(t) > 4000 for t in texts):
                    continue
                multiple = bool(root.locator('input[type="checkbox"], [role="checkbox"], .el-checkbox').count())
                multiple = multiple or "多选" in text
                return Quiz(Question(text, texts, multiple), root, options)
        return None

    def _option_text(self, option: Locator) -> str:
        """智慧树选项同时包含字母标记和答案文字，只取答案文字。"""
        content = option.locator('.item-topic')
        return self.image_text.read(content.first if content.count() else option, '.topic-option-item')

    def checked(self, option: Locator) -> bool | None:
        return option.evaluate("""(el, selected) => {
            if (el.matches('.topic-item') && el.querySelector('.topic-option-item')) {
                return !!el.querySelector('.topic-option-item.active, .item-topic.active');
            }
            const input = el.matches('input') ? el : el.querySelector('input[type=radio],input[type=checkbox]');
            if (input) return input.checked;
            const aria = el.getAttribute('aria-checked') ?? el.querySelector('[aria-checked]')?.getAttribute('aria-checked');
            if (aria === 'true' || aria === 'false') return aria === 'true';
            if (el.matches(selected) || el.querySelector(selected)) return true;
            if (el.matches('.el-radio,.el-checkbox')) return false;
            return null;
        }""", self.selectors["selected"])

    def submit(self, quiz: Quiz, answers: tuple[int, ...]) -> None:
        current = self.read_quiz()
        if current is None or current.question != quiz.question:
            raise ValueError("请求 AI 期间题目发生变化，取消提交")
        quiz = current
        key = self.submission_key(quiz.question)
        if key in self.submitted:
            raise ValueError("本题已提交过，等待反馈，避免重复提交")
        if (not answers or len(set(answers)) != len(answers)
                or any(type(i) is not int or not 0 <= i < len(quiz.options) for i in answers)
                or (not quiz.question.multiple and len(answers) != 1)):
            raise ValueError("待提交答案不符合题型或选项范围")
        # 智慧树 AI 助教题是点击选项即判题，没有提交按钮。
        immediate_answer = all(option.locator('.topic-option-item').count() for option in quiz.options)
        if immediate_answer:
            states = [self.checked(option) for option in quiz.options]
            if any(state is None for state in states):
                raise ValueError("无法确认智慧树弹题的选中状态")
            # 点击即向平台保存答案，先记录，超时也不自动重复提交。
            self.submitted.add(key)
            if quiz.question.multiple:
                for i, option in enumerate(quiz.options):
                    if states[i] != (i in answers):
                        option.click(timeout=3000)
            elif states[answers[0]] is not True:
                quiz.options[answers[0]].click(timeout=3000)
            selected = [self.checked(option) for option in quiz.options]
            if any(state is None for state in selected):
                raise ValueError("无法确认智慧树弹题的选中状态，需要适配页面")
            if selected != [i in answers for i in range(len(quiz.options))]:
                raise ValueError("页面选中状态与答案不一致，取消提交")
            return

        # 其他网页的选项必须能读出真实选中状态，不能只按坐标盲点。
        states = [self.checked(option) for option in quiz.options]
        if any(state is None for state in states):
            raise ValueError("无法确认自定义选项的选中状态，需要适配页面")
        if quiz.question.multiple:
            for i, option in enumerate(quiz.options):
                if states[i] != (i in answers):
                    option.click(timeout=3000)
        elif not states[answers[0]]:
            quiz.options[answers[0]].click(timeout=3000)
        selected = [self.checked(option) for option in quiz.options]
        if selected != [i in answers for i in range(len(quiz.options))]:
            raise ValueError("页面选中状态与答案不一致，取消提交")
        latest = self.read_quiz()
        if latest is None or latest.question != quiz.question:
            raise ValueError("选择选项后题目发生变化，取消提交")
        button = (quiz.root.locator(self.selectors["submit"]) if self.selectors["submit"]
                  else quiz.root.get_by_role("button", name=re.compile(r"^(提交|提交答案|确定|确认)$")))
        visible = [loc for loc in button.all() if loc.is_visible() and loc.is_enabled()]
        if len(visible) != 1:
            raise ValueError("没有唯一可用的提交按钮，需要适配页面")
        # 在点击前占用本题；即使点击超时，也不能自动重交可能已经成功的请求。
        self.submitted.add(key)
        visible[0].click(timeout=4000)

    def continue_after_answer(self) -> bool:
        for root in self.dialogs():
            is_zhs = root.evaluate("el => !!el.closest('#playTopic-dialog')")
            if is_zhs:
                quiz = self.read_quiz()
                ours = quiz is not None and self.submission_key(quiz.question) in self.submitted
                feedback = [loc for loc in root.locator('.answer, .topic-title .right, .topic-title .error').all()
                            if loc.is_visible() and loc.inner_text().strip()]
                if not ours and not feedback:
                    continue
                # 必须做完当前页再翻页；末页才使用页脚关闭按钮。
                next_question = root.locator('.btn-next')
                if next_question.count() == 1 and next_question.is_visible() and next_question.is_enabled():
                    next_question.click(timeout=3000)
                    return True
                close = root.locator('.dialog-footer .btn')
                if close.count() == 1 and close.is_visible():
                    close.click(timeout=3000)
                    return True
                continue
            button = (root.locator(self.selectors["continue"]) if self.selectors["continue"]
                      else root.get_by_role("button", name=re.compile(r"^(继续学习|继续播放)$")))
            visible = [loc for loc in button.all() if loc.is_visible() and loc.is_enabled()]
            if len(visible) == 1:
                visible[0].click(timeout=3000)
                return True
        return False

    def has_answer_feedback(self) -> bool:
        return any(loc.is_visible() and loc.inner_text().strip()
                   for frame in self.page.frames
                   for loc in frame.locator('#playTopic-dialog .answer, #playTopic-dialog .topic-title .right, #playTopic-dialog .topic-title .error').all())

    def video(self) -> Locator | None:
        for frame in self.page.frames:
            for loc in frame.locator(self.selectors["video"]).all():
                if loc.is_visible():
                    return loc
        return None

    def video_state(self) -> dict | None:
        video = self.video()
        if video is None:
            return None
        state = video.evaluate("""el => ({time: el.currentTime, duration: Number.isFinite(el.duration) ? el.duration : 0,
            paused: el.paused, ended: el.ended, ready: el.readyState, src: el.currentSrc,
            rate: el.playbackRate, error: el.error ? el.error.code : null})""")
        state["key"] = hashlib.sha256((self.lesson() + state.pop("src")).encode()).hexdigest()
        return state

    def set_rate(self, rate: float) -> bool:
        """优先通过课程播放器的倍速菜单设置；普通视频使用媒体元素回退。

        浏览器对倍速有可设区间（Chrome 约 0.0625–16），越界时 setter 会抛
        DOMException；那属于"这一次没设上"，返回 False 就行。页面级的异常
        （窗口被关、执行上下文销毁）**不在这里吞**，照旧交给主循环按浏览器错误处理。
        """
        video = self.video()
        if video is None:
            return False
        return bool(video.evaluate(
            """(el, rate) => {
                const container = el.ownerDocument.querySelector('#container');
                const speedBox = container?.querySelector('.speedBox');
                if (container?.contains(el) && speedBox) {
                    const tab = [...container.querySelectorAll('.speedTab[rate]')]
                        .find(item => Number(item.getAttribute('rate')) === rate);
                    if (!tab) return false;
                    try { tab.click(); } catch (_) { return false; }
                    return el.playbackRate === rate
                        && speedBox.querySelector('span')?.textContent?.trim() === `X ${rate === 1 ? '1.0' : rate}`;
                }
                try { el.playbackRate = rate; return el.playbackRate === rate; }
                catch (_) { return false; }
            }""", rate))

    def pause_video(self) -> bool:
        """暂停视频。助手手动暂停时靠它压住网站的自动续播。"""
        video = self.video()
        if video is None:
            return False
        return bool(video.evaluate("el => { el.pause(); return el.paused; }"))

    def resume(self) -> bool:
        if self.has_dialog():
            return False
        video = self.video()
        if video is None:
            return False
        return video.evaluate("""async el => {
            if (el.ended || !el.paused || el.readyState < 2) return false;
            try { await el.play(); return true; } catch (_) { return false; }
        }""")

    def next_lesson(self) -> bool:
        state = self.video_state()
        if self.has_dialog() or not state or not state["ended"]:
            return False
        player = self.page.locator(self.selectors["player"])
        if player.count() and player.first.is_visible():
            player.first.hover(timeout=3000)
        next_button = self.page.locator(self.selectors["next"])
        if next_button.count() == 1:
            try:
                next_button.wait_for(state='visible', timeout=3000)
            except BrowserTimeout:
                return False
        buttons = [loc for loc in next_button.all()
                   if loc.is_visible() and loc.is_enabled()
                   and loc.get_attribute("aria-disabled") != "true"
                   and not re.search(r"\bdisabled\b", loc.get_attribute("class") or "")]
        if len(buttons) != 1:
            return False
        latest = self.video_state()
        if not latest or latest['key'] != state['key'] or not latest['ended']:
            return False
        buttons[0].click(timeout=3000)
        return True
