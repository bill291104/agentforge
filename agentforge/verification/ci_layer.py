from __future__ import annotations

import logging
import os
from pathlib import Path

from agentforge.core.models import CIResult, TaskInstruction, TaskReport

logger = logging.getLogger(__name__)


class CIVerifier:
    async def verify(
        self,
        instruction: TaskInstruction,
        report: TaskReport,
        workspace_root: str | None = None,
    ) -> CIResult:
        failed: list[str] = []
        verified: list[str] = []

        ws = Path(workspace_root) if workspace_root else None

        # 1. Schema — report is always a valid TaskReport
        verified.append("report_schema_valid")

        # 2. Deliverable files exist in workspace
        for rel_path in report.deliverables:
            if ws:
                exists = (ws / rel_path).exists()
            else:
                exists = Path(rel_path).exists()

            if exists:
                verified.append(f"file_exists:{rel_path}")
            else:
                failed.append(f"missing_file:{rel_path}")
                logger.warning("CI: missing deliverable %s (workspace=%s)", rel_path, ws)

        # 3. Test results in evidence
        tests_failed = report.evidence.get("tests_failed", 0)
        if isinstance(tests_failed, int) and tests_failed > 0:
            failed.append(f"tests_failed:{tests_failed}")
        elif "tests_passed" in report.evidence:
            verified.append("tests_passed")

        # 4. Run tests if workspace has a tests/ dir
        if ws and not _is_mock():
            test_dir = ws / "tests"
            if test_dir.exists():
                test_result = await self._run_tests(ws)
                if test_result.success:
                    verified.append(f"tests_run_ok:{test_result.runner}")
                elif not test_result.skipped:
                    failed.append(f"tests_run_failed:{test_result.runner}")
                    logger.info("CI test output:\n%s", test_result.output[-500:])

        # 5. Acceptance criteria auto-check
        for criterion in instruction.acceptance_criteria:
            result = await self._auto_check(criterion, report, ws)
            if result is True:
                verified.append(f"criterion:{criterion[:40]}")
            elif result is False:
                failed.append(f"criterion_failed:{criterion[:40]}")

        return CIResult(passed=len(failed) == 0, failed_criteria=failed, auto_verified=verified)

    async def _run_tests(self, ws: Path) -> "TestResult":
        from agentforge.workspace.manager import WorkspaceManager, TestResult
        manager = WorkspaceManager(ws.name)
        manager.root = ws
        return await manager.run_tests("tests")

    async def _auto_check(
        self, criterion: str, report: TaskReport, ws: Path | None
    ) -> bool | None:
        if _is_mock():
            return True
        criterion_lower = criterion.lower()
        if "typescript" in criterion_lower and ws:
            return await self._run_tsc(ws)
        return None

    async def _run_tsc(self, ws: Path) -> bool:
        from agentforge.sandbox.docker_executor import DockerExecutor
        executor = DockerExecutor()
        result = await executor.run_command("tsc --noEmit")
        return result.success


class TestResult:
    def __init__(self, success: bool, output: str = "", runner: str = "none", skipped: bool = False):
        self.success = success
        self.output = output
        self.runner = runner
        self.skipped = skipped


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
