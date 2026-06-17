"""MCP 客户端接入 (M3)。首版接官方 filesystem-mcp 读本地 PDF 目录。

证明: 懂协议化能力接入, 而非把所有东西写死成本地函数。
TODO(Trae): 用官方 mcp SDK 连接 server, 把 MCP 暴露的能力包装成 @tool。
"""
from __future__ import annotations


def connect_filesystem_mcp(root_dir: str):
    """连接 filesystem-mcp, 返回可用能力。占位。

    步骤 (后期):
      1) pip install mcp
      2) 用 stdio 启动官方 server-filesystem
      3) list_tools / call_tool 包装为本项目的 @tool
    """
    raise NotImplementedError("MCP 接入 M3 实现")
