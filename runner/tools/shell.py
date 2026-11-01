"""
Shell Tool

Runs shell commands.
"""

import subprocess
from typing import Annotated, Any, Dict, Optional

from loguru import logger
from pydantic import Field

from runner.kernel.logtext import shown

from .base import FilePath, Tool, ToolArguments, forwarded


class ShellArgs(ToolArguments):
    command: Annotated[str, Field(description="Shell command to execute")]
    timeout: Annotated[float | None, Field(description="Timeout in seconds (default: 60)")] = None
    cwd: Annotated[FilePath | None, Field(description="Working directory")] = None


class ShellTool(Tool[ShellArgs]):
    """Run shell commands."""

    arguments = ShellArgs

    def __init__(self, config: Dict[str, Any]):
        super().__init__(
            name="shell",
            description="Execute shell commands",
        )
        self.config = config
        self.default_timeout = config.get("tools", {}).get("shell", {}).get("timeout", 60)

    def call(self, args: ShellArgs) -> Dict[str, Any]:
        return self.execute(**forwarded(args))

    def execute(
        self,
        command: str,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run one shell command."""
        logger.info(f"Executing shell command: {shown(command)}")

        timeout = timeout or self.default_timeout

        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd
            )

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Command timeout after {timeout}s: {shown(command)}")
            return {
                "success": False,
                "error": f"Command timeout after {timeout}s",
                "returncode": -1,
            }

        except Exception as e:
            logger.error(f"Command execution error: {e}")
            return {"success": False, "error": str(e), "returncode": -1}
