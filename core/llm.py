"""LLM 网关层: 统一 chat / embedding 接口, 屏蔽 DeepSeek / GLM 差异。

设计要点:
- 二者都兼容 OpenAI SDK, 只是 base_url / key / model 不同。
- 上层只调 chat() / embed(), 不关心底层是谁。

M4 工程化:
- 稳定性: chat/embed 对 429 限流与 5xx 服务端错误做指数退避重试。
- 限流: 客户端最小请求间隔 (settings.llm_min_interval), 防止打爆配额。
- 高可用: chat 主模型重试耗尽后, 降级到 settings.fallback_chat_model 兜底。
- 可观测: 重试/降级动作经 core.obs.log_event 落结构化日志。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, RateLimitError

from config import settings
from core.obs import log_event

# 触发重试的异常: 限流 / 超时 / 连接错误, 以及 5xx 服务端错误。
_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, _RETRYABLE):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 429
    return False


class _RateLimiter:
    """进程内最小请求间隔限流 (线程安全), 间隔 <=0 时直通。"""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min_interval


def _retry_call(fn, *, op: str, model: str | None = None):
    """带指数退避的同步调用包装。失败重试 settings.llm_max_retries 次。"""
    attempts = settings.llm_max_retries
    last_exc: Exception | None = None
    for i in range(attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i >= attempts or not _is_retryable(exc):
                raise
            delay = settings.llm_backoff_base * (2 ** i)
            log_event("llm.retry", level="WARNING", op=op, model=model,
                      attempt=i + 1, max_attempts=attempts, delay_s=round(delay, 2),
                      error=str(exc))
            time.sleep(delay)
    raise last_exc  # pragma: no cover


class LLM:
    """对话模型网关 (重试退避 + 限流 + 主备降级)。"""

    def __init__(self) -> None:
        api_key, base_url = settings.chat_credentials()
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = settings.chat_model()
        self._fallback = settings.fallback_chat_model
        self._limiter = _RateLimiter(settings.llm_min_interval)

    def _create(self, model: str, params: dict[str, Any]):
        self._limiter.acquire()
        call_params = dict(params, model=model)
        return _retry_call(
            lambda: self._client.chat.completions.create(**call_params),
            op="chat", model=model,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        **kwargs: Any,
    ):
        """单轮对话。返回原始 response, 由调用方解析 (含 tool_calls)。

        主模型重试耗尽后, 若配置了不同的兜底模型则降级重试一次。
        """
        params: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        params.update(kwargs)

        try:
            return self._create(self._model, params)
        except Exception as exc:  # noqa: BLE001
            if not self._fallback or self._fallback == self._model:
                raise
            log_event("llm.fallback", level="WARNING", op="chat",
                      primary=self._model, fallback=self._fallback, error=str(exc))
            return self._create(self._fallback, params)

    def chat_text(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """便捷方法: 只取文本回复。"""
        resp = self.chat(messages, **kwargs)
        return resp.choices[0].message.content or ""


class Embedder:
    """Embedding 抽象接口。初版 = API; 后期可换本地自训模型, 对上层透明。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class _EmbedCache:
    """Embedding 内容缓存 (SQLite 持久化, 按 model+文本 hash 命中)。

    同一篇论文重复摄入 / 相同查询重复 embedding 时直接命中, 省调用省钱。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._init = False

    def _conn(self) -> sqlite3.Connection:
        settings.ensure_dirs()
        conn = sqlite3.connect(settings.sqlite_path)
        if not self._init:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS embed_cache (
                    key TEXT PRIMARY KEY,
                    vec TEXT
                )"""
            )
            self._init = True
        return conn

    @staticmethod
    def _key(model: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()

    def get_many(self, model: str, texts: list[str]) -> dict[int, list[float]]:
        """返回 {索引: 向量}, 仅含命中项。"""
        if not settings.cache_enabled or not texts:
            return {}
        keys = [self._key(model, t) for t in texts]
        with self._lock:
            conn = self._conn()
            placeholders = ",".join("?" * len(keys))
            rows = conn.execute(
                f"SELECT key, vec FROM embed_cache WHERE key IN ({placeholders})", keys
            ).fetchall()
            conn.close()
        found = {k: v for k, v in rows}
        return {i: json.loads(found[k]) for i, k in enumerate(keys) if k in found}

    def put_many(self, model: str, pairs: list[tuple[str, list[float]]]) -> None:
        if not settings.cache_enabled or not pairs:
            return
        with self._lock:
            conn = self._conn()
            with conn:
                conn.executemany(
                    "REPLACE INTO embed_cache (key, vec) VALUES (?,?)",
                    [(self._key(model, t), json.dumps(v)) for t, v in pairs],
                )
            conn.close()


_embed_cache = _EmbedCache()


class APIEmbedder(Embedder):
    """走 OpenAI 兼容 embedding 端点 (默认 GLM embedding-3)。"""

    # 部分厂商单次请求有条数上限 (如 GLM embedding-3 为 64), 超出需分批。
    _BATCH_SIZE = 64

    def __init__(self) -> None:
        self._client = OpenAI(api_key=settings.embed_api_key, base_url=settings.embed_base_url)
        self._model = settings.embed_model
        self._limiter = _RateLimiter(settings.llm_min_interval)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 先查缓存, 只对未命中的文本发起 API 请求。
        cached = _embed_cache.get_many(self._model, texts)
        result: list[list[float] | None] = [cached.get(i) for i in range(len(texts))]
        miss_idx = [i for i, v in enumerate(result) if v is None]
        if cached:
            log_event("embed.cache", op="embed", model=self._model,
                      total=len(texts), hit=len(cached), miss=len(miss_idx))
        new_pairs: list[tuple[str, list[float]]] = []
        for j in range(0, len(miss_idx), self._BATCH_SIZE):
            idx_batch = miss_idx[j:j + self._BATCH_SIZE]
            batch = [texts[i] for i in idx_batch]
            self._limiter.acquire()
            resp = _retry_call(
                lambda b=batch: self._client.embeddings.create(model=self._model, input=b),
                op="embed", model=self._model,
            )
            for i, d in zip(idx_batch, resp.data):
                result[i] = d.embedding
                new_pairs.append((texts[i], d.embedding))
        _embed_cache.put_many(self._model, new_pairs)
        return [v for v in result]  # type: ignore[misc]


# 单例 (按需创建, 避免 import 即连接)
_llm: LLM | None = None
_embedder: Embedder | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = APIEmbedder()
    return _embedder
