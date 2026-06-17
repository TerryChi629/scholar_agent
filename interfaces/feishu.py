"""飞书适配器 (可选, M3)。属于集成层, 不侵入核心。

姿势 1 (推荐先做): Webhook 通知 —— 任务完成推送摘要 + 产物路径到飞书群。
姿势 2 (进阶): 飞书 Bot 接收指令回调 FastAPI。
姿势 3 (可选): 产出直出飞书云文档。
"""
from __future__ import annotations

import json
import urllib.request

from config import settings


def notify(text: str) -> bool:
    """通过自定义机器人 Webhook 推送文本消息。"""
    url = settings.feishu_webhook_url
    if not url:
        return False
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


def notify_task_done(topic: str, artifacts: list[str]) -> bool:
    """任务完成通知。Orchestrator/_run_map 完成后调用。"""
    body = f"【ScholarStance】立场图谱已生成\n方向: {topic}\n产物:\n" + "\n".join(artifacts)
    return notify(body)

# TODO(Trae): 姿势 2/3 (Bot 回调 + 飞书云文档) 后期实现。
