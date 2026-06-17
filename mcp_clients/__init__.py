"""MCP 客户端接入 (M3)。证明懂协议化能力接入, 而非把能力写死成本地函数。

设计:
- server: 用官方 `mcp` Python SDK 自建最小 filesystem server (见 fs_server.py)。
  不用官方 Node 版 @modelcontextprotocol/server-filesystem —— 它需 npx 在线拉取,
  受限网络 (企业自签 CA) 下 npm 无法验证证书; 自建 Python server 走 stdio 子进程,
  不依赖 node/网络, 同样实现标准 MCP 协议 (initialize/list_tools/call_tool)。
- client: 本项目工具层是同步的, 故用 asyncio.run 做同步桥接, 每次调用建立一个
  短连接 (用完即关), 简单可靠、无常驻进程。
- 把 MCP 暴露的能力包装成本项目的 @tool, 与本地工具同构, 对 Agent 透明。
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from config import settings


def _server_params(root_dir: str):
    """构造自建 filesystem MCP server 的 stdio 启动参数 (用当前 Python 解释器拉起)。"""
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_clients.fs_server", root_dir],
    )


async def _with_session(root_dir: str, fn):
    """建立一次 MCP 会话, 执行 fn(session) 后关闭。fn 为 async 回调。"""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    params = _server_params(root_dir)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def _run(root_dir: str, fn) -> Any:
    """同步入口: 跑一次 MCP 会话。"""
    return asyncio.run(_with_session(root_dir, fn))


def _text_of(result) -> str:
    """把 MCP CallToolResult 的内容块拼为纯文本。"""
    parts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def mcp_list_dir(root_dir: str, sub_path: str = ".") -> list[str]:
    """通过 filesystem-mcp 列目录。返回条目文本行。"""
    async def _fn(session):
        res = await session.call_tool("list_directory", {"path": sub_path})
        return _text_of(res)

    text = _run(root_dir, _fn)
    return [ln for ln in text.splitlines() if ln.strip()]


def mcp_read_file(root_dir: str, file_path: str) -> str:
    """通过 filesystem-mcp 读取文件内容。"""
    async def _fn(session):
        res = await session.call_tool("read_text_file", {"path": file_path})
        return _text_of(res)

    return _run(root_dir, _fn)


def mcp_available_tools(root_dir: str) -> list[str]:
    """握手 filesystem-mcp 并列出其暴露的工具名 (验证协议连通)。"""
    async def _fn(session):
        res = await session.list_tools()
        return [t.name for t in res.tools]

    return _run(root_dir, _fn)


def connect_filesystem_mcp(root_dir: str | None = None) -> list[str]:
    """连接 filesystem-mcp, 返回其暴露的工具名列表 (用于自检/演示)。"""
    return mcp_available_tools(root_dir or str(settings.storage_dir))
