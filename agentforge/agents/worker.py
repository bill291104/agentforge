from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import anthropic

from agentforge.agents.base import BaseAgent
from agentforge.core.models import (
    MODEL_IDS,
    ModelTier,
    TaskInstruction,
    TaskReport,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Worker tools (file system + git)
# ---------------------------------------------------------------------------

_WORKER_TOOLS = [
    {
        "name": "read_file",
        "description": "워크스페이스에서 파일을 읽습니다",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "워크스페이스 기준 상대 경로"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "워크스페이스에 파일을 작성합니다. 디렉토리가 없으면 자동 생성됩니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "워크스페이스 기준 상대 경로"},
                "content": {"type": "string", "description": "파일 전체 내용"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "워크스페이스 내 파일 목록을 반환합니다",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "조회할 디렉토리 (기본값: 루트)"},
            },
        },
    },
    {
        "name": "git_commit",
        "description": "모든 변경사항을 스테이징하고 커밋합니다. check_criterion으로 모든 수락 기준 확인 후 호출하세요.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "커밋 메시지"}},
            "required": ["message"],
        },
    },
    {
        "name": "check_criterion",
        "description": (
            "수락 기준 하나가 현재 워크스페이스 상태에서 충족되는지 확인합니다. "
            "git_commit 호출 전에 각 수락 기준을 반드시 확인하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "criterion": {
                    "type": "string",
                    "description": "확인할 수락 기준 문자열 (지시서의 acceptance_criteria 항목 그대로)",
                }
            },
            "required": ["criterion"],
        },
    },
    {
        "name": "search_files",
        "description": "워크스페이스에서 텍스트 패턴을 검색합니다. 기존 구현 확인, import 경로 파악 등에 활용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "검색할 텍스트 또는 정규식"},
                "file_glob": {
                    "type": "string",
                    "description": "검색 대상 파일 패턴 (예: **/*.ts, **/*.py). 기본값: **/*",
                },
            },
            "required": ["pattern"],
        },
    },
]

_MAX_TURNS  = 25
_WARN_AFTER = 18  # inject "commit now" warning when turn index reaches this

_WORKER_SYSTEM_PROMPT = """\
당신은 소프트웨어 개발 워커 에이전트입니다.

작업 순서:
1. read_file로 지시 파일을 읽고 요구사항과 수락 기준을 파악하세요
2. write_file로 필요한 파일들을 하나씩 작성하세요
3. 커밋 전에 각 수락 기준을 check_criterion으로 반드시 확인하세요
4. 모든 기준이 ✅ 확인된 후 git_commit을 호출하세요
5. check_criterion이 ❌를 반환하면 해당 부분을 수정하고 다시 확인하세요

규칙:
- git_commit 없이 종료하면 작업 실패로 처리됩니다
- 파일은 한 번에 하나씩 write_file로 작성하세요
- 핵심 기능을 먼저 구현하고 git_commit을 호출하세요. 추가 파일은 그 다음에 작성하세요.
- 커밋 메시지 형식: feat({task_id}): 간략한 설명
- 기존 파일 구조나 import 경로를 파악할 때 search_files를 활용하세요
"""


class WorkerAgent(BaseAgent):
    def __init__(self, model_tier: ModelTier = ModelTier.HAIKU) -> None:
        super().__init__(model_tier)
        # AF_WORKER_MODEL은 기본(HAIKU) 티어에만 적용; 에스컬레이션 업그레이드는 존중
        if model_tier == ModelTier.HAIKU:
            self._model = os.getenv("AF_WORKER_MODEL", MODEL_IDS[ModelTier.HAIKU])
        else:
            self._model = MODEL_IDS.get(model_tier, MODEL_IDS[ModelTier.SONNET])

    async def execute(
        self,
        instruction: TaskInstruction,
        workspace_root: str | None = None,
    ) -> TaskReport:
        if _is_mock():
            return _mock_report(instruction, workspace_root)

        start   = time.monotonic()
        ws_root = Path(workspace_root).resolve() if workspace_root else None
        branch  = f"task/{instruction.task_id}"

        if ws_root:
            ws = _make_ws(ws_root)
            ws.create_branch(branch)
            logger.info("[worker:%s] branch=%s", instruction.task_id, branch)
        else:
            logger.warning(
                "[worker:%s] workspace_root is None — files will NOT be saved",
                instruction.task_id,
            )

        instruction_path = f"instructions/{instruction.task_id}.md"
        prompt = (
            f"지시 파일을 읽고 요구사항을 파악한 뒤 구현을 시작하세요.\n"
            f"지시 파일 경로: {instruction_path}\n\n"
            f"작업 ID: {instruction.task_id}"
        )

        client        = anthropic.AsyncAnthropic()
        messages      = [{"role": "user", "content": prompt}]
        total_tokens    = 0
        last_commit_sha: str | None = None   # SHA of most recent commit (multiple commits allowed)
        written_files: list[str] = []
        last_summary    = "작업 완료"

        logger.info(
            "[worker:%s] tool-use loop start model=%s timeout=%dm",
            instruction.task_id, self._model, instruction.timeout_minutes,
        )

        for turn in range(_MAX_TURNS):
            try:
                response = await self._run_with_timeout(
                    client.messages.create(
                        model=self._model,
                        max_tokens=8192,
                        system=[{
                            "type": "text",
                            "text": _WORKER_SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        messages=messages,
                        tools=_WORKER_TOOLS,
                    ),
                    instruction.timeout_minutes,
                )
            except TimeoutError:
                return TaskReport(
                    task_id=instruction.task_id,
                    status=TaskStatus.TIMEOUT,
                    summary=f"타임아웃 ({instruction.timeout_minutes}분)",
                    tokens_used=total_tokens,
                    duration_seconds=time.monotonic() - start,
                )

            total_tokens += response.usage.input_tokens + response.usage.output_tokens
            tool_blocks   = [b for b in response.content if b.type == "tool_use"]
            text_contents = [b.text for b in response.content if b.type == "text"]
            if text_contents:
                last_summary = text_contents[-1][:300]

            logger.info(
                "[worker:%s] turn=%d stop=%s tools=%s",
                instruction.task_id, turn, response.stop_reason,
                [b.name for b in tool_blocks],
            )

            messages.append({"role": "assistant", "content": response.content})

            # Model signalled it is done (no tool call) — exit regardless of commit state
            if response.stop_reason == "end_turn" and not tool_blocks:
                break

            tool_results = []
            for block in tool_blocks:
                if block.name == "check_criterion":
                    result = await _check_criterion(block.input, ws_root, written_files)
                elif block.name == "search_files":
                    result = _search_files(block.input, ws_root)
                else:
                    result = _execute_worker_tool(block.name, block.input, ws_root)
                if block.name == "write_file" and result.startswith("✅"):
                    written_files.append(block.input["path"])
                if block.name == "git_commit" and "sha=" in result:
                    last_commit_sha = result.split("sha=")[1].strip()
                    logger.info(
                        "[worker:%s] commit turn=%d sha=%s total_files=%d",
                        instruction.task_id, turn, last_commit_sha, len(written_files),
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            # Inject turn-limit warning when approaching limit and no commit yet
            turns_left = _MAX_TURNS - turn - 1
            if turns_left <= (_MAX_TURNS - _WARN_AFTER) and not last_commit_sha and written_files:
                warning = (
                    f"\n\n🚨 경고: 남은 API 호출 횟수 {turns_left}회. "
                    f"이미 {len(written_files)}개 파일 작성 완료. "
                    f"추가 파일 작성보다 지금 즉시 git_commit 호출이 최우선입니다!"
                )
                tool_results[-1]["content"] += warning
                logger.warning(
                    "[worker:%s] turn=%d turns_left=%d — injecting commit warning",
                    instruction.task_id, turn, turns_left,
                )

            messages.append({"role": "user", "content": tool_results})

        duration = time.monotonic() - start

        # Normal completion: model committed at least once
        if last_commit_sha:
            logger.info(
                "[worker:%s] done commit=%s files=%d tokens=%d duration=%.1fs",
                instruction.task_id, last_commit_sha, len(written_files),
                total_tokens, duration,
            )
            return TaskReport(
                task_id=instruction.task_id,
                status=TaskStatus.COMPLETED,
                deliverables=written_files,
                evidence={"commit_sha": last_commit_sha, "files_written": len(written_files)},
                summary=last_summary,
                tokens_used=total_tokens,
                duration_seconds=duration,
            )

        # Safety net: files written but git_commit never called — auto-commit
        if written_files and ws_root:
            try:
                ws = _make_ws(ws_root)
                sha = ws.commit(f"feat({instruction.task_id}): auto-commit (turn limit reached)")
                if sha and sha != "(no changes)":
                    logger.warning(
                        "[worker:%s] auto-committed after turn limit — sha=%s files=%d",
                        instruction.task_id, sha, len(written_files),
                    )
                    return TaskReport(
                        task_id=instruction.task_id,
                        status=TaskStatus.COMPLETED,
                        deliverables=written_files,
                        evidence={"commit_sha": sha, "files_written": len(written_files), "auto_commit": True},
                        summary=f"턴 제한 도달 후 자동 커밋 ({len(written_files)}개 파일 작성됨)",
                        tokens_used=total_tokens,
                        duration_seconds=duration,
                    )
                logger.warning("[worker:%s] auto-commit: nothing to commit", instruction.task_id)
            except Exception as auto_exc:
                logger.error("[worker:%s] auto-commit failed: %s", instruction.task_id, auto_exc)

        return TaskReport(
            task_id=instruction.task_id,
            status=TaskStatus.FAILED,
            summary="git_commit 미호출 — 작업 미완료",
            tokens_used=total_tokens,
            duration_seconds=duration,
        )

    async def _run_with_timeout(self, coro, timeout_minutes: int):
        return await asyncio.wait_for(coro, timeout=timeout_minutes * 60)

    @staticmethod
    async def summarize(report: TaskReport) -> str:
        if _is_mock():
            return f"[{report.task_id}] {report.summary}"
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=MODEL_IDS[ModelTier.HAIKU],
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": (
                    f"다음 보고서를 한 문장으로 요약: {report.summary} "
                    f"/ 산출물: {report.deliverables}"
                ),
            }],
        )
        return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Worker tool executor
# ---------------------------------------------------------------------------

def _execute_worker_tool(name: str, args: dict, ws_root: Path | None) -> str:
    if ws_root is None:
        return "오류: workspace_root 없음"
    ws = _make_ws(ws_root)
    try:
        if name == "read_file":
            return ws.read_file(args["path"])
        if name == "write_file":
            ws.write_files([{"path": args["path"], "content": args["content"]}])
            return f"✅ {args['path']} 작성 완료"
        if name == "list_files":
            d = ws.root / args.get("directory", ".")
            if not d.exists():
                return f"(디렉토리 없음: {args.get('directory', '.')})"
            files = sorted(p.relative_to(ws.root) for p in d.rglob("*") if p.is_file())
            return "\n".join(str(f) for f in files) or "(빈 디렉토리)"
        if name == "git_commit":
            sha = ws.commit(args["message"])
            return f"커밋 완료 sha={sha}"
    except Exception as exc:
        return f"오류: {exc}"
    return f"알 수 없는 도구: {name}"


async def _check_criterion(args: dict, ws_root: Path | None, written_files: list[str]) -> str:
    criterion = args.get("criterion", "")
    if not criterion:
        return "❌ criterion 파라미터가 필요합니다"
    if ws_root is None:
        return "⚠️ workspace_root 없음 — 파일 기반 확인 불가"
    from agentforge.verification.ci_layer import CIVerifier
    verifier = CIVerifier()
    dummy_report = TaskReport(
        task_id="self_check",
        status=TaskStatus.COMPLETED,
        deliverables=written_files,
        summary="",
    )
    try:
        result = await verifier._auto_check(criterion, dummy_report, ws_root)
    except Exception as exc:
        return f"⚠️ 자동 확인 중 오류: {exc}"
    if result is True:
        return f"✅ 기준 충족: {criterion}"
    if result is False:
        return f"❌ 기준 미충족: {criterion} — 수정 후 다시 확인하세요"
    return f"⚠️ 자동 확인 불가 (수동 판단 필요): {criterion}"


def _search_files(args: dict, ws_root: Path | None) -> str:
    if ws_root is None:
        return "오류: workspace_root 없음"
    pattern = args.get("pattern", "")
    file_glob = args.get("file_glob", "**/*")
    if not pattern:
        return "오류: pattern 파라미터가 필요합니다"
    import re
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(re.escape(pattern))
    matches: list[str] = []
    for path in ws_root.glob(file_glob):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = path.relative_to(ws_root)
                matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                if len(matches) >= 30:
                    break
        if len(matches) >= 30:
            matches.append("(결과 30개로 제한됨)")
            break
    return "\n".join(matches) if matches else f"'{pattern}' 패턴을 찾을 수 없습니다"


def _make_ws(ws_root: Path):
    from agentforge.workspace.manager import WorkspaceManager
    ws = WorkspaceManager(ws_root.name)
    ws.root = ws_root
    return ws


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

def _mock_report(instruction: TaskInstruction, workspace_root: str | None) -> TaskReport:
    files = [
        {
            "path": f"src/{instruction.task_id}.py",
            "content": f"# Mock implementation of {instruction.title}\nprint('hello')\n",
        },
        {
            "path": f"tests/test_{instruction.task_id}.py",
            "content": (
                f"# Mock tests for {instruction.task_id}\n"
                "def test_placeholder():\n    assert True\n"
            ),
        },
    ]

    if workspace_root:
        ws_root = Path(workspace_root).resolve()
        ws = _make_ws(ws_root)
        ws.create_branch(f"task/{instruction.task_id}")
        ws.write_files(files)
        sha = ws.commit(f"feat({instruction.task_id}): mock implementation")
        deliverables = [f["path"] for f in files]
    else:
        sha = "(mock)"
        deliverables = [f["path"] for f in files]

    return TaskReport(
        task_id=instruction.task_id,
        status=TaskStatus.COMPLETED,
        deliverables=deliverables,
        evidence={"commit_sha": sha, "files_written": len(files)},
        summary=f"[Mock] {instruction.title} 완료",
        tokens_used=100,
        duration_seconds=0.1,
    )


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
