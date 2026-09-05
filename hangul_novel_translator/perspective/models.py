# 由 tools/split_package.py 拆分生成
from __future__ import annotations
from dataclasses import (asdict, dataclass)
from pathlib import Path
from ._consts import PERSPECTIVE_PROMPT_VERSION


class PerspectiveError(RuntimeError):
    """视角转换输入、模型返回或状态存档不满足安全条件。"""



class PerspectiveCancelled(PerspectiveError):
    """用户停止视角转换时抛出，当前批次保持待处理。"""



@dataclass
class PerspectiveOptions:
    narrator_name: str
    short_name: str = ""
    pronoun: str = "他"
    style: str = "pronoun"
    rewrite_dialogue: bool = False
    rewrite_letters: bool = False
    rewrite_inner_monologue: bool = False
    strategy: str = "conservative"
    chunk_chars: int = 1800
    prompt_version: str = PERSPECTIVE_PROMPT_VERSION

    def normalized(self) -> "PerspectiveOptions":
        name = self.narrator_name.strip()
        pronoun = self.pronoun.strip() or "他"
        style = self.style if self.style in {"full_name", "short_name", "pronoun"} else "pronoun"
        strategy = self.strategy if self.strategy in {"conservative", "coverage"} else "conservative"
        if not name:
            raise ValueError("叙述者/主角名称不能为空")
        if self.chunk_chars <= 0:
            raise ValueError("每批字符数必须大于 0")
        return PerspectiveOptions(
            narrator_name=name,
            short_name=self.short_name.strip(),
            pronoun=pronoun,
            style=style,
            rewrite_dialogue=bool(self.rewrite_dialogue),
            rewrite_letters=bool(self.rewrite_letters),
            rewrite_inner_monologue=bool(self.rewrite_inner_monologue),
            strategy=strategy,
            chunk_chars=int(self.chunk_chars),
            prompt_version=self.prompt_version or PERSPECTIVE_PROMPT_VERSION,
        )



@dataclass
class PerspectiveBlock:
    id: str
    file: str
    index: int
    source_segments: list[str]
    source_text: str
    classification: str = "narration"

    @property
    def char_count(self) -> int:
        return len(self.source_text)



@dataclass
class PerspectiveFailedBlock:
    state_path: Path
    block_id: str
    file: str
    source_segments: list[str]
    translated_segments: list[str]
    error: str
    classification: str = "narration"
