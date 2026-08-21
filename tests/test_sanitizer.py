import unittest
from hangul_novel_translator.sanitizer import ExportSanitizer, SanitizerConfig, CustomRule

class TestSanitizer(unittest.TestCase):
    def setUp(self):
        self.sanitizer = ExportSanitizer()

    def test_strip_numbered_prefix(self):
        raw = "[1] 男人平静地解释着。"
        self.assertEqual(self.sanitizer.clean_paragraph(raw), "男人平静地解释着。")

        raw2 = "(12) 孩子大概是觉得照片里年幼的男孩很神奇。"
        self.assertEqual(self.sanitizer.clean_paragraph(raw2), "孩子大概是觉得照片里年幼的男孩很神奇。")

        raw3 = "3. 晨曦穿透了薄雾。"
        self.assertEqual(self.sanitizer.clean_paragraph(raw3), "晨曦穿透了薄雾。")

    def test_strip_json_key_residue(self):
        raw = '{"paragraphs": [ "今天天气真好。"'
        self.assertEqual(self.sanitizer.clean_paragraph(raw), "今天天气真好。")

        raw2 = '"p1": "夜色渐浓。"'
        self.assertEqual(self.sanitizer.clean_paragraph(raw2), "夜色渐浓。")

    def test_strip_outer_json_quotes_with_inner_chinese_quotes(self):
        raw = '"“新身份证的照片不是那张吧？”"'
        self.assertEqual(self.sanitizer.clean_paragraph(raw), "“新身份证的照片不是那张吧？”")

    def test_strip_outer_plain_quotes(self):
        raw = '"男人平静地解释着。孩子大概是觉得照片里年幼的男孩竟然是自己的父亲很神奇，一直在仔细端详。"'
        cleaned = self.sanitizer.clean_paragraph(raw)
        self.assertEqual(cleaned, "男人平静地解释着。孩子大概是觉得照片里年幼的男孩竟然是自己的父亲很神奇，一直在仔细端详。")

    def test_custom_rules(self):
        config = SanitizerConfig(
            custom_rules=[
                CustomRule(pattern=r"\[水印.*?\]", replace="", is_regex=True),
                CustomRule(pattern="广告测试", replace="正文内容", is_regex=False),
            ]
        )
        custom_sanitizer = ExportSanitizer(config)
        text = "[水印：某某小说网] 这是广告测试，请注意。"
        cleaned = custom_sanitizer.clean_paragraph(text)
        self.assertEqual(cleaned, "这是正文内容，请注意。")

if __name__ == '__main__':
    unittest.main()
