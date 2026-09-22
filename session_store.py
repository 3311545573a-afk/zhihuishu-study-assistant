"""补充持久浏览器目录：保存 Chrome 重启可能丢失的会话 Cookie 和 Web Storage。"""
import json
import logging
from pathlib import Path
import time
from urllib.parse import urlsplit
from playwright.sync_api import Error

LOG = logging.getLogger('study')


def allowed_host(host: str) -> bool:
    host = host.lstrip('.')
    return host == 'zhihuishu.com' or host.endswith('.zhihuishu.com')


class SessionStore:
    def __init__(self, path: Path):
        self.path = path

    def save(self, context) -> bool:
        try:
            course = [p for p in context.pages if not p.is_closed()
                      and urlsplit(p.url).hostname == 'studyvideoh5.zhihuishu.com'
                      and p.locator('#lessonOrder').count() and p.locator('video').count()]
            if not course:
                return False
            # storage_state() 会为已访问但已关闭的域名创建临时页，有头 Chrome 因此闪屏。
            # 只读取现有页/框架；未打开域名沿用上次备份，禁止为采集而导航或新建标签页。
            previous = {}
            if self.path.exists():
                try:
                    previous = json.loads(self.path.read_text(encoding='utf-8'))
                    if not isinstance(previous, dict):
                        previous = {}
                except (ValueError, OSError):
                    LOG.warning('旧会话备份无法读取，将从当前已登录页面重新保存')
            origins = {o['origin']: o for o in previous.get('origins', [])
                       if allowed_host(urlsplit(o['origin']).hostname or '')}
            sessions = {origin: items for origin, items in previous.get('session_storage', {}).items()
                        if allowed_host(urlsplit(origin).hostname or '')}
            state = {'cookies': [c for c in context.cookies() if allowed_host(c['domain'])]}
            for page in context.pages:
                if page.is_closed():
                    continue
                for frame in page.frames:
                    if not allowed_host(urlsplit(frame.url).hostname or ''):
                        continue
                    try:
                        data = frame.evaluate('''() => ({
                            origin: location.origin,
                            localStorage: Object.keys(localStorage).map(name => ({name, value: localStorage.getItem(name)})),
                            sessionStorage: Object.fromEntries(Object.entries(sessionStorage))
                        })''')
                        origin = data['origin']
                        if not allowed_host(urlsplit(origin).hostname or ''):
                            continue
                        origins[origin] = {'origin': origin, 'localStorage': data['localStorage']}
                        sessions[origin] = data['sessionStorage']
                    except Error:
                        # 页面恰好跳转或受限框架不可读时，保留该域名的旧备份。
                        continue
            state['origins'] = list(origins.values())
            state['session_storage'] = sessions
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.path)
            return True
        except (Error, OSError):
            LOG.warning('本次登录状态保存失败，已有备份保持不变')
            return False

    def restore(self, context) -> bool:
        if not self.path.exists():
            return False
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
            existing = {(c['domain'], c['path'], c['name']) for c in context.cookies()}
            cookies = [c for c in state.get('cookies', [])
                       if allowed_host(c['domain']) and (c['domain'], c['path'], c['name']) not in existing
                       and (c.get('expires', -1) == -1 or c['expires'] > time.time())]
            if cookies:
                context.add_cookies(cookies)
            local = {o['origin']: o.get('localStorage', []) for o in state.get('origins', [])
                     if allowed_host(urlsplit(o['origin']).hostname or '')}
            session = {o: items for o, items in state.get('session_storage', {}).items()
                       if allowed_host(urlsplit(o).hostname or '')}
            data = json.dumps({'local': local, 'session': session}, ensure_ascii=True)
            context.add_init_script('''(() => {
                const saved = ''' + data + ''';
                try {
                    for (const item of saved.local[location.origin] || []) {
                        if (localStorage.getItem(item.name) === null) localStorage.setItem(item.name, item.value);
                    }
                    for (const [key, value] of Object.entries(saved.session[location.origin] || {})) {
                        if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, value);
                    }
                } catch (_) {}
            })();''')
            return True
        except (Error, OSError, ValueError, TypeError, KeyError):
            LOG.warning('登录状态备份无法恢复，请在浏览器重新登录')
            return False
