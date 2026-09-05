# TODO

> 本文件汇总 `docs/` 中尚未落地的功能方案（原方案 05「进阶功能」、06「TXT 增强」的剩余项）。
> 已实现的功能已在根目录 [`README.md`](../README.md) 与代码中体现，不再单独保留方案文档。

## 架构拆分（进行中）

已按职责把若干单体模块拆成包子模块，子模块 `__init__.py` 回导出全部公共接口，
对外 import 保持不变：

- `book.py` → `book/`（`models` 数据类 / `markup` 行内标记 / `metadata` 元数据 / `txt` 文本输入输出 / `epub` EPUB 读取与导出 / `text` 共用文本工具）
- `translator.py` → `translator/`（`models` / `response` 响应清洗 / `malformed` 畸形块 / `chunking` 分块 / `state` 存档 / `pipeline` 编排）
- `perspective.py` → `perspective/`（`models` / `analysis` 节点分析 / `rewrite` 改写 / `state` 存档 / `converter` 转换器）
- `merge.py` → `merge/`（`state` 状态审查 / `resources` 静态资源合并 / `books` 书目合并 / `export` 导出）
- `glossary.py` → `glossary/`（`models` / `extract` LLM 抽取 / `payload` 载荷解析）
- `gui/dialogs.py` → `gui/dialogs/`（每个对话框一个子模块）

拆分工具与导入校验见 [`tools/`](../tools/)。

- [x] `gui/app.py`（原来 2654 行的单体）按 tab 拆成 `gui/views/` 下的一组 View mixin，`App` 通过继承聚合实现；
  窗口几何 / 语言翻译主流程归入 `shell_mixin`，各 tab 的构建与事件归入对应 mixin。
- [x] 用 `start.bat` 人工验证 Tk/customtkinter 渲染与各 tab 交互（词表 / 多卷修正 / 成品矫正 / 设置 / 视角转换均正常）
- [ ] `.venv` 桌面环境复跑 `tests/`（bundled 运行时缺 `openai/bs4/ebooklib`，仅能覆盖纯逻辑部分）

## 第一人称改第三人称（已实现）

已完成：独立「视角转换」tab，输入已翻译/多卷合并后的中文 EPUB，默认只改写叙述性旁白；对白、书信/聊天、日记、引用和内心独白默认保护。支持主角名称、简称、代词、替换风格、外部词表、范围预览、独立状态文件、失败块查看、单块/批量切换模型和手动补写。

- [x] ZIP 原样复制 EPUB 资源，只修改选定正文文本节点
- [x] 保存每块原文分段、译文分段和失败原因，手动补写后可无模型重建输出
- [x] 失败块查看器支持批量逐块重试、进度显示和中途停止
- [x] 视角转换页支持直接加载 `.perspective_state.json`，恢复参数、路径和块状态后继续生成 EPUB
- [x] 视角转换支持保守筛选与正文全覆盖两种策略，策略随作业存档恢复
- [x] 工作区文件列表只显示 NFC 规范化文件名，悬停显示完整原始路径
- [x] 校验模型返回的 block/segment 数量，异常返回不写入正文
- [ ] 旁白与对白混合段落的精细拆分（当前含对白段落默认整体保护）
- [ ] 视角转换前后差异预览与逐句确认

## 排版规范化（Punctuation Normalizer）

已完成：韩式省略号 `...`/`..` → `……`、连续感叹/问号收敛（在导出清洗器 `sanitizer.py` 中）。

- [ ] 连续波浪号 `~~~` → `～` 或 `——`
- [ ] 盘古之白：中文与英文、数字之间插入半角空格（`第3卷`→`第 3 卷`、`VIP房间`→`VIP 房间`）
- [ ] 全中文语境下英文标点全角化（`,.?!:` → `，。！？：`）
- [ ] 独立的 **Punct Normalizer** 整体开关（当前仅作为导出清洗器的一部分）

## 失败块查看器（Failed Chunks Inspector）

已完成：批量「校验并重试」补翻失败块（最多 3 次/块）；「多卷修正」tab 新增「✏ 失败块查看」入口，
弹窗列出失败块（块 ID / 章名 / 失败原因）、展示持久化原文、可手动填译文或切换 Base URL/API Key/模型后用所选模型翻译，
保存即写入存档 `completed` 并从失败列表移除。

- [ ] 翻译结束且 `failed_chunks > 0` 时，主流程状态栏提示「⚠️ 存在 X 个未完成块 [查看详情]」按钮（当前入口在「多卷修正」tab，需手动打开）
- [ ] 单块精准重试：在弹窗里用「所选模型」翻译时，重试成功后自动续跑该存档剩余失败块（当前只翻译选中那一块）

## 词条例句速查（Glossary Concordance Search）

- [ ] 词表表格双击行或右键「🔍 检索全书例句」
- [ ] 弹窗在当前已载入书籍中正则匹配该韩文词汇，展示前 5~10 处上下文
- [ ] 原句中对专有名词高亮标红

## TXT 输入增强（TXT Input Enhancements）

- [ ] 扩充章节识别正则并接入 `book/txt.py::parse_txt`：`#01`、`EP.01`、`[1화]`、`〈01〉`、`No.12` 等非标前缀；复合副标题；英文/罗马数字章节
- [ ] 智能空行/分割线分章 fallback：未命中标题时检测 `***`、`---`、`◆◆◆`、`===` 与连续空行切分，替代按 8000 字硬切
- [ ] 硬换行合并（`unwrap_broken_paragraphs`）：上一行未以句末标点结尾且下一行非对话引号时合并为一段（逻辑已在 `tests/test_txt_parser.py`，需接入产品代码）
- [ ] 爬虫广告清洗：剔除正文首/末 10 行的广告与 TXT 分享者签名
- [ ] TXT 导出 EPUB 时自动注入现代小说排版 CSS（段首缩进、行高、页边距、居中标题等）

## 「设置」Tab

> 完整方案见 [`settings_tab_plan.md`](./settings_tab_plan.md)。

- [x] 阶段一：新增「设置」tab；迁移左侧 API / 翻译参数；补全 `.gui_config.json` 持久化（app_config）
- [x] 阶段二：LLM API 预设列表（增删改、上移下移、一键切换）；失败块弹窗支持手动模型配置
- [x] 阶段三：词表/输入列表字体大小、行高，改动即时生效
- [x] 阶段四：词表采样预算、翻译容错、外观主题、输出编码等高级项；与命令行配置对齐
