"""用真正的 Chrome、短音频视频元素和模拟 API 验证完整控制循环。"""
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import urllib.error
import wave

from playwright.sync_api import Error as BrowserError, sync_playwright
from answer_engine import Question, ask_ai
from browser_adapter import CoursePage
import image_text
from study_assistant import DEFAULT_URL, run_loop, wait_for_course_ready
from session_store import SessionStore
import run_control


class FakeClock:
    """可快进的单调时钟：让等待窗在测试里立刻过期，不必真的等 15 秒。"""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def jump(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def silent_wav(seconds: float) -> str:
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\x00\x00' * int(8000 * seconds))
    return 'data:audio/wav;base64,' + base64.b64encode(audio.getvalue()).decode()


class APITests(unittest.TestCase):
    def setUp(self):
        self.q = Question("SQL 查询关键字？", ("SELECT", "INSERT"), False)
        self.cfg = {"base_url": "https://ai.example/v1", "model": "test-model", "api_key": "test-secret"}

    def test_protocol_sends_only_question_and_checks_result(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": '{"answers":[1],"confidence":0.9}'}}]}).encode())
        with patch('answer_engine.urllib.request.build_opener') as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(ask_ai(self.q, self.cfg), (0,))
            req = factory.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, 'https://ai.example/v1/chat/completions')
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-secret')
            self.assertNotIn(b'test-secret', req.data)
            user_data = json.loads(json.loads(req.data)['messages'][1]['content'])
            self.assertEqual(set(user_data), {'question', 'type', 'options'})

    def test_api_error_does_not_include_key(self):
        with patch('answer_engine.urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect = urllib.error.HTTPError('url', 401, 'test-secret', {}, None)
            with self.assertRaises(ValueError) as caught:
                ask_ai(self.q, self.cfg)
            self.assertIn('401', str(caught.exception))
            self.assertNotIn('test-secret', str(caught.exception))


class _StopLoop(BaseException):
    """测试专用：让轮询回调结束主循环；它既不是断言失败，也不是运行错误。"""


class FlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_ready_course_starts_without_terminal_input(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.browser.new_context()
            self.addCleanup(context.close)
            context.route(DEFAULT_URL, lambda route: route.fulfill(
                body='<div id="lessonOrder">lesson</div><video></video>', content_type='text/html'))
            page = context.new_page()
            page.goto(DEFAULT_URL)
            with patch('study_assistant.SESSIONS', SessionStore(Path(directory) / 'state.json')), patch('builtins.input', side_effect=AssertionError('不应需要按回车')):
                self.assertEqual(wait_for_course_ready(context, {'selectors': {}}), page)

    def test_video_quiz_submit_continue_next_and_finish(self):
        self._exercise_flow()

    def test_dialog_opening_between_scans_is_read_on_next_poll(self):
        self._exercise_flow(show_after_scan=True)

    def _session_patch(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session_patch = patch('study_assistant.SESSIONS', SessionStore(Path(temp.name) / 'state.json'))
        session_patch.start()
        self.addCleanup(session_patch.stop)
        # 主循环现在每轮都写 run_state.json：老测试也必须把运行目录指到临时目录，
        # 否则跑一次测试就会在真实 logs/ 里留下状态文件。
        self._patch_run_dir(Path(temp.name) / 'run')
        # manual_pause() 会在 input() 之前调 diagnostics()：一旦走到暂停路径就会往真实
        # diagnostics/ 写现场截图，所以跑 run_loop 的老测试也必须把它挡掉。
        diagnostics_patch = patch('study_assistant.diagnostics', return_value=None)
        diagnostics_patch.start()
        self.addCleanup(diagnostics_patch.stop)

    def test_same_question_reappearing_is_closed_not_resubmitted(self):
        """平台把同一题再弹一次时：不重复提交，也不能拿旧时间戳立刻判超时暂停。"""
        self._session_patch()
        html = '''<div id="lessonOrder">第一节</div><div id="vjs_container"><video muted></video></div>
<button id="nextBtn" onclick="window.nexts++;document.querySelector('#lessonOrder').textContent='第二节';this.disabled=true;v.currentTime=0;v.play()">下一节</button>
<div role="dialog" id="dlg" style="display:none"><h3>SQL 查询关键字？（单选题）</h3>
<label><input type="radio" name="q">SELECT</label><label><input type="radio" name="q">INSERT</label>
<button id="submitBtn" onclick="window.submits++;this.disabled=true;document.querySelector('#continue').hidden=false">提交</button>
<button id="continue" hidden onclick="closeQuiz()">继续学习</button></div>
<script>window.submits=0;window.nexts=0;window.continues=0;window.reopens=0;window.asked=false;var v=document.querySelector('video');
function closeQuiz(){window.continues++;document.querySelector('#dlg').style.display='none';v.play();
if(!window.reopened){window.reopened=true;setTimeout(function(){window.reopens++;
document.querySelector('#continue').hidden=true;document.querySelector('#dlg').style.display='block';
setTimeout(function(){document.querySelector('#continue').hidden=false;},500);},1100);}}
v.ontimeupdate=function(){if(!window.asked&&v.currentTime>0.1){window.asked=true;v.pause();document.querySelector('#dlg').style.display='block';}};
</script>'''
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route(DEFAULT_URL, lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        page = context.new_page()
        page.goto(DEFAULT_URL)
        page.locator('video').evaluate('(v, src)=>{v.src=src;v.play()}', silent_wav(3))
        clock = FakeClock()
        # 第一次处理完反馈后把时钟快进，模拟“同一题隔了很久又弹出来”。
        jumper = threading.Timer(0.6, clock.jump, args=(30,))
        jumper.start()
        self.addCleanup(jumper.cancel)
        config = {"selectors": {}, "ai": {}, "poll_seconds": 0.05}
        with patch('study_assistant.CLOCK', clock), \
                patch('study_assistant.FEEDBACK_WAIT_SECONDS', 1.0), \
                patch('study_assistant.ask_ai', return_value=(0,)) as ai, \
                patch('builtins.input', side_effect=AssertionError('不应需要手动介入')):
            with self.assertLogs('study', level='INFO') as logs:
                run_loop(context, config)
        self.assertEqual(ai.call_count, 1, '同一题不应重复问 AI')
        self.assertEqual(page.evaluate('window.submits'), 1, '同一题不应重复提交')
        self.assertEqual(page.evaluate('window.reopens'), 1, '弹题确实又出现了一次')
        self.assertEqual(page.evaluate('window.continues'), 2, '两次弹题都应被关闭')
        self.assertTrue(any('本题已提交过，弹窗再次出现' in line for line in logs.output), logs.output)

    def test_transient_error_while_video_ending_retries_once(self):
        """视频刚结束、页面正在切课时控件失效一次，应重试而不是停下等你按回车。"""
        self._session_patch()
        html = '''<div id="lessonOrder">第一节</div><div id="vjs_container"><video muted></video></div>
<button id="nextBtn" onclick="window.nexts++;document.querySelector('#lessonOrder').textContent='第二节';this.disabled=true;v.currentTime=0;v.play()">下一节</button>
<script>window.nexts=0;</script>'''
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route(DEFAULT_URL, lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        page = context.new_page()
        page.goto(DEFAULT_URL)
        page.locator('video').evaluate('(v, src)=>{v.src=src;v.play()}', silent_wav(1))
        original_next = CoursePage.next_lesson
        calls = {'count': 0}

        def flaky_next(adapter):
            calls['count'] += 1
            if calls['count'] == 1:
                raise BrowserError('模拟切课时控件短暂失效')
            return original_next(adapter)

        config = {"selectors": {}, "ai": {}, "poll_seconds": 0.05}
        with patch.object(CoursePage, 'next_lesson', flaky_next), \
                patch('builtins.input', side_effect=AssertionError('不应需要手动介入')):
            with self.assertLogs('study', level='INFO') as logs:
                run_loop(context, config)
        self.assertEqual(page.evaluate('window.nexts'), 1, '重试后应当照常切到下一节')
        self.assertGreaterEqual(calls['count'], 3)
        self.assertTrue(any('稍后重试本次检查' in line for line in logs.output), logs.output)

    def _video_page(self, seconds=30):
        """一个真的在播放的课程页：够长，不会在断言前自己播完。"""
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route(DEFAULT_URL, lambda route: route.fulfill(
            body='<div id="lessonOrder">1.3.1 函数极限</div>'
                 '<div id="vjs_container"><video muted></video></div>',
            content_type='text/html; charset=utf-8'))
        page = context.new_page()
        page.goto(DEFAULT_URL)
        page.locator('video').evaluate('(v, src)=>{v.src=src;v.play()}', silent_wav(seconds))
        page.wait_for_function('() => !document.querySelector("video").paused', timeout=10000)
        return context, page

    def _drive_loop(self, context, page, step, max_reads=400):
        """在主线程里跑主循环，每读一次视频状态就回调一次 `step(reads, page)`。

        Playwright 的同步 API 只允许在创建它的线程里调用：把 `run_loop` 丢进后台线程
        会立刻抛 `Cannot switch to a different thread`（连 `context.pages` 都读不到），
        所以"一边跑循环一边看页面"只能靠循环自己的轮询回调；`step` 抛 `_StopLoop`
        就结束本次运行。
        """
        original_state = CoursePage.video_state
        errors = []
        reads = 0
        exceeded = []

        def hooked(adapter):
            nonlocal reads
            reads += 1
            if reads > max_reads:
                exceeded.append(reads)
                raise _StopLoop
            step(reads, page)
            return original_state(adapter)

        try:
            with patch.object(CoursePage, 'video_state', hooked), \
                    patch('study_assistant.diagnostics', return_value=None), \
                    patch('builtins.input', side_effect=AssertionError('这些场景都不该需要手动介入')):
                run_loop(context, {"selectors": {}, "ai": {}, "poll_seconds": 0.05})
        except _StopLoop:
            pass
        except AssertionError:
            raise  # 断言失败（含"这些场景都不该需要手动介入"）要原样冒出来，不能藏进 errors
        except BaseException as exc:  # noqa: BLE001 - 循环里的意外异常要带回主线程断言
            errors.append(exc)
        if exceeded:
            self.fail(f'读了 {max_reads} 次视频状态仍未满足停止条件，页面行为与预期不符')
        return errors

    def _patch_run_dir(self, run_dir):
        """把运行目录指到临时目录：测试绝不碰真实 logs/（必须在起循环之前生效）。"""
        patcher = patch.object(run_control, 'RUN_DIR', run_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_clock(self, clock):
        patcher = patch('study_assistant.CLOCK', clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _scripted_recovery(self, video_state, *, read_quiz=None, has_dialog=None,
                           next_lesson=None, max_polls=30, clock=None, submit=None):
        self._session_patch()
        clock = clock or FakeClock()
        self._patch_clock(clock)
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route(DEFAULT_URL, lambda route: route.fulfill(
            body='<div id="lessonOrder">第一节</div><video muted></video>', content_type='text/html'))
        page = context.new_page()
        page.goto(DEFAULT_URL)
        calls = {'state': 0, 'manual': []}

        def state(adapter):
            calls['state'] += 1
            if calls['state'] > max_polls:
                raise _StopLoop
            return video_state(calls['state'], clock)

        def manual(adapter, reason):
            calls['manual'].append(reason)
            raise _StopLoop

        try:
            with patch.object(CoursePage, 'video_state', state), \
                    patch.object(CoursePage, 'read_quiz', read_quiz or (lambda adapter: None)), \
                    patch.object(CoursePage, 'has_dialog', has_dialog or (lambda adapter: False)), \
                    patch.object(CoursePage, 'next_lesson', next_lesson or (lambda adapter: False)), \
                    patch.object(CoursePage, 'submit', submit or CoursePage.submit), \
                    patch('study_assistant.manual_pause', manual):
                run_loop(context, {'selectors': {}, 'ai': {}, 'poll_seconds': 0.01})
        except _StopLoop:
            pass
        return calls, clock

    def test_image_read_failure_recovers_and_persistent_failure_pauses(self):
        state = lambda count, clock: {'key': 'one', 'time': count, 'duration': 100,
                                      'rate': 1, 'paused': False, 'ended': False, 'error': None}
        reads = {'count': 0}

        def once(adapter):
            reads['count'] += 1
            if reads['count'] == 1:
                raise getattr(image_text, 'ImageNotReadyError', ValueError)('图片暂不可用')
            raise _StopLoop

        calls, _ = self._scripted_recovery(state, read_quiz=once)
        self.assertEqual(reads['count'], 2)
        self.assertEqual(calls['manual'], [])

        def forever(adapter):
            raise getattr(image_text, 'ImageNotReadyError', ValueError)('图片暂不可用')

        def advancing(count, clock):
            clock.jump(10)
            return state(count, clock)

        calls, _ = self._scripted_recovery(advancing, read_quiz=forever)
        self.assertEqual(len(calls['manual']), 1)
        self.assertIn('图片', calls['manual'][0])

    def test_visible_empty_dialog_has_grace_period_then_pauses(self):
        def state(count, clock):
            clock.jump(5)
            return {'key': 'one', 'time': count, 'duration': 100,
                    'rate': 1, 'paused': False, 'ended': False, 'error': None}

        calls, _ = self._scripted_recovery(state, has_dialog=lambda adapter: True)
        self.assertGreater(calls['state'], 2)
        self.assertEqual(len(calls['manual']), 1)

    def test_two_browser_failures_during_transition_are_retried(self):
        attempts = {'count': 0}

        def state(count, clock):
            return {'key': 'one', 'time': 30, 'duration': 30,
                    'rate': 1, 'paused': True, 'ended': True, 'error': None}

        def next_lesson(adapter):
            attempts['count'] += 1
            if attempts['count'] <= 2:
                raise BrowserError('临时切课失败')
            raise _StopLoop

        calls, _ = self._scripted_recovery(state, next_lesson=next_lesson)
        self.assertEqual(attempts['count'], 3)
        self.assertEqual(calls['manual'], [])

    def test_same_ended_video_retries_next_button_at_most_three_times(self):
        clicks = []

        def state(count, clock):
            clock.jump(5)
            return {'key': 'one', 'time': 30, 'duration': 30,
                    'rate': 1, 'paused': True, 'ended': True, 'error': None}

        def next_lesson(adapter):
            clicks.append(1)
            return True

        calls, _ = self._scripted_recovery(state, next_lesson=next_lesson)
        self.assertEqual(len(clicks), 3)
        self.assertEqual(len(calls['manual']), 1)

    def test_next_button_temporarily_unavailable_after_click_keeps_transition(self):
        clicks = []

        def state(count, clock):
            clock.jump(5)
            return {'key': 'one', 'time': 30, 'duration': 30,
                    'rate': 1, 'paused': True, 'ended': True, 'error': None}

        def next_lesson(adapter):
            clicks.append(1)
            return len(clicks) == 1

        calls, _ = self._scripted_recovery(state, next_lesson=next_lesson)
        self.assertGreaterEqual(len(clicks), 2)
        self.assertEqual(len(calls['manual']), 1)

    def test_long_quiz_does_not_consume_playback_stall_budget(self):
        reads = {'count': 0}
        test_clock = FakeClock()
        submitted = []
        quiz = SimpleNamespace(question=Question('SQL 查询关键字？', ('SELECT', 'INSERT'), False))

        def state(count, clock):
            if count == 8:
                raise _StopLoop
            return {'key': 'one', 'time': 5, 'duration': 100,
                    'rate': 1, 'paused': False, 'ended': False, 'error': None}

        def slow_quiz(adapter):
            reads['count'] += 1
            if reads['count'] == 2:
                test_clock.jump(120)
                return quiz
            return None

        with patch('study_assistant.ask_ai', return_value=(0,)):
            calls, _ = self._scripted_recovery(
                state, read_quiz=slow_quiz, clock=test_clock,
                submit=lambda adapter, quiz, answers: submitted.append(answers))
        self.assertEqual(submitted, [(0,)])
        self.assertEqual(calls['manual'], [])

    def test_low_confidence_error_still_pauses_immediately(self):
        def state(count, clock):
            return {'key': 'one', 'time': 1, 'duration': 100,
                    'rate': 1, 'paused': False, 'ended': False, 'error': None}

        calls, _ = self._scripted_recovery(
            state, read_quiz=lambda adapter: (_ for _ in ()).throw(ValueError('置信度不足')))
        self.assertEqual(len(calls['manual']), 1)
        self.assertEqual(calls['state'], 1)

    def test_empty_dialog_then_question_is_submitted(self):
        reads = {'count': 0}
        submitted = []
        question = Question('SQL 查询关键字？', ('SELECT', 'INSERT'), False)
        quiz = SimpleNamespace(question=question)

        def read(adapter):
            reads['count'] += 1
            if reads['count'] == 1:
                return None
            if reads['count'] == 2:
                return quiz
            raise _StopLoop

        def state(count, clock):
            clock.jump(5)
            return {'key': 'one', 'time': 1, 'duration': 100,
                    'rate': 1, 'paused': True, 'ended': False, 'error': None}

        with patch('study_assistant.ask_ai', return_value=(0,)):
            calls, _ = self._scripted_recovery(
                state, read_quiz=read, has_dialog=lambda adapter: True,
                submit=lambda adapter, quiz, answers: submitted.append(answers))
        self.assertEqual(submitted, [(0,)])
        self.assertEqual(calls['manual'], [])

    def test_next_retry_stops_when_video_key_changes(self):
        clicks = []

        def state(count, clock):
            clock.jump(5)
            if count > 14:
                raise _StopLoop
            ended = len(clicks) < 2
            return {'key': 'one' if ended else 'two', 'time': 30 if ended else 0,
                    'duration': 30, 'rate': 1, 'paused': ended,
                    'ended': ended, 'error': None}

        def next_lesson(adapter):
            clicks.append(1)
            return True

        calls, _ = self._scripted_recovery(state, next_lesson=next_lesson)
        self.assertEqual(len(clicks), 2)
        self.assertEqual(calls['manual'], [])

    def test_persistent_browser_failure_exhausts_recovery_window(self):
        def state(count, clock):
            clock.jump(10)
            return {'key': 'one', 'time': 30, 'duration': 30,
                    'rate': 1, 'paused': True, 'ended': True, 'error': None}

        def broken(adapter):
            raise BrowserError('临时切课失败')

        calls, _ = self._scripted_recovery(state, next_lesson=broken)
        self.assertEqual(len(calls['manual']), 1)
        self.assertIn('Error:', calls['manual'][0])

    def test_alternating_browser_error_and_missing_video_share_recovery_deadline(self):
        polls = {'count': 0}

        def read(adapter):
            polls['count'] += 1
            clock.jump(10)
            if polls['count'] % 2:
                raise BrowserError('页面切换中')
            return None

        clock = FakeClock()
        calls, _ = self._scripted_recovery(
            lambda count, clock: None, read_quiz=read, clock=clock, max_polls=100)
        self.assertEqual(len(calls['manual']), 1, '交替失败不能无限重置恢复窗口')
        self.assertEqual(polls['count'], 6, '首次失败后超过 45 秒的首轮就应人工处理')
        self.assertEqual(clock.now, 1060)

    def test_valid_video_recovery_gives_later_failure_a_fresh_deadline(self):
        polls = {'count': 0}

        def read(adapter):
            polls['count'] += 1
            clock.jump(10)
            if polls['count'] == 1 or (polls['count'] >= 5 and polls['count'] % 2):
                raise BrowserError('页面切换中')
            return None

        def state(count, clock):
            # 第四轮确实恢复视频；前后均模拟同一课程页内的控件过渡。
            if polls['count'] == 4:
                return {'key': 'one', 'time': 1, 'duration': 100,
                        'rate': 1, 'paused': False, 'ended': False, 'error': None}
            return None

        clock = FakeClock()
        with patch.object(CoursePage, 'resume', return_value=None):
            calls, _ = self._scripted_recovery(
                state, read_quiz=read, clock=clock, max_polls=100)
        self.assertEqual(len(calls['manual']), 1)
        self.assertEqual(polls['count'], 10, '有效视频恢复后，后续故障应重新等待 45 秒')
        self.assertEqual(clock.now, 1100)

    def test_fixed_playback_position_still_pauses_after_60_seconds(self):
        def state(count, clock):
            clock.jump(25)
            return {'key': 'one', 'time': 1, 'duration': 100,
                    'rate': 1, 'paused': False, 'ended': False, 'error': None}

        calls, _ = self._scripted_recovery(state)
        self.assertEqual(len(calls['manual']), 1)
        self.assertIn('播放进度', calls['manual'][0])

    def test_control_pause_applies_while_image_recovery_is_pending(self):
        reads = {'count': 0}
        paused = []

        def state(count, clock):
            if count == 1:
                run_control.write_command('pause')
            return {'key': 'one', 'time': 1, 'duration': 100,
                    'rate': 1, 'paused': False, 'ended': False, 'error': None}

        def read(adapter):
            reads['count'] += 1
            if reads['count'] == 1:
                raise image_text.ImageNotReadyError('图片暂不可用')
            if reads['count'] == 2:
                self.assertEqual(paused, [True], '第二次图片仍不可用前应已执行暂停')
                raise image_text.ImageNotReadyError('图片仍不可用')
            raise _StopLoop

        def pause(adapter):
            paused.append(run_control.read_state(max_age=None)['manual_paused'])
            return True

        with patch.object(CoursePage, 'pause_video', pause):
            calls, _ = self._scripted_recovery(state, read_quiz=read)
        self.assertGreaterEqual(len(paused), 1)
        self.assertTrue(all(paused))
        self.assertEqual(calls['manual'], [])

    def test_control_pause_applies_while_browser_recovery_is_pending(self):
        reads = {'count': 0}
        paused = []

        def state(count, clock):
            if count == 1:
                run_control.write_command('pause')
            ended = count <= 4
            return {'key': 'one', 'time': 1, 'duration': 100,
                    'rate': 1, 'paused': ended, 'ended': ended, 'error': None}

        def read(adapter):
            reads['count'] += 1
            if reads['count'] == 1:
                raise BrowserError('页面切换中')
            if reads['count'] == 2:
                self.assertEqual(paused, [True], '第二次浏览器故障前应已执行暂停')
                raise BrowserError('页面仍在切换')
            raise _StopLoop

        def pause(adapter):
            paused.append(run_control.read_state(max_age=None)['manual_paused'])
            return True

        with patch.object(CoursePage, 'pause_video', pause):
            calls, _ = self._scripted_recovery(state, read_quiz=read)
        self.assertGreaterEqual(len(paused), 1)
        self.assertTrue(all(paused))
        self.assertEqual(calls['manual'], [])

    def test_manual_pause_stops_video_and_skips_stall_detection(self):
        """暂停真的让视频停下，并且状态文件如实回报。"""
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        context, page = self._video_page()
        paused_reads = []

        def step(reads, page):
            if reads == 1:
                # 命令必须在助手跑起来之后再发：控制台是运行中随时点按钮的；而"启动前
                # 就存在的命令"会被启动基线当成已见过，那正是规格要的行为。
                run_control.write_command('pause')
            if not page.evaluate('() => document.querySelector("video").paused'):
                return
            paused_reads.append(reads)
            if len(paused_reads) < 4:
                # 连着几轮读到的都是暂停：确认它被按住了，而不是刚好停了一瞬。
                return
            self.assertTrue(page.evaluate('() => document.querySelector("video").paused'))
            state = run_control.read_state(max_age=None)
            self.assertTrue(state['manual_paused'])
            self.assertEqual(state['applied_seq'], 1)
            raise _StopLoop

        errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [], '暂停期间不该触发手动介入')
        self.assertGreaterEqual(len(paused_reads), 4, '暂停命令始终没有让视频停下')

    def test_manual_pause_survives_clock_jump(self):
        """暂停期间把时钟快进 300 秒，助手仍不能判定"进度卡住"。"""
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        clock = FakeClock()
        self._patch_clock(clock)
        context, page = self._video_page()
        after_jump = []

        def step(reads, page):
            if reads == 1:
                run_control.write_command('pause')
            if not page.evaluate('() => document.querySelector("video").paused'):
                return
            if not after_jump:
                # 暂停期间时间照样流逝：卡死判定必须被跳过，否则这一跳后就会叫停。
                clock.jump(300)
                after_jump.append(reads)
                return
            if len(after_jump) < 9:
                after_jump.append(reads)
                return
            self.assertTrue(page.evaluate('() => document.querySelector("video").paused'))
            raise _StopLoop

        errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [], '暂停期间不该触发手动介入')
        self.assertGreaterEqual(len(after_jump), 9, '暂停命令始终没有让视频停下')

    def test_resume_command_plays_video_again(self):
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        context, page = self._video_page()
        sent = []

        def step(reads, page):
            if reads == 1:
                run_control.write_command('pause')
            paused = page.evaluate('() => document.querySelector("video").paused')
            if not sent:
                if paused:
                    run_control.write_command('resume')
                    sent.append(reads)
                return
            if paused:
                return
            self.assertFalse(run_control.read_state(max_age=None)['manual_paused'])
            self.assertEqual(run_control.read_state(max_age=None)['applied_seq'], 2)
            raise _StopLoop

        errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [])
        self.assertTrue(sent, '暂停命令始终没有让视频停下')

    def test_rate_command_is_consumed_without_modifying_media(self):
        """兼容旧倍速命令，但助手不得写入播放器倍速。"""
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        context, page = self._video_page()
        calls = []

        def step(reads, page):
            if reads == 1:
                run_control.write_command('rate', 1.5)
            if reads >= 4:
                state = run_control.read_state(max_age=None)
                self.assertEqual(page.evaluate('() => document.querySelector("video").playbackRate'), 1)
                self.assertIsNone(state['requested_rate'])
                raise _StopLoop

        with patch.object(CoursePage, 'set_rate', side_effect=lambda rate: calls.append(rate)):
            errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [])

    def test_rate_command_is_ignored_when_media_already_matches(self):
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        context, page = self._video_page()
        calls = []
        def step(reads, page):
            if reads == 1:
                page.locator('video').evaluate('video => { video.playbackRate = 1.5; }')
                run_control.write_command('rate', 1.5)
            if reads >= 4:
                state = run_control.read_state(max_age=None)
                if state and state['applied_seq'] >= 1:
                    raise _StopLoop

        with patch.object(CoursePage, 'set_rate', side_effect=lambda rate: calls.append(rate)):
            errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [])

    def test_no_rate_command_leaves_existing_media_rate_alone(self):
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        context, page = self._video_page()
        page.locator('video').evaluate('video => { video.playbackRate = 1.25; }')
        calls = []

        def step(reads, page):
            if reads >= 4:
                state = run_control.read_state(max_age=None)
                self.assertIsNone(state['requested_rate'])
                self.assertEqual(state['rate'], 1.25)
                raise _StopLoop

        with patch.object(CoursePage, 'set_rate', side_effect=lambda rate: calls.append(rate)):
            errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [])
        self.assertEqual(calls, [])

    def test_startup_baseline_ignores_commands_written_before_start(self):
        """重启助手等于恢复自动播放：启动前就存在的旧命令不能被重放。

        这条是规格的钉子：曾经有实现为了让"先写命令再启动"的测试通过，在启动基线
        上加了"状态显示未应用就退回 0"的兜底，结果是重启后把上一次没来得及应用的
        暂停/倍速又执行了一遍。
        """
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        self._patch_clock(FakeClock())
        run_control.write_command('pause')  # 助手启动之前就存在的命令
        context, page = self._video_page()
        reads = []

        def step(count, page):
            reads.append(count)
            if len(reads) < 6:
                return
            self.assertFalse(page.evaluate('() => document.querySelector("video").paused'),
                             '启动前就存在的命令不该被重放')
            state = run_control.read_state(max_age=None)
            self.assertFalse(state['manual_paused'])
            self.assertEqual(state['applied_seq'], 1, '基线就是启动那一刻的命令序号')
            raise _StopLoop

        errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [])

    def test_resume_resets_stall_timer(self):
        """暂停很久后点开始：恢复后的第一轮不能被"进度 60 秒没变化"误判成卡死。"""
        self._session_patch()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self._patch_run_dir(Path(temp.name) / 'run')
        clock = FakeClock()
        self._patch_clock(clock)
        context, page = self._video_page()
        stage = {'paused': False, 'resumed': False}

        def step(reads, page):
            paused = page.evaluate('() => document.querySelector("video").paused')
            if not stage['paused']:
                if reads == 1:
                    run_control.write_command('pause')
                elif paused:
                    stage['paused'] = True
                    clock.jump(300)  # 暂停期间时间照样流逝
                    run_control.write_command('resume')
                return
            if paused or run_control.read_state(max_age=None)['manual_paused']:
                return
            # 走到这里说明恢复后的第一轮已经完整跑完（卡死判定就在那一轮末尾）
            stage['resumed'] = True
            raise _StopLoop

        errors = self._drive_loop(context, page, step)
        self.assertEqual(errors, [], '恢复后不该被误判为进度卡死')
        self.assertTrue(stage['resumed'])

    def _exercise_flow(self, show_after_scan=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session_patch = patch('study_assistant.SESSIONS', SessionStore(Path(temp.name) / 'state.json'))
        session_patch.start()
        self.addCleanup(session_patch.stop)
        # 主循环每轮都写 run_state.json：这里同样指到临时目录，不碰真实 logs/。
        run_dir_patch = patch.object(run_control, 'RUN_DIR', Path(temp.name) / 'run')
        run_dir_patch.start()
        self.addCleanup(run_dir_patch.stop)
        # 同上：暂停路径会在 input() 之前写现场截图，必须挡掉真实 diagnostics/。
        diagnostics_patch = patch('study_assistant.diagnostics', return_value=None)
        diagnostics_patch.start()
        self.addCleanup(diagnostics_patch.stop)
        audio = io.BytesIO()
        with wave.open(audio, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\x00\x00' * 6400)
        src = 'data:audio/wav;base64,' + base64.b64encode(audio.getvalue()).decode()
        html = '''<div id="lessonOrder">第一节</div><div id="vjs_container"><video muted></video></div>
<button id="nextBtn" onclick="window.nexts++;document.querySelector('#lessonOrder').textContent='第二节';this.disabled=true;v.currentTime=0;v.play()">下一节</button>
<div role="dialog" style="display:none"><h3>SQL 查询关键字？（单选题）</h3>
<label><input type="radio" name="q">SELECT</label><label><input type="radio" name="q">INSERT</label>
<button onclick="window.submits++;this.disabled=true;document.querySelector('#continue').hidden=false">提交</button>
<button id="continue" hidden onclick="this.parentElement.remove();v.play()">继续学习</button></div>
<script>window.submits=0;window.nexts=0;window.asked=false;var v=document.querySelector('video');
v.ontimeupdate=()=>{if(!window.asked && v.currentTime>0.1){window.asked=true;v.pause();document.querySelector('[role=dialog]').style.display='block';}};
</script>'''
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route(DEFAULT_URL, lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        page = context.new_page()
        page.goto(DEFAULT_URL)
        page.locator('video').evaluate('(v, src)=>{v.src=src;v.play()}', src)
        adapter = CoursePage(page, {})
        self.assertFalse(adapter.next_lesson(), "未结束时不能切换章节")
        config = {"selectors": {}, "ai": {}, "poll_seconds": 0.05}
        original_read = CoursePage.read_quiz

        def read_with_transition(adapter):
            quiz = original_read(adapter)
            if show_after_scan and quiz is None and not page.evaluate('window.asked'):
                page.evaluate("window.asked=true;v.pause();document.querySelector('[role=dialog]').style.display='block'")
            return quiz

        reader_patch = patch.object(CoursePage, 'read_quiz', read_with_transition)
        reader_patch.start()
        self.addCleanup(reader_patch.stop)
        with patch('study_assistant.ask_ai', return_value=(0,)) as ai, patch('builtins.input', side_effect=AssertionError('不应需要手动介入')):
            run_loop(context, config)
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertEqual(page.evaluate('window.nexts'), 1)
        self.assertEqual(page.locator('#lessonOrder').inner_text(), '第二节')
        self.assertTrue(page.locator('video').evaluate('v=>v.ended'))
