import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from hangul_novel_translator.glossary import Glossary, GlossaryEntry
from hangul_novel_translator.perspective import (
    PerspectiveBlock,
    PerspectiveConverter,
    PerspectiveError,
    PerspectiveOptions,
    build_perspective_prompt,
    load_failed_perspective_blocks,
    perspective_state_path,
    render_perspective_state,
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
