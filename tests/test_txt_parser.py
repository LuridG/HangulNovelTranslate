import re
import unittest

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


if __name__ == "__main__":
    unittest.main()
