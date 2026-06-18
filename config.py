"""全局配置: 从环境变量 / .env 读取, 统一出口。

约定: 业务代码一律通过 `from config import settings` 使用, 不直接读 os.environ。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # 读取项目根目录 .env

ROOT = Path(__file__).resolve().parent


def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass
class Settings:
    # —— LLM ——
    llm_provider: str = field(default_factory=lambda: _get("LLM_PROVIDER", "deepseek"))

    deepseek_api_key: str = field(default_factory=lambda: _get("DEEPSEEK_API_KEY"))
    deepseek_base_url: str = field(default_factory=lambda: _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    deepseek_chat_model: str = field(default_factory=lambda: _get("DEEPSEEK_CHAT_MODEL", "deepseek-chat"))

    glm_api_key: str = field(default_factory=lambda: _get("GLM_API_KEY"))
    glm_base_url: str = field(default_factory=lambda: _get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    glm_chat_model: str = field(default_factory=lambda: _get("GLM_CHAT_MODEL", "glm-4-flash"))

    # —— M4 高可用: 主备模型降级 (主模型连续失败时降级到更稳的兜底模型) ——
    fallback_chat_model: str = field(default_factory=lambda: _get("FALLBACK_CHAT_MODEL", "glm-4-flash"))

    # —— M7 混合模型分发: 三档模型 (provider:model) + Agent→档位映射 ——
    # 不同 Agent 任务难度不同, 按档位分发以平衡成本与质量 (低=GLM, 中=ds-flash, 高=ds-pro)。
    model_tier_low: str = field(default_factory=lambda: _get("MODEL_TIER_LOW", "glm:glm-4-flash"))
    model_tier_mid: str = field(default_factory=lambda: _get("MODEL_TIER_MID", "deepseek:deepseek-v4-flash"))
    model_tier_high: str = field(default_factory=lambda: _get("MODEL_TIER_HIGH", "deepseek:deepseek-v4-pro"))
    # Agent→档位 (low/mid/high); 留空则回退到 LLM_PROVIDER 默认单模型 (向后兼容)。
    agent_model_retriever: str = field(default_factory=lambda: _get("AGENT_MODEL_RETRIEVER", ""))
    agent_model_reader: str = field(default_factory=lambda: _get("AGENT_MODEL_READER", ""))
    agent_model_synthesizer: str = field(default_factory=lambda: _get("AGENT_MODEL_SYNTHESIZER", ""))
    agent_model_critic: str = field(default_factory=lambda: _get("AGENT_MODEL_CRITIC", ""))

    # —— M4 稳定性: API 重试退避 + 客户端限流 ——
    llm_max_retries: int = field(default_factory=lambda: int(_get("LLM_MAX_RETRIES", "3")))
    llm_backoff_base: float = field(default_factory=lambda: float(_get("LLM_BACKOFF_BASE", "1.0")))
    llm_min_interval: float = field(default_factory=lambda: float(_get("LLM_MIN_INTERVAL", "0.0")))

    # —— M4 成本: 缓存开关与 TTL ——
    cache_enabled: bool = field(default_factory=lambda: _get("CACHE_ENABLED", "1") not in ("0", "false", "False"))
    retrieval_cache_ttl: int = field(default_factory=lambda: int(_get("RETRIEVAL_CACHE_TTL", "600")))

    # —— M6 记忆: 卡片级语义记忆 (跨任务复用已精读论文的 topic 无关字段) ——
    memory_enabled: bool = field(default_factory=lambda: _get("MEMORY_ENABLED", "1") not in ("0", "false", "False"))

    # —— M4 可观测: 结构化日志 ——
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    log_to_file: bool = field(default_factory=lambda: _get("LOG_TO_FILE", "1") not in ("0", "false", "False"))

    # —— Embedding ——
    embed_provider: str = field(default_factory=lambda: _get("EMBED_PROVIDER", "glm"))
    embed_model: str = field(default_factory=lambda: _get("EMBED_MODEL", "embedding-3"))
    embed_api_key: str = field(default_factory=lambda: _get("EMBED_API_KEY"))
    embed_base_url: str = field(default_factory=lambda: _get("EMBED_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))

    # —— 运行参数 ——
    reader_concurrency: int = field(default_factory=lambda: int(_get("READER_CONCURRENCY", "4")))
    max_loop_rounds: int = field(default_factory=lambda: int(_get("MAX_LOOP_ROUNDS", "12")))
    critic_max_retry: int = field(default_factory=lambda: int(_get("CRITIC_MAX_RETRY", "2")))
    context_token_budget: int = field(default_factory=lambda: int(_get("CONTEXT_TOKEN_BUDGET", "24000")))

    # —— 路径 ——
    storage_dir: Path = field(default_factory=lambda: ROOT / _get("STORAGE_DIR", "./storage").lstrip("./"))
    chroma_dir: Path = field(default_factory=lambda: ROOT / "storage" / "chroma")
    sqlite_path: Path = field(default_factory=lambda: ROOT / "storage" / "scholarstance.db")

    # —— 飞书 ——
    feishu_webhook_url: str = field(default_factory=lambda: _get("FEISHU_WEBHOOK_URL"))
    # 富卡片按钮指向的产物可达地址 (B1: FastAPI 静态托管 storage 产物)。
    # 同内网协作时填本机可达地址, 如 http://192.168.x.x:8000; 默认 localhost。
    public_base_url: str = field(default_factory=lambda: _get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))

    def chat_model(self) -> str:
        return self.glm_chat_model if self.llm_provider == "glm" else self.deepseek_chat_model

    def chat_credentials(self) -> tuple[str, str]:
        """返回 (api_key, base_url)。"""
        if self.llm_provider == "glm":
            return self.glm_api_key, self.glm_base_url
        return self.deepseek_api_key, self.deepseek_base_url

    def credentials_for(self, provider: str) -> tuple[str, str]:
        """按 provider 名返回 (api_key, base_url)。供混合模型分发跨厂取凭证。"""
        if provider == "glm":
            return self.glm_api_key, self.glm_base_url
        return self.deepseek_api_key, self.deepseek_base_url

    def _tier_spec(self, tier: str) -> str:
        """档位别名 -> "provider:model" 规格串。"""
        return {"low": self.model_tier_low, "mid": self.model_tier_mid,
                "high": self.model_tier_high}.get(tier, "")

    def resolve_agent_model(self, agent: str | None) -> tuple[str, str | None, str, str]:
        """解析某 Agent 应使用的模型, 返回 (provider, model, api_key, base_url)。

        映射: AGENT_MODEL_<NAME> 给出档位别名 (low/mid/high) -> MODEL_TIER_<档> 给出
        "provider:model"。未配置该 Agent 档位时, model 返回 None, 表示走默认单模型
        路径 (保留 M4 主备降级, 向后兼容: 不开启分发时行为完全不变)。
        """
        tier = {
            "retriever": self.agent_model_retriever,
            "reader": self.agent_model_reader,
            "synthesizer": self.agent_model_synthesizer,
            "critic": self.agent_model_critic,
        }.get(agent or "", "")
        spec = self._tier_spec(tier) if tier else ""
        if spec and ":" in spec:
            provider, model = spec.split(":", 1)
            provider, model = provider.strip(), model.strip()
            key, base_url = self.credentials_for(provider)
            return provider, model, key, base_url
        # 未配置分发: model=None -> 调用方走默认单模型 (含主备降级)
        key, base_url = self.chat_credentials()
        return self.llm_provider, None, key, base_url

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
