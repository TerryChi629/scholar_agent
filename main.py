"""ScholarStance 统一入口。

用法:
    python main.py                # 显示帮助
    python main.py ingest <dir>   # 入库 PDF 目录
    python main.py ask "<问题>"   # 库内问答
    python main.py map "<方向>"   # 生成立场图谱
    python main.py tasks          # 列出历史任务
    python main.py resume <id>    # 恢复任务
    python main.py selfcheck      # 不联网, 自检脚手架是否完整
"""
from __future__ import annotations

import sys


def selfcheck() -> None:
    """不调用任何 API, 验证模块能 import、tool 能注册、黑板能序列化。"""
    import tools  # noqa: F401
    from core.tool_registry import registry
    from core.blackboard import Blackboard, PaperCard

    print("== ScholarStance 自检 ==")
    print(f"已注册 tools ({len(registry.list_names())}): {registry.list_names()}")
    bb = Blackboard(task_id="selftest", topic="In-context Learning")
    bb.cards["p1"] = PaperCard(paper_id="p1", title="示例", core_claim="测试")
    raw = bb.to_json()
    bb2 = Blackboard.from_json(raw)
    assert bb2.cards["p1"].title == "示例", "黑板序列化往返失败"
    print("黑板序列化/反序列化: OK")
    schemas = registry.openai_schemas()
    print(f"tool schema 生成: OK (示例: {schemas[0]['function']['name']})")
    print("== 自检通过: 脚手架完整, 填好 .env 即可联网运行 ==")


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "selfcheck":
        selfcheck()
        return
    from interfaces.cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
