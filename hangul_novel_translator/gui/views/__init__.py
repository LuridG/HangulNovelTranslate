# 由 tools/split_class.py 拆分生成
"""视图 mixin 包：App 通过继承聚合各 tab 的实现。"""
from __future__ import annotations

from .shell_mixin import ShellMixin
from .left_panel_mixin import LeftPanelMixin
from .glossary_mixin import GlossaryMixin
from .settings_mixin import SettingsMixin
from .fixer_mixin import FixerMixin
from .merge_mixin import MergeMixin
from .perspective_mixin import PerspectiveMixin

__all__ = ['ShellMixin', 'LeftPanelMixin', 'GlossaryMixin', 'SettingsMixin', 'FixerMixin', 'MergeMixin', 'PerspectiveMixin']
