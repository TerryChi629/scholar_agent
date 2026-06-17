"""Chroma 向量库封装 + SQLite 元数据。

存:  chunk 文本 + 向量 + 元数据 (paper_id, title, year, venue, section)。
TODO(Trae): rerank、更细的元数据过滤、批量优化。
"""
from __future__ import annotations

from dataclasses import dataclass

import chromadb

from config import settings
from rag.embedder import get_embedder


@dataclass
class Chunk:
    chunk_id: str
    paper_id: str
    text: str
    metadata: dict
    score: float = 0.0


class VectorStore:
    def __init__(self, collection: str = "papers") -> None:
        settings.ensure_dirs()
        self._client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        self._col = self._client.get_or_create_collection(name=collection)
        self._embedder = get_embedder()

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        embeddings = self._embedder.embed([c.text for c in chunks])
        self._col.add(
            ids=[c.chunk_id for c in chunks],
            embeddings=embeddings,
            documents=[c.text for c in chunks],
            metadatas=[{**c.metadata, "paper_id": c.paper_id} for c in chunks],
        )

    def query(self, text: str, top_k: int = 8, where: dict | None = None) -> list[Chunk]:
        qvec = self._embedder.embed([text])[0]
        res = self._col.query(
            query_embeddings=[qvec],
            n_results=top_k,
            where=where or None,
        )
        out: list[Chunk] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i, cid in enumerate(ids):
            meta = metas[i] or {}
            out.append(Chunk(
                chunk_id=cid,
                paper_id=meta.get("paper_id", ""),
                text=docs[i],
                metadata=meta,
                score=1.0 - (dists[i] if i < len(dists) else 0.0),
            ))
        return out

    def count(self) -> int:
        return self._col.count()

    def all_chunks(self, where: dict | None = None) -> list[Chunk]:
        """取出全部 (或按元数据过滤的) chunk, 不含向量。供 BM25 建索引用。"""
        res = self._col.get(where=where or None, include=["documents", "metadatas"])
        ids = res.get("ids", []) or []
        docs = res.get("documents", []) or []
        metas = res.get("metadatas", []) or []
        out: list[Chunk] = []
        for i, cid in enumerate(ids):
            meta = metas[i] or {}
            out.append(Chunk(
                chunk_id=cid,
                paper_id=meta.get("paper_id", ""),
                text=docs[i] if i < len(docs) else "",
                metadata=meta,
            ))
        return out

    def list_papers(self) -> dict[str, dict]:
        """库内真实存在的论文: {paper_id: {title, year}}。

        供 Retriever 校验候选 (防幻觉 paper_id)、Reader 补全卡片元数据。
        """
        res = self._col.get(include=["metadatas"])
        papers: dict[str, dict] = {}
        for meta in res.get("metadatas", []) or []:
            pid = (meta or {}).get("paper_id")
            if pid and pid not in papers:
                papers[pid] = {"title": meta.get("title", ""), "year": meta.get("year", 0)}
        return papers


_store: VectorStore | None = None


def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
