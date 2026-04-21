from __future__ import annotations

import os
from pathlib import Path

from agentforge.core.models import CIResult, SandboxResult, TaskInstruction, TaskReport


class CIVerifier:
    async def verify(self, instruction: TaskInstruction, report: TaskReport) -> CIResult:
        failed: list[str] = []
        verified: list[str] = []

        # 1. Schema validation — report must be a valid TaskReport (already is)
        verified.append("report_schema_valid")

        # 2. Deliverable files exist
        for path in report.deliverables:
            if not Path(path).exists():
                failed.append(f"missing_file:{path}")
            else:
                verified.append(f"file_exists:{path}")

        # 3. Test results in evidence
        evidence = report.evidence
        tests_failed = evidence.get("tests_failed", 0)
        if isinstance(tests_failed, int) and tests_failed > 0:
            failed.append(f"tests_failed:{tests_failed}")
        elif "tests_passed" in evidence:
            verified.append("tests_passed")

        # 4. Acceptance criteria that can be auto-checked
        for criterion in instruction.acceptance_criteria:
            result = await self._auto_check(criterion, report)
            if result is True:
                verified.append(f"criterion_auto:{criterion}")
            elif result is False:
                failed.append(f"criterion_failed:{criterion}")
            # None means "cannot auto-check", leave for semantic layer

        return CIResult(passed=len(failed) == 0, failed_criteria=failed, auto_verified=verified)

    async def _auto_check(self, criterion: str, report: TaskReport) -> bool | None:
        """Return True/False if auto-checkable, None if not."""
        import os

        if _is_mock():
            return True

        criterion_lower = criterion.lower()

        if "typescript" in criterion_lower and "오류" in criterion_lower:
            result = await self._run_tsc()
            return result.success

        return None  # Defer to semantic layer

    async def _run_tsc(self) -> SandboxResult:
        from agentforge.sandbox.docker_executor import DockerExecutor
        executor = DockerExecutor()
        return await executor.run_command("tsc --noEmit")


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
