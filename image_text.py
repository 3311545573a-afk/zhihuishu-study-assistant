"""读取题目原图，支持视觉公式转写和懒加载；不截图、不创建标签页。"""
import base64
from functools import lru_cache
import hashlib
from io import BytesIO
import logging
import math
from urllib.parse import unquote_to_bytes, urlsplit

from playwright.sync_api import Error as BrowserError, Locator
from answer_engine import transcribe_image

LOG = logging.getLogger('study')
MAX_IMAGE_BYTES = 10_000_000


class ImageNotReadyError(ValueError):
    """题目图片暂时没有可读取的原图。"""


@lru_cache(maxsize=1)
def _ocr_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
        # 不提前过滤低分行，否则可能把缺字的题目当成完整题目。
        return RapidOCR(text_score=0.0, use_angle_cls=False, width_height_ratio=5)
    except Exception as exc:
        raise ValueError('本地图片识别组件加载失败，请运行 .venv\\Scripts\\python.exe -m pip install -r requirements.txt') from exc


def recognize_image(data: bytes) -> str:
    engine = _ocr_engine()
    try:
        import numpy as np
        from PIL import Image, ImageOps
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError('图片为空或超过大小限制')
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > 8_000_000:
                raise ValueError('图片尺寸过大')
            rgba = source.convert('RGBA')
            background = Image.new('RGBA', rgba.size, 'white')
            background.alpha_composite(rgba)
            img = background.convert('RGB')
        # 小字先放大并补白；透明公式图片使用白底，避免黑底导致识别失败。
        if img.height < 90:
            img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
        img = ImageOps.expand(img, border=20, fill='white')
        lines, _ = engine(np.array(img))
    except Exception as exc:
        raise ValueError('题目图片读取或识别失败，请手动处理当前题目') from exc
    if not lines or any(not line[1].strip() or not math.isfinite(float(line[2]))
                        or float(line[2]) < 0.85 for line in lines):
        raise ValueError('题目图片文字识别不清晰，已停止自动提交，请手动处理当前题目')
    return '\n'.join(line[1].strip() for line in lines)


class ImageTextReader:
    def __init__(self, ai_config: dict | None = None):
        self.cache: dict[str, str] = {}
        self.ai_config = ai_config or {}

    def read(self, locator: Locator, exclude: str = '') -> str:
        """按 DOM 顺序拼接文字和图片文字，保留混合题干与选项的对应关系。"""
        # 已加载的离屏图片直接读取原图；仅未加载图片需要滚动触发网站懒加载。
        for image in locator.locator('img').all():
            eligible = image.evaluate("""(el, exclude) => {
                for (let node = el; node; node = node.parentElement) {
                    if (exclude && node.matches(exclude)) return false;
                    const style = getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden' ||
                        node.matches('script,style,input,svg')) return false;
                }
                return true;
            }""", exclude)
            if not eligible:
                continue
            loaded = image.evaluate('el => el.complete && el.naturalWidth > 0')
            if loaded:
                continue
            source = image.evaluate('el => el.currentSrc || el.src')
            if source.startswith('data:image/'):
                try:
                    header, payload = source.split(',', 1)
                    if ';base64' in header:
                        base64.b64decode(payload, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError('题目图片编码无效') from exc
            try:
                image.scroll_into_view_if_needed(timeout=3000)
                loaded = image.evaluate("""el => new Promise(resolve => {
                    const deadline=Date.now()+5000;
                    const poll=()=>{
                        if(el.complete && el.naturalWidth > 0) return resolve(true);
                        if(Date.now()>=deadline) return resolve(false);
                        setTimeout(poll,50);
                    }; poll();
                })""")
            except BrowserError:
                loaded = False
            if not loaded:
                raise ImageNotReadyError('题目图片滚动加载失败或暂不可用，请检查网络后继续')
        parts = locator.evaluate("""(root, exclude) => {
            const parts = [];
            const walk = node => {
                if (node.nodeType === Node.TEXT_NODE) { parts.push(node.textContent); return; }
                if (node.nodeType !== Node.ELEMENT_NODE) return;
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' ||
                    node.matches('script,style,input,svg') || (exclude && node.matches(exclude))) return;
                if (node.tagName === 'IMG') {
                    const src = node.currentSrc || node.src;
                    let data = '';
                    if (node.complete && node.naturalWidth && node.naturalWidth * node.naturalHeight <= 8000000) {
                        try {
                            const canvas = document.createElement('canvas');
                            canvas.width = node.naturalWidth; canvas.height = node.naturalHeight;
                            canvas.getContext('2d').drawImage(node, 0, 0);
                            data = canvas.toDataURL('image/png');
                        } catch (_) { /* 跨域图片改由浏览器请求上下文读取原图。 */ }
                    }
                    parts.push({src, data, loaded: node.complete && node.naturalWidth > 0});
                    return;
                }
                if (node.tagName === 'BR') parts.push('\\n');
                for (const child of node.childNodes) walk(child);
                if (['P', 'DIV', 'LI'].includes(node.tagName)) parts.push('\\n');
            };
            walk(root);
            return parts;
        }""", exclude)
        return ''.join(part if isinstance(part, str) else self._image_text(locator, part)
                       for part in parts).strip()

    def _image_text(self, locator: Locator, part: dict) -> str:
        if not part['loaded'] or not part['src']:
            raise ImageNotReadyError('题目图片尚未加载或暂不可用，请等待图片加载后继续')
        source = part['src']
        # 同源图片使用实际像素作为缓存键，跨域图片使用平台资源地址。
        key = hashlib.sha256((part['data'] or source).encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        data_url = part['data'] or (source if source.startswith('data:image/') else '')
        if data_url:
            try:
                header, payload = data_url.split(',', 1)
                data = (base64.b64decode(payload, validate=True) if ';base64' in header
                        else unquote_to_bytes(payload))
            except (ValueError, TypeError) as exc:
                raise ValueError('题目图片编码无效') from exc
        else:
            if urlsplit(source).scheme not in ('https', 'http'):
                raise ValueError('暂不支持此题目图片地址类型')
            try:
                response = locator.page.context.request.get(source, timeout=15000)
                try:
                    if not response.ok:
                        raise ImageNotReadyError('题目图片下载失败，请检查网络后继续')
                    data = response.body()
                finally:
                    response.dispose()
            except BrowserError:
                raise ImageNotReadyError('题目图片下载失败或超时，请检查网络后继续') from None
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError('题目图片为空或超过大小限制')
        if all(self.ai_config.get(k) for k in ('base_url', 'model', 'api_key')):
            LOG.info('正在识别题目原图（含数学公式）；首次识别需等待 AI 返回')
            try:
                text = transcribe_image(data, self.ai_config)
            except (OSError, SyntaxError):
                raise ValueError('题目图片损坏，无法进行视觉识别') from None
        else:
            text = recognize_image(data)
        if len(self.cache) >= 128:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = text
        LOG.info('已识别题目图片文字：%s', text)
        return text
