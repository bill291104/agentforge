from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()

app = typer.Typer(help="AgentForge - Agent-based development team orchestrator")
console = Console()


def _configure_logging(debug: bool = False) -> None:
    level_name = "DEBUG" if debug else os.getenv("AF_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if debug:
        from datetime import date
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"agentforge_{date.today():%Y%m%d}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        handlers.append(fh)
        console.print(f"[dim]Debug 로그 파일: {log_file}[/dim]")

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

_AGENTS_MD = Path("AGENTS.md")
_AGENTS_MD_CONTENT = """\
# AGENTS.md — AgentForge 에이전트 공유 규칙

> 이 파일은 AgentForge 내 모든 에이전트가 공유하는 행동 규범입니다.
> Linux Foundation Agent Protocol 표준을 따릅니다.

## 공통 원칙

1. **최소 권한**: 태스크 완료에 필요한 최소한의 작업만 수행한다.
2. **투명성**: 모든 판단 근거를 TaskReport.evidence에 기록한다.
3. **안전 우선**: 불확실한 경우 에스컬레이션하고 사용자에게 묻는다.
4. **멱등성**: 동일한 입력에 항상 동일한 결과를 생성하도록 노력한다.

## 역할별 규칙

### Leader (Opus 4.7)
- 요구사항을 WorkflowSpec으로 변환할 때 명시되지 않은 가정은 반드시 기록한다.
- 검증(verify_semantic)은 `[VERIFICATION MODE - EFFORT: XHIGH]` 프롬프트를 사용한다.
- acceptance_criteria 각 항목에 대해 독립적으로 PASS/FAIL을 판정한다.

### Worker (Haiku 4.5)
- Docker 샌드박스 외부에서 코드를 실행하지 않는다.
- TaskReport는 항상 deliverables와 evidence를 채운다.
- 실패 시 오류 메시지 전체를 evidence["error"]에 기록한다.

### SubOrchestrator (Sonnet 4.6)
- 위임받은 태스크만 처리하고 독립적인 서브그래프를 구성한다.
- 완료 시 completed_summaries에 1줄 요약을 추가한다.

## 에스컬레이션 프로토콜

| 레벨 | 트리거 | 동작 |
|------|--------|------|
| L0 | 첫 실패 | 동일 에이전트 재시도 |
| L1 | 두 번째 실패 | 새 에이전트 스폰 |
| L2 | 세 번째 실패 | 모델 티어 업그레이드 |
| L3 | 네 번째 실패 | 의존 태스크 BLOCKED, 나머지 계속 |
| L4 | 다섯 번째 실패 | 사용자 개입 요청 (Slack 버튼) |

## 메모리 & 일지

- 세션 종료 시 `memory/journal/YYYY-MM-DD_session_{id}.md` 에 실행 기록을 남긴다.
- 개선 제안은 `memory/proposals/pending/` 에 저장하고 사용자 수락 전에 적용하지 않는다.
"""


def _ensure_agents_md() -> None:
    if not _AGENTS_MD.exists():
        _AGENTS_MD.write_text(_AGENTS_MD_CONTENT, encoding="utf-8")
        console.print("[green]OK AGENTS.md 생성됨[/green]")


def _ensure_dirs() -> None:
    for d in [
        "memory/journal",
        "memory/retrospectives",
        "memory/proposals/pending",
        "memory/proposals/applied",
    ]:
        Path(d).mkdir(parents=True, exist_ok=True)


def _open_db() -> Optional[sqlite3.Connection]:
    db_path = os.getenv("AF_DB_PATH", "agentforge.db")
    if not Path(db_path).exists():
        console.print("[yellow]데이터베이스 없음 — 실행된 세션이 없습니다.[/yellow]")
        return None
    try:
        return sqlite3.connect(db_path)
    except Exception as exc:
        console.print(f"[red]DB 오류: {exc}[/red]")
        return None


def _decode_metadata(raw: bytes | str | None) -> dict:
    """Decode LangGraph checkpoint metadata (JSON bytes or str)."""
    if not raw:
        return {}
    try:
        if isinstance(raw, (bytes, bytearray)):
            return json.loads(raw.decode())
        return json.loads(raw)
    except Exception:
        return {}


@app.command()
def start(
    mock: bool  = typer.Option(False, "--mock",  help="Mock mode (no API calls)"),
    debug: bool = typer.Option(False, "--debug", help="Debug logging to logs/ directory"),
    port: int   = typer.Option(3000, "--port",   help="Health-check HTTP port"),
) -> None:
    """Start the AgentForge Slack bot."""
    if mock:
        os.environ["AF_MOCK_MODE"] = "true"
    if debug:
        os.environ["AF_LOG_LEVEL"] = "DEBUG"

    _configure_logging(debug=debug)
    _ensure_agents_md()
    _ensure_dirs()
    console.print("[bold cyan]AgentForge[/bold cyan] 시작 중...")

    if os.getenv("AF_MOCK_MODE", "false").lower() == "true":
        console.print("[yellow]Mock 모드 활성화 -- API 호출 없음[/yellow]")
        console.print("[green]OK 준비 완료 (mock)[/green]")
        return

    from agentforge.interfaces.slack_interface import SlackInterface

    iface = SlackInterface()

    async def _run() -> None:
        console.print("[green]OK Slack Bot 연결 중...[/green]")
        await iface.start()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]종료됨[/yellow]")


@app.command()
def status() -> None:
    """List all sessions with step count and latest checkpoint."""
    conn = _open_db()
    if conn is None:
        return

    try:
        cur = conn.cursor()
        # One row per session: checkpoint count and latest checkpoint_id
        cur.execute(
            """
            SELECT
                thread_id,
                COUNT(*) AS steps,
                MAX(checkpoint_id) AS latest_checkpoint,
                MAX(rowid) AS last_rowid
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY last_rowid DESC
            LIMIT 50
            """
        )
        rows = cur.fetchall()

        # Fetch metadata for the latest checkpoint of each session
        session_meta: dict[str, dict] = {}
        for thread_id, _steps, latest_cp, _rowid in rows:
            cur.execute(
                "SELECT metadata FROM checkpoints WHERE thread_id=? AND checkpoint_id=?",
                (thread_id, latest_cp),
            )
            row = cur.fetchone()
            session_meta[thread_id] = _decode_metadata(row[0] if row else None)

    finally:
        conn.close()

    if not rows:
        console.print("[yellow]저장된 세션 없음[/yellow]")
        return

    table = Table(title="AgentForge 세션 목록", show_lines=True)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Steps", justify="right")
    table.add_column("마지막 노드", style="green")
    table.add_column("에스컬레이션", justify="right")
    table.add_column("Latest Checkpoint", style="dim")

    for thread_id, steps, latest_cp, _rowid in rows:
        meta = session_meta.get(thread_id, {})
        last_node = meta.get("source", "") or "-"
        writes = meta.get("writes") or {}
        esc_level = "-"
        if isinstance(writes, dict):
            for node_data in writes.values():
                if isinstance(node_data, dict) and "current_escalation_level" in node_data:
                    esc_level = str(node_data["current_escalation_level"])
                    break

        table.add_row(
            thread_id,
            str(steps),
            last_node,
            esc_level,
            latest_cp[:36] if latest_cp else "-",
        )

    console.print(table)
    console.print(
        f"\n[dim]상세 조회: agentforge session <SESSION_ID>[/dim]\n"
        f"[dim]세션 삭제: agentforge kill <SESSION_ID>[/dim]"
    )


@app.command()
def session(
    session_id: str = typer.Argument(..., help="Session ID (prefix OK)"),
) -> None:
    """Show detailed checkpoint history for a session."""
    conn = _open_db()
    if conn is None:
        return

    try:
        cur = conn.cursor()
        # Support prefix matching
        cur.execute(
            """
            SELECT checkpoint_id, parent_checkpoint_id, metadata
            FROM checkpoints
            WHERE thread_id LIKE ?
            ORDER BY rowid ASC
            """,
            (f"{session_id}%",),
        )
        rows = cur.fetchall()

        # Resolve full thread_id from prefix
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
            (f"{session_id}%",),
        )
        thread_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        console.print(f"[red]세션을 찾을 수 없습니다: {session_id}[/red]")
        raise typer.Exit(1)

    if len(thread_ids) > 1:
        console.print(f"[yellow]여러 세션이 매칭됩니다. 더 긴 ID를 입력하세요:[/yellow]")
        for tid in thread_ids:
            console.print(f"  {tid}")
        raise typer.Exit(1)

    full_id = thread_ids[0]
    console.print(Panel(f"[bold cyan]{full_id}[/bold cyan]", title="세션 상세"))

    table = Table(show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Checkpoint ID", style="dim")
    table.add_column("Source")
    table.add_column("Step", justify="right")
    table.add_column("Writes (nodes)")

    for i, (cp_id, parent_id, raw_meta) in enumerate(rows, 1):
        meta = _decode_metadata(raw_meta)
        source = meta.get("source", "-")
        step = str(meta.get("step", "-"))
        writes = meta.get("writes") or {}
        write_nodes = ", ".join(writes.keys()) if isinstance(writes, dict) else "-"
        table.add_row(str(i), cp_id[:36] if cp_id else "-", source, step, write_nodes or "-")

    console.print(table)
    console.print(f"\n총 체크포인트: [bold]{len(rows)}[/bold]")


@app.command()
def kill(
    session_id: str = typer.Argument(..., help="Session ID (prefix OK)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Delete a session and all its checkpoints from the database."""
    conn = _open_db()
    if conn is None:
        return

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
            (f"{session_id}%",),
        )
        thread_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    if not thread_ids:
        console.print(f"[red]세션을 찾을 수 없습니다: {session_id}[/red]")
        raise typer.Exit(1)

    if len(thread_ids) > 1:
        console.print("[yellow]여러 세션이 매칭됩니다. 더 긴 ID를 입력하세요:[/yellow]")
        for tid in thread_ids:
            console.print(f"  {tid}")
        raise typer.Exit(1)

    full_id = thread_ids[0]

    if not yes:
        confirm = typer.confirm(f"세션 '{full_id}' 을(를) 삭제하시겠습니까?")
        if not confirm:
            console.print("[yellow]취소됨[/yellow]")
            raise typer.Exit(0)

    conn = _open_db()
    if conn is None:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM checkpoints WHERE thread_id=?", (full_id,))
        deleted_cp = cur.rowcount
        # writes table (LangGraph SQLite secondary table)
        try:
            cur.execute("DELETE FROM writes WHERE thread_id=?", (full_id,))
            deleted_wr = cur.rowcount
        except sqlite3.OperationalError:
            deleted_wr = 0
        conn.commit()
    finally:
        conn.close()

    console.print(
        f"[green]삭제 완료[/green] — "
        f"체크포인트 {deleted_cp}개, 쓰기 레코드 {deleted_wr}개 제거됨"
    )


@app.command()
def resume(
    session_id: str = typer.Argument(..., help="Session ID to resume"),
    user_choice: str = typer.Option("continue", "--choice", help="L4 choice: continue or abort"),
) -> None:
    """Resume a paused (L4) session from its checkpoint."""
    _ensure_agents_md()

    from agentforge.core.checkpoint import init_checkpointer
    from langgraph.types import Command
    from workflows.builder import GraphBuilder

    console.print(f"[cyan]세션 재개:[/cyan] {session_id}")

    async def _run() -> None:
        checkpointer = await init_checkpointer()
        config = {"configurable": {"thread_id": session_id}}
        state = await checkpointer.aget(config)

        if state is None:
            console.print(f"[red]세션을 찾을 수 없습니다: {session_id}[/red]")
            raise typer.Exit(1)

        workflow_spec = state.get("workflow_spec")
        if workflow_spec is None:
            console.print("[red]워크플로우 스펙 없음 — 재개 불가[/red]")
            raise typer.Exit(1)

        graph = GraphBuilder().from_spec(workflow_spec)
        async for event in graph.astream_events(
            Command(resume=user_choice), config=config, version="v2"
        ):
            event_name = event.get("event", "")
            node_name = event.get("name", "")
            if event_name == "on_chain_start":
                console.print(f"  -> {node_name}")
            elif event_name == "on_chain_end" and node_name == "finalize":
                output = event.get("data", {}).get("output", {})
                console.print("\n[bold green]완료[/bold green]")
                console.print(output.get("final_report", ""))

    asyncio.run(_run())


def main() -> None:
    _ensure_agents_md()
    app()


if __name__ == "__main__":
    main()
