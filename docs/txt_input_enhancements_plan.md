# TXT 纯文本输入解析与排版增强方案（TXT Input Enhancements）

## 一、 背景与痛点分析

虽然系统支持 `.epub` 与 `.txt`，但纯文本（`.txt`）小说文件结构松散、格式多样，当前实现（`book.py:parse_txt`）在面对韩网真实 TXT 文件时存在以下典型问题：
1. **非标标题识别率低**：遇到 `#01`、`EP.12`、`[1화]`、带副标题的行时无法识别为章节，导致整本书被当作一个大文件。
2. **退化硬切破坏上下文**：未识别出章节时，直接按 8000 字生硬切分，容易打断高潮场景与对话。
3. **硬换行与碎片段落**：网文常在一句话中间硬回车，导致段落破碎、Token 浪费严重。
4. **导出 EPUB 样式单调**：TXT 转 EPUB 缺乏基础样式，无段首缩进与现代排版。

---

## 二、 核心改进方案

### 1. 扩充韩网非标章节正则库（Smart Heading Regex）
扩展 `_CHAPTER_PATTERNS`，支持全套网文常见前缀：
- **符号/编号型**：`^\s*(?:#|EP\.?|No\.)\s*\d+`、`^\s*\[\s*\d+\s*(?:화|장)?\s*\]`、`^\s*〈\s*\d+\s*〉`
- **复合副标题**：`^\s*(?:제\s*)?\d+\s*(?:화|장|편)\s*[\.\:\-\~]\s*(.+)`
- **英文/罗马数字**：`^\s*(?:CHAPTER|ACT|EPISODE)\s*([IVXLCDM\d]+)`

### 2. 智能空行/分割线分章（Fallback Semantic Split）
当未命中任何正则标题时，取代生硬 8000 字截断：
- 优先检测文本中的分隔线（`***`、`---`、`◆◆◆`、`===`）与大空行（连续 2 个以上换行）。
- 切分必须在段落结束处（确保句号/问号/引号闭合后）进行，保持叙事连贯。

### 3. 段落合并与爬虫广告清洗（Paragraph Unwrapper）
- **硬换行合并**：若上一行未以句末标点（`.` `!` `?` `"` `”` `~`）结束且下一行非对话引号，自动合并为一段。
- **噪声过滤**：自动识别并剔除前 10 行与末尾 10 行常见的爬虫站点广告、TXT 分享者签名。

### 4. TXT 导出 EPUB 时自动注入现代精美排版 CSS
自动为从 TXT 生成的 EPUB 挂载标准小说排版模板：
```css
body {
    font-family: "PingFang SC", "Microsoft YaHei", "Source Han Sans", sans-serif;
    line-height: 1.8;
    color: #2c3e50;
    margin: 5% 8%;
}
h1, h2 {
    text-align: center;
    margin-top: 1.5em;
    margin-bottom: 1.2em;
    font-weight: bold;
}
p {
    text-indent: 2em;
    margin-top: 0;
    margin-bottom: 0.8em;
    text-align: justify;
}
```
