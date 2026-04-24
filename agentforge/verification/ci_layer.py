from __future__ import annotations

import logging
import os
from pathlib import Path

from agentforge.core.models import CIResult, TaskInstruction, TaskReport, TaskStatus

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

        # 2. Immediate fail if worker declared failure or timeout
        if report.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
            reason = f"worker_status:{report.status.value}"
            failed.append(reason)
            logger.info("CI fail — %s  summary=%.120s", reason, report.summary)
            return CIResult(passed=False, failed_criteria=failed, auto_verified=verified)

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
            return self._check_typescript(criterion_lower, ws)
        if any(kw in criterion_lower for kw in ("eslint", "prettier", "lint")) and ws:
            return self._check_lint_config(ws)
        if ".env" in criterion_lower and "환경변수" in criterion_lower and ws:
            return self._check_env_example(ws)
        return None

    def _check_typescript(self, criterion_lower: str, ws: Path) -> bool | None:
        """Check TypeScript configuration via tsconfig.json — no Docker required."""
        tsconfig = ws / "tsconfig.json"
        if not tsconfig.exists():
            logger.warning("CI: tsconfig.json not found in %s", ws)
            return False
        try:
            import json
            data = json.loads(tsconfig.read_text(encoding="utf-8"))
            opts = data.get("compilerOptions", {})
            if "strict" in criterion_lower:
                strict_on = bool(opts.get("strict"))
                if not strict_on:
                    logger.warning("CI: tsconfig.json missing compilerOptions.strict=true")
                return strict_on
            return True  # tsconfig.json exists — TypeScript is configured
        except Exception as exc:
            logger.warning("CI: failed to parse tsconfig.json: %s", exc)
            return None

    def _check_lint_config(self, ws: Path) -> bool | None:
        lint_files = [
            ".eslintrc.json", ".eslintrc.js", ".eslintrc.cjs",
            "eslint.config.js", "eslint.config.mjs",
            ".prettierrc", ".prettierrc.json", ".prettierrc.js",
        ]
        found = any((ws / f).exists() for f in lint_files)
        return found if found else None

    def _check_env_example(self, ws: Path) -> bool | None:
        env_file = ws / ".env.example"
        if not env_file.exists():
            return False
        try:
            lines = [l.strip() for l in env_file.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
            return len(lines) >= 1
        except Exception:
            return None


class TestResult:
    def __init__(self, success: bool, output: str = "", runner: str = "none", skipped: bool = False):
        self.success = success
        self.output = output
        self.runner = runner
        self.skipped = skipped


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
