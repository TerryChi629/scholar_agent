"""memory2 数据模型: MemoryItem + 两类记忆枚举。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 两类记忆 (薄版): 用户长期规则 / 用户偏好。event / profile 后续阶段。
PROCEDURE = "procedure"   # 怎么做事的规则, 如"比较论文先列方法再列指标"
PREFERENCE = "preference"  # 用户偏好, 如"回答都用中文""偏好近三年论文"
CATEGORIES = (PROCEDURE, PREFERENCE)


def content_hash(category: str, content: str) -> str:
    """精确去重键: 按 (category, 规整后 content) 取 sha256。

    规整 = 去首尾空白 + 压缩内部连续空白, 让"同义不同空格"也判为同一条。
    """
    norm = " ".join((content or "").split())
    return hashlib.sha256(f"{category}\x00{norm}".encode("utf-8")).hexdigest()


@dataclass
class MemoryItem:
    """一条长期记忆。

    freq: 被重复确认 (reinforcement) 的次数, 用于 hotness。
    superseded_by: 若被新条目取代, 指向新条目 id (软删, 不参与召回)。
    """
    id: str
    category: str
    content: str
    chash: str = ""
    freq: int = 1
    created_at: float = 0.0
    last_used_at: float = 0.0
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.chash:
            self.chash = content_hash(self.category, self.content)

    def to_row(self) -> tuple:
        return (self.id, self.category, self.content, self.chash,
                self.freq, self.created_at, self.last_used_at, self.superseded_by)

    @classmethod
    def from_row(cls, row: tuple) -> "MemoryItem":
        return cls(
            id=row[0], category=row[1], content=row[2], chash=row[3],
            freq=row[4], created_at=row[5], last_used_at=row[6], superseded_by=row[7],
        )
