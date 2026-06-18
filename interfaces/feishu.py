"""飞书适配器 (集成层, 不侵入核心)。

姿势 1 (已实现, A+B1): Webhook 推送**富文本交互卡片** —— 任务完成后把结构化摘要
(规模/成本/质量) 推到飞书群, 并附「查看图谱 / 查看综述」按钮, 按钮指向 FastAPI
静态托管的产物 URL (B1), 同内网可点开。
姿势 2 (未做, C): 飞书 Bot 事件回调发起任务 —— 需公网可达 + 应用鉴权, 与本地定位冲突, 暂缓。
姿势 3 (未做): 产出直出飞书云文档 —— 需开放平台 App 鉴权, 暂缓。
"""
from __future__ import annotations

import json
import os
import urllib.request

from config import settings


def _post(payload: dict) -> bool:
    """向自定义机器人 Webhook POST 一条消息; 未配置 URL 或失败返回 False。"""
    url = settings.feishu_webhook_url
    if not url:
        return False
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:  # noqa: BLE001  通知失败不应影响主流程
        return False


def notify(text: str) -> bool:
    """推送纯文本消息 (兜底用)。标题/正文需含自定义机器人的关键词 ScholarStance。"""
    return _post({"msg_type": "text", "content": {"text": text}})


def _artifact_url(path: str) -> str:
    """把本地产物路径映射为可点开的 URL (B1: FastAPI /artifacts 静态托管)。"""
    name = os.path.basename(str(path))
    return f"{settings.public_base_url}/artifacts/{name}"


def _build_card(topic: str, artifacts: list[str], stats: dict | None) -> dict:
    """构造飞书交互卡片 (msg_type=interactive)。

    stats 可含: cards/nodes/edges/gaps/tokens/elapsed_s/critic_passed; 缺字段自动省略。
    标题固定带 "ScholarStance" 关键词, 兼容自定义机器人的关键词安全校验。
    """
    stats = stats or {}
    # —— 规模行 ——
    scale_bits = []
    if stats.get("cards") is not None:
        scale_bits.append(f"论文 **{stats['cards']}** 篇")
    if stats.get("nodes") is not None:
        scale_bits.append(f"节点 **{stats['nodes']}**")
    if stats.get("edges") is not None:
        scale_bits.append(f"关系边 **{stats['edges']}**")
    if stats.get("gaps") is not None:
        scale_bits.append(f"研究空白 **{stats['gaps']}**")

    # —— 成本行 ——
    cost_bits = []
    if stats.get("tokens") is not None:
        cost_bits.append(f"Token **{stats['tokens']:,}**")
    if stats.get("elapsed_s") is not None:
        cost_bits.append(f"耗时 **{stats['elapsed_s']}s**")

    # —— 质量行 ——
    if "critic_passed" in stats:
        quality = "Critic: ✅ 通过" if stats["critic_passed"] else "Critic: ⚠️ 未通过 (产物仅供参考)"
    else:
        quality = ""

    lines = [f"**研究方向**: {topic}"]
    if scale_bits:
        lines.append(" · ".join(scale_bits))
    if cost_bits:
        lines.append(" · ".join(cost_bits))
    if quality:
        lines.append(quality)

    elements: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
    ]

    # —— 产物按钮 (B1 可点开的 URL) ——
    buttons = []
    for path in artifacts:
        name = os.path.basename(str(path)).lower()
        if name.endswith("_graph.html"):
            label = "查看立场图谱"
        elif name.endswith(".html"):
            label = "查看可视化"
        elif name.endswith(".md"):
            label = "查看综述"
        else:
            label = name
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "primary",
            "url": _artifact_url(path),
        })
    if buttons:
        elements.append({"tag": "action", "actions": buttons})

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "📊 ScholarStance · 立场图谱已生成"},
                "template": "blue",
            },
            "elements": elements,
        },
    }


def notify_task_done(topic: str, artifacts: list[str], stats: dict | None = None) -> bool:
    """任务完成通知 (富卡片)。Orchestrator 完成后调用。

    stats 为可选的结构化摘要 (规模/成本/质量); 缺省时退化为只展示方向 + 产物按钮。
    未配置 webhook 时静默跳过 (返回 False)。
    """
    return _post(_build_card(topic, artifacts, stats))
