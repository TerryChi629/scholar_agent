"""digest: 每日论文速递 (M12)。

把用户的长期记忆 (问答记录 / 任务主题 / 偏好 / 已读论文方法族) 聚合成兴趣画像,
据此从 arXiv 拉取最新论文, 经 embedding 粗排 + LLM 精排 + 去重后, 推送到飞书。

子模块:
- profile: 多源记忆聚合 + LLM 聚类 -> 兴趣主题 (含英文检索词 + 来源溯源)。
- rank:    embedding 粗排 + LLM 精排 + 去重。
- store:   digest_pushed 去重表 (记录已推 arXiv id, 避免重复)。
- runner:  串联画像 -> 拉新 -> 排序 -> 推送 的全流程。
"""
from __future__ import annotations

from digest.profile import InterestTopic, build_profile
from digest.runner import run_digest

__all__ = ["InterestTopic", "build_profile", "run_digest"]
