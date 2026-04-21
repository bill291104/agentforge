from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"


class SelfImproveWorkflow:
    """
    Applies accepted improvement proposals to AgentForge's own codebase.

    Safety constraints:
    - Always creates a git branch before making changes.
    - Config changes (YAML/env) → apply directly, no restart needed.
    - Code changes → run existing test suite via Docker sandbox; abort on failure.
    """

    def __init__(self, project_root: Optional[Path] = None) -> None:
        self._root = project_root or Path(".")

    async def apply(self, proposal: "ImprovementProposal") -> "ReloadGuide":  # noqa: F821
        """Apply a proposal and return a ReloadGuide for the user."""
        from agentforge.core.models import ImprovementProposal, ReloadGuide

        if _MOCK:
            return ReloadGuide(
                proposal_id=proposal.proposal_id,
                changed_files=proposal.target_files,
                restart_required=False,
                instructions="[mock] No changes made in mock mode.",
                claude_code_command=None,
            )

        branch = f"self-improve/proposal-{proposal.proposal_id}"
        self._git("checkout", "-b", branch)

        try:
            if proposal.change_type == "config":
                changed = await self._apply_config(proposal)
                restart_needed = False
            else:
                changed = await self._apply_code(proposal)
                restart_needed = True
                # Verify tests still pass
                ok = await self._run_tests()
                if not ok:
                    self._git("checkout", "-")
                    self._git("branch", "-D", branch)
                    raise RuntimeError(
                        f"Tests failed after applying proposal {proposal.proposal_id}. Branch deleted."
                    )
        except Exception:
            # Attempt rollback on any error
            try:
                self._git("checkout", "-")
                self._git("branch", "-D", branch)
            except Exception:
                pass
            raise

        # Mark proposal as applied
        self._move_proposal(proposal.proposal_id)

        guide_text = self._build_guide_text(branch, changed, restart_needed)
        claude_cmd = (
            f'claude "memory/proposals/applied/proposal_{proposal.proposal_id}.md 를 읽고 변경사항을 검토하여 적용하라"'
            if restart_needed
            else None
        )
        return ReloadGuide(
            proposal_id=proposal.proposal_id,
            changed_files=changed,
            restart_required=restart_needed,
            instructions=guide_text,
            claude_code_command=claude_cmd,
        )

    # ------------------------------------------------------------------
    # Config change: direct file edit
    # ------------------------------------------------------------------

    async def _apply_config(self, proposal: "ImprovementProposal") -> list[str]:  # noqa: F821
        changed: list[str] = []
        if not proposal.diff_preview:
            return changed

        for target in proposal.target_files:
            path = self._root / target
            if not path.exists():
                logger.warning("Target file not found: %s", path)
                continue
            # diff_preview format: simple `- old_line\n+ new_line`
            text = path.read_text(encoding="utf-8")
            for line in proposal.diff_preview.splitlines():
                if line.startswith("- "):
                    text = text.replace(line[2:].strip(), "", 1)
                elif line.startswith("+ "):
                    text += "\n" + line[2:].strip()
            path.write_text(text, encoding="utf-8")
            changed.append(target)

        if changed:
            self._git("add", *changed)
            self._git("commit", "-m", f"self-improve: apply proposal {proposal.proposal_id}")
        return changed

    # ------------------------------------------------------------------
    # Code change: run AgentForge workflow on its own codebase
    # ------------------------------------------------------------------

    async def _apply_code(self, proposal: "ImprovementProposal") -> list[str]:  # noqa: F821
        """Use the Leader+Worker workflow to apply a code change proposal."""
        from agentforge.core.models import WorkflowSpec, TaskSpec
        from agentforge.core.state import make_initial_state
        from workflows.builder import GraphBuilder

        description = (
            f"제안서 #{proposal.proposal_id} 적용:\n"
            f"문제: {proposal.problem}\n"
            f"변경: {proposal.diff_preview or proposal.root_cause}\n"
            f"대상 파일: {', '.join(proposal.target_files)}"
        )
        spec = WorkflowSpec(
            name=f"self_improvement_{proposal.proposal_id}",
            tasks=[
                TaskSpec(
                    id="apply_change",
                    title="코드 변경 적용",
                    description=description,
                    acceptance_criteria=["변경 사항이 target_files에 반영됨", "기존 테스트 통과"],
                    depends_on=[],
                ),
            ],
        )
        state = make_initial_state(
            session_id=f"self-improve-{proposal.proposal_id}",
            user_request=description,
        )
        state["workflow_spec"] = spec
        graph = GraphBuilder().from_spec(spec)
        config = {"configurable": {"thread_id": state["session_id"]}}
        result = await graph.ainvoke(state, config=config)
        return proposal.target_files

    # ------------------------------------------------------------------
    # Test runner
    # ------------------------------------------------------------------

    async def _run_tests(self) -> bool:
        """Run the full test suite; return True if all pass."""
        try:
            from agentforge.sandbox.docker_executor import DockerExecutor

            executor = DockerExecutor()
            result = await executor.run_tests("tests/")
            return result.success
        except Exception as exc:
            logger.warning("Test run failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> None:
        cmd = ["git"] + list(args)
        result = subprocess.run(cmd, cwd=str(self._root), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr}")

    def _move_proposal(self, proposal_id: str) -> None:
        pending = self._root / "memory/proposals/pending" / f"proposal_{proposal_id}.md"
        applied = self._root / "memory/proposals/applied" / f"proposal_{proposal_id}.md"
        if pending.exists():
            applied.parent.mkdir(parents=True, exist_ok=True)
            pending.rename(applied)

    def _build_guide_text(self, branch: str, changed: list[str], restart: bool) -> str:
        files = "\n".join(f"  • {f}" for f in changed) or "  (없음)"
        if restart:
            return (
                f"변경 파일:\n{files}\n\n"
                f"적용 방법:\n"
                f"  git merge {branch}\n"
                f"  uv run agentforge start   # 재시작"
            )
        return (
            f"변경 파일:\n{files}\n\n"
            f"적용 방법: 재시작 불필요 — 다음 태스크부터 자동 적용됩니다."
        )
