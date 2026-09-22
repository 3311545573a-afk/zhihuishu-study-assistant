import unittest
from playwright.sync_api import sync_playwright
try:
    from browser_adapter import CoursePage
except ImportError:
    CoursePage = None

HTML = '''<div id="lessonOrder">1.1 数据库</div><div id="vjs_container"><video></video></div>
<div role="dialog"><h3 class="question">SQL 查询使用哪个关键字？（单选题）</h3>
<label><input type="radio" name="q">SELECT</label>
<label><input type="radio" name="q">INSERT</label>
<button onclick="window.submits=(window.submits||0)+1;this.disabled=true;document.querySelector('#continue').hidden=false">提交</button>
<button id="continue" hidden onclick="this.parentElement.remove()">继续学习</button></div>'''


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.assertIsNotNone(CoursePage, "尚未实现浏览器适配器")
        self.page = self.browser.new_page()
        self.page.set_content(HTML)
        self.adapter = CoursePage(self.page, {"question": ".question"})
        self.addCleanup(self.page.close)

    def test_read_select_submit_continue(self):
        quiz = self.adapter.read_quiz()
        self.assertEqual(quiz.question.options, ("SELECT", "INSERT"))
        self.adapter.submit(quiz, (0,))
        self.assertTrue(self.page.locator('input').first.is_checked())
        self.assertEqual(self.page.evaluate('window.submits'), 1)
        with self.assertRaises(ValueError):
            self.adapter.submit(self.adapter.read_quiz(), (0,))
        self.assertTrue(self.adapter.continue_after_answer())
        self.assertIsNone(self.adapter.read_quiz())

    def test_unknown_modal_blocks_play(self):
        self.page.set_content('<div role="dialog">身份确认</div><video></video>')
        self.assertIsNone(self.adapter.read_quiz())
        self.assertTrue(self.adapter.has_dialog())
        self.assertFalse(self.adapter.resume())

    def test_question_changed_before_submit(self):
        quiz = self.adapter.read_quiz()
        self.page.locator('.question').evaluate('(el) => el.textContent="另一道题"')
        with self.assertRaises(ValueError):
            self.adapter.submit(quiz, (0,))
        self.assertIsNone(self.page.evaluate('window.submits'))

    def test_multiple_selection_clears_previous(self):
        self.page.set_content(HTML.replace('type="radio" name="q"', 'type="checkbox"').replace('单选题', '多选题'))
        self.page.locator('input').nth(1).check()
        quiz = self.adapter.read_quiz()
        self.assertTrue(quiz.question.multiple)
        self.adapter.submit(quiz, (0,))
        self.assertTrue(self.page.locator('input').nth(0).is_checked())
        self.assertFalse(self.page.locator('input').nth(1).is_checked())

    def test_custom_div_options_without_selection_state_are_rejected(self):
        self.page.set_content('<div role="dialog"><h3 class="question">单选题</h3><div class="opt">A</div><div class="opt">B</div><button>提交</button></div>')
        adapter = CoursePage(self.page, {"question": ".question", "option": ".opt"})
        quiz = adapter.read_quiz()
        with self.assertRaises(ValueError):
            adapter.submit(quiz, (0,))

    def test_multiple_radio_groups_are_not_treated_as_one_question(self):
        self.page.set_content(HTML.replace('<button onclick=', '<label><input type="radio" name="q2">第三项</label><label><input type="radio" name="q2">第四项</label><button onclick='))
        self.assertIsNone(self.adapter.read_quiz())

    def test_zhihuishu_ai_tutor_custom_judge_question(self):
        html = '''<div id="playTopic-dialog" style="display:block"><div class="el-dialog" role="dialog">
<div class="el-dialog__body"><div class="topic-title">【判断题】实体间的联系共有3种类型。</div>
<li class="topic-item"><span class="topic-option-item">A.</span><div class="item-topic">对</div></li>
<li class="topic-item"><span class="topic-option-item">B.</span><div class="item-topic">错</div></li></div>
<div class="el-dialog__footer"><span class="dialog-footer"><div class="btn">关闭</div></span></div></div></div>'''
        self.page.set_content(html)
        adapter = CoursePage(self.page, {})
        quiz = adapter.read_quiz()
        self.assertIsNotNone(quiz)
        self.assertEqual(quiz.question.options, ("对", "错"))
        self.assertFalse(quiz.question.multiple)
        self.page.locator('.topic-option-item').nth(0).evaluate("el => el.classList.add('active')")
        self.assertTrue(adapter.checked(quiz.options[0]))

    def test_zhihuishu_custom_question_selects_and_closes(self):
        html = '''<div id="playTopic-dialog" style="display:block"><div class="el-dialog" role="dialog">
<div class="el-dialog__body"><div class="topic-title">【判断题】实体间的联系共有3种类型。</div>
<li class="topic-item" onclick="this.querySelector('.topic-option-item').classList.add('active');document.querySelector('.answer').textContent='正确答案：A'"><span class="topic-option-item">A.</span><div class="item-topic">对</div></li>
<li class="topic-item"><span class="topic-option-item">B.</span><div class="item-topic">错</div></li><div class="answer"></div></div>
<div class="el-dialog__footer"><span class="dialog-footer"><div class="btn" onclick="this.closest('#playTopic-dialog').remove()">关闭</div></span></div></div></div>'''
        self.page.set_content(html)
        adapter = CoursePage(self.page, {})
        quiz = adapter.read_quiz()
        adapter.submit(quiz, (0,))
        self.assertTrue(self.page.locator('.topic-option-item').nth(0).evaluate("el => el.classList.contains('active')"))
        self.assertTrue(adapter.continue_after_answer())
        self.assertIsNone(adapter.read_quiz())

    def test_zhihuishu_answered_question_moves_to_next_before_close(self):
        self.page.set_content('''<div id="playTopic-dialog"><div class="el-dialog" role="dialog">
<p class="topic-title"><span class="right">正确</span>【判断题】第一题</p>
<li class="topic-item"><span class="topic-option-item active">A.</span><div class="item-topic active">对</div></li>
<li class="topic-item"><span class="topic-option-item">B.</span><div class="item-topic">错</div></li>
<p class="answer">正确答案：A</p>
<button class="btn-next" onclick="document.querySelector('.topic-title').textContent='【判断题】第二题';document.querySelector('.answer').remove();this.disabled=true">下一题</button>
<span class="dialog-footer"><div class="btn" onclick="window.closedQuiz=true">关闭</div></span></div></div>''')
        adapter = CoursePage(self.page, {})
        self.assertEqual(adapter.read_quiz().question.text, '【判断题】第一题')
        self.assertTrue(adapter.has_answer_feedback())
        self.assertTrue(adapter.continue_after_answer())
        self.assertEqual(adapter.read_quiz().question.text, '【判断题】第二题')
        self.assertIsNone(self.page.evaluate('window.closedQuiz'))
        self.assertFalse(adapter.continue_after_answer(), '不能关闭还没答完的下一题')

    def test_zhihuishu_multiselect_uses_active_children(self):
        self.page.set_content('''<div id="playTopic-dialog"><div class="el-dialog" role="dialog">
<p class="topic-title">【多选题】哪些是数据库？</p>
<li class="topic-item" onclick="this.querySelector('.topic-option-item').classList.toggle('active')"><span class="topic-option-item">A.</span><div class="item-topic">MySQL</div></li>
<li class="topic-item" onclick="this.querySelector('.topic-option-item').classList.toggle('active')"><span class="topic-option-item">B.</span><div class="item-topic">PostgreSQL</div></li>
<li class="topic-item" onclick="this.querySelector('.topic-option-item').classList.toggle('active')"><span class="topic-option-item active">C.</span><div class="item-topic">记事本</div></li>
</div></div>''')
        adapter = CoursePage(self.page, {})
        quiz = adapter.read_quiz()
        self.assertTrue(quiz.question.multiple)
        adapter.submit(quiz, (0, 1))
        self.assertEqual([adapter.checked(option) for option in quiz.options], [True, True, False])

    def test_next_waits_for_hover_controls_to_appear(self):
        self.page.set_content('''<div id="lessonOrder">第一节</div>
<div id="vjs_container" style="width:300px;height:100px" onmouseenter="setTimeout(()=>document.querySelector('#nextBtn').style.display='block',200)"><video></video>
<button id="nextBtn" style="display:none" onclick="window.nextClicked=true">下一节</button></div>''')
        self.page.locator('video').evaluate("v=>Object.defineProperty(v,'ended',{value:true})")
        self.assertTrue(self.adapter.next_lesson())
        self.assertTrue(self.page.evaluate('window.nextClicked'))


if __name__ == '__main__':
    unittest.main()
