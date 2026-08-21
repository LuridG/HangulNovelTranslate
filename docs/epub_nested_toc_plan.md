# EPUB 多层级嵌套目录（Treeview TOC）与多卷合并支持设计方案

## 一、 背景与痛点

1. **现状现象**：
   - 现阶段导出的单卷与多卷 EPUB 在阅读器（如 Apple Books、微信读书、Kobo 等）中打开时，目录均为一维线性平铺列表。
   - 正文里虽然生成了 `<h1>`（卷标题）与 `<h2>`（章标题），但目录无法进行下拉折叠（Accordion 展开/收起效果），需要用户在 Sigil 等编辑器中手动“根据标题重新生成目录”。
2. **多卷/多层级诉求**：
   - 原始 EPUB 可能本身就包含多级嵌套结构（如：`第1篇 -> 第1章 -> 第1节`）。
   - 用户希望在多卷合并时：
     1. 完整保留原书各卷自身的子级嵌套目录。
     2. 在最外层自动追加一层“第 X 卷”的顶级目录节点。
     3. 导出符合 EPUB 规范（EPUB 2 NCX + EPUB 3 Nav）的真正树状嵌套目录。

---

## 二、 核心原理分析

### 1. 阅读器折叠目录的决定因素
- 阅读器对目录的折叠/展开效果，只认 `toc.ncx`（EPUB 2）和 `nav.xhtml`（EPUB 3）中 XML 节点的**树状父子嵌套关系**，而不取决于正文 HTML 中的 `<h1>`/`<h2>` 标签。
- **平铺结构（当前现状）**：
  ```xml
  <navPoint id="1"><navLabel><text>第1卷</text></navLabel><content src="chap_0000.xhtml"/></navPoint>
  <navPoint id="2"><navLabel><text>第 1 章</text></navLabel><content src="chap_0001.xhtml"/></navPoint>
  ```
- **树状结构（目标效果）**：
  ```xml
  <navPoint id="1">
    <navLabel><text>第1卷</text></navLabel>
    <content src="chap_0000.xhtml"/>
    <navPoint id="2">
      <navLabel><text>第 1 章</text></navLabel>
      <content src="chap_0001.xhtml"/>
    </navPoint>
  </navPoint>
  ```

### 2. `ebooklib` 库的树状语法
Python `ebooklib` 原生支持嵌套元组结构构造 TOC：
```python
out.toc = (
    # 第1卷
    (
        epub.Section("第 1 卷", href="chap_0000.xhtml"),
        (
            # 原始二级嵌套
            (
                epub.Section("第 1 篇 序幕", href="chap_0001.xhtml"),
                (
                    epub.Link("chap_0002.xhtml", "第 1 节", "c2"),
                    epub.Link("chap_0003.xhtml", "第 2 节", "c3"),
                )
            ),
            epub.Link("chap_0004.xhtml", "第 2 篇 ...", "c4"),
        )
    ),
    # 第2卷
    (
        epub.Section("第 2 卷", href="chap_0005.xhtml"),
        (...)
    )
)
```

---

## 三、 改动设计方案

### 1. 数据结构扩展 (`hangul_novel_translator/book.py`)

#### (1) `Chapter` 增加层级与父子关系标记
```python
@dataclass
class Chapter:
    index: int
    title: str
    paragraphs: list[str] = field(default_factory=list)
    source_id: str = ""
    styles: list[ParagraphStyle | None] = field(default_factory=list)
    title_zh: str = ""
    heading_level: int = 1     # 对应 h1/h2/h3 (1-6)
    parent_index: int | None = None  # 父级章节 index（用于重构树结构）
    is_section: bool = False   # 是否作为目录卷/篇的容器节点
```

#### (2) 原始 EPUB 读取改造 (`load_book_from_epub`)
- 不直接使用扁平化函数丢弃层级，而是在解析 `book.toc` 时递归保留树形深度 `depth`。
- 将 `depth` 映射为 `heading_level`（如 depth 0 -> h1, depth 1 -> h2, depth 2 -> h3）。

---

### 2. 翻译状态存储与传递 (`.translation_state.json`)

#### (1) 扩展 `chapter_titles`
```json
{
  "chapter_titles": {
    "0": { "ko": "제1편", "zh": "第1篇", "heading_level": 1, "parent_index": null },
    "1": { "ko": "제1장", "zh": "第1章", "heading_level": 2, "parent_index": 0 }
  }
}
```
- 保留 `heading_level` 与 `parent_index`，确保多卷合并读取该 json 存档时可以无损还原原书的多层目录树。

---

### 3. 多卷合并逻辑改造 (`hangul_novel_translator/merge.py`)

#### (1) 合并时层级自动下沉（`level += 1`）
- 为每卷插入的顶层卷标题设为 `heading_level = 1`, `is_section = True`。
- 卷内包含的所有原始章节，层级整体下沉一级（原 h1 变 h2，原 h2 变 h3，最高不超过 h6）。
- 子章节的 `parent_index` 指向对应的父节点或卷节点。

---

### 4. EPUB 导出模块改造 (`export_epub`)

- 编写通用的递归树构建函数 `_build_epub_toc_tree(chapters, chapter_items)`：
  - 根据 `Chapter.parent_index` 或分层结构，将 `chapter_items` 组装为嵌套的 `(epub.Section(...), (children...))` 元组。
  - 赋值给 `out.toc`，自动生成支持多层折叠的 `toc.ncx` 与 `nav.xhtml`。

---

## 四、 影响评估与兼容性

1. **向后兼容**：
   - 现有的旧 `.translation_state.json` 若没有 `parent_index` 和 `heading_level`，默认退化为单层/平铺模式，不影响旧项目合并。
2. **纯前端/业务安全性**：
   - 翻译核心引擎（LLM 提示词、分块器、多线程工作流、词表替换）完全不受影响。
   - 仅在导入/导出 EPUB 的包装层进行结构升级。
