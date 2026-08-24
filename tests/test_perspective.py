import io
import json
import threading
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from hangul_novel_translator.config import AppConfig
from hangul_novel_translator.glossary import Glossary, GlossaryEntry
from hangul_novel_translator.llm import LLMCancelled, LLMClient
from hangul_novel_translator.perspective import (
    PerspectiveBlock,
    PerspectiveCancelled,
    PerspectiveConverter,
    PerspectiveError,
    PerspectiveOptions,
    build_perspective_prompt,
    inspect_perspective_epub,
    load_failed_perspective_blocks,
    load_perspective_state,
    perspective_state_path,
    render_perspective_state,
    save_perspective_failure,
    save_manual_perspective_translation,
)


class FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.messages = []

    def chat(self, messages, *, temperature=None, json_mode=False):
        self.messages.append(messages)
        if self.error:
            raise self.error
        return self.response


class BatchFakeLLM:
    def chat(self, messages, *, temperature=None, json_mode=False, cancel_event=None):
        user = messages[-1]["content"]
        if "::b00001" in user:
            blocks = [
                {
                    "id": "OEBPS/Text/chapter.xhtml::b00001",
                    "segments": ["\u4ed6\u8d70\u8fdb\u4e86", "\u623f\u95f4", "\u3002"],
                }
            ]
        elif "::b00002" in user:
            blocks = [
                {
                    "id": "OEBPS/Text/chapter.xhtml::b00002",
                    "segments": ["\u4ed6\u5750\u4e0b\u4e86\u3002"],
                }
            ]
        else:
            raise AssertionError("unexpected block")
        return json.dumps({"blocks": blocks}, ensure_ascii=False)


class CancellableFakeLLM:
    def __init__(self):
        self.started = threading.Event()

    def chat(self, messages, *, temperature=None, json_mode=False, cancel_event=None):
        self.started.set()
        while cancel_event is None or not cancel_event.is_set():
            time.sleep(0.01)
        raise LLMCancelled("cancelled")


class BlockingCompletions:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def create(self, **kwargs):
        self.started.set()
        self.release.wait(2)
        raise RuntimeError("request released")


class BlockingClient:
    def __init__(self, completions):
        self.chat = type("Chat", (), {"completions": completions})()


def build_epub() -> bytes:
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        '<link rel="stylesheet" href="../Styles/main.css"/>'
        '</head><body>'
        '<h1>第一章</h1>'
        '<p>我走进了<span class="emphasis">房间</span>。</p>'
        '<p>“我不会去。”</p>'
        '<blockquote><p>我留下。</p></blockquote>'
        '<p class="inner-monologue">（我真的累了。）</p>'
        '</body></html>'
    ).encode("utf-8")
    resources = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b"<container />",
        "OEBPS/content.opf": b"<package><item href=\"Text/chapter.xhtml\" /></package>",
        "OEBPS/Styles/main.css": b".emphasis { font-family: TestFont; }",
        "OEBPS/Fonts/TestFont.ttf": b"font-bytes",
        "OEBPS/Images/cover.png": b"png-bytes",
        "OEBPS/Text/chapter.xhtml": chapter,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        mimetype = zipfile.ZipInfo("mimetype")
        mimetype.compress_type = zipfile.ZIP_STORED
        archive.writestr(mimetype, resources["mimetype"])
        for name, content in resources.items():
            if name != "mimetype":
                archive.writestr(name, content)
    return buffer.getvalue()


def replace_chapter_text(epub_bytes: bytes, old: str, new: str) -> bytes:
    source_buffer = io.BytesIO(epub_bytes)
    target_buffer = io.BytesIO()
    with zipfile.ZipFile(source_buffer, "r") as source, zipfile.ZipFile(
        target_buffer, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "OEBPS/Text/chapter.xhtml":
                content = content.replace(old.encode("utf-8"), new.encode("utf-8"))
            target.writestr(info, content)
    return target_buffer.getvalue()


class PerspectiveCoreTest(unittest.TestCase):
    def test_coverage_strategy_includes_all_non_heading_body_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.epub"
            source.write_bytes(build_epub())
            conservative = inspect_perspective_epub(
                source,
                PerspectiveOptions(narrator_name="林野"),
            )
            coverage = inspect_perspective_epub(
                source,
                PerspectiveOptions(narrator_name="林野", strategy="coverage"),
            )

        self.assertEqual(conservative["eligible_blocks"], 1)
        self.assertEqual(conservative["protected_blocks"], 4)
        self.assertEqual(coverage["eligible_blocks"], 4)
        self.assertEqual(coverage["protected_blocks"], 1)

    def test_coverage_prompt_preserves_character_speech(self):
        block = PerspectiveBlock(
            id="chapter::b00001",
            file="chapter.xhtml",
            index=1,
            source_segments=["我看着他说：‘我不走。’"],
            source_text="我看着他说：‘我不走。’",
            classification="dialogue",
        )
        messages = build_perspective_prompt(
            PerspectiveOptions(narrator_name="林野", strategy="coverage"),
            None,
            [block],
        )

        self.assertIn("策略 2", messages[0]["content"])
        self.assertIn("人物原话", messages[0]["content"])
        self.assertIn("CLASSIFICATION: dialogue", messages[1]["content"])

    def test_legacy_state_without_strategy_resumes_as_conservative(self):
        options = PerspectiveOptions(narrator_name="林野")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(build_epub())
            PerspectiveConverter(FakeLLM(error=RuntimeError("model down")), options).convert(source, output)
            state_path = perspective_state_path(source, output)
            data = json.loads(state_path.read_text(encoding="utf-8"))
            data["options"].pop("strategy", None)
            state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            loaded = load_perspective_state(state_path)
            result = PerspectiveConverter(
                FakeLLM(error=RuntimeError("still down")),
                options,
            ).convert(source, output, state_path=state_path)

        self.assertEqual(loaded["options"]["strategy"], "conservative")
        self.assertEqual(result["failed_blocks"], 1)

    def test_llm_request_wait_can_be_cancelled(self):
        completions = BlockingCompletions()
        client = LLMClient(AppConfig(max_retries=1, timeout=2))
        client.client = BlockingClient(completions)
        cancel_event = threading.Event()
        result = {}

        def worker():
            try:
                client.chat([{"role": "user", "content": "test"}], cancel_event=cancel_event)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(completions.started.wait(1))
        started_at = time.monotonic()
        cancel_event.set()
        thread.join(1)
        elapsed = time.monotonic() - started_at
        completions.release.set()
        thread.join(1)

        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 0.8)
        self.assertIsInstance(result.get("error"), LLMCancelled)

    def test_stop_during_perspective_request_preserves_pending_batch(self):
        options = PerspectiveOptions(narrator_name="林野")
        fake_llm = CancellableFakeLLM()
        cancel_event = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(build_epub())
            result = {}

            def worker():
                result["value"] = PerspectiveConverter(
                    fake_llm,
                    options,
                    cancel_event=cancel_event,
                ).convert(source, output)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(fake_llm.started.wait(1))
            cancel_event.set()
            thread.join(2)

            self.assertFalse(thread.is_alive())
            converted = result["value"]
            self.assertTrue(converted["cancelled"])
            self.assertEqual(converted["completed_blocks"], 0)
            self.assertEqual(converted["failed_blocks"], 0)
            self.assertTrue(output.exists())
            state = json.loads(perspective_state_path(source, output).read_text(encoding="utf-8"))
            statuses = [item["status"] for item in state["blocks"].values()]
            self.assertIn("pending", statuses)
            self.assertNotIn("failed", statuses)

    def test_progress_total_stays_fixed_across_batches(self):
        first_html = "<p>\u6211\u8d70\u8fdb\u4e86<span class=\"emphasis\">\u623f\u95f4</span>\u3002</p>"
        second_html = "<p>\u6211\u5750\u4e0b\u4e86\u3002</p>"
        source_bytes = replace_chapter_text(build_epub(), first_html, first_html + second_html)
        progress = []
        options = PerspectiveOptions(narrator_name="\u6797\u91ce", chunk_chars=1)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(source_bytes)
            result = PerspectiveConverter(
                BatchFakeLLM(),
                options,
                progress_callback=lambda stage, done, total, message: progress.append((done, total, message)),
            ).convert(source, output)

        self.assertEqual(result["completed_blocks"], 2, result)
        self.assertTrue(progress)
        self.assertEqual({total for _done, total, _message in progress}, {2})
        self.assertEqual(progress[-1][0], 2)

    def test_save_perspective_failure_updates_error_without_resetting_block(self):
        options = PerspectiveOptions(narrator_name="\u6797\u91ce")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(build_epub())
            PerspectiveConverter(FakeLLM(error=RuntimeError("model down")), options).convert(source, output)
            state_path = perspective_state_path(source, output)
            failed = load_failed_perspective_blocks(state_path)
            self.assertEqual(len(failed), 1)

            stats = save_perspective_failure(state_path, failed[0].block_id, "retry failed")
            self.assertEqual(stats["failed"], 1)
            refreshed = load_failed_perspective_blocks(state_path)
            self.assertEqual(refreshed[0].error, "retry failed")

    def test_explicit_state_path_restores_and_continues_job(self):
        options = PerspectiveOptions(narrator_name="\u6797\u91ce")
        response = json.dumps(
            {
                "blocks": [
                    {
                        "id": "OEBPS/Text/chapter.xhtml::b00001",
                        "segments": ["\u4ed6\u8d70\u8fdb\u4e86", "\u623f\u95f4", "\u3002"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "renamed-output.epub"
            state_path = tmp / "moved-job.json"
            source.write_bytes(build_epub())
            initial = PerspectiveConverter(
                FakeLLM(error=RuntimeError("stop before request")),
                options,
            ).convert(source, output, state_path=state_path)
            self.assertEqual(initial["failed_blocks"], 1)
            loaded = load_perspective_state(state_path)
            self.assertEqual(Path(loaded["output_path"]), output)
            resumed = PerspectiveConverter(FakeLLM(response=response), options).convert(
                source,
                output,
                state_path=state_path,
            )
            self.assertEqual(resumed["completed_blocks"], 1)
            self.assertEqual(resumed["state_path"], state_path)
            self.assertTrue(state_path.exists())

    def test_prompt_uses_glossary_and_short_name(self):
        options = PerspectiveOptions(
            narrator_name="林野",
            short_name="阿野",
            style="short_name",
        )
        glossary = Glossary(entries=[GlossaryEntry(ko="린야", zh="林野", kind="person")])
        block = PerspectiveBlock(
            id="Text/chapter.xhtml::b00000",
            file="Text/chapter.xhtml",
            index=0,
            source_segments=["我走了。"],
            source_text="我走了。",
        )
        messages = build_perspective_prompt(options, glossary, [block])
        self.assertIn("阿野", messages[0]["content"])
        self.assertIn("린야 -> 林野", messages[0]["content"])
        self.assertIn(block.id, messages[1]["content"])

    def test_rejects_missing_or_mismatched_segments(self):
        options = PerspectiveOptions(narrator_name="林野")
        block = PerspectiveBlock(
            id="chapter::b00000",
            file="chapter.xhtml",
            index=0,
            source_segments=["我", "走了。"],
            source_text="我走了。",
        )
        llm = FakeLLM('{"blocks":[{"id":"chapter::b00000","segments":["他走了。"]}]}')
        with self.assertRaises(PerspectiveError):
            from hangul_novel_translator.perspective import rewrite_blocks

            rewrite_blocks(llm, options, None, [block])

    def test_preserves_resources_and_protected_blocks(self):
        options = PerspectiveOptions(narrator_name="林野")
        response = json.dumps(
            {
                "blocks": [
                    {
                        "id": "OEBPS/Text/chapter.xhtml::b00001",
                        "segments": ["他走进了", "房间", "。"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        llm = FakeLLM(response=response)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(build_epub())
            result = PerspectiveConverter(llm, options).convert(source, output)
            self.assertEqual(result["completed_blocks"], 1)
            self.assertEqual(result["failed_blocks"], 0)
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as converted:
                for name in (
                    "mimetype",
                    "META-INF/container.xml",
                    "OEBPS/content.opf",
                    "OEBPS/Styles/main.css",
                    "OEBPS/Fonts/TestFont.ttf",
                    "OEBPS/Images/cover.png",
                ):
                    self.assertEqual(converted.read(name), original.read(name), name)
                chapter = converted.read("OEBPS/Text/chapter.xhtml").decode("utf-8")
                self.assertIn("他走进了", chapter)
                self.assertIn('<span class="emphasis">房间</span>', chapter)
                self.assertIn("“我不会去。”", chapter)
                self.assertIn("我留下。", chapter)
                self.assertIn("（我真的累了。）", chapter)
                self.assertEqual(converted.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)

    def test_repeated_text_in_protected_block_is_not_replaced(self):
        source_bytes = replace_chapter_text(
            build_epub(),
            '<p>“我不会去。”</p>',
            '<p>“我走进了房间。”</p>',
        )
        options = PerspectiveOptions(narrator_name="林野")
        response = json.dumps(
            {
                "blocks": [
                    {
                        "id": "OEBPS/Text/chapter.xhtml::b00001",
                        "segments": ["他走进了", "房间", "。"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(source_bytes)
            result = PerspectiveConverter(FakeLLM(response=response), options).convert(source, output)
            self.assertEqual(result["completed_blocks"], 1)
            with zipfile.ZipFile(output) as archive:
                chapter = archive.read("OEBPS/Text/chapter.xhtml").decode("utf-8")
                self.assertIn("他走进了", chapter)
                self.assertIn("“我走进了房间。”", chapter)

    def test_failed_block_is_persisted_and_manual_render_works(self):
        options = PerspectiveOptions(narrator_name="林野")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.epub"
            output = tmp / "third.epub"
            source.write_bytes(build_epub())
            result = PerspectiveConverter(
                FakeLLM(error=RuntimeError("model down")),
                options,
            ).convert(source, output)
            self.assertEqual(result["completed_blocks"], 0)
            self.assertEqual(result["failed_blocks"], 1)
            state_path = perspective_state_path(source, output)
            failed = load_failed_perspective_blocks(state_path)
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0].source_segments, ["我走进了", "房间", "。"])
            save_manual_perspective_translation(
                state_path,
                failed[0].block_id,
                ["他走进了", "房间", "。"],
            )
            render_perspective_state(source, output, state_path)
            with zipfile.ZipFile(output) as archive:
                chapter = archive.read("OEBPS/Text/chapter.xhtml").decode("utf-8")
                self.assertIn("他走进了", chapter)
                self.assertIn("“我不会去。”", chapter)


if __name__ == "__main__":
    unittest.main()
