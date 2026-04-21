from __future__ import annotations

import asyncio
import os
import time

from agentforge.core.models import SandboxResult

_SANDBOX_IMAGE = os.getenv("AF_SANDBOX_IMAGE", "python:3.11-slim")
_MEMORY_LIMIT = os.getenv("AF_SANDBOX_MEMORY", "512m")
_CPU_LIMIT = os.getenv("AF_SANDBOX_CPUS", "1")


class DockerExecutor:
    async def run_code(self, code: str, language: str = "python") -> SandboxResult:
        if language == "python":
            cmd = f'python -c {_quote(code)}'
        else:
            cmd = code
        return await self.run_command(cmd)

    async def run_tests(self, test_dir: str) -> SandboxResult:
        return await self.run_command(f"pytest {test_dir} -v --tb=short")

    async def run_command(self, cmd: str, workdir: str = "/workspace") -> SandboxResult:
        if _is_mock():
            return SandboxResult(success=True, stdout="mock output", exit_code=0)

        docker_cmd = [
            "docker", "run", "--rm",
            "--network=none",
            f"--memory={_MEMORY_LIMIT}",
            f"--cpus={_CPU_LIMIT}",
            "--read-only",
            "--tmpfs", "/tmp",
            "-w", workdir,
            _SANDBOX_IMAGE,
            "sh", "-c", cmd,
        ]

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
            duration = time.monotonic() - start
            success = proc.returncode == 0
            return SandboxResult(
                success=success,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                exit_code=proc.returncode or 0,
                duration_seconds=duration,
            )
        except asyncio.TimeoutError:
            return SandboxResult(
                success=False,
                stderr="Command timed out",
                exit_code=124,
                duration_seconds=time.monotonic() - start,
            )
        except FileNotFoundError:
            return SandboxResult(
                success=False,
                stderr="Docker not found — install Docker or set AF_MOCK_MODE=true",
                exit_code=127,
            )


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
