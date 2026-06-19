"""CLI 入口 (Rich)。命令: ingest / ask / map / resume / tasks。"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.panel import Panel

import tools  # noqa: F401  导入即注册所有 tool
from core.blackboard import Blackboard
from core.harness import list_tasks, load_checkpoint
from agents.orchestrator import Orchestrator

console = Console()


def _on_step(step: dict) -> None:
    t = step.get("type")
    if t == "act":
        console.print(f"  [cyan]→ 调用[/cyan] {step['tool']}({step.get('args', {})})")
    elif t == "observe":
        console.print(f"  [dim]← 观测[/dim] {step.get('result_preview', '')[:120]}")
    elif t == "final":
        console.print(f"  [green]✓ 完成[/green]")


def cmd_ingest(directory: str) -> None:
    from rag.ingest import ingest_dir
    console.print(f"[bold]入库[/bold]: {directory}")
    stat = ingest_dir(directory)
    console.print(f"完成: {stat}")


def cmd_ask(question: str) -> None:
    from tools import rag_query
    hits = rag_query(question, top_k=5)
    for h in hits:
        console.print(Panel(h["text"], title=f"{h['meta'].get('title','?')} (score={h['score']})"))


def cmd_chat(question: str) -> None:
    from chat import ChatAgent
    res = ChatAgent().answer(question)
    console.print(Panel(res.answer, title=f"ChatAgent · {res.intent}"))
    if res.evidence:
        console.print("[dim]证据:[/dim]")
        for e in res.evidence:
            console.print(f"  [{e['ref']}] 《{e['title']}》({e['year']}) p{e['page']}")
    if res.used_memory:
        console.print("[dim]命中记忆:[/dim]")
        for m in res.used_memory:
            console.print(f"  [{m['category']}] {m['content']}")


def cmd_map(topic: str) -> None:
    bb = Blackboard(task_id=uuid.uuid4().hex[:8], topic=topic)
    console.print(Panel(f"研究方向: [bold]{topic}[/bold]\ntask_id: {bb.task_id}", title="ScholarStance · map"))
    orch = Orchestrator(on_step=_on_step)
    bb = orch.run(bb)
    console.print(Panel(f"状态: {bb.status}\n卡片: {len(bb.cards)}\n产物: {bb.artifacts}", title="结果"))


def cmd_resume(task_id: str) -> None:
    bb = load_checkpoint(task_id)
    if not bb:
        console.print(f"[red]未找到 task: {task_id}[/red]")
        return
    console.print(f"恢复 task {task_id} (状态={bb.status})")
    Orchestrator(on_step=_on_step).run(bb)


def cmd_tasks() -> None:
    for tid, topic, status in list_tasks():
        console.print(f"  {tid}  [{status}]  {topic}")


def cmd_digest(arg: str) -> None:
    """每日论文速递: 画像 -> arXiv 拉新 -> 排序 -> 飞书推送 (M12)。dry 不推送只预览。"""
    from digest.runner import run_digest
    dry = arg.strip() == "dry"
    res = run_digest(dry_run=dry)
    if not res.get("ok"):
        console.print(f"[yellow]未推送[/yellow]: {res.get('reason', '无结果')}")
        return
    for g in res.get("groups", []):
        console.print(Panel(
            "\n".join(f"・{p['title']}\n  💡 {p['reason']}" for p in g["papers"]),
            title=f"🔖 {g['name']}  ({g['reason']})",
        ))
    tail = "(dry-run, 未推送)" if dry else f"已推送 {res.get('pushed', 0)} 篇 (sent={res.get('sent')})"
    console.print(f"[green]速递完成[/green]: 主题 {res.get('topics')} 个, {tail}")


HELP = """ScholarStance CLI
  ingest <dir>     入库本地 PDF 目录
  ask <question>   基于私有库问答 (基础 RAG)
  chat <question>  对话式 RAG (检索+合成+记忆, M8)
  map <topic>      生成立场图谱 + 综述初稿
  resume <id>      恢复中断的任务
  tasks            列出历史任务
  digest [dry]     每日论文速递 (画像+arXiv拉新+推送); dry 仅预览不推送
"""


def main() -> None:
    if len(sys.argv) < 2:
        console.print(HELP)
        return
    cmd, rest = sys.argv[1], " ".join(sys.argv[2:])
    if cmd == "ingest":
        cmd_ingest(rest)
    elif cmd == "ask":
        cmd_ask(rest)
    elif cmd == "chat":
        cmd_chat(rest)
    elif cmd == "map":
        cmd_map(rest)
    elif cmd == "resume":
        cmd_resume(rest)
    elif cmd == "tasks":
        cmd_tasks()
    elif cmd == "digest":
        cmd_digest(rest)
    else:
        console.print(HELP)


if __name__ == "__main__":
    main()
