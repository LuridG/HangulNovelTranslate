import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hangul_novel_translator.glossary import (
    EXTRACTION_MORE_SYSTEM,
    EXTRACTION_SYSTEM,
    NICKNAME_JUDGE_SYSTEM,
    Glossary,
    GlossaryEntry,
    enrich_glossary,
    extract_more_glossary,
    find_nickname_entries,
    payload_to_glossary,
)


class GlossaryMergeTest(unittest.TestCase):
    def test_append_unique_skips_existing_ko(self):
        glossary = Glossary(entries=[GlossaryEntry(ko="김준희", zh="金俊熙", confirmed=True)])
        new = [
            GlossaryEntry(ko="김준희", zh="金俊熙（错误译名）"),
            GlossaryEntry(ko="제원", zh="宰元"),
        ]
        added, skipped = glossary.append_unique(new)
        self.assertEqual((added, skipped), (1, 1))
        self.assertEqual(glossary.entries[0].zh, "金俊熙")
        self.assertTrue(glossary.entries[0].confirmed)
        self.assertEqual(glossary.entries[1].ko, "제원")

    def test_append_unique_dedupes_within_new(self):
        glossary = Glossary()
        new = [
            GlossaryEntry(ko="제원", zh="宰元"),
            GlossaryEntry(ko="제원", zh="宰元2"),
        ]
        added, skipped = glossary.append_unique(new)
        self.assertEqual((added, skipped), (1, 1))
        self.assertEqual(len(glossary.entries), 1)
        self.assertEqual(glossary.entries[0].zh, "宰元")

    def test_append_unique_ignores_empty(self):
        glossary = Glossary()
        new = [GlossaryEntry(ko="", zh=""), GlossaryEntry(ko="준희", zh="俊熙")]
        added, skipped = glossary.append_unique(new)
        self.assertEqual((added, skipped), (1, 0))
        self.assertEqual(len(glossary.entries), 1)

    def test_dedupe_keeps_first(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="제원", zh="宰元", confirmed=True),
                GlossaryEntry(ko="제원", zh="宰元2"),
                GlossaryEntry(ko="준희", zh="俊熙"),
            ]
        )
        removed = glossary.dedupe()
        self.assertEqual(removed, 1)
        self.assertEqual([e.zh for e in glossary.entries], ["宰元", "俊熙"])


class FakeLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls: list[list[dict]] = []

    def chat(self, messages, *, temperature=None, json_mode=False):
        self.calls.append(messages)
        return self.payload


class ExtractMoreGlossaryTest(unittest.TestCase):
    def test_prompt_includes_existing_and_sample(self):
        existing = Glossary(entries=[GlossaryEntry(ko="김준희", zh="金俊熙", confirmed=True)])
        llm = FakeLLM('{"entries":[{"ko":"제원","zh":"宰元","kind":"person"}]}')
        result = extract_more_glossary(llm, "새로운 샘플 본문입니다.", existing, limit=10)
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].ko, "제원")
        user = llm.calls[0][1]["content"]
        self.assertIn("김준희", user)
        self.assertIn("새로운 샘플 본문입니다.", user)
        system = llm.calls[0][0]["content"]
        self.assertIn("还没有", system)

    def test_requires_sample(self):
        llm = FakeLLM("{}")
        with self.assertRaises(ValueError):
            extract_more_glossary(llm, "   ", Glossary(), limit=10)


class GlossaryReplacementTest(unittest.TestCase):
    def test_alternative_list_parsing(self):
        entry = GlossaryEntry(ko="준희", zh="俊熙", alternatives="俊希, 俊曦 ,,俊熙2")
        self.assertEqual(entry.alternative_list(), ["俊希", "俊曦", "俊熙2"])
        self.assertEqual(GlossaryEntry(ko="a", zh="b", alternatives="  ").alternative_list(), [])

    def test_apply_replacements_ko_and_alternatives(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=True, alternatives="俊希,俊曦"),
            ]
        )
        text = "俊希来了。俊曦也来了。준희가 왔다."
        self.assertEqual(glossary.apply_replacements(text), "俊熙来了。俊熙也来了。俊熙가 왔다.")

    def test_apply_replacements_alternatives_only_confirmed(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=False, alternatives="俊希"),
            ]
        )
        # 未确认词条：韩文原词仍替换，但可能译法不参与替换。
        self.assertEqual(glossary.apply_replacements("俊希来了。준희가 왔다."), "俊希来了。俊熙가 왔다.")

    def test_apply_replacements_long_first(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="김준희", zh="金俊熙", confirmed=True, alternatives="金俊希,俊希"),
            ]
        )
        text = "金俊希是主角，俊希也出现了。"
        # 长词“金俊希”先于“俊希”替换，避免破坏长词。
        self.assertEqual(glossary.apply_replacements(text), "金俊熙是主角，金俊熙也出现了。")

    def test_from_dict_missing_alternatives_default(self):
        entry = GlossaryEntry.from_dict({"ko": "a", "zh": "b"})
        self.assertEqual(entry.alternatives, "")
        self.assertEqual(entry.alternative_list(), [])

    def test_single_char_alternative_skipped(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=True, alternatives="熙"),
            ]
        )
        # 单字变体不参与替换；韩文原词仍正常替换。
        self.assertEqual(glossary.apply_replacements("熙来了。준희가 왔다."), "熙来了。俊熙가 왔다.")


class PromptSpecTest(unittest.TestCase):
    def test_extraction_system_specs(self):
        self.assertIn("原形", EXTRACTION_SYSTEM)
        self.assertIn("助词", EXTRACTION_SYSTEM)
        self.assertIn("준희가", EXTRACTION_SYSTEM)
        self.assertIn("待确认", EXTRACTION_SYSTEM)
        self.assertIn("person|person-nickname|place|org|term|title", EXTRACTION_SYSTEM)
        self.assertIn("分别建条目", EXTRACTION_SYSTEM)
        self.assertIn("分别建条目", EXTRACTION_MORE_SYSTEM)
        self.assertIn("JSON", EXTRACTION_SYSTEM)

    def test_extraction_more_system_specs(self):
        self.assertIn("还没有", EXTRACTION_MORE_SYSTEM)
        self.assertIn("原形", EXTRACTION_MORE_SYSTEM)
        self.assertIn("同一实体", EXTRACTION_MORE_SYSTEM)
        self.assertIn("待确认", EXTRACTION_MORE_SYSTEM)


class AlternativesFeatureTest(unittest.TestCase):
    def test_upsert_preserves_alternatives(self):
        glossary = Glossary(entries=[GlossaryEntry(ko="준희", zh="俊熙")])
        glossary.upsert(GlossaryEntry(ko="준희", zh="俊熙", alternatives="俊希,俊曦"))
        self.assertEqual(glossary.entries[0].alternatives, "俊希,俊曦")

    def test_payload_parses_and_cleans_alts(self):
        glossary = payload_to_glossary(
            {
                "entries": [
                    {"ko": "준희", "zh": "俊熙", "alts": "俊希, 俊曦,,俊熙,준희,熙"},
                    {"ko": "제원", "zh": "宰元", "alternatives": ["宰沅", "宰元", "在元"]},
                ]
            }
        )
        # 去掉与 zh/ko 相同的项、单字项、空项与重复项。
        self.assertEqual(glossary.entries[0].alternatives, "俊希,俊曦")
        self.assertEqual(glossary.entries[1].alternatives, "宰沅,在元")

    def test_enrich_glossary_merges_without_source_text(self):
        existing = Glossary(
            entries=[
                GlossaryEntry(ko="준희", zh="俊熙", confirmed=True),
                GlossaryEntry(ko="제원", zh="宰元", alternatives="宰沅"),
            ]
        )
        llm = FakeLLM(
            '{"entries":['
            '{"ko":"준희","zh":"俊熙","alts":"俊希,俊曦,俊希"},'
            '{"ko":"제원","zh":"宰元","alts":"在元","note":"主角之一"}'
            "]}"
        )
        stats = enrich_glossary(llm, existing)
        self.assertEqual(stats["updated_alts"], 2)
        self.assertEqual(stats["updated_notes"], 1)
        by_ko = {e.ko: e for e in existing.entries}
        self.assertEqual(by_ko["준희"].alternatives, "俊希,俊曦")
        self.assertEqual(by_ko["제원"].alternatives, "宰沅,在元")
        self.assertEqual(by_ko["제원"].note, "主角之一")
        # 不传原文：用户消息不含任何样章内容。
        user = llm.calls[0][1]["content"]
        self.assertNotIn("샘플", user)
        self.assertIn("ko=준희", user)

    def test_enrich_requires_entries(self):
        with self.assertRaises(ValueError):
            enrich_glossary(FakeLLM("{}"), Glossary())


class ShortNameReplacementTest(unittest.TestCase):
    def test_short_form_alternative_skipped_by_default(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="범진", zh="崔范镇", confirmed=True, alternatives="范镇,范振"),
            ]
        )
        # “范镇”是全名子串 → 视为合法短称，默认不替换；
        # “范振”是错别字 → 仍替换为全名。
        text = "范镇来了。范振也来了。"
        self.assertEqual(glossary.apply_replacements(text), "范镇来了。崔范镇也来了。")

    def test_short_form_replacement_opt_in(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(
                    ko="범진",
                    zh="崔范镇",
                    confirmed=True,
                    alternatives="范镇",
                    replace_short=True,
                ),
            ]
        )
        self.assertEqual(glossary.apply_replacements("范镇来了。"), "崔范镇来了。")

    def test_from_dict_replace_short_default(self):
        entry = GlossaryEntry.from_dict({"ko": "a", "zh": "b"})
        self.assertFalse(entry.replace_short)


class FindNicknameTest(unittest.TestCase):
    def test_adds_confirmed_nickname_entry(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="최범진", zh="崔范镇", kind="person", confirmed=True),
            ]
        )
        llm = FakeLLM('{"entries":[{"ko":"최범진","nicknames":[{"ko":"범진","zh":"范镇"}]}]}')
        stats = find_nickname_entries(llm, glossary, "범진이는 집에 갔다. 최범진은 웃었다.")
        self.assertEqual(stats["nickname_added"], 1)
        self.assertEqual(stats["nickname_skipped"], 0)
        self.assertEqual(stats["nickname_not_found"], 0)
        added = [e for e in glossary.entries if e.kind == "person-nickname"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].ko, "범진")
        self.assertEqual(added[0].zh, "范镇")
        self.assertEqual(added[0].note, "崔范镇的昵称")
        self.assertFalse(added[0].confirmed)

    def test_skips_nickname_only_inside_full_name(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="최범진", zh="崔范镇", kind="person", confirmed=True),
            ]
        )
        # “범진”只出现在“최범진/최범진은”里，不是独立昵称，应判为未找到。
        llm = FakeLLM('{"entries":[{"ko":"최범진","nicknames":[{"ko":"범진","zh":"范镇"}]}]}')
        stats = find_nickname_entries(llm, glossary, "최범진은 웃었다. 최범진이 나왔다.")
        self.assertEqual(stats["nickname_added"], 0)
        self.assertEqual(stats["nickname_not_found"], 1)

    def test_skips_duplicate_or_invalid_nicknames(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="최범진", zh="崔范镇", kind="person", confirmed=True),
                GlossaryEntry(ko="범진", zh="范镇", kind="person-nickname", confirmed=True),
            ]
        )
        llm = FakeLLM(
            '{"entries":[{"ko":"최범진","nicknames":['
            '{"ko":"범진","zh":"范镇"},'
            '{"ko":"최범진","zh":"崔范镇"},'
            '{"ko":"진","zh":"镇"},'
            '{"ko":"범진우","zh":"范镇宇"}'
            "]}]}"
        )
        stats = find_nickname_entries(llm, glossary, "범진이가 왔다. 범진우도 왔다.")
        self.assertEqual(stats["nickname_added"], 1)  # 只有 범진우 是新词条
        self.assertEqual(stats["nickname_skipped"], 3)  # 已存在、全名本身、单字
        self.assertEqual(stats["nickname_not_found"], 0)
        self.assertIn("범진우", {e.ko for e in glossary.entries})

    def test_no_person_entries_skips_llm(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="서울", zh="首尔", kind="place", confirmed=True),
            ]
        )
        llm = FakeLLM("{}")
        stats = find_nickname_entries(llm, glossary, "서울에 갔다.")
        self.assertEqual(stats["nickname_added"], 0)
        self.assertEqual(llm.calls, [])

    def test_prompt_only_person_entries(self):
        glossary = Glossary(
            entries=[
                GlossaryEntry(ko="최범진", zh="崔范镇", kind="person", confirmed=True),
                GlossaryEntry(ko="서울", zh="首尔", kind="place", confirmed=True),
            ]
        )
        llm = FakeLLM('{"entries":[]}')
        find_nickname_entries(llm, glossary, "범진이 왔다.")
        user = llm.calls[0][1]["content"]
        self.assertIn("최범진", user)
        self.assertNotIn("서울", user)
        self.assertIn("昵称", NICKNAME_JUDGE_SYSTEM)

if __name__ == "__main__":
    unittest.main()