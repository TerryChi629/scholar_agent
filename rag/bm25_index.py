"""持久化 + 增量 BM25 索引 (M9 索引工程)。

此前 BM25 每次查询都 `store.all_chunks()` 全量取 + 重新分词 + 重建 BM25Okapi,
库一变大检索就线性变慢。这里把"分词语料 + chunk 元信息"持久化到 pickle:
- 进程内缓存 BM25Okapi 对象 + 语料指纹, 仅在语料变更 (ingest/删除) 时重建;
- ingest 增量 `add_chunks` 追加并落盘, 查询直接复用内存对象, 不再每查重建。

回退: bm25_persist_enabled=False 或加载失败时, 调用方仍可走 store 全量重建路径,
功能不受影响 (向后兼容)。
"""
from __future__ import annotations

import pickle
import threading
import time

from config import settings
from core.obs import log_event
from rag.store import Chunk, get_store


class _BM25Index:
    """内存中的 BM25 索引: tokenized 语料 + chunk 元信息 + 懒构建的 BM25Okapi。"""

    def __init__(self) -> None:
        # 与 chunk 一一对应 (顺序一致): 分词结果 / chunk 轻量信息。
        self.tokenized: list[list[str]] = []
        self.chunks: list[dict] = []  # [{chunk_id, paper_id, text, metadata}]
        self._bm25 = None  # 懒构建, 语料变更后置空

    # —— 构建 / 查询 ——
    def _ensure_model(self):
        if self._bm25 is None and self.tokenized:
            from rank_bm25 import BM25Okapi
            self._bm25 = BM25Okapi(self.tokenized)
        return self._bm25

    def search(self, query_tokens: list[str], top_k: int,
               where_paper_id: str | None = None, year_min: int | None = None) -> list[Chunk]:
        """对索引做 BM25 召回, 支持按 paper_id / year 过滤 (在打分后筛, 索引不分片)。"""
        model = self._ensure_model()
        if model is None or not query_tokens:
            return []
        scores = model.get_scores(query_tokens)
        idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out: list[Chunk] = []
        for i in idx:
            info = self.chunks[i]
            meta = info.get("metadata", {}) or {}
            if where_paper_id and info.get("paper_id") != where_paper_id:
                continue
            if year_min is not None and int(meta.get("year", 0) or 0) < year_min:
                continue
            out.append(Chunk(
                chunk_id=info["chunk_id"], paper_id=info.get("paper_id", ""),
                text=info.get("text", ""), metadata=meta,
            ))
            if len(out) >= top_k:
                break
        return out

    # —— 维护 ——
    def add_chunks(self, chunks: list[Chunk], tokenizer) -> None:
        """增量追加 (按 chunk_id 去重覆盖), 语料变更后置空 BM25 触发下次重建。"""
        if not chunks:
            return
        existing = {info["chunk_id"]: i for i, info in enumerate(self.chunks)}
        for c in chunks:
            entry = {"chunk_id": c.chunk_id, "paper_id": c.paper_id,
                     "text": c.text, "metadata": c.metadata or {}}
            toks = tokenizer(c.text)
            if c.chunk_id in existing:  # 覆盖 (重入库)
                pos = existing[c.chunk_id]
                self.chunks[pos] = entry
                self.tokenized[pos] = toks
            else:
                self.chunks.append(entry)
                self.tokenized.append(toks)
        self._bm25 = None

    def size(self) -> int:
        return len(self.chunks)

    # —— 持久化 (只存语料, 不存 BM25Okapi 对象, 加载后懒重建) ——
    def to_payload(self) -> dict:
        return {"tokenized": self.tokenized, "chunks": self.chunks, "v": 1}

    @classmethod
    def from_payload(cls, payload: dict) -> "_BM25Index":
        idx = cls()
        idx.tokenized = payload.get("tokenized", []) or []
        idx.chunks = payload.get("chunks", []) or []
        return idx


_lock = threading.Lock()
_index: _BM25Index | None = None


def _tokenizer():
    """惰性取 retrieve 里的分词器 (避免循环 import)。"""
    from rag.retrieve import _tokenize
    return _tokenize


def _load_from_disk() -> _BM25Index | None:
    path = settings.bm25_index_path
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            payload = pickle.load(f)
        return _BM25Index.from_payload(payload)
    except Exception as exc:  # noqa: BLE001  索引损坏不应阻断, 回退重建
        log_event("bm25_index.load_failed", level="WARNING", error=str(exc))
        return None


def _build_from_store() -> _BM25Index:
    """从向量库全量重建索引 (首次 / 索引缺失时)。"""
    t0 = time.time()
    idx = _BM25Index()
    chunks = get_store().all_chunks()
    idx.add_chunks(chunks, _tokenizer())
    log_event("bm25_index.rebuild", chunks=idx.size(), ms=round((time.time() - t0) * 1000, 1))
    return idx


def _save(idx: _BM25Index) -> None:
    try:
        settings.ensure_dirs()
        with settings.bm25_index_path.open("wb") as f:
            pickle.dump(idx.to_payload(), f, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # noqa: BLE001
        log_event("bm25_index.save_failed", level="WARNING", error=str(exc))


def get_index() -> _BM25Index:
    """取进程内 BM25 索引单例: 优先磁盘, 缺失则从 store 全量重建并落盘。"""
    global _index
    with _lock:
        if _index is not None:
            return _index
        idx = _load_from_disk()
        if idx is None:
            idx = _build_from_store()
            _save(idx)
        _index = idx
        return _index


def add_chunks(chunks: list[Chunk]) -> None:
    """ingest 时增量更新索引并落盘 (与 store.add 配套调用)。"""
    if not chunks:
        return
    with _lock:
        idx = _index if _index is not None else (_load_from_disk() or _build_from_store())
        idx.add_chunks(chunks, _tokenizer())
        _save(idx)
        globals()["_index"] = idx


def reset() -> None:
    """清空内存与磁盘索引 (清库重入场景)。"""
    global _index
    with _lock:
        _index = None
        try:
            if settings.bm25_index_path.is_file():
                settings.bm25_index_path.unlink()
        except OSError:
            pass
