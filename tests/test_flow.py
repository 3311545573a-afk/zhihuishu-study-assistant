"""用真正的 Chrome、短音频视频元素和模拟 API 验证完整控制循环。"""
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import wave

from playwright.sync_api import sync_playwright
from answer_engine import Question, ask_ai
from browser_adapter import CoursePage
from study_assistant import DEFAULT_URL, run_loop, wait_for_course_ready
from session_store import SessionStore


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

    def _exercise_flow(self, show_after_scan=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        session_patch = patch('study_assistant.SESSIONS', SessionStore(Path(temp.name) / 'state.json'))
        session_patch.start()
        self.addCleanup(session_patch.stop)
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
