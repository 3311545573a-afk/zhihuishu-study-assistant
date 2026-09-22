import tempfile
import json
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
try:
    from session_store import SessionStore
except ImportError:
    SessionStore = None

URL = 'https://studyvideoh5.zhihuishu.com/stuStudy'


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.assertIsNotNone(SessionStore, '尚未实现显式会话保存')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'state.json')

    def context(self):
        context = self.browser.new_context()
        context.route('https://studyvideoh5.zhihuishu.com/**', lambda route: route.fulfill(
            body='<div id="lessonOrder">lesson</div><video></video>', content_type='text/html'))
        self.addCleanup(context.close)
        return context

    def test_restores_session_cookie_and_web_storage_without_extending_expiry(self):
        first = self.context()
        first.add_cookies([{'name': 'test-session', 'value': 'sample-only', 'domain': '.zhihuishu.com', 'path': '/', 'secure': True}])
        page = first.new_page()
        page.goto(URL)
        page.evaluate("localStorage.setItem('local-test','l');sessionStorage.setItem('session-test','s')")
        self.assertTrue(self.store.save(first))
        second = self.context()
        self.assertTrue(self.store.restore(second))
        cookie = next(c for c in second.cookies() if c['name'] == 'test-session')
        self.assertEqual(cookie['expires'], -1)
        restored = second.new_page()
        restored.goto(URL)
        self.assertEqual(restored.evaluate("[localStorage.getItem('local-test'),sessionStorage.getItem('session-test')]"), ['l', 's'])
        restored.evaluate("localStorage.setItem('local-test','new')")
        restored.reload()
        self.assertEqual(restored.evaluate("localStorage.getItem('local-test')"), 'new')

    def test_logged_out_page_does_not_overwrite_saved_session(self):
        context = self.context()
        self.store.path.write_text('previous', encoding='utf-8')
        self.assertFalse(self.store.save(context))
        self.assertEqual(self.store.path.read_text(encoding='utf-8'), 'previous')

    def test_restore_preserves_existing_cookie(self):
        first = self.context()
        first.add_cookies([{'name': 'auth', 'value': 'old', 'domain': '.zhihuishu.com', 'path': '/'}])
        first.new_page().goto(URL)
        self.store.save(first)
        second = self.context()
        second.add_cookies([{'name': 'auth', 'value': 'new', 'domain': '.zhihuishu.com', 'path': '/'}])
        self.store.restore(second)
        self.assertEqual(next(c['value'] for c in second.cookies() if c['name'] == 'auth'), 'new')

    def test_save_after_login_redirect_does_not_open_or_navigate_tabs(self):
        context = self.context()
        context.route('https://login.zhihuishu.com/**', lambda route: route.fulfill(body='<html></html>'))
        page = context.new_page()
        page.goto('https://login.zhihuishu.com/')
        page.evaluate("localStorage.setItem('login-marker','test')")
        page.goto(URL)
        opened, navigated = [], []
        # storage_state 的临时页被 Playwright 从 context.pages 隐藏，需监听浏览器目标。
        cdp = self.browser.new_browser_cdp_session()
        self.addCleanup(cdp.detach)
        cdp.send('Target.setDiscoverTargets', {'discover': True})
        cdp.on('Target.targetCreated', lambda event: opened.append(event['targetInfo']['type'])
               if event['targetInfo']['type'] == 'page' else None)
        context.on('page', lambda new_page: opened.append(new_page))
        page.on('framenavigated', lambda frame: navigated.append(frame.url))
        for _ in range(3):
            self.assertTrue(self.store.save(context))
        self.assertEqual(opened, [], '定时保存不能临时新建标签页，否则会闪屏')
        self.assertEqual(navigated, [], '定时保存不能导航已有课程页')
        self.assertEqual(page.url, URL)

    def test_save_updates_live_storage_and_retains_unopened_saved_origin(self):
        context = self.context()
        page = context.new_page()
        page.goto(URL)
        page.evaluate("localStorage.setItem('new','value');sessionStorage.setItem('tab','fresh')")
        self.store.path.write_text(json.dumps({
            'cookies': [],
            'origins': [
                {'origin': 'https://login.zhihuishu.com', 'localStorage': [{'name': 'login', 'value': 'saved'}]},
                {'origin': 'https://studyvideoh5.zhihuishu.com', 'localStorage': [{'name': 'old', 'value': 'removed'}]},
                {'origin': 'https://unrelated.example', 'localStorage': [{'name': 'private', 'value': 'ignore'}]},
            ],
            'session_storage': {'https://login.zhihuishu.com': {'login-tab': 'saved'}},
        }), encoding='utf-8')
        self.assertTrue(self.store.save(context))
        state = json.loads(self.store.path.read_text(encoding='utf-8'))
        origins = {o['origin']: o['localStorage'] for o in state['origins']}
        self.assertEqual(origins['https://login.zhihuishu.com'], [{'name': 'login', 'value': 'saved'}])
        self.assertEqual(origins['https://studyvideoh5.zhihuishu.com'], [{'name': 'new', 'value': 'value'}])
        self.assertNotIn('https://unrelated.example', origins)
        self.assertEqual(state['session_storage']['https://studyvideoh5.zhihuishu.com'], {'tab': 'fresh'})
        self.assertEqual(state['session_storage']['https://login.zhihuishu.com'], {'login-tab': 'saved'})
