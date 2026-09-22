import unittest

try:
    from answer_engine import Question, parse_answer, match_bank
except ImportError:
    Question = parse_answer = match_bank = None


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


if __name__ == '__main__':
    unittest.main()
