"""以用户截图中的图片题复现空选项，并验证原有提交/关闭流程。"""
import base64
from pathlib import Path
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from playwright.sync_api import Locator, sync_playwright
from browser_adapter import CoursePage
import image_text


FIXTURES = Path(__file__).parent / 'fixtures' / 'image_quiz'


def image_tag(name):
    data = base64.b64encode((FIXTURES / f'{name}.png').read_bytes()).decode()
    return f'<img src="data:image/png;base64,{data}">'


def quiz_html():
    options = ''.join(
        '<li class="topic-item" onclick="this.querySelector(\'.topic-option-item\').classList.add(\'active\');'
        'document.querySelector(\'.answer\').textContent=\'正确答案：A\'">'
        f'<span class="topic-option-item">{letter.upper()}.</span>'
        f'<div class="item-topic">{image_tag(letter)}</div></li>' for letter in 'abc')
    return ('<div id="lessonOrder">1.1.2、函数的基本性质</div>'
            '<div id="playTopic-dialog"><div class="el-dialog" role="dialog">'
            f'<p class="topic-title">【单选题】{image_tag("question")}</p>' + options +
            '<li class="topic-item"><span class="topic-option-item">D.</span>'
            '<div class="item-topic">非单调函数可以在某个区间内单调</div></li>'
            '<p class="answer"></p><span class="dialog-footer">'
            '<div class="btn" onclick="this.closest(\'#playTopic-dialog\').remove()">关闭</div>'
            '</span></div></div>')


class ImageQuizTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.set_content(quiz_html())
        self.adapter = CoursePage(self.page, {})

    def test_screenshot_image_question_selects_and_closes(self):
        quiz = self.adapter.read_quiz()
        self.assertIsNotNone(quiz, '图片题必须能被识别，不能丢弃空 innerText 选项')
        self.assertIn('关于函数的描述', quiz.question.text)
        self.assertIn('以下错误的是', quiz.question.text)
        self.assertIn('周期函数一定有最小正周期', quiz.question.options[0])
        self.assertIn('奇函数', quiz.question.options[1])
        self.assertIn('偶函数与奇函数的乘积为奇函数', quiz.question.options[2])
        self.assertEqual(len(quiz.question.options), 4)
        self.adapter.submit(quiz, (0,))
        self.assertTrue(self.adapter.has_answer_feedback())
        self.assertTrue(self.adapter.continue_after_answer())
        self.assertIsNone(self.adapter.read_quiz())

    def test_changed_image_cancels_submission(self):
        quiz = self.adapter.read_quiz()
        self.assertIsNotNone(quiz)
        self.page.locator('.item-topic img').first.evaluate(
            '(el, src) => el.src=src', self.page.locator('.item-topic img').nth(2).get_attribute('src'))
        self.page.wait_for_function('Array.from(document.images).every(i => i.complete)')
        with self.assertRaisesRegex(ValueError, '题目发生变化'):
            self.adapter.submit(quiz, (0,))
        self.assertFalse(self.adapter.has_answer_feedback())

    def test_broken_image_does_not_submit_partial_text(self):
        self.page.locator('.item-topic').first.evaluate(
            "el => el.innerHTML='部分文字<img src=\"data:image/png;base64,bm90LXBuZw==\">'")
        with self.assertRaisesRegex(ValueError, '图片'):
            self.adapter.read_quiz()
        self.assertFalse(self.adapter.has_answer_feedback())

    def test_repeated_checks_do_not_take_screenshots(self):
        # OCR 直接读取题目图片，避免轮询截图或滚动网页导致闪屏。
        with patch.object(Locator, 'screenshot', side_effect=AssertionError('不能截图')), \
                patch.object(self.page.context, 'new_page', side_effect=AssertionError('不能新建页面')):
            quiz = self.adapter.read_quiz()
            self.assertIsNotNone(quiz)
            with patch('image_text.recognize_image', side_effect=AssertionError('不能重复 OCR')):
                self.assertEqual(self.adapter.read_quiz().question, quiz.question)

    def test_cross_origin_image_is_fetched_once_without_navigation(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.end_headers()
                self.wfile.write((FIXTURES / 'a.png').read_bytes())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.page.set_content(f'<div id="text"><img src="http://127.0.0.1:{server.server_port}/option.png"></div>')
            loc = self.page.locator('#text')
            text = self.adapter.image_text.read(loc)
            self.assertEqual(text, '周期函数一定有最小正周期')
            self.assertEqual(self.adapter.image_text.read(loc), text)
            self.assertEqual(len(requests), 2, '浏览器显示一次，OCR 下载一次，后续读取复用缓存')
            self.assertEqual(self.page.url, 'about:blank')
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_scroll_container_reads_all_options_and_clicks_bottom_option(self):
        self.page.add_style_tag(content='.el-dialog {height:140px;overflow:auto} .topic-item {height:180px}')
        # 四个选项已加载，即使后三个在滚动区域外也必须完整读取。
        quiz = self.adapter.read_quiz()
        self.assertEqual(len(quiz.options), 4)
        self.page.locator('.topic-item').nth(3).evaluate("el=>el.onclick=()=>el.querySelector('span').classList.add('active')")
        self.adapter.submit(quiz, (3,))
        self.assertTrue(self.adapter.checked(quiz.options[3]))
        self.assertGreater(self.page.locator('.el-dialog').evaluate('el=>el.scrollTop'), 0)

    def test_scroll_loads_lazy_image_before_recognition(self):
        self.page.set_content('<div style="height:120px;overflow:auto" id="scroll">'
                              '<div style="height:1000px"></div><div id="option">'
                              + image_tag('a').replace('src=', 'data-src=') + '</div></div>')
        self.page.evaluate("""() => {
            const img=document.querySelector('img'); img.width=395; img.height=47;
            const observer=new IntersectionObserver(entries=>{
                if(entries.some(e=>e.isIntersecting)){img.src=img.dataset.src;observer.disconnect();}
            },{root:document.querySelector('#scroll')}); observer.observe(img);
        }""")
        try:
            text = self.adapter.image_text.read(self.page.locator('#option'))
        except ValueError as exc:
            self.fail(f'必须先滚动触发懒加载再读取：{exc}')
        self.assertEqual(text, '周期函数一定有最小正周期')

    def test_zero_size_displayed_image_waits_for_late_source(self):
        source = image_tag('a').split('src="', 1)[1].split('"', 1)[0]
        self.page.set_content('<div id="text">选项<img style="width:0;height:0"></div>')
        self.page.evaluate("(src) => setTimeout(() => document.querySelector('img').src = src, 100)", source)
        self.assertIn('周期函数一定有最小正周期',
                      self.adapter.image_text.read(self.page.locator('#text')))

    def test_hidden_and_excluded_images_do_not_block_reading(self):
        self.page.set_content('<div id="text">有效文字'
                              '<div style="display:none"><img src="broken"></div>'
                              '<div class="skip"><img src="broken"></div></div>')
        self.assertEqual(self.adapter.image_text.read(self.page.locator('#text'), '.skip'), '有效文字')

    def test_unavailable_image_uses_recoverable_exception(self):
        self.page.set_content('<div id="text"><img style="width:0;height:0"></div>')
        with self.assertRaises(ValueError) as caught:
            self.adapter.image_text.read(self.page.locator('#text'))
        self.assertIsInstance(caught.exception, image_text.ImageNotReadyError)

    def test_invalid_image_encoding_is_not_retried(self):
        self.page.set_content('<div id="text"><img src="data:image/png;base64,***"></div>')
        with self.assertRaises(ValueError) as caught:
            self.adapter.image_text.read(self.page.locator('#text'))
        self.assertNotIsInstance(caught.exception, image_text.ImageNotReadyError)

    def test_configured_vision_reads_formula_and_caches_result(self):
        import inspect
        self.assertIn('ai_config', inspect.signature(CoursePage).parameters)
        cfg = {'base_url':'https://example.test','model':'vision','api_key':'secret'}
        adapter = CoursePage(self.page, {}, ai_config=cfg)
        with patch('image_text.transcribe_image', return_value='公式 x^2，-2≤x<0') as vision, \
                patch('image_text.recognize_image', side_effect=AssertionError('不能用普通 OCR 读公式')):
            quiz = adapter.read_quiz()
            self.assertIn('x^2', quiz.question.text)
            self.assertEqual(adapter.read_quiz().question, quiz.question)
            self.assertEqual(vision.call_count, 4)


if __name__ == '__main__':
    unittest.main()
