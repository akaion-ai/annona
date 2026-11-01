"""Adapters from the existing registry and permission manager to L0 ports.

The agent loop depends on :class:`~runner.kernel.ports.ToolExecutor` and
:class:`~runner.kernel.ports.PolicyGate`, not on the concrete
:class:`~runner.tools.registry.ToolRegistry` and
:class:`~runner.permissions.manager.PermissionManager`. These two thin adapters
are what make that true without rewriting either.

The indirection pays for itself immediately in Phase 1: the perimeter's
default-deny capability kernel becomes a different :class:`PolicyGate`
implementation, and nothing in the loop changes.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from runner.kernel.blocks import tool_spec_from_schema
from runner.kernel.logtext import shown
from runner.kernel.types import ToolCall, ToolResult, ToolSpec

__all__ = ["PermissionGate", "RegistryToolExecutor"]


class RegistryToolExecutor:
    """Runs tool calls against a :class:`ToolRegistry`.

    Total by construction: every call produces a :class:`ToolResult`, including
    for unknown tools and for tools that raise. The model always receives
    something it can reason about, and the run continues.
    """

    def __init__(self, registry: Any) -> None:
        """
        Args:
            registry: An object exposing ``get_all_schemas()`` and
                ``get_tool(name)``. ``None`` is accepted and yields no tools,
                which is how a plain chat run works.
        """
        self._registry = registry

    def specs(self) -> tuple[ToolSpec, ...]:
        if not self._registry:
            return ()
        return tuple(tool_spec_from_schema(s) for s in self._registry.get_all_schemas())

    def invoke(self, call: ToolCall) -> ToolResult:
        # The name and which arguments, not their values: a path or a query is
        # the step's material as much as the prompt is (#37).
        logger.info(
            f"Executing tool: {call.name}({', '.join(call.arguments)}) {shown(call.arguments)}"
        )

        if not self._registry:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content={"error": f"Tool not found: {call.name}"},
                is_error=True,
            )

        try:
            tool = self._registry.get_tool(call.name)
            content = tool.run(call.arguments)
        except Exception as exc:  # noqa: BLE001 - tools are arbitrary code
            # Deliberately broad: a tool is third-party code and may raise
            # anything. Turning that into a result the model can read is the
            # whole job of this adapter — see the port's docstring.
            logger.warning(f"Tool {call.name} failed: {exc}")
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content={"error": str(exc)},
                is_error=True,
            )

        return ToolResult(call_id=call.id, name=call.name, content=content, is_error=False)


class PermissionGate:
    """Asks a :class:`PermissionManager` whether a tool call may run.

    Phase 0 behaviour is unchanged, which means **allow by default**: an
    unrecognised tool name is permitted, and an empty allow-list permits
    everything in its category. That is a documented gap, not a design — see the
    gap table in the README and ``docs/design/sovereign-runtime.md`` invariant I1.
    """

    def __init__(self, permissions: Any) -> None:
        """
        Args:
            permissions: An object exposing ``check_tool_permission(name, args)``.
                ``None`` means no policy is enforced.
        """
        self._permissions = permissions

    @property
    def enforcing(self) -> bool:
        """Whether any policy is actually consulted."""
        return self._permissions is not None

    def permits(self, call: ToolCall) -> bool:
        if not self._permissions:
            return True
        allowed = bool(self._permissions.check_tool_permission(call.name, dict(call.arguments)))
        if not allowed:
            logger.warning(f"Policy denied tool call: {call.name}")
        return allowed
