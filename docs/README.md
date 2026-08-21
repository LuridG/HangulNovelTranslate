# Hangul Novel Translator 架构演进与设计方案汇总（Design Docs Index）

本目录归档了项目关于目录结构、词表提取、成品矫正、排版规范以及文本解析的完整设计与演进方案。

---

## 📑 方案导航与实施优先级

| 序号 | 方案文档 | 核心功能与收益 | 实施优先级 |
| :---: | :--- | :--- | :---: |
| **01** | [`epub_nested_toc_plan.md`](./epub_nested_toc_plan.md) | **EPUB 多层级嵌套目录与多卷合并树状 TOC**<br>解决阅读器中目录平铺无下拉折叠的问题，支持多卷合集多层树状目录。 | 🥇 **P1** |
| **02** | [`glossary_sampling_plan.md`](./glossary_sampling_plan.md) | **专有名词全书跨度采样（Strided Sampling）**<br>彻底解决 50~200 万字大合集小说中后期角色/名词采样遗漏的问题。 | 🥇 **P1** |
| **03** | [`epub_export_sanitizer_plan.md`](./epub_export_sanitizer_plan.md) | **EPUB 导出期文本清洗过滤管线（Export Sanitizer）**<br>自动剥离段首标号 `[1]`、清除 JSON 语法残渣、精准剥离半角引号，支持用户自定义规则。 | 🥇 **P1** |
| **04** | [`epub_glossary_fixer_plan.md`](./epub_glossary_fixer_plan.md) | **成品 EPUB 词表无损原地矫正（In-Place Fixer）**<br>秒级原地文本节点替换，100% 保持已调好的封面插图、CSS 样式与排版结构。 | 🥈 **P2** |
| **05** | [`feature_enhancements_plan.md`](./feature_enhancements_plan.md) | **排版规范化、失败块单块重试与例句速查**<br>① 标点中韩排版美化<br>② 失败块可视化查看与单块精准重试<br>③ 词表全书上下文例句速查 | 🥉 **P3** |
| **06** | [`txt_input_enhancements_plan.md`](./txt_input_enhancements_plan.md) | **TXT 纯文本输入解析与排版增强**<br>非标韩文网文章节识别、智能语义断句分章、单句硬回车合并、导出 EPUB 样式注入。 | 📦 **P4（归档）** |

---

## 🛠 对应计划的单元测试（Test Cases）规划

在后续实际编写代码实施上述方案时，建议补充以下测试套件以保证 100% 质量稳定：

1. **`tests/test_sanitizer.py`**（对应方案 03 & 05）：
   - 测试段首 `[1]`、`(12)`、`3.` 等标号剥离。
   - 测试 `{"paragraphs":` 等 JSON 残渣净化。
   - 测试半角英文双引号 `"` 与全角中文对话引号 `“` `”` 的分离与单边引号剔除。
   - 测试韩式省略号 `...` 转换为 `……` 与重复感叹问号收敛。

2. **`tests/test_strided_sampling.py`**（对应方案 02）：
   - 测试多章节跨度采样在长小说中的均匀分桶覆盖性。
   - 测试极短章节（`< 300` 字）的智能过滤。

3. **`tests/test_epub_fixer.py`**（对应方案 04）：
   - 测试在真实 EPUB 压缩包内执行纯文本节点替换，验证图片/CSS/元数据未损坏。
   - 验证只有 `confirmed=True` 的词条才会执行可能译名替换。

4. **`tests/test_txt_parser.py`**（对应方案 06）：
   - 测试各种非标韩文章节前缀（`#1`、`EP.01`、`[1화]`、带副标题）的识别准确率。
   - 测试段落换行合并（Unwrap）逻辑。
