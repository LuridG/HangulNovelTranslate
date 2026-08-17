# 韩语小说批量翻译工具

这是一个 Python + CustomTkinter 的桌面工具，用于把韩语 `.txt` / `.epub` 小说批量翻译成简体中文。它面向长篇小说设计：分章解析、按批翻译、自动提取专有名词词表、人工确认词表、断点续传、自动重组成 TXT/EPUB。

## 功能

- 支持 `.txt` 和 `.epub` 输入
- 自动识别章节目录，保留章节与段落结构
- 先读取样章，用 LLM 自动提取人名、地名、组织、设定术语等固定名词
- 词表提供可视化列表，支持新增、编辑、删除、批量/全部确认、清理重复、点击列头排序（再次点击同列切换升降序）
- 可在已有词表基础上对另一本书“提取更多词表”，自动跳过已有词条（已确认词条不会被覆盖）
- 提取词表时 LLM 会为每个词条自动建议“可能译法”；也可对已加载词表点“完善信息”补充可能译法、备注并自动发现人物昵称
- 翻译时把词表注入提示词，并在结果中做保底替换，保证全书译名一致
- 词条可填写“可能译法”（半角逗号分隔）：提示词只提供人工确认译名；回传检出可能误译时自动替换为确认译名（仅已确认词条生效，单字变体自动忽略，1-2 字变体保存时会提醒）
- 人物昵称/短称单独建条目（kind=person-nickname，note 注明“某某的昵称”）：提取时 LLM 按要求自动拆分，全名与昵称互不干扰
- “完善信息”会先让 LLM 分析 person 词条的可能昵称，再在原文中搜索韩文昵称，确认独立出现后自动新增 person-nickname 词条
- 旧词表中可能译法若是全名的子串（如“范镇”）仍默认视为合法短称不替换；可勾选“替换短称”强制替换为全名
- 自动分片：按字符数把章节切成小批次发送给 LLM
- 自动解析返回的 JSON 段落并重组小说
- 断点续传：每完成一个批次就落盘，中断后可继续
- 支持失败块保留原文并提示，重新运行会只补失败块
- 输出 `TXT` 和 `EPUB` 两种格式

## 目录结构

```text
hangulTranslate/
├─ main.py
├─ requirements.txt
└─ hangul_novel_translator/
   ├─ config.py        # 配置
   ├─ llm.py           # OpenAI 兼容客户端
   ├─ book.py          # TXT/EPUB 解析与导出
   ├─ glossary.py      # 专有名词词表
   ├─ translator.py    # 分片、翻译、续传、组装
   └─ gui.py           # CustomTkinter 界面
```

## 安装

建议使用 Python 3.10+。

```powershell
cd G:\MiniProject2026\hangulTranslate
py -m pip install -r requirements.txt
```

如果系统没有 `py`，可改用 `python -m pip install -r requirements.txt`。

## 启动 GUI

```powershell
py main.py
```

GUI 操作流程：

1. 选择输入 `.txt` 或 `.epub`，选择输出目录。
2. 填写 `Base URL`、`API Key`、`Model`。
3. 点击“提取词表”，程序读取前几章并生成词表。
4. 在词表列表中人工修正、新增、删除、标记确认。
5. 点击“开始翻译”。

## 命令行模式

```powershell
py main.py --input "D:\books\novel.epub" --output "D:\books\zh" --glossary "glossary.json"
```

常用参数：

- `--input`：输入文件；不传则启动 GUI
- `--output`：输出目录
- `--glossary`：已有词表 JSON
- `--model` / `--base-url` / `--api-key`：覆盖 GUI 默认配置
- `--workers`：并发线程数，默认 1；本地网关稳定时可提高到 3–5
- `--chunk-chars`：每批原文字符数，默认 1800
- `--no-extract`：跳过自动提取词表

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

- `kind` 可取：`person`、`place`、`org`、`term`、`title`
- `confirmed` 只作为人工确认标记，翻译时不会因为未确认而跳过词条

## 断点续传

状态文件自动保存在输出目录下，文件名形如 `.<书名>.translation_state.json`。翻译中断后再次运行同一输入文件、同一输出目录，会自动跳过已完成批次，只补剩余部分。

## 大文件建议

- 默认每批 `1800` 个字符，40 万字大约 220–260 个批次。
- 模型输出要求为 JSON 段落数组；若返回格式异常，程序会自动降级为按行解析。
- 如果本地网关支持并发，建议逐步提高 `并发数`；过高可能触发限流，失败块会在下一次运行时重试。
- 翻译前建议先跑一个小章节确认模型、提示词和词表效果，再整本翻译。

## 输出

输出目录中会生成：

```text
书名.zh.txt
书名.zh.epub
.<书名>.translation_state.json
```

TXT 使用 `utf-8-sig` 编码，Windows 记事本可直接打开。
