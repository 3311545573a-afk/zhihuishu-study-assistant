import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import answer_engine


class VisionTests(unittest.TestCase):
    def transcribe(self, content):
        fn = getattr(answer_engine, 'transcribe_image', None)
        self.assertIsNotNone(fn, '需要支持题目原图的视觉转写接口')
        response = io.BytesIO(json.dumps({'choices':[{'message':{'content':content}}]}).encode())
        cfg = {'base_url':'https://example.test','api_key':'secret','model':'vision'}
        with patch('answer_engine.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            image = Path(__file__).parent / 'fixtures' / 'formula_quiz' / 'question.png'
            text = fn(image.read_bytes(), cfg)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
            self.assertEqual(payload['messages'][1]['content'][1]['type'], 'image_url')
            self.assertNotIn('secret', json.dumps(payload))
            return text

    def test_formula_transcription_preserves_math(self):
        expected = r'f(x)=\begin{cases}x^2,&-2\le x<0\\2,&x=0\\1+x,&0<x\le3\end{cases}'
        self.assertEqual(self.transcribe(json.dumps({'text':expected,'confidence':0.98})), expected)

    def test_incomplete_or_low_confidence_transcription_is_rejected(self):
        for data in ({'text':'','confidence':0.99}, {'text':'猜测','confidence':0.3},
                     {'text':'x','confidence':True}, {'text':'x','confidence':2}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.transcribe(json.dumps(data))
