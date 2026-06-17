"""最小 filesystem MCP server (自建, M3)。

为什么自建而非用官方 @modelcontextprotocol/server-filesystem:
- 官方 server 是 Node 包, 需 npx 在线拉取; 受限网络 (企业自签 CA) 下 npm 无法验证证书。
- 本 server 用官方 `mcp` Python SDK 实现标准 MCP 协议 (initialize/list_tools/call_tool),
  以 stdio 子进程方式被 client 拉起, 不依赖 node/网络, 同样证明"协议化能力接入"。

安全: 所有路径都收敛在启动时指定的授权根目录内, 拒绝目录穿越。
用法 (被 client 以 stdio 拉起): python -m mcp_clients.fs_server <root_dir>
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# 启动参数: argv[1] = 授权根目录 (缺省为当前目录)
_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()

mcp = FastMCP("scholarstance-fs")


def _safe(rel_or_abs: str) -> Path:
    """把入参路径收敛到授权根目录内, 防目录穿越 (../ 逃逸)。"""
    p = Path(rel_or_abs)
    target = (p if p.is_absolute() else _ROOT / p).resolve()
    if target != _ROOT and _ROOT not in target.parents:
        raise ValueError(f"路径越权: {rel_or_abs} 不在授权根目录 {_ROOT} 内")
    return target


@mcp.tool()
def list_directory(path: str = ".") -> str:
    """列出授权根目录内某子目录的条目 (目录加 / 后缀)。"""
    target = _safe(path)
    if not target.is_dir():
        return f"[error] 不是目录: {path}"
    lines = []
    for child in sorted(target.iterdir()):
        lines.append(f"{child.name}/" if child.is_dir() else child.name)
    return "\n".join(lines) or "(空目录)"


@mcp.tool()
def read_text_file(path: str) -> str:
    """读取授权根目录内某文本文件的内容。"""
    target = _safe(path)
    if not target.is_file():
        return f"[error] 不是文件: {path}"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"[error] 非文本文件无法读取: {path}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
