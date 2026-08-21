import unittest
import re

def sanitize_paragraph(text: str) -> str:
    # 1. Strip prompt/json residue like {"paragraphs": [ or [1], (1)
    text = re.sub(r'^\s*\{?\"(?:paragraphs|translation|text|content|p\d*)\"\s*:\s*\[?\s*', '', text)
    text = re.sub(r'^\s*\[\d+\]\s*', '', text)
    text = re.sub(r'^\s*\(\d+\)\s*', '', text)
    
    # 2. Strip surrounding unclosed quotes left from JSON splitting
    if text.startswith('"') and not text.endswith('"'):
        if text.count('"') % 2 != 0:
            text = text[1:].lstrip()
            
    # Check if text is enclosed in double quotes with dialogue quotes inside:
    # e.g. "“新身份证的照片不是那张吧？”"
    if text.startswith('"“') and text.endswith('”"'):
        text = text[1:-1]
    elif text.startswith('"') and text.endswith('"') and text.count('"') == 2:
        if not (text.startswith('"') and text.endswith('"') and any(c in text[1:-1] for c in ['“', '”'])):
            text = text[1:-1]
            
    return text.strip()

class TestSanitizer(unittest.TestCase):
    def test_strip_numbered_prefix(self):
        raw = "[1] 男人平静地解释着。"
        self.assertEqual(sanitize_paragraph(raw), "男人平静地解释着。")

        raw2 = "(12) 孩子大概是觉得照片里年幼的男孩很神奇。"
        self.assertEqual(sanitize_paragraph(raw2), "孩子大概是觉得照片里年幼的男孩很神奇。")

    def test_strip_json_key_residue(self):
        raw = '{"paragraphs": [ "今天天气真好。"'
        self.assertEqual(sanitize_paragraph(raw), '今天天气真好。')

    def test_strip_outer_json_quotes_with_inner_chinese_quotes(self):
        raw = '"“新身份证的照片不是那张吧？”"'
        self.assertEqual(sanitize_paragraph(raw), '“新身份证的照片不是那张吧？”')

    def test_strip_outer_plain_quotes(self):
        raw = '"男人平静地解释着。孩子大概是觉得照片里年幼的男孩竟然是自己的父亲很神奇，一直在仔细端详。"'
        cleaned = sanitize_paragraph(raw)
        self.assertEqual(cleaned, '男人平静地解释着。孩子大概是觉得照片里年幼的男孩竟然是自己的父亲很神奇，一直在仔细端详。')

if __name__ == '__main__':
    unittest.main()
