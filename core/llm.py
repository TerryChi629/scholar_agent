"""LLM 网关层: 统一 chat / embedding 接口, 屏蔽 DeepSeek / GLM 差异。

设计要点:
- 二者都兼容 OpenAI SDK, 只是 base_url / key / model 不同。
- 上层只调 chat() / embed(), 不关心底层是谁。
- TODO(Trae): 加重试退避、token 计数、流式输出、错误分类。
"""
from __future__ import annotations

from typing import Any

from openai import OpenAI

from config import settings


class LLM:
    """对话模型网关。"""

    def __init__(self) -> None:
        api_key, base_url = settings.chat_credentials()
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = settings.chat_model()

    def chat(
        self,
        messages: list[dict[str, str]],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        **kwargs: Any,
    ):
        """单轮对话。返回原始 response, 由调用方解析 (含 tool_calls)。

        TODO(Trae): 包一层重试 (指数退避), 捕获 429 限流; 记录 token 用量。
        """
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        params.update(kwargs)
        return self._client.chat.completions.create(**params)

    def chat_text(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        """便捷方法: 只取文本回复。"""
        resp = self.chat(messages, **kwargs)
        return resp.choices[0].message.content or ""


class Embedder:
    """Embedding 抽象接口。初版 = API; 后期可换本地自训模型, 对上层透明。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class APIEmbedder(Embedder):
    """走 OpenAI 兼容 embedding 端点 (默认 GLM embedding-3)。"""

    # 部分厂商单次请求有条数上限 (如 GLM embedding-3 为 64), 超出需分批。
    _BATCH_SIZE = 64

    def __init__(self) -> None:
        self._client = OpenAI(api_key=settings.embed_api_key, base_url=settings.embed_base_url)
        self._model = settings.embed_model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._BATCH_SIZE):
            batch = texts[i:i + self._BATCH_SIZE]
            resp = self._client.embeddings.create(model=self._model, input=batch)
            out.extend(d.embedding for d in resp.data)
        return out


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
