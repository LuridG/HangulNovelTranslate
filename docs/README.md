# Hangul Novel Translator 文档与待办索引

本目录归档项目的设计方案与待办事项。已实现的功能见根目录 [`README.md`](../README.md)；尚未落地的功能统一集中在 [`TODO.md`](./TODO.md) 跟踪。

## 📌 方案状态

- 已实现并归档的方案文档（01 嵌套目录、02 跨度采样、03 导出清洗、04 成品矫正）已从本目录移除，实现见 `hangul_novel_translator/`。
- 原方案 05「进阶功能」、06「TXT 增强」的剩余待办已合并到 [`TODO.md`](./TODO.md)。
- 新方案文档：[`settings_tab_plan.md`](./settings_tab_plan.md)（「设置」tab，阶段一至四已落地，文档保留为设计记录）。

## ✅ 已实现能力速览

| 能力 | 实现位置 |
| :--- | :--- |
| EPUB 多层级嵌套目录 + 多卷合并树状 TOC | `book/` / `merge/` / `book.export_epub` |
| 专有名词全书跨度采样（前/中/后分区、预算放大、轮次偏移） | `sampling.py` |
| EPUB 导出期文本清洗管线（Export Sanitizer） | `sanitizer.py` |
| 成品 EPUB 词表无损原地矫正 | `epub_fixer.py` |
| 中文 EPUB 第一人称改第三人称（独立 Tab、失败块存档、原样复制资源） | `perspective/` / `gui/` |
