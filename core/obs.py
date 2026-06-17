"""结构化可观测 (M4)。统一 JSON 行日志 + 轻量计时上下文。

设计:
- 每条日志是一行 JSON, 含统一字段 (ts/level/event + 任意业务字段), 便于采集与排查。
- 关键链路字段: task_id / agent / step / latency_ms / tokens / retried / fallback。
- 同时输出到 stderr 与 storage/scholarstance.log (可配), 不阻塞主流程。
- 不引入重型日志框架, 标准库 logging 即可。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any

from config import settings

_logger: logging.Logger | None = None


class _JsonFormatter(logging.Formatter):
    """把 LogRecord 渲染为单行 JSON。业务字段放在 record.fields。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger() -> logging.Logger:
    """惰性初始化全局结构化 logger (stderr + 可选文件)。"""
    global _logger
    if _logger is not None:
        return _logger
    logger = logging.getLogger("scholarstance")
    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    logger.propagate = False
    fmt = _JsonFormatter()

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if settings.log_to_file:
        try:
            settings.ensure_dirs()
            fh = logging.FileHandler(settings.storage_dir / "scholarstance.log", encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass  # 文件不可写不应中断主流程

    _logger = logger
    return logger


def log_event(event: str, level: str = "INFO", **fields: Any) -> None:
    """记一条结构化事件。fields 会平铺进 JSON (如 task_id/agent/latency_ms/tokens)。"""
    logger = get_logger()
    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.log(lvl, event, extra={"fields": fields})


@contextmanager
def timed(event: str, **fields: Any):
    """计时上下文: 进入即记 start, 退出记 latency_ms 与成败。

    用法:
        with timed("agent.run", task_id=..., agent="reader"):
            ...
    """
    t0 = time.time()
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        log_event(event, level="ERROR",
                  latency_ms=round((time.time() - t0) * 1000, 1),
                  ok=False, error=str(exc), **fields)
        raise
    else:
        log_event(event,
                  latency_ms=round((time.time() - t0) * 1000, 1),
                  ok=True, **fields)
