# main.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hangul_novel_translator.config import AppConfig
from hangul_novel_translator.glossary import Glossary


def _progress(stage: str, done: int, total: int, message: str) -> None:
    print(f"\r[{stage}] {done}/{total}  {message}", flush=True)


def run_cli(args: argparse.Namespace) -> int:
    from hangul_novel_translator.translator import Translator

    if args.config and Path(args.config).exists():
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        config = AppConfig.from_dict(data)
    else:
        config = AppConfig()

    config.extract_glossary = not args.no_extract
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key
    if args.workers:
        config.max_workers = args.workers
    if args.chunk_chars:
        config.chunk_chars = args.chunk_chars

    glossary = Glossary.load(Path(args.glossary)) if args.glossary else Glossary()
    translator = Translator(config, progress_callback=_progress)
    try:
        result = translator.translate_file(args.input, args.output, glossary)
    except Exception as exc:  # noqa: BLE001
        print(f"\n翻译失败：{exc}", file=sys.stderr)
        return 1

    print("\n翻译完成。")
    for path in result.output_paths:
        print(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="韩语小说批量翻译工具")
    parser.add_argument("--input", help="输入 .txt 或 .epub 文件；不传则启动 GUI")
    parser.add_argument("--output", default="output", help="输出目录，默认 output")
    parser.add_argument("--glossary", help="词表 JSON 文件（可选）")
    parser.add_argument("--config", help="配置 JSON 文件（可选）")
    parser.add_argument("--model", help="覆盖模型名")
    parser.add_argument("--base-url", help="覆盖 API base_url")
    parser.add_argument("--api-key", help="覆盖 API key")
    parser.add_argument("--workers", type=int, help="并发线程数")
    parser.add_argument("--chunk-chars", type=int, help="每批原文字符数")
    parser.add_argument("--no-extract", action="store_true", help="翻译前不自动提取词表")
    args = parser.parse_args()

    if args.input:
        return run_cli(args)

    try:
        from hangul_novel_translator.gui import run_gui

        run_gui()
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"无法启动 GUI：{exc}", file=sys.stderr)
        print("可使用 --input 参数运行命令行模式。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
