# 韩语小说批量翻译工具

这是一个基于 Python + CustomTkinter 的桌面工具，用来把韩语 `.txt` / `.epub` 小说批量翻译成简体中文。它面向长篇小说设计：分章解析、按批翻译、自动提取专有名词词表、人工确认词表、断点续传、自动重组成 TXT / EPUB，并能在成品上做多卷合并修正与第一人称改第三人称。

一句话概括：**选书 → 提取词表 → 确认词表 → 开始翻译 → 输出 TXT/EPUB**；需要时再用「多卷修正」按最新词表做替换，或用「视角转换」改人称。

---

## 快速开始

### 方式一：双击 `start.bat`（推荐，零依赖）

把整个项目放到任意目录，直接双击 `start.bat` 即可。脚本会自动完成环境准备：

1. 若项目目录下已有 `.venv\Scripts\python.exe`，直接沿用。
2. 没有 `.venv` 时，优先用系统里已有的 `python` / `py` 创建虚拟环境。
3. 系统里也没有 Python 时，会自动下载 `python-3.12.10-amd64.exe` 到项目目录 `.python\`，安装到本地（不写系统、不改 PATH），再据此创建 `.venv`。
4. 确保依赖已安装：执行 `pip install -r requirements.txt`，成功后写入标记，后续启动自动跳过。
5. 完成校验后启动 `main.py` 并打开 GUI。

> 依赖与 Python 只在首次或环境缺失时准备一次；再次双击会直接进入 GUI。`start.bat` 是 GBK/ANSI 编码，中文提示在默认 Windows 终端下能正常显示。

### 方式二：手动装环境

如果你已装好 Python 3.10+，可以手动初始化：

```powershell
cd <项目目录>
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

> 若系统没有 `py`，把 `py -m venv` 换成 `python -m venv` 即可。

---

## 功能

### 输入与解析

- 支持 `.txt` 和 `.epub` 输入，可一次添加多本（同一小说的不同卷），按列表顺序批量处理。
- 自动识别章节目录，保留段落与章节结构。
- 支持 EPUB 多层级嵌套目录，以及多卷按顺序合并成树状 TOC。

### 样式与格式保留

- 翻译 EPUB 时保留段落样式：块级 tag/class/内联样式与父容器结构（`div` / `section` 等）逐一还原；章节内联 CSS、原书 CSS 文件及其引用的字体、背景图一并复制进输出书，译文段落自动套回对应样式。
- 局部行内格式以 `⟦标签⟧` 形式随正文传递并还原：加粗 / 斜体 / 下划线、彩色字、字体变色（`span` / `font` 内联样式）、插图、脚注引用。
- TXT 输出自动去掉行内标记（插图显示为 `【插图】`），词表采样同样忽略标记。
- CSS、字体、图片等静态资源写入翻译存档 JSON（base64 编码）；多卷拼合时直接复用：同名同内容自动去重共享，仅内容不同才改名隔离，正文插图标记随改名同步。

### 脚注与跨文件链接

- 原书脚注常用跨文件引用，例如正文引用指向独立注释页 `../Text/Section0011.html#cmm01`，注释页又反向指回正文章节。
- 导出时每个章节会重命名为 `chap_NNNN.xhtml`，程序会自动把这些跨文件 `href` 改写为合并后的实际文件名（按「卷 + 源文件名」定位，必要时按 `#锚点` 回查），确保导出后脚注的正向与反向链接都能正确落地。

### 专有名词词表

- 先读取样章，用 LLM 自动提取人名、地名、组织、设定术语等固定名词。
- 词表提供可视化列表，支持新增、编辑、删除、批量/全部确认、清理重复、点击列头排序（再次点击同列切换升降序）。
- 可在已有词表基础上对另一本书「提取更多词表」，自动跳过已有词条（已确认词条不会被覆盖）。
- 多本依次提取词表：第一本「新建」，后续每本自动走「提取更多」追加到同一词表；单本失败继续下一本，最后汇总失败清单。
- 提取时 LLM 会为每个词条自动建议「可能译法」；也可对已加载词表点「完善信息」补充可能译法、备注并自动发现人物昵称。
- 翻译时把词表注入提示词，并在结果中做保底替换，保证全书译名一致。
- 词条可填写「可能译法」（半角逗号分隔）：提示词只提供人工确认译名；回传检出可能误译时自动替换为确认译名（仅已确认词条生效，单字变体自动忽略，1-2 字变体保存时会提醒）。
- 人物昵称 / 短称单独建条目（`kind=person-nickname`，note 注明「某某的昵称」）：提取时 LLM 按要求自动拆分，全名与昵称互不干扰。
- 「完善信息」先检测昵称（原文搜索验证后新增 `person-nickname`），再补可能译法；按钮旁两个勾选框「补可能译法 / 检测昵称」默认全开，可单独关掉任一步。
- 词表记录译名变更历史（`zh_history`）：修改确认译名后，「多卷修正」会自动生成「旧译名 → 新译名」替换对，无需重新翻译。
- 旧词表中可能译法若是全名的子串（如「范镇」）仍默认视为合法短称不替换；可勾选「替换短称」强制替换为全名。

### 翻译与断点续传

- 自动分片：按字符数把章节切成小批次发送给 LLM。
- 自动解析返回的 JSON 段落并重组小说。
- 断点续传：每完成一个批次就落盘，中断后可继续。
- 支持失败块保留原文并提示，重新运行只会补失败块。
- 支持响应式停止与并发：可根据本地网关调整并发数。

### 多卷修正

- 「多卷修正」tab 可同时添加多卷翻译存档（`.translation_state.json`）按顺序拼合，每卷开头自动插入「{合并书名} 第X卷」章节，按当前词表机器修正后输出 TXT/EPUB，支持先预览替换统计。
- 新增「校验并重试」：检查存档里的失败块并自动补翻（最多 3 次/块），仍有失败块的存档不会参与「修正并输出」。

### 视角转换（第一人称 → 第三人称）

- 独立「视角转换」tab：对已经合并完成的中文 EPUB 执行第一人称改第三人称，默认只处理叙述性旁白，保护对白、书信/聊天、日记、引用和内心独白。
- 可选择主角名称、简称、代词和替换风格。
- 采用 EPUB ZIP 原样复制：只改写正文 XHTML/HTML 的可见文本节点，CSS、图片、字体、OPF、目录及其它资源按原字节保留，输出新的第三人称 EPUB，不覆盖输入文件。
- 提供「策略 1：保守筛选」和「策略 2：正文全覆盖」；策略 2 纳入全部非标题、非代码正文，由模型区分旁白与人物原话。
- 独立保存 `perspective_state` 状态：每块记录原文分段、译文分段和失败原因；「查看失败块」支持单块切换模型/手动补写，也支持批量逐块重试，再重新生成输出 EPUB。
- 支持响应式停止：网络请求等待和重试等待都会监听「停止」，已完成块会保存并输出，剩余块可再次开始转换续传。

### 输出

- 输出文件按「小说名 第X卷.zh.txt/.epub」命名（填了小说名时），否则按源文件名命名（源文件通常已含卷号）。
- 断点续传存档（`.translation_state.json`）同样按「小说名 第X卷」命名（隐藏文件，需开启「显示隐藏项目」可见），文件清洗规则保留韩文/中日韩字符；找不到新命名时会自动回退读取旧规则存档。
- 支持输出 `TXT` 和 `EPUB` 两种格式。

---

## 安装依赖

依赖见 [requirements.txt](requirements.txt)：

```text
openai>=1.40
customtkinter>=5.2.2
ebooklib>=0.18
beautifulsoup4>=4.12
lxml>=5.0
```

建议使用 Python 3.10+；`start.bat` 自动安装版固定用 Python 3.12.10。

---

## 启动 GUI

最省事的方式是双击 `start.bat`。若已手动装好环境，也可以：

```powershell
py main.py
```

### GUI 操作流程

1. 点击「添加 .txt/.epub」选择一本或多本（同一小说的不同卷），列表顺序即卷序；可用上移/下移调整顺序。
2. 可选填「小说名」：填了则输出为「小说名 第X卷.zh.txt/.epub」，留空则按源文件名命名。
3. 选择输出目录，填写 `Base URL`、`API Key`、`Model`。
4. 点击「提取词表」：第一本新建词表，后续每本自动追加（重复词条自动跳过，已确认词条不覆盖）。
5. 在词表列表中人工修正、新增、删除、标记确认。
6. 点击「开始翻译」：逐本翻译、逐本输出，单本失败继续下一本，最后汇总失败清单。
7. 若有失败块，可在「多卷修正」tab 添加对应存档后点「校验并重试」补翻（最多 3 次/块），仍有失败块的存档不会参与「修正并输出」。
8. 需要把已完成的中文 EPUB 改成第三人称时，打开「视角转换」tab，选择输入/输出文件，填写叙述者名称，先预览保护范围，再开始转换；失败块可在同一 tab 中查看和手动补写。
9. 关闭 GUI 后可点击「加载上次作业」，直接选择 `.perspective_state.json`；程序会恢复输入/输出路径、视角参数和块状态，点击「开始转换」即可继续并生成 EPUB。

---

## 命令行模式

```powershell
py main.py --input "D:\books\novel.epub" --output "D:\books\zh" --glossary "glossary.json"
```

常用参数：

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `--input` | 输入文件，`.txt` 或 `.epub`；不传则启动 GUI | 无 |
| `--output` | 输出目录 | `output` |
| `--glossary` | 已有词表 JSON 文件 | 无 |
| `--config` | 配置 JSON 文件（字段见下） | 无 |
| `--model` | 覆盖模型名 | 无 |
| `--base-url` | 覆盖 API `base_url` | 无 |
| `--api-key` | 覆盖 API key | 无 |
| `--workers` | 并发线程数 | `1` |
| `--chunk-chars` | 每批原文字符数 | `1800` |
| `--no-extract` | 翻译前不自动提取词表 | 关 |

> 命令行默认 `Base URL` 为 `http://localhost:8045/v1`，可用 `--base-url` 覆盖。

### 配置 JSON（`--config`）

`--config` 会按项目配置结构加载，可覆盖常用项：

```json
{
  "base_url": "http://localhost:8045/v1",
  "model": "gemini-3.7-flash-tiered",
  "api_key": "sk-...",
  "chunk_chars": 1800,
  "max_workers": 1,
  "max_retries": 4,
  "timeout": 600.0,
  "temperature": 0.2,
  "extract_glossary": true,
  "extract_sample_chars": 30000,
  "extract_sample_chapters": 6,
  "glossary_limit": 120,
  "output_txt": true,
  "output_epub": true,
  "output_encoding": "utf-8-sig",
  "resume": true
}
```

---

## 词表 JSON 格式

词表可以手工维护，示例：

```json
{
  "source_text": "",
  "entries": [
    {
      "ko": "김하늘",
      "zh": "金夏恩",
      "kind": "person",
      "note": "女主",
      "confirmed": true
    },
    {
      "ko": "서울특별시",
      "zh": "首尔特别市",
      "kind": "place",
      "note": "",
      "confirmed": false
    }
  ]
}
```

- `kind` 可取：`person`、`person-nickname`、`place`、`org`、`term`、`title`。
- `zh_history`：该词条此前确认过的译名列表（自动记录）；「多卷修正」时用于把旧译文中的旧译名替换为新译名。
- `confirmed` 只作为人工确认标记，翻译时不会因为未确认而跳过词条。

---

## 断点续传

状态文件自动保存在输出目录下，文件名形如 `.<书名>.translation_state.json`。翻译中断后再次运行同一输入文件、同一输出目录，会自动跳过已完成批次，只补剩余部分。

这些存档也可在「多卷修正」tab 中按顺序拼合，按最新词表机器修正后重新输出 TXT/EPUB（无需重新翻译）。

---

## 输出文件

输出目录中会生成：

```text
书名.zh.txt
书名.zh.epub
.<书名>.translation_state.json       # 翻译断点存档（隐藏文件）
书名.第三人称.epub                    # 视角转换产物
视角转换状态文件（perspective_state）
```

TXT 使用 `utf-8-sig` 编码，Windows 记事本可直接打开。

EPUB 样式保留的边界：正文段落的块级样式与父容器结构会完整还原，但封面图、原书目录页/部标题页的固定排版、以及 PDF 式固定版式（fixed-layout）目前不复制；译文书籍的封面为空白，需在阅读器中自行设置。

---

## 目录结构

```text
hangulTranslate/
├─ start.bat                          # 一键启动：检测/下载 Python、建 .venv、装依赖、启动 GUI
├─ main.py                            # 入口：GUI（默认）或命令行（--input 参数）
├─ requirements.txt
├─ README.md
├─ docs/                              # 设计文档与待办索引
│  ├─ README.md
│  ├─ settings_tab_plan.md
│  └─ TODO.md
├─ hangul_novel_translator/
│  ├─ __init__.py
│  ├─ gui/                            # 图形界面（主窗口、主题、状态、控件、对话框）
│  │  ├─ __init__.py
│  │  ├─ app.py                       # 主窗口 App（薄壳：聚合各 View mixin）+ run_gui
│  │  ├─ views/                       # 各 tab 的 View mixin（App 通过继承聚合实现）
│  │  │  ├─ __init__.py
│  │  │  ├─ shell_mixin.py            # 布局/分栏/窗口几何/翻译主流程
│  │  │  ├─ left_panel_mixin.py       # 左侧输入文件与 API/翻译参数
│  │  │  ├─ glossary_mixin.py         # 词表 tab
│  │  │  ├─ settings_mixin.py         # 设置 tab
│  │  │  ├─ fixer_mixin.py            # 成品矫正 tab
│  │  │  ├─ merge_mixin.py            # 多卷修正 tab
│  │  │  └─ perspective_mixin.py      # 视角转换 tab
│  │  ├─ theme.py                     # 配色与 Tk 主题
│  │  ├─ state.py                     # .gui_config.json 读写（窗口尺寸/分栏宽度）
│  │  ├─ widgets.py                   # TreeviewTooltip、DebouncedScrollableFrame
│  │  └─ dialogs/                     # 每个对话框一个子模块
│  │     ├─ __init__.py
│  │     ├─ glossary_edit.py          # 词条编辑弹窗
│  │     ├─ sanitizer_rule.py         # 导出清洗规则弹窗
│  │     ├─ failed_chunk.py           # 失败块查看/补翻
│  │     ├─ malformed_block.py        # 畸形块查看/批量修复/回翻
│  │     └─ perspective_failed.py     # 视角转换失败块查看/重试
│  ├─ config.py                       # 配置（AppConfig）
│  ├─ llm.py                          # OpenAI 兼容客户端
│  ├─ book/                           # TXT/EPUB 解析与导出（含脚注链接改写）
│  │  ├─ __init__.py
│  │  ├─ models.py                    # BlockStyle/ParagraphStyle/Chapter/Book
│  │  ├─ markup.py                    # 行内格式/标记
│  │  ├─ metadata.py                  # 元数据与封面/字体回退
│  │  ├─ txt.py                       # TXT 解析与导出
│  │  ├─ text.py                      # 共用文本工具
│  │  └─ epub.py                      # EPUB 读取与导出
│  ├─ glossary/                       # 专有名词词表
│  │  ├─ __init__.py
│  │  ├─ models.py                    # GlossaryEntry/Glossary
│  │  ├─ extract.py                   # LLM 抽取/丰富
│  │  └─ payload.py                   # 载荷解析
│  ├─ merge/                          # 多卷修正：存档重组、拼合、词表修正与输出
│  │  ├─ __init__.py
│  │  ├─ state.py                     # 存档审查/审计/重组
│  │  ├─ resources.py                 # CSS/图片等静态资源合并
│  │  ├─ books.py                     # 书目拼合与词表修正
│  │  └─ export.py                    # 导出
│  ├─ perspective/                    # 中文 EPUB 第一人称改第三人称
│  │  ├─ __init__.py
│  │  ├─ models.py                    # 选项/块/失败块
│  │  ├─ analysis.py                  # 节点扫描与分类
│  │  ├─ rewrite.py                   # 改写载荷
│  │  ├─ state.py                     # 存档读写
│  │  └─ converter.py                 # PerspectiveConverter
│  ├─ translator/                     # 分片、翻译、续传、组装、失败块重试
│  │  ├─ __init__.py
│  │  ├─ models.py                    # Chunk/FailedChunk/TranslationResult
│  │  ├─ response.py                  # 模型响应清洗/归一化
│  │  ├─ malformed.py                 # 畸形块检测/修复
│  │  ├─ chunking.py                  # 分块与采样
│  │  ├─ state.py                     # 翻译存档读写
│  │  └─ pipeline.py                  # Translator 编排
│  ├─ sampling.py                     # 专有名词全书跨度采样
│  ├─ sanitizer.py                    # 导出期文本清洗（Export Sanitizer）
│  ├─ epub_fixer.py                   # 成品 EPUB 词表无损原地矫正
│  ├─ epub_validator.py               # EPUB 成品校验
│  └─ utils.py                        # 通用工具
├─ tests/                             # 单元测试
│  ├─ test_core.py
│  ├─ test_epub_fixer.py
│  ├─ test_epub_validator.py
│  ├─ test_glossary.py
│  ├─ test_merge.py
│  ├─ test_perspective.py
│  ├─ test_sanitizer.py
│  ├─ test_strided_sampling.py
│  ├─ test_translator.py
│  └─ test_txt_parser.py
└─ .venv/                             # 虚拟环境（自动生成，无需提交）
```

> 首次运行 `start.bat` 还会在项目目录下生成 `._setup\`（下载与依赖标记）和 `.python\`（本机 Python），均已在 `.gitignore` 中忽略。

---

## 测试

项目内置单元测试，可对整个 `tests/` 目录执行：

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

若只跑某个文件：

```powershell
.venv\Scripts\python.exe -m unittest tests.test_core -v
```

其中 `tests/test_core.py` 包含 EPUB 跨文件脚注链接改写、TXT/EPUB 解析与导出等相关用例。

---

## 大文件建议

- 默认每批 `1800` 个字符，40 万字大约 220–260 个批次。
- 模型输出要求为 JSON 段落数组；若返回格式异常，程序会自动降级为按行解析。
- 如果本地网关支持并发，建议逐步提高「并发数」；过高可能触发限流，失败块会在下一次运行时重试。
- 翻译前建议先跑一个小章节确认模型、提示词和词表效果，再整本翻译。

---

## 常见问题

**双击 `start.bat` 没反应或一闪而过？**
在资源管理器地址栏输入 `cmd` 回车打开终端，再 `cd` 到项目目录执行 `start.bat`，可看到完整输出与错误信息。

**下载依赖失败？**
检查网络后重试；`start.bat` 会在依赖装好前不启动主程序。也可按「手动装环境」的步骤用 pip 手动装依赖。

**中文乱码？**
本工具输出文件用 `utf-8-sig` 编码，Windows 记事本可直接打开。`start.bat` 的提示为 GBK 编码，请在默认中文 Windows 控制台运行。

**命令行跑不起来 GUI？**
GUI 依赖桌面环境；若在纯命令行环境用 `--input` 参数走 CLI 模式即可。

---

## 相关文档

- [`docs/README.md`](docs/README.md)：设计方案与已实现能力索引。
- [`docs/TODO.md`](docs/TODO.md)：尚未落地的功能待办。
- [`BAT脚本详解.md`](BAT脚本详解.md)：批处理入门教程（示例基于历史版本的 `start.bat`）。
