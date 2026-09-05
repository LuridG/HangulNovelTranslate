import re
import tempfile
import unittest
from pathlib import Path

from hangul_novel_translator.book import load_book
from hangul_novel_translator.book import parse_txt_with_patterns
from hangul_novel_translator.book import preview_txt_chapters
from hangul_novel_translator.config import AppConfig

_EXTENDED_PATTERNS = [
    re.compile(r"^\s*(?:제\s*)?(?:\d{1,4}|[一二三四五六七八九十百千零〇]+)\s*(?:장|화|부|편|권|막)\b", re.IGNORECASE),
    re.compile(r"^\s*(?:프롤로그|에필로그|서문|발문|후기|외전|번외\s*편?|side\s*story|chapter\s*\d+|prologue|epilogue)\s*[:.]?", re.IGNORECASE),
    # 扩展非标前缀：#01, EP.01, [1화], 〈01〉
    re.compile(r"^\s*(?:#|EP\.?|No\.)\s*\d+\b", re.IGNORECASE),
    re.compile(r"^\s*\[\s*\d+\s*(?:화|장)?\s*\]", re.IGNORECASE),
    re.compile(r"^\s*〈\s*\d+\s*〉", re.IGNORECASE),
]


def looks_like_heading_extended(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90:
        return False
    for pat in _EXTENDED_PATTERNS:
        if pat.match(line):
            return True
    return False


def unwrap_broken_paragraphs(lines: list[str]) -> list[str]:
    """对应 docs/txt_input_enhancements_plan.md 中的硬换行合并逻辑。"""
    paragraphs = []
    current_para = []
    end_puncts = (".", "!", "?", "\"", "”", "…", "〜", "~")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
            continue

        if not current_para:
            current_para.append(stripped)
        else:
            prev = current_para[-1]
            # 如果上一行以标点结尾，或者当前行是对话开头（“ 或 "），则开启新段
            if prev.endswith(end_puncts) or stripped.startswith(("“", '"', "「", "『")):
                paragraphs.append(" ".join(current_para))
                current_para = [stripped]
            else:
                # 中间生硬折行，自动连接
                current_para.append(stripped)

    if current_para:
        paragraphs.append(" ".join(current_para))
    return paragraphs


class TestTxtEnhancements(unittest.TestCase):
    def test_extended_headings(self):
        self.assertTrue(looks_like_heading_extended("#01"))
        self.assertTrue(looks_like_heading_extended("EP.12 새로운 시작"))
        self.assertTrue(looks_like_heading_extended("[1화] 각성"))
        self.assertTrue(looks_like_heading_extended("〈05〉 운명의 날"))
        self.assertTrue(looks_like_heading_extended("제1장 만남"))
        self.assertFalse(looks_like_heading_extended("그는 조용히 고개를 끄덕였다."))

    def test_unwrap_broken_lines(self):
        raw_lines = [
            "그는 책을 펼쳐 들고",
            "한참 동안이나",
            "글자를 들여다보았다.", # 这三行应该合为一段
            "",
            "“이게 정말 사실인가?”",
            "동식이 나직하게 물었다.",
        ]
        unwrapped = unwrap_broken_paragraphs(raw_lines)
        self.assertEqual(len(unwrapped), 3)
        self.assertEqual(unwrapped[0], "그는 책을 펼쳐 들고 한참 동안이나 글자를 들여다보았다.")
        self.assertEqual(unwrapped[1], "“이게 정말 사실인가?”")
        self.assertEqual(unwrapped[2], "동식이 나직하게 물었다.")


class TestTxtPatternParsing(unittest.TestCase):
    def _write(self, content: str) -> Path:
        fd, name = tempfile.mkstemp(suffix=".txt")
        with open(name, "w", encoding="utf-8") as handle:
            handle.write(content)
        return Path(name)

    def test_korean_chapter_regex_splits(self):
        path = self._write(
            "제1장 시작\n첫 문단입니다.\n둘째 문단입니다.\n제2장 전개\n두 번째 장의 문단입니다.\n"
        )
        book = parse_txt_with_patterns(path, [r"^\s*제\s*\d+\s*장"])
        self.assertEqual(len(book.chapters), 2)
        self.assertEqual(book.chapters[0].title, "제1장 시작")
        self.assertEqual(book.chapters[1].title, "제2장 전개")
        self.assertEqual(book.chapters[0].paragraphs[0], "첫 문단입니다.")

    def test_multiple_patterns_in_order(self):
        path = self._write(
            "프롤로그 인사\n여기서 이야기가 시작된다.\nChapter 1 만남\n첫 만남의 장면이 펼쳐진다.\n"
            "1화 각성\n각성하는 순간이다.\n"
        )
        book = parse_txt_with_patterns(
            path,
            [r"^\s*(?:프롤로그|에필로그)\b", r"^\s*chapter\s*\d+\b", r"^\s*\d+\s*화\b"],
        )
        titles = [ch.title for ch in book.chapters]
        self.assertEqual(titles, ["프롤로그 인사", "Chapter 1 만남", "1화 각성"])

    def test_no_match_falls_back(self):
        path = self._write("그냥 본문이다.\n그래서 계속 이어진다.\n" * 20)
        book = parse_txt_with_patterns(path, [r"^\s*제\s*\d+\s*장"])
        # 无标题命中时按字数切块，至少出一个章节。
        self.assertGreaterEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].title.startswith("第"), True)

    def test_invalid_pattern_skipped(self):
        path = self._write("제1장 시작\n본문입니다.\n")
        book = parse_txt_with_patterns(path, [r"(未闭合", r"^\s*제\s*\d+\s*장"])
        self.assertEqual(len(book.chapters), 1)
        self.assertEqual(book.chapters[0].title, "제1장 시작")

    def test_preview_txt_chapters(self):
        path = self._write("EP.1 첫 장면\n내용입니다.\nEP.2 두 번째\n내용2입니다.\n")
        titles = preview_txt_chapters(path, [r"^\s*EP\.?\s*\d+\b"])
        self.assertEqual(titles, ["EP.1 첫 장면", "EP.2 두 번째"])

    def test_load_book_passes_patterns(self):
        path = self._write("5화 중반\n본문입니다.\n6화 결말\n본문2입니다.\n")
        book = load_book(path, txt_patterns=[r"^\s*\d+\s*화\b"])
        self.assertEqual(len(book.chapters), 2)
        self.assertEqual(book.chapters[1].title, "6화 결말")

    def test_drop_zero_chapters_removes_empty(self):
        path = self._write("제1장 시작\n" + "본문입니다. " * 20 + "\n제2장 빈장\n제3장 전개\n" + "본문내용입니다. " * 20 + "\n")
        book = load_book(path, txt_patterns=[r"^\s*제\s*\d+\s*장"])
        self.assertEqual(len(book.chapters), 3)
        dropped = load_book(path, txt_patterns=[r"^\s*제\s*\d+\s*장"], drop_zero=True)
        self.assertEqual(len(dropped.chapters), 2)
        titles = [ch.title for ch in dropped.chapters]
        self.assertEqual(titles, ["제1장 시작", "제3장 전개"])
        self.assertEqual([ch.index for ch in dropped.chapters], [0, 1])

    def test_config_roundtrip_keeps_patterns(self):
        cfg = AppConfig(
            txt_patterns=[r"^\s*제\s*\d+\s*장", r"^\s*chapter\s*\d+\b"],
            ignore_zero_chapters=True,
        )
        restored = AppConfig.from_dict(cfg.to_dict())
        self.assertEqual(restored.txt_patterns, cfg.txt_patterns)
        self.assertEqual(restored.ignore_zero_chapters, True)


if __name__ == "__main__":
    unittest.main()
