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

    def chat_model(self) -> str:
        return self.glm_chat_model if self.llm_provider == "glm" else self.deepseek_chat_model

    def chat_credentials(self) -> tuple[str, str]:
        """返回 (api_key, base_url)。"""
        if self.llm_provider == "glm":
            return self.glm_api_key, self.glm_base_url
        return self.deepseek_api_key, self.deepseek_base_url

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
