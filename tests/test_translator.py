import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 未安装 openai 时，仅用替身完成纯逻辑测试。
sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kwargs: None))

from hangul_novel_translator import translator as translator_module
from hangul_novel_translator.book import Book, Chapter, load_book
from hangul_novel_translator.config import AppConfig
from hangul_novel_translator.glossary import Glossary
from hangul_novel_translator.translator import (
    Translator,
    _chunk_signature,
    _sanitize_state_name,
    build_chunks,
    collect_sample_text,
    load_failed_chunks,
    reconcile_paragraphs,
    save_manual_translation,
)


class TranslatorLogicTest(unittest.TestCase):
    def test_build_chunks_keeps_chapter_groups(self):
        book = Book(
            title="测试",
            chapters=[
                Chapter(0, "제1장", ["가나다" * 300, "라마바" * 300]),
                Chapter(1, "제2장", ["사아자" * 300]),
            ],
        )
        config = AppConfig(chunk_chars=700, max_paragraph_chars=1000)
        chunks = build_chunks(book, config)
        self.assertTrue(all(c.id.startswith("ch-") for c in chunks))
        self.assertLessEqual(max(c.char_count for c in chunks), 750)
        self.assertEqual(len({c.chapter_index for c in chunks}), 2)

    def test_reconcile_pads_or_truncates(self):
        self.assertEqual(reconcile_paragraphs(["가", "나"], 2), ["가", "나"])
        self.assertEqual(reconcile_paragraphs(["가"], 3), ["가", "", ""])
        self.assertEqual(reconcile_paragraphs(["가", "나", "다"], 2), ["가", "나"])

    def test_collect_sample_text(self):
        book = Book(
            title="测试",
            chapters=[Chapter(0, "제1장", ["가" * 500, "나" * 500]), Chapter(1, "제2장", ["다" * 500])],
        )
        config = AppConfig(extract_sample_chars=800, extract_sample_chapters=2)
        sample = collect_sample_text(book, config)
        self.assertIn("가", sample)
        self.assertGreater(len(sample), 0)

    def test_collect_sample_prefers_longest_chapter(self):
        book = Book(
            title="测试",
            chapters=[
                Chapter(0, "목차", ["목차"]),
                Chapter(1, "제1장", ["가나다라마바사" * 1000]),
            ],
        )
        config = AppConfig(extract_sample_chars=200, extract_sample_chapters=2)
        sample = collect_sample_text(book, config)
        self.assertNotIn("목차", sample)
        self.assertIn("가나다라마바사", sample)




class FakeLLM:
    """替身：可配置前 N 次调用失败，其余返回固定 JSON 译文。"""

    def __init__(self, config):
        self.config = config
        self.calls = 0
        self.fail_calls = 0

    def chat(self, messages, *, temperature=None, json_mode=False):
        self.calls += 1
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise RuntimeError("simulated failure")
        return '{"paragraphs": ["译文"]}'


class TranslatorRetryTest(unittest.TestCase):
    def _make_source(self, tmp: Path) -> Path:
        src = tmp / "novel.txt"
        src.write_text("제1장\n가나다\n", encoding="utf-8")
        return src

    def _make_state(self, tmp: Path, src: Path, chunk_id: str) -> Path:
        state_path = tmp / ".novel.translation_state.json"
        state = {
            "source": str(src),
            "completed": {},
            "failed": {chunk_id: "boom"},
            "total_chunks": 1,
            "chunk_chars": 1800,
            "max_paragraph_chars": 2600,
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        return state_path

    def test_retry_failed_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True)
            chunk_id = build_chunks(load_book(src), config)[0].id
            state_path = self._make_state(tmp, src, chunk_id)
            fake = FakeLLM(config)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                result = translator.retry_failed(state_path, Glossary(), max_attempts=3)
            finally:
                translator_module.LLMClient = old
            self.assertEqual(result["found"], 1)
            self.assertEqual(result["recovered"], 1)
            self.assertEqual(result["still_failed"], 0)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn(chunk_id, data["completed"])
            self.assertNotIn(chunk_id, data["failed"])

    def test_retry_failed_still_failed_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True)
            chunk_id = build_chunks(load_book(src), config)[0].id
            state_path = self._make_state(tmp, src, chunk_id)
            fake = FakeLLM(config)
            fake.fail_calls = 100
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                result = translator.retry_failed(state_path, Glossary(), max_attempts=3)
            finally:
                translator_module.LLMClient = old
            self.assertEqual(result["found"], 1)
            self.assertEqual(result["recovered"], 0)
            self.assertEqual(result["still_failed"], 1)
            self.assertEqual(fake.calls, 3)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn(chunk_id, data["failed"])

    def test_retry_failed_without_failed_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            state_path = tmp / ".novel.translation_state.json"
            state_path.write_text(
                json.dumps({"source": str(src), "completed": {}, "failed": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: FakeLLM(cfg)
            try:
                translator = Translator(AppConfig(resume=True))
                result = translator.retry_failed(state_path, Glossary(), max_attempts=3)
            finally:
                translator_module.LLMClient = old
            self.assertEqual(
                result, {"found": 0, "recovered": 0, "still_failed": 0, "missing": 0}
            )

    def test_load_failed_chunks_stored_paragraphs(self):
        """新版失败块：直接从存档读取原文，原书不存在也不影响。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "novel.txt"  # 不创建，模拟原书缺失
            state_path = tmp / ".novel.translation_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "source": str(src),
                        "completed": {},
                        "failed": {
                            "ch-00000-00000": {
                                "error": "模型返回了空 content",
                                "chapter_index": 0,
                                "chapter_title": "제1장",
                                "chunk_index": 0,
                                "paragraphs": ["가나다", "라마바"],
                            }
                        },
                        "chunk_chars": 1800,
                        "max_paragraph_chars": 2600,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = load_failed_chunks(state_path, AppConfig(resume=True))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].chunk_id, "ch-00000-00000")
            self.assertEqual(items[0].paragraphs, ["가나다", "라마바"])
            self.assertEqual(items[0].chapter_title, "제1장")
            self.assertFalse(items[0].missing)
            self.assertIn("空 content", items[0].error)

    def test_load_failed_chunks_legacy_rebuilds(self):
        """旧版失败块（值=错误字符串）：按原书重新分块还原原文。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True)
            chunk_id = build_chunks(load_book(src), config)[0].id
            state_path = self._make_state(tmp, src, chunk_id)
            items = load_failed_chunks(state_path, config)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].paragraphs, ["가나다"])
            self.assertEqual(items[0].chapter_title, "제1장")
            self.assertFalse(items[0].missing)

    def test_save_manual_translation_writes_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            chunk_id = "ch-00000-00000"
            state_path = self._make_state(tmp, src, chunk_id)
            result = save_manual_translation(state_path, chunk_id, ["译文一", "译文二"])
            self.assertEqual(result["failed"], 0)
            self.assertEqual(result["completed"], 1)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(data["completed"][chunk_id], ["译文一", "译文二"])
            self.assertNotIn(chunk_id, data["failed"])

    def test_retry_failed_recovers_from_stored_paragraphs_without_source(self):
        """失败块在存档里持久化了原文，原书缺失也能自包含恢复。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "novel.txt"  # 不实际创建：模拟原书已不存在
            config = AppConfig(resume=True)
            chunk_id = "ch-00000-00000"
            state_path = tmp / ".novel.translation_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "source": str(src),
                        "completed": {},
                        "failed": {
                            chunk_id: {
                                "error": "boom",
                                "chapter_index": 0,
                                "chapter_title": "제1장",
                                "chunk_index": 0,
                                "paragraphs": ["가나다"],
                            }
                        },
                        "total_chunks": 1,
                        "chunk_chars": 1800,
                        "max_paragraph_chars": 2600,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            fake = FakeLLM(config)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                result = translator.retry_failed(state_path, Glossary(), max_attempts=3)
            finally:
                translator_module.LLMClient = old
            self.assertEqual(result["recovered"], 1)
            self.assertEqual(result["missing"], 0)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn(chunk_id, data["completed"])
            self.assertNotIn(chunk_id, data["failed"])

    def test_translate_file_output_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=False, extract_glossary=False, output_txt=True, output_epub=True)
            fake = FakeLLM(config)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                result = translator.translate_file(src, tmp, Glossary(), output_stem="某某 第1卷")
            finally:
                translator_module.LLMClient = old
            names = {p.name for p in result.output_paths}
            self.assertIn("某某 第1卷.zh.txt", names)
            self.assertIn("某某 第1卷.zh.epub", names)

    def test_sanitize_state_name_keeps_hangul_and_cjk(self):
        self.assertEqual(
            _sanitize_state_name("코즈믹 호러는 어떠세요_ (외전)"),
            "코즈믹_호러는_어떠세요_외전",
        )
        self.assertEqual(_sanitize_state_name("宇宙恐怖怎么样？ 第1卷"), "宇宙恐怖怎么样_第1卷")
        self.assertEqual(_sanitize_state_name("..??  "), "book")
        self.assertNotEqual(
            _sanitize_state_name("코즈믹 호러는 어떠세요_ (외전)"),
            _sanitize_state_name("코즈믹 호러는 어떠세요_ (특별 외전)"),
        )

    def test_translate_file_refuses_stale_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            state_path = tmp / ".某某.translation_state.json"
            state = {
                "source": str(src),
                "completed": {"ch-00000-00000": ["旧译文"]},
                "failed": {},
                "total_chunks": 5,  # 与实际解析（1 块）不一致
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            config = AppConfig(resume=True, extract_glossary=False, output_txt=False, output_epub=False)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: FakeLLM(cfg)
            try:
                translator = Translator(config)
                with self.assertRaises(ValueError) as ctx:
                    translator.translate_file(src, tmp, Glossary(), output_stem="某某")
            finally:
                translator_module.LLMClient = old
            self.assertIn("章节结构", str(ctx.exception))
            self.assertIn("请删除存档", str(ctx.exception))

    def test_translate_file_resumes_matching_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True, extract_glossary=False, output_txt=False, output_epub=False)
            chunks = build_chunks(load_book(src), config)
            state_path = tmp / ".某某.translation_state.json"
            state = {
                "source": str(src),
                "completed": {chunks[0].id: ["译文"]},
                "failed": {},
                "total_chunks": len(chunks),
                "chunk_signature": _chunk_signature(chunks),
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            fake = FakeLLM(config)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                result = translator.translate_file(src, tmp, Glossary(), output_stem="某某")
            finally:
                translator_module.LLMClient = old
            self.assertEqual(result.completed_chunks, 1)
            self.assertEqual(fake.calls, 1)  # 正文复用存档，仅章节名批量翻译调用 1 次 LLM

    def test_translate_file_refuses_signature_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True, extract_glossary=False, output_txt=False, output_epub=False)
            chunks = build_chunks(load_book(src), config)
            state_path = tmp / ".某某.translation_state.json"
            state = {
                "source": str(src),
                "completed": {chunks[0].id: ["旧译文"]},
                "failed": {},
                "total_chunks": len(chunks),
                "chunk_signature": "deadbeef" * 8,  # 与实际签名不一致
            }
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: FakeLLM(cfg)
            try:
                translator = Translator(config)
                with self.assertRaises(ValueError) as ctx:
                    translator.translate_file(src, tmp, Glossary(), output_stem="某某")
            finally:
                translator_module.LLMClient = old
            self.assertIn("请删除存档", str(ctx.exception))

    def test_translate_file_state_named_by_output_stem(self):

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = self._make_source(tmp)
            config = AppConfig(resume=True, extract_glossary=False, output_txt=True, output_epub=True)
            fake = FakeLLM(config)
            old = translator_module.LLMClient
            translator_module.LLMClient = lambda cfg: fake
            try:
                translator = Translator(config)
                translator.translate_file(src, tmp, Glossary(), output_stem="宇宙恐怖怎么样？ 第1卷")
            finally:
                translator_module.LLMClient = old
            expected = tmp / ".宇宙恐怖怎么样_第1卷.translation_state.json"
            self.assertTrue(expected.exists(), [p.name for p in tmp.iterdir()])



class TitleFakeLLM:
    """替身：返回章节名翻译 JSON。"""

    def __init__(self, config):
        self.config = config
        self.messages = None

    def chat(self, messages, *, temperature=None, json_mode=False):
        self.messages = messages
        return '{"titles": {"0": "第一章译", "1": "第二章译"}}'


class ChapterTitleTranslateTest(unittest.TestCase):
    def test_translate_chapter_titles(self):
        from hangul_novel_translator.translator import Translator

        book = Book(
            title="测试",
            chapters=[Chapter(0, "제1장", ["가"]), Chapter(1, "제2장", ["나"])],
        )
        fake = TitleFakeLLM(AppConfig())
        old = translator_module.LLMClient
        translator_module.LLMClient = lambda cfg: fake
        try:
            translator = Translator(AppConfig())
            translator._translate_chapter_titles(book, Glossary())
        finally:
            translator_module.LLMClient = old
        self.assertEqual(book.chapters[0].title_zh, "第一章译")
        self.assertEqual(book.chapters[1].title_zh, "第二章译")
        self.assertIn("제1장", fake.messages[1]["content"])

    def test_translate_chapter_titles_skips_decorative(self):
        from hangul_novel_translator.translator import Translator

        book = Book(
            title="测试",
            chapters=[
                Chapter(0, "제1장", ["가"]),
                Chapter(1, "S\u0337\u0308tr\u0301\u0337ange dream", ["나"]),
            ],
        )
        fake = TitleFakeLLM(AppConfig())
        old = translator_module.LLMClient
        translator_module.LLMClient = lambda cfg: fake
        try:
            translator = Translator(AppConfig())
            translator._translate_chapter_titles(book, Glossary())
        finally:
            translator_module.LLMClient = old
        # 正常标题翻译；zalgo 装饰标题跳过翻译、保留原样
        self.assertEqual(book.chapters[0].title_zh, "第一章译")
        self.assertEqual(book.chapters[1].title_zh, "")
        self.assertEqual(book.chapters[1].display_title, "S\u0337\u0308tr\u0301\u0337ange dream")


    def test_translate_chapter_titles_keeps_original_on_failure(self):
        from hangul_novel_translator.translator import Translator

        book = Book(title="测试", chapters=[Chapter(0, "제1장", ["가"])])

        class BoomLLM:
            def __init__(self, config):
                self.config = config

            def chat(self, messages, *, temperature=None, json_mode=False):
                raise RuntimeError("boom")

        old = translator_module.LLMClient
        translator_module.LLMClient = lambda cfg: BoomLLM(cfg)
        try:
            translator = Translator(AppConfig())
            translator._translate_chapter_titles(book, Glossary())
        finally:
            translator_module.LLMClient = old
        self.assertEqual(book.chapters[0].title_zh, "")
        self.assertEqual(book.chapters[0].display_title, "제1장")


if __name__ == "__main__":
    unittest.main()
