import io
import json
import unittest
import urllib.error
from unittest.mock import patch

try:
    from answer_engine import Question, _chat, api_error_message, match_bank, parse_answer
except ImportError:
    Question = parse_answer = match_bank = api_error_message = _chat = None


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Question, "尚未实现答案引擎")
        self.q = Question("SQL 查询使用哪个关键字？", ("SELECT", "INSERT"), False)

    def test_valid_answer(self):
        self.assertEqual(parse_answer('{"answers":[1],"confidence":0.95}', self.q, 0.8), (0,))

    def test_invalid_answers_are_rejected(self):
        for value in ('[0]', '[3]', '[1,1]', '[1,2]', '[]', '[true]', '[1.0]', '["1"]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_answer('{"answers":' + value + ',"confidence":0.95}', self.q, 0.8)

    def test_invalid_confidence(self):
        for value in ('0.2', 'true', 'NaN', '2', '"0.9"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_answer('{"answers":[1],"confidence":' + value + '}', self.q, 0.8)

    def test_multiselect(self):
        q = Question("哪些是 SQL 语句？", ("SELECT", "INSERT", "hello"), True)
        self.assertEqual(parse_answer('```json\n{"answers":[2,1],"confidence":0.9}\n```', q, 0.8), (0, 1))

    def test_bank_matches_option_text_after_shuffle(self):
        bank = [{"question": self.q.text, "answers": ["SELECT"]}]
        q = Question(self.q.text, ("INSERT", "SELECT"), False)
        self.assertEqual(match_bank(q, bank), (1,))
        self.assertIsNone(match_bank(Question("另一道题", q.options, False), bank))

    def test_ambiguous_bank_is_rejected(self):
        q = Question("同名选项", ("相同", "相同"), False)
        self.assertIsNone(match_bank(q, [{"question": q.text, "answers": ["相同"]}]))

    def test_fingerprint_includes_order_and_kind(self):
        self.assertNotEqual(self.q.fingerprint, Question(self.q.text, tuple(reversed(self.q.options)), False).fingerprint)
        self.assertNotEqual(self.q.fingerprint, Question(self.q.text, self.q.options, True).fingerprint)

    def test_malformed_json_is_rejected(self):
        for content in ('答案是 A', '[]', '{}', '{"answers": [1]'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_answer(content, self.q, 0.8)


class ApiErrorTests(unittest.TestCase):
    """401 之类的报错要带上接口自己的说明，否则分不清是密钥、额度还是地址的问题。"""

    CONFIG = {"base_url": "https://api.deepseek.com", "api_key": "sk-unittest-secret", "model": "deepseek-flash"}

    def setUp(self):
        self.assertIsNotNone(api_error_message, "尚未实现接口错误信息")

    def chat_with_error(self, error):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(ValueError) as caught:
                _chat([{"role": "user", "content": "hi"}], self.CONFIG)
        return str(caught.exception)

    def test_http_error_carries_api_message(self):
        body = io.BytesIO(json.dumps(
            {"error": {"message": "Authentication Fails, Your api key is invalid"}}).encode("utf-8"))
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 401, "Unauthorized", {}, body)
        message = self.chat_with_error(error)
        self.assertIn("HTTP 401", message)
        self.assertIn("Authentication Fails", message)
        self.assertNotIn(self.CONFIG["api_key"], message)

    def test_http_error_without_body_stays_short(self):
        error = urllib.error.HTTPError(
            "https://api.deepseek.com/chat/completions", 402, "Payment Required", {}, None)
        self.assertEqual(self.chat_with_error(error),
                         "AI 接口 HTTP 402；请检查模型、密钥、额度和接口地址")

    def test_long_html_body_is_truncated(self):
        body = io.BytesIO(b"<html>" + b"x" * 900 + b"</html>")
        error = urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 502,
                                       "Bad Gateway", {}, body)
        message = self.chat_with_error(error)
        self.assertIn("HTTP 502", message)
        self.assertLess(len(message), 420)

    def test_plain_text_body_is_included(self):
        self.assertIn("quota exceeded", api_error_message(429, "quota exceeded"))

    def test_error_type_field_is_used_when_message_missing(self):
        self.assertIn("insufficient_quota",
                      api_error_message(429, json.dumps({"error": {"type": "insufficient_quota"}})))


if __name__ == '__main__':
    unittest.main()
