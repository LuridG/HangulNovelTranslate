# hangul_novel_translator/__init__.py
"""韩语小说批量翻译工具。"""

__all__ = ["AppConfig"]


def __getattr__(name: str):
    if name == "AppConfig":
        from .config import AppConfig

        return AppConfig
    if name in {"Translator", "TranslationCancelled", "TranslationResult"}:
        from . import translator

        return getattr(translator, name)
    raise AttributeError(name)
