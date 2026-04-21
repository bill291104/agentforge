from __future__ import annotations

from langchain_core.tools import tool

from agentforge.sandbox.docker_executor import DockerExecutor

_executor = DockerExecutor()


@tool
async def docker_python(code: str) -> str:
    """Execute Python code in an isolated Docker container and return stdout/stderr."""
    result = await _executor.run_code(code, language="python")
    if result.success:
        return result.stdout or "(no output)"
    return f"ERROR (exit {result.exit_code}):\n{result.stderr}"


@tool
async def docker_bash(command: str) -> str:
    """Execute a bash command in an isolated Docker container and return output."""
    result = await _executor.run_command(command)
    if result.success:
        return result.stdout or "(no output)"
    return f"ERROR (exit {result.exit_code}):\n{result.stderr}"


@tool
async def docker_run_tests(test_path: str = ".") -> str:
    """Run pytest in an isolated Docker container and return test results."""
    result = await _executor.run_tests(test_path)
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    return output.strip() or "(no output)"


# Convenience list for binding to agents
ALL_DEV_TOOLS = [docker_python, docker_bash, docker_run_tests]
