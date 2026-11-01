"""
Task Executor

Executes tasks using the tool registry and a model.
"""

from typing import Any, Dict

from loguru import logger

from .ai_client import AIClient
from .kernel.logtext import shown
from .permissions.manager import PermissionManager
from .tools.registry import ToolRegistry


class TaskExecutor:
    """Executor per i task"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

        # Tool registry
        self.tools = ToolRegistry(config)

        # Permission manager
        self.permissions = PermissionManager(config)

        # AI client
        self.ai_client = AIClient(config)

    def execute(self, task: Dict[str, Any]) -> Any:
        """
        Execute a task

        Args:
            task: Task object con tipo, payload, etc.

        Returns:
            The execution result
        """
        task_type = task.get("type", "command")
        payload = task.get("payload", {})

        logger.info(f"Executing task type: {task_type}")

        # Routing per tipo di task
        if task_type == "command":
            return self._execute_command(payload)

        elif task_type == "tool":
            return self._execute_tool(payload)

        elif task_type == "ai_task":
            return self._execute_ai_task(payload)

        elif task_type == "explore":
            return self._execute_explore(payload)

        elif task_type == "workflow":
            return self._execute_workflow(payload)

        else:
            raise ValueError(f"Unknown task type: {task_type}")

    def _execute_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an instruction written in natural language.

        Through the agentic loop, which is where the perimeter lives: a command
        is a task with a prompt, and there is no reason for it to take a
        different route from an `ai_task`.

        It used to call `AIClient.execute_command`, which posts the instruction
        to the control plane's runner endpoint. On a machine with no
        credentials that client is `None`, so `annona run --once --task …` — the
        documented way to run one thing — died with

            'NoneType' object has no attribute 'runner_execute'

        and, worse, when it *did* work it bypassed placement entirely.
        """
        command = payload.get("command")

        if not command:
            raise ValueError("No command provided")

        logger.info(f"Executing command: {shown(command)}")

        result = self.ai_client.reason_and_execute(
            prompt=command,
            context=payload.get("context", {}),
            tools=self.tools,
            permissions=self.permissions,
        )

        return {"type": "command_result", "command": command, "result": result}

    def _execute_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute one specific tool."""
        tool_name = payload.get("tool")
        tool_args = payload.get("args", {})

        if not tool_name:
            raise ValueError("No tool specified")

        # Policy check
        if not self.permissions.check_tool_permission(tool_name, tool_args):
            raise PermissionError(f"Permission denied for tool: {tool_name}")

        # Esegui il tool
        tool = self.tools.get_tool(tool_name)
        result = tool.run(tool_args)

        return {"type": "tool_result", "tool": tool_name, "result": result}

    def _execute_ai_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a task that needs model reasoning."""
        prompt = payload.get("prompt")
        context = payload.get("context", {})

        if not prompt:
            raise ValueError("No prompt provided")

        logger.info(f"Executing AI task: {shown(prompt)}")

        # Usa AI per ragionare ed eseguire
        result = self.ai_client.reason_and_execute(
            prompt=prompt, context=context, tools=self.tools, permissions=self.permissions
        )

        return {"type": "ai_result", "prompt": prompt, "result": result}

    def _execute_explore(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Explore a directory and read documents through the agentic loop.
        Payload: { prompt, path, context? }
        """
        prompt = payload.get("prompt")
        path = payload.get("path")
        context = payload.get("context", {})

        if not prompt:
            raise ValueError("No prompt provided for explore task")

        # Inject path into prompt if given
        if path:
            prompt = f"Path: {path}\n\nTask: {prompt}"
            context["working_path"] = path

        logger.info(f"Executing explore task: {shown(prompt)}")

        result = self.ai_client.reason_and_execute(
            prompt=prompt,
            context=context,
            tools=self.tools,
            permissions=self.permissions,
        )

        return {
            "type": "explore_result",
            "prompt": prompt,
            "result": result,
        }

    def _execute_workflow(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a multi-step workflow."""
        steps = payload.get("steps", [])

        if not steps:
            raise ValueError("No workflow steps provided")

        logger.info(f"Executing workflow with {len(steps)} steps")

        results = []
        context = {}

        for i, step in enumerate(steps):
            logger.info(f"Executing step {i+1}/{len(steps)}")

            # Each step is a task
            step_result = self.execute(step)
            results.append(step_result)

            # Passa il risultato come context al prossimo step
            context[f"step_{i}_result"] = step_result

            # Optionally let the step update the shared context
            if "context_update" in step:
                context.update(step["context_update"])

        return {"type": "workflow_result", "steps": len(steps), "results": results}
