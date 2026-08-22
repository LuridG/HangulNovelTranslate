# 「设置」Tab 方案（Settings Tab Plan）

> 目标：新增一个独立的「设置」tab，把当前散落在左侧面板的配置项集中、补齐持久化，并新增
> 「LLM API 预设一键切换」「词表字体大小」等可调能力。本文档为设计方案，落地进度见
> [`TODO.md`](./TODO.md)。

---

## 一、现状与痛点

1. **设置分散**：Base URL / API Key / Model、每批字符数、并发数、三个开关目前都放在左侧面板
   （`hangul_novel_translator/gui.py` 的 `_build_left`，约 897–921 行），与词表、多卷修正、成品矫正
   三个 tab 混在一起，没有独立入口。
2. **几乎不持久化**：`.gui_config.json` 目前只保存窗口几何 / 窗口状态 / 清洗规则（`sanitizer_config`），
   见 `gui.py` 的 `_save_window_geometry` 与 `_on_close`。**Base URL / API Key / Model / 每批字符 / 并发数
   重启即丢失**，回到 `config.py` 里的默认值。
3. **字体写死**：词表树、输入文件列表字号是硬编码 `size=13`、`rowheight=30/34`
   （`gui.py` 约 847、971 行），用户无法调整，长时间使用易疲劳。
4. **只切一个模型**：只能手填单一 `base_url/api_key/model`，没有多套 API 预设，切换模型需反复粘贴。
5. **部分配置没暴露**：`config.py` 里已有 `max_retries`、`timeout`、`temperature`、
   `extract_sample_chars`、`extract_sample_per_region`、`glossary_limit` 等，GUI 均未开放。

---

## 二、目标

- 新增「设置」tab，作为第 4 个 tab（词表 / 多卷修正 / 成品矫正 / 设置）。
- 所有常用配置**持久化**到 `.gui_config.json`，重启保持。
- 支持 **LLM API 预设**：保存多套 `base_url/api_key/model`，一键切换。
- 支持 **词表字体大小 / 行高**，改动即时生效。
- 逐步暴露高级项：词表采样预算、翻译容错、外观主题、输出编码等。

---

## 三、方案总览

### 1. 通用设置（迁移 + 持久化）

把左侧面板的常用项迁入设置 tab，并全部落盘：

- `base_url`、`api_key`、`model`
- `chunk_chars`（每批字符数）、`max_workers`（并发数）
- 开关：`extract_glossary`（翻译前自动提取词表）、`output_txt`、`output_epub`
- `output`（默认输出目录）

> 建议：左侧保留常用 API / 参数入口，设置 tab 放全量 + 高级项，两者**双向同步**，避免常用项被埋深。

### 2. LLM API 预设（核心新增）

- 数据结构：`llm_profiles: list[{name, base_url, api_key, model, note}]`。
- 设置 tab 内提供预设列表，支持**新增 / 编辑 / 删除 / 上移 / 下移 / 重命名**。
- 「当前使用」下拉框：选中某预设即把 `base_url/api_key/model` 写回 `base_url_var` 等并刷新。
- 预填当前默认值作为第一个预设，保证开箱即用。
- 联动：`失败块查看 / 手动编辑` 弹窗内的 Base URL / API Key / Model 改为可选「用所选预设」，
  实现换模型补翻失败块。

### 3. 界面字体大小（核心新增）

- 新增 `glossary_font_size`（词表树字号）、`row_height`（行高，随字号联动）、
  可选 `input_font_size`（输入文件列表）。
- 用下拉 / 滑杆选择（建议 11–18），**改完即时**重设 `Glossary.Treeview`、`Input.Treeview`
  的字体与行高，无需重启，并持久化。
- 可作后续增量：日志框、按钮字号、整体 UI 缩放。

### 4. 其它可调项（按价值排序）

- **词表采样**：`extract_sample_chars`（样章字符预算）、`extract_sample_chapters` / `extract_sample_per_region`、
  `glossary_limit`（词条上限）。
- **翻译容错**：`max_retries`、`retry_delay`、`timeout`、`temperature`。
- **外观**：`appearance_mode`（dark / light / system）、颜色主题（当前写死 `dark-blue`）。
- **输出**：`output_encoding`（默认 `utf-8-sig`）、是否保留行内标记、插图占位符文案。

---

## 四、持久化与安全

- 所有设置统一写入 `.gui_config.json`（沿用现有 `_load_ui_state` / `_save_ui_state`）。
- 新增顶层键：`app_config`（常用+高级参数）、`llm_profiles`、`appearance`、`fonts`。
- `api_key` 以**明文**存储（与现状一致）。建议确认 `.gitignore` 已忽略 `.gui_config.json`；
  可提供「仅本次会话不落盘」选项，以及默认**打码显示**、可切换明文。

---

## 五、分阶段落地

| 阶段 | 内容 | 风险 |
| :--- | :--- | :--- |
| **阶段一** | 新增「设置」tab；迁移左侧 API / 参数项；**补全持久化**（`app_config`） | 低，不触碰翻译/合并逻辑 |
| **阶段二** | LLM API 预设列表 + 一键切换；失败块弹窗用预设下拉 | 中，涉及配置结构 |
| **阶段三** | 词表/输入列表字体大小、行高即时生效 | 低 |
| **阶段四** | 采样参数、容错、主题、输出编码等高级项；与 `--config` 命令行对齐 | 中，需回归 |

---

## 六、需拍板的问题

1. **左侧面板**：保留常用项、设置 tab 放全量（推荐），还是全部收进设置 tab？
2. **API 预设**：是否支持「导出 / 导入 JSON」、以及「密钥默认打码/可切换明文」？
3. **字体大小**：先只做词表树 + 行高，还是连输入列表、日志、按钮一起做？
4. **api_key 落盘**：默认是否记住，还是默认「仅本次会话」？

确认方向后建议从**阶段一**开始实现（最安全、收益最大）。
