from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_WORKSPACE_BASE = Path(os.getenv("AF_WORKSPACE_DIR", "workspace"))


class WorkspaceManager:
    """
    Per-session workspace with git version control.

    Layout:
        workspace/{session_id}/
            .git/
            src/
            tests/
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.root = _WORKSPACE_BASE / session_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create directory and initialize git repo."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            self._git("init")
            self._git("config", "user.name", "AgentForge")
            self._git("config", "user.email", "bot@agentforge.local")
            # Initial commit so we always have a valid HEAD
            readme = self.root / "README.md"
            readme.write_text(f"# AgentForge Session {self.session_id[:8]}\n", encoding="utf-8")
            self._git("add", "README.md")
            self._git("commit", "-m", "chore: init workspace")
            logger.info("Workspace initialized: %s", self.root)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def write_files(self, files: list[dict]) -> list[Path]:
        """
        Write files to the workspace.
        Each entry: {"path": "src/app.py", "content": "..."}
        Returns list of absolute paths written.
        """
        written: list[Path] = []
        for f in files:
            rel = f.get("path", "")
            content = f.get("content", "")
            if not rel:
                continue
            # Prevent path traversal outside workspace
            target = (self.root / rel).resolve()
            if not str(target).startswith(str(self.root.resolve())):
                logger.warning("Skipping unsafe path: %s", rel)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(target)
        return written

    def file_exists(self, rel_path: str) -> bool:
        return (self.root / rel_path).exists()

    def read_file(self, rel_path: str) -> str:
        return (self.root / rel_path).read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Git
    # ------------------------------------------------------------------

    def commit(self, message: str) -> str:
        """Stage all changes and commit. Returns short commit hash."""
        self._git("add", "-A")
        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.root, capture_output=True, text=True
        )
        if not status.stdout.strip():
            logger.debug("Nothing to commit in %s", self.root)
            return "(no changes)"
        self._git("commit", "-m", message)
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=self.root, capture_output=True, text=True
        )
        sha = result.stdout.strip()
        logger.info("Committed %s: %s", sha, message[:60])
        return sha

    def git_log(self, n: int = 10) -> str:
        result = subprocess.run(
            ["git", "log", f"--oneline", f"-{n}"],
            cwd=self.root, capture_output=True, text=True
        )
        return result.stdout.strip()

    def git_diff(self, ref: str = "HEAD~1") -> str:
        result = subprocess.run(
            ["git", "diff", ref],
            cwd=self.root, capture_output=True, text=True
        )
        return result.stdout

    # ------------------------------------------------------------------
    # Test runner (Docker → local fallback)
    # ------------------------------------------------------------------

    async def run_tests(self, test_path: str = "tests") -> "TestResult":
        """Run tests in Docker if available, otherwise locally."""
        abs_test = self.root / test_path
        if not abs_test.exists():
            return TestResult(success=True, output="(no tests found)", skipped=True)

        # Try Docker first
        docker_result = await self._run_docker_tests(test_path)
        if docker_result is not None:
            return docker_result

        # Fallback: local subprocess
        return await self._run_local_tests(test_path)

    async def _run_docker_tests(self, test_path: str) -> Optional["TestResult"]:
        """Run pytest in a Docker container with workspace mounted."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "run", "--rm",
                "--network=none",
                "--memory=512m",
                "--cpus=1",
                "-v", f"{self.root.resolve()}:/workspace",
                "-w", "/workspace",
                "python:3.11-slim",
                "sh", "-c", f"pip install pytest -q 2>/dev/null && pytest {test_path} -v --tb=short",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            return TestResult(success=proc.returncode == 0, output=output, runner="docker")
        except FileNotFoundError:
            logger.debug("Docker not available — falling back to local test runner")
            return None
        except asyncio.TimeoutError:
            return TestResult(success=False, output="Tests timed out (120s)", runner="docker")
        except Exception as exc:
            logger.warning("Docker test run failed: %s", exc)
            return None

    async def _run_local_tests(self, test_path: str) -> "TestResult":
        """Run pytest locally in the workspace directory."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "python", "-m", "pytest", test_path, "-v", "--tb=short",
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            return TestResult(success=proc.returncode == 0, output=output, runner="local")
        except asyncio.TimeoutError:
            return TestResult(success=False, output="Tests timed out (120s)", runner="local")
        except Exception as exc:
            return TestResult(success=False, output=f"Test runner error: {exc}", runner="local")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )


class TestResult:
    def __init__(
        self,
        success: bool,
        output: str,
        runner: str = "none",
        skipped: bool = False,
    ) -> None:
        self.success = success
        self.output = output
        self.runner = runner
        self.skipped = skipped

    def __repr__(self) -> str:
        status = "PASS" if self.success else "FAIL"
        return f"TestResult({status}, runner={self.runner})"
