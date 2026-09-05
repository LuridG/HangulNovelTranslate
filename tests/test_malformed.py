"""畸形块归一化 / 连叠引号折叠的单元测试。"""

from __future__ import annotations

import unittest

from hangul_novel_translator.sanitizer import ExportSanitizer, SanitizerConfig
from hangul_novel_translator.translator import (
    _looks_like_json_fragment,
    normalize_completed_paragraphs,
    sanitize_committed_paragraphs,
)
from hangul_novel_translator.utils import coerce_raw_to_paragraphs


class CollapseQuoteTest(unittest.TestCase):
    def setUp(self):
        self.s = ExportSanitizer(
            SanitizerConfig(
                enabled=True,
                strip_numbers=True,
                strip_json_residue=True,
                fix_quotes=True,
                polish_punctuation=True,
                collapse_quotes=True,
            )
        )

    def test_four_quotes_collapsed(self):
        self.assertEqual(self.s.clean_paragraph('"““““……明白了。””””",'), "“……明白了。”")

    def test_double_nested_quotes_collapsed(self):
        self.assertEqual(self.s.clean_paragraph("“““““是的。”””””"), "“是的。”")

    def test_json_element_residue_stripped(self):
        self.assertEqual(
            self.s.clean_paragraph('"“既然推翻了处分程序。”",'),
            "“既然推翻了处分程序。”",
        )
        self.assertEqual(self.s.clean_paragraph('"普通文本",'), "普通文本")

    def test_normal_paragraph_untouched(self):
        self.assertEqual(self.s.clean_paragraph("“……明白了。”"), "“……明白了。”")


class MalformedNormalizeTest(unittest.TestCase):
    def test_blob_collapsed_recovers_paragraphs(self):
        # 整段 JSON 塌缩进首段，其余为空串。
        blob = '{"paragraphs": ["“你好，伊森。”", "“……嗯。”", "那么，我们走吧。"]}'
        values = [blob, "", "", ""]
        paras, status = normalize_completed_paragraphs(
            values, expected_count=3, source_paragraphs=[], fill_from_source=False
        )
        nonempty = [p for p in paras if p.strip()]
        self.assertIn(status, ("recovered", "cleaned"))
        self.assertGreaterEqual(len(nonempty), 2)
        self.assertIn("你好，伊森。", nonempty[0])

    def test_fragment_list_recovers(self):
        values = ['{"paragraphs": [', '"第一段",', '"第二段",', '"第三段"]}']
        self.assertTrue(_looks_like_json_fragment(values))
        paras, status = normalize_completed_paragraphs(
            values, expected_count=3, source_paragraphs=[], fill_from_source=False
        )
        nonempty = [p for p in paras if p.strip()]
        self.assertEqual(nonempty[0], "第一段")
        self.assertIn(status, ("recovered", "cleaned"))

    def test_clean_list_stays_ok(self):
        paras, status = normalize_completed_paragraphs(
            ["“你好。”", "“……嗯。”"], expected_count=2, source_paragraphs=[], fill_from_source=False
        )
        self.assertEqual(status, "ok")
        self.assertEqual(paras, ["“你好。”", "“……嗯。”"])

    def test_unrecoverable_truncated_blob(self):
        values = ['{"paragraphs":', "", ""]
        self.assertFalse(_looks_like_json_fragment(values))
        paras, status = normalize_completed_paragraphs(
            values, expected_count=2, source_paragraphs=[], fill_from_source=False
        )
        self.assertFalse(any(p.strip() for p in paras))
        self.assertEqual(status, "unresolved")

    def test_committed_cleans_quotes_and_residue(self):
        para = sanitize_committed_paragraphs(['"““““是。””””",', '"第二段",'])
        self.assertEqual(para[0], "“是。”")
        self.assertEqual(para[1], "第二段")


class RawResponseCoerceTest(unittest.TestCase):
    def test_json_blob_recovers(self):
        raw = '{"paragraphs": ["你好。", "再见。"]}'
        self.assertEqual(coerce_raw_to_paragraphs(raw), ["你好。", "再见。"])

    def test_plain_text_splits_lines(self):
        raw = "第一段\n第二段\n\n第三段"
        self.assertEqual(coerce_raw_to_paragraphs(raw), ["第一段", "第二段", "第三段"])

    def test_empty_returns_empty(self):
        self.assertEqual(coerce_raw_to_paragraphs(""), [])
        self.assertEqual(coerce_raw_to_paragraphs("   \n\n"), [])


if __name__ == "__main__":
    unittest.main()
