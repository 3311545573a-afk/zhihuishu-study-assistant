import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
try:
    from study_assistant import load_config
except ImportError:
    load_config = None


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(load_config, "尚未实现配置读取")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.json'

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding='utf-8-sig')

    def test_environment_key_overrides_file(self):
        self.write({'course_url': 'https://studyvideoh5.zhihuishu.com/stuStudy', 'ai': {'api_key': 'old'}})
        with patch.dict(os.environ, {'ZHS_AI_API_KEY': 'test-key'}):
            self.assertEqual(load_config(self.path)['ai']['api_key'], 'test-key')

    def test_invalid_url(self):
        for url in ('http://studyvideoh5.zhihuishu.com/stuStudy', 'https://evil.example/stuStudy',
                    'https://studyvideoh5.zhihuishu.com.evil.example/stuStudy'):
            self.write({'course_url': url})
            with self.subTest(url=url), self.assertRaises(ValueError):
                load_config(self.path)

    def test_confidence_range(self):
        for value in (-1, 2, True, 'high'):
            self.write({'ai': {'min_confidence': value}})
            with self.subTest(value=value), self.assertRaises(ValueError):
                load_config(self.path)
