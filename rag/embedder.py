"""Embedder 接口: 初版 API, 后期可换本地自训模型, 对上层透明。

复用 core.llm 的实现, 这里只做再导出 + 占位本地实现。
"""
from __future__ import annotations

from core.llm import APIEmbedder, Embedder, get_embedder

__all__ = ["Embedder", "APIEmbedder", "get_embedder", "LocalEmbedder"]


class LocalEmbedder(Embedder):
    """本地自训 embedding 占位。后期由你 (算法工程师) 实现。

    TODO(后期): 加载本地模型 (如 BGE / 自训), 实现 embed()。
    只要遵守 Embedder 接口, 上层 RAG 无需任何改动。
    """

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("本地 embedding 后期实现")
