"""Reading and validating a policy document (layer L2).

Parsing is separated from the model on purpose: every rejection message in this
module names the offending key, because the operator fixing a policy is usually
not the person who wrote it, and "invalid policy" with no path is the error
message that gets worked around by disabling the perimeter.

Nothing here returns a partially valid policy. A document either satisfies the
schema in full or raises :class:`~runner.kernel.errors.PolicyError`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from runner.kernel.errors import PolicyError
from runner.kernel.types import SensitivityClass
from runner.policy.models import (
    ClassSpec,
    EgressPolicy,
    Policy,
    Rule,
    SealedSpec,
    SkillPolicy,
    Substrate,
    ToolPolicy,
)
from runner.policy.redaction import RedactionPolicy

__all__ = [
    "default_policy",
    "default_policy_document",
    "load_policy",
    "parse_policy",
    "write_default_policy",
]


def _require_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PolicyError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _require_sequence(value: Any, what: str) -> Sequence[Any]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise PolicyError(f"{what} must be a list, got {type(value).__name__}")
    return list(value)


def _parse_classes(raw: Mapping[str, Any]) -> dict[SensitivityClass, ClassSpec]:
    classes: dict[SensitivityClass, ClassSpec] = {}

    for name, body in raw.items():
        try:
            klass = SensitivityClass.parse(name)
        except ValueError as exc:
            raise PolicyError(str(exc)) from exc

        spec = _require_mapping(body, f"classes.{name}")
        patterns: list[re.Pattern[str]] = []
        for expr in _require_sequence(spec.get("patterns"), f"classes.{name}.patterns"):
            try:
                patterns.append(re.compile(str(expr)))
            except re.error as exc:
                raise PolicyError(
                    f"classes.{name}.patterns: invalid regex {expr!r}: {exc}"
                ) from exc

        classes[klass] = ClassSpec(
            paths=tuple(str(p) for p in _require_sequence(spec.get("paths"), "paths")),
            patterns=tuple(patterns),
            default=bool(spec.get("default", False)),
        )

    return classes


def _parse_substrates(raw: Sequence[Any]) -> tuple[Substrate, ...]:
    substrates: list[Substrate] = []
    seen: set[str] = set()

    for index, entry in enumerate(raw):
        body = _require_mapping(entry, f"substrates[{index}]")
        sid = str(body.get("id", "")).strip()
        if not sid:
            raise PolicyError(f"substrates[{index}] has no id")
        if sid in seen:
            raise PolicyError(f"substrates[{index}]: duplicate id {sid!r}")
        seen.add(sid)

        if "max_class" not in body:
            raise PolicyError(f"substrate {sid!r} does not declare max_class")
        try:
            max_class = SensitivityClass.parse(body["max_class"])
        except ValueError as exc:
            raise PolicyError(f"substrate {sid!r}: {exc}") from exc

        substrates.append(
            Substrate(
                id=sid,
                kind=str(body.get("kind", "openai-compatible")),
                max_class=max_class,
                jurisdiction=str(body.get("jurisdiction", "world")),
                endpoint=str(body.get("endpoint", "")),
                model=str(body.get("model", "")),
                api_key_env=str(body.get("api_key_env", "")),
                attestation=str(body.get("attestation", "")),
                tools=bool(body.get("tools", True)),
                vision=bool(body.get("vision", False)),
                context_window=int(body.get("context_window", 0)),
                cost_per_mtok=float(body.get("cost_per_mtok", 0.0)),
                quality=int(body.get("quality", 50)),
                probe=bool(body.get("probe", False)),
            )
        )

    if not substrates:
        raise PolicyError("a policy must declare at least one substrate")
    return tuple(substrates)


def _parse_rules(raw: Sequence[Any], known: set[str]) -> tuple[Rule, ...]:
    rules: list[Rule] = []

    for index, entry in enumerate(raw):
        body = _require_mapping(entry, f"rules[{index}]")
        match = _require_mapping(body.get("match"), f"rules[{index}].match")
        if "class" not in match:
            raise PolicyError(f"rules[{index}].match must select a class")
        try:
            klass = SensitivityClass.parse(match["class"])
        except ValueError as exc:
            raise PolicyError(f"rules[{index}]: {exc}") from exc

        allow = tuple(str(s) for s in _require_sequence(body.get("allow"), f"rules[{index}].allow"))
        unknown = [s for s in allow if s not in known]
        if unknown:
            raise PolicyError(f"rules[{index}] allows undeclared substrates: {', '.join(unknown)}")

        on_unavailable = str(body.get("on_unavailable", "hold"))
        if on_unavailable not in ("hold", "queue", "brief", "redact"):
            raise PolicyError(
                f"rules[{index}].on_unavailable must be hold, queue, brief or redact, "
                f"got {on_unavailable!r}"
            )

        prefer = str(body.get("prefer", "privacy"))
        if prefer not in ("privacy", "cost", "latency", "quality"):
            raise PolicyError(
                f"rules[{index}].prefer must be privacy, cost, latency or quality, got {prefer!r}"
            )

        rules.append(
            Rule(
                klass=klass,
                allow=allow,
                on_unavailable=on_unavailable,  # type: ignore[arg-type]
                prefer=prefer,  # type: ignore[arg-type]
                id=str(body.get("id") or f"rules[{index}]"),
            )
        )

    return tuple(rules)


def _parse_egress(raw: Mapping[str, Any], known: set[str]) -> EgressPolicy:
    brief = _require_mapping(raw.get("brief"), "egress.brief")
    produced_by = str(brief.get("produced_by", ""))
    if produced_by and produced_by not in known:
        raise PolicyError(f"egress.brief.produced_by names an undeclared substrate: {produced_by}")

    allowed_raw = brief.get("allowed_for", ["internal"])
    try:
        allowed = tuple(SensitivityClass.parse(v) for v in _require_sequence(allowed_raw, "x"))
    except ValueError as exc:
        raise PolicyError(f"egress.brief.allowed_for: {exc}") from exc

    if SensitivityClass.RESTRICTED in allowed:
        raise PolicyError(
            "egress.brief.allowed_for may not include 'restricted': a brief is an "
            "egress mechanism, and restricted material does not leave by any route"
        )

    redact = _require_mapping(raw.get("redact"), "egress.redact")
    try:
        redact_allowed = tuple(
            SensitivityClass.parse(v)
            for v in _require_sequence(redact.get("allowed_for", []), "egress.redact.allowed_for")
        )
    except ValueError as exc:
        raise PolicyError(f"egress.redact.allowed_for: {exc}") from exc

    sealed_raw = _require_mapping(raw.get("sealed"), "egress.sealed")
    try:
        sealed = SealedSpec(
            paths=tuple(str(p) for p in _require_sequence(sealed_raw.get("paths"), "x")),
            patterns=tuple(
                re.compile(str(p), re.IGNORECASE)
                for p in _require_sequence(sealed_raw.get("patterns"), "x")
            ),
        )
    except re.error as exc:
        raise PolicyError(f"egress.sealed.patterns: {exc}") from exc

    return EgressPolicy(
        brief_produced_by=produced_by,
        brief_max_tokens=int(brief.get("max_tokens", 512)),
        brief_must_clear=bool(brief.get("must_clear", True)),
        allowed_for=allowed,
        redact_allowed_for=redact_allowed,
        canaries=tuple(str(c) for c in _require_sequence(raw.get("canaries"), "egress.canaries")),
        sealed=sealed,
    )


def _parse_skills(raw: Any) -> SkillPolicy:
    """Parse the ``skills:`` allow-list.

    Default-deny, like tools: a skill that is not named here is not offered to
    the model, however many SKILL.md files are sitting on disk. Capabilities
    that appear because a file was copied into a directory are not capabilities
    anyone decided to have.
    """
    if raw is None:
        return SkillPolicy()
    if isinstance(raw, Mapping):
        raw = raw.get("allow")
    return SkillPolicy(allow=tuple(str(name) for name in _require_sequence(raw, "skills")))


def _parse_redaction(raw: Mapping[str, Any]) -> RedactionPolicy:
    """Parse the ``redaction:`` section.

    The label map is the interesting part: a detector speaks its own vocabulary
    — rizzo-pii has 22 Italian categories — and this is where an operator says
    what each of them means *here*. An unmapped label falls to
    ``default_label_class`` rather than to public, because a detector finding
    something it can name is evidence of sensitivity, not of its absence.
    """
    if not raw:
        return RedactionPolicy()

    provider = str(raw.get("provider", "") or "")
    labels: dict[str, SensitivityClass] = {}
    for label, value in _require_mapping(raw.get("labels"), "redaction.labels").items():
        try:
            labels[str(label).upper()] = SensitivityClass.parse(value)
        except ValueError as exc:
            raise PolicyError(f"redaction.labels.{label}: {exc}") from exc

    try:
        default_class = SensitivityClass.parse(raw.get("default_label_class", "internal"))
    except ValueError as exc:
        raise PolicyError(f"redaction.default_label_class: {exc}") from exc

    try:
        floor = SensitivityClass.parse(raw.get("floor", "public"))
    except ValueError as exc:
        raise PolicyError(f"redaction.floor: {exc}") from exc

    on_error = str(raw.get("on_error", "hold"))
    if on_error not in ("hold", "ignore"):
        raise PolicyError(
            f"redaction.on_error must be hold or ignore, got {on_error!r}. "
            "'hold' means a redactor outage stops the step; 'ignore' means the "
            "step proceeds on regex classification alone, which is a decision "
            "someone should make on purpose."
        )

    return RedactionPolicy(
        provider=provider,
        endpoint=str(raw.get("endpoint", "")),
        timeout=float(raw.get("timeout", 15.0)),
        labels=labels,
        default_label_class=default_class,
        classify=bool(raw.get("classify", False)),
        on_error=on_error,
        floor=floor,
    )


def _parse_tools(raw: Mapping[str, Any]) -> ToolPolicy:
    allow_raw = _require_mapping(raw.get("allow"), "tools.allow")
    allow = {
        str(tool): tuple(str(p) for p in _require_sequence(paths, f"tools.allow.{tool}"))
        for tool, paths in allow_raw.items()
    }
    return ToolPolicy(
        allow=allow,
        deny_paths=tuple(
            str(p) for p in _require_sequence(raw.get("deny_paths"), "tools.deny_paths")
        ),
    )


def parse_policy(document: Mapping[str, Any], *, source: str = "<memory>") -> Policy:
    """Validate a parsed YAML document into a :class:`Policy`.

    Every failure raises :class:`~runner.kernel.errors.PolicyError` with the
    path of the offending key, because the operator fixing it is usually not the
    person who wrote it.
    """
    if not isinstance(document, Mapping):
        raise PolicyError("a policy must be a mapping at the top level")

    default = str(document.get("default", "deny"))
    if default != "deny":
        raise PolicyError(
            "policy.default must be 'deny'. An allow-by-default perimeter is not a "
            "perimeter, and this project will not pretend otherwise."
        )

    classes = _parse_classes(_require_mapping(document.get("classes"), "classes"))
    if not classes:
        raise PolicyError("a policy must declare at least one class")

    substrates = _parse_substrates(_require_sequence(document.get("substrates"), "substrates"))
    known = {s.id for s in substrates}
    rules = _parse_rules(_require_sequence(document.get("rules"), "rules"), known)
    egress = _parse_egress(_require_mapping(document.get("egress"), "egress"), known)
    tools = _parse_tools(_require_mapping(document.get("tools"), "tools"))
    redaction = _parse_redaction(_require_mapping(document.get("redaction"), "redaction"))
    skills = _parse_skills(document.get("skills"))

    policy = Policy(
        version=int(document.get("version", 1)),
        classes=classes,
        substrates=substrates,
        rules=rules,
        egress=egress,
        tools=tools,
        redaction=redaction,
        skills=skills,
        source=source,
    )

    # A rule cannot ask for an action the deployment cannot perform. Discovering
    # that `on_unavailable: redact` was a no-op at the moment it mattered is the
    # kind of surprise this project exists to prevent.
    for rule in rules:
        if rule.on_unavailable == "redact" and not redaction.enabled:
            raise PolicyError(
                f"{rule.id} asks for redaction, but no redaction.provider is configured"
            )

    # A rule that allows a substrate which cannot legally hold its class is not a
    # typo the perimeter should quietly work around: it means the author believes
    # something false about where their material goes.
    for rule in rules:
        for sid in rule.allow:
            sub = policy.substrate(sid)
            if sub and not sub.can_hold(rule.klass):
                raise PolicyError(
                    f"{rule.id} allows '{sid}' for class {rule.klass.label}, but that "
                    f"substrate is capped at {sub.max_class.label}"
                )

    return policy


def load_policy(path: str | Path) -> Policy:
    """Read and validate a policy file.

    Raises:
        PolicyError: the file is missing, unreadable, not valid YAML, or does
            not satisfy the schema. Never returns a partially valid policy.
    """
    p = Path(path).expanduser()
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PolicyError(f"no policy at {p}") from exc
    except OSError as exc:
        raise PolicyError(f"cannot read policy at {p}: {exc}") from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{p} is not valid YAML: {exc}") from exc

    return parse_policy(document or {}, source=str(p))


_POLICY_HEADER = (
    "# Annona policy — what may run, where, and what may cross.\n"
    "# Every decision the perimeter takes is a function of this file.\n"
    "# Reference: https://github.com/akaion-ai/annona/blob/main/docs/design/hld.md\n"
    "#\n"
    "# Tools are default-deny: one that is not named below does not run. The\n"
    "# runner ships five — filesystem, shell, browser, document_reader,\n"
    "# explorer — and this file enables the three that only read. Add the\n"
    "# others deliberately, with paths, and know what you are doing:\n"
    "#\n"
    "#   tools:\n"
    "#     allow:\n"
    "#       shell:   []          # no path allow-list means the tool is refused;\n"
    "#                            # shell has no path argument, so enabling it is\n"
    "#                            # an all-or-nothing decision. Prefer not to.\n"
    "#       browser: []          # the browser reaches the network, which is an\n"
    "#                            # egress this policy cannot classify. Phase 2.\n"
    "#\n"
    "# To send material to a model outside this machine, add a substrate and a\n"
    "# rule that allows it. Nothing here does, on purpose.\n"
    "#\n"
    "# Two egress sections are absent and worth knowing about before you need\n"
    "# them (docs/reference/anonymisation.md):\n"
    "#\n"
    "#   egress:\n"
    "#     redact:\n"
    "#       allowed_for: [internal]     # replacing identifiers sends the document\n"
    "#                                   # ENTIRE minus the names. Off until named.\n"
    "#     sealed:\n"
    "#       paths:    ['~/Pratiche/M&A/**']\n"
    "#       patterns: ['Progetto\\s+\\w+', 'term sheet']\n"
    "#\n"
    "# A seal is not a class. Classes say where material may run and drop when\n"
    "# identifiers are removed; a seal survives every transformation, because\n"
    "# some secrets are the subject rather than the names in it.\n\n"
)


def default_policy_document(
    *,
    local_endpoint: str = "http://localhost:11434",
    local_model: str = "qwen2.5:14b",
) -> dict[str, Any]:
    """The policy ``annona init`` writes, as a document.

    It is deliberately usable and deliberately strict: everything under the home
    directory is internal, credentials and keys are unreadable by any tool, and
    nothing is registered that could send material outside the machine. Adding a
    remote substrate is an explicit act, performed by someone who then owns the
    consequence.
    """
    return {
        "version": 1,
        "default": "deny",
        "classes": {
            "restricted": {
                "paths": [
                    "~/.ssh/**",
                    "~/.gnupg/**",
                    "~/.aws/**",
                    "~/.config/gcloud/**",
                    # Medical imaging, wherever it is on the disk. A DICOM file
                    # carries a patient's name, date of birth and referring
                    # physician in its header; classifying it by the folder
                    # somebody happened to save it in would be a guess, and the
                    # wrong one the first time a study lands in ~/Downloads.
                    "/**/*.dcm",
                    "/**/*.dicom",
                ],
                "patterns": [
                    r"[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]",
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                    r"\bIT\d{2}[A-Z]\d{10}[0-9A-Z]{12}\b",
                    # DICOM header fields, as they appear in a read of one. Health
                    # data is restricted by law before it is restricted by policy.
                    r"\b(?:PatientName|PatientID|PatientBirthDate|StudyInstanceUID)\b",
                ],
            },
            # `default: true` marks the floor — the class of material nothing
            # recognised. It is internal, not public, because a regex cannot
            # recognise "my name is X, I am 27, write to Y": that text matches no
            # pattern and lives at no path, and a floor of public would make it
            # placeable on any substrate an operator ever adds for public. The
            # class of the unrecognised has to be the cautious one, or the
            # guarantee only covers the material you already knew about.
            "internal": {"paths": ["~/**"], "default": True},
            # Reachable only if you lower the floor deliberately. Left here so a
            # deployment that genuinely has public material can say so.
            "public": {},
        },
        "substrates": [
            {
                "id": "local-gpu",
                "kind": "ollama",
                "endpoint": local_endpoint,
                "model": local_model,
                "jurisdiction": "on-prem",
                "max_class": "restricted",
                "tools": True,
                "context_window": 32768,
                "cost_per_mtok": 0.0,
                "quality": 60,
                "probe": True,
            }
        ],
        "rules": [
            {"match": {"class": "restricted"}, "allow": ["local-gpu"], "on_unavailable": "hold"},
            {"match": {"class": "internal"}, "allow": ["local-gpu"], "on_unavailable": "hold"},
            {
                "match": {"class": "public"},
                "allow": ["local-gpu"],
                "on_unavailable": "hold",
                "prefer": "cost",
            },
        ],
        "egress": {"brief": {"produced_by": "local-gpu", "max_tokens": 512, "must_clear": True}},
        "tools": {
            # `~/Documents/Annona/Inbox` is where the desktop window puts an
            # attached file. It is listed explicitly, redundant though it is
            # under `~/Documents/**`, so that an operator who narrows these
            # paths can see what they are about to switch off.
            "allow": {
                "document_reader": [
                    "~/Documents/**",
                    "~/Downloads/**",
                    "~/Documents/Annona/Inbox/**",
                ],
                "explorer": ["~/Documents/**", "~/Downloads/**"],
                "filesystem": ["~/Documents/**", "~/Downloads/**"],
            },
            "deny_paths": [
                "~/.ssh/**",
                "~/.gnupg/**",
                "~/.aws/**",
                "~/.config/gcloud/**",
                "~/.annona/**",
                "~/.akaion/**",
                "**/.env",
                "**/.env.*",
                "**/id_rsa*",
                "**/*.pem",
            ],
        },
    }


def default_policy(
    *,
    local_endpoint: str = "http://localhost:11434",
    local_model: str = "qwen2.5:14b",
) -> Policy:
    """The shipped default policy, already validated."""
    return parse_policy(
        default_policy_document(local_endpoint=local_endpoint, local_model=local_model),
        source="<default>",
    )


def write_policy_document(path: str | Path, document: dict[str, Any]) -> Path:
    """Write any policy document to ``path``, with the header. Never overwrites.

    Split out of :func:`write_default_policy` so the profiles in
    ``runner.policy.profiles`` reach disk through the same function, and every
    policy this project writes carries the same explanation of what it is —
    including the ones that were chosen rather than defaulted into.
    """
    p = Path(path).expanduser()
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    p.write_text(_POLICY_HEADER + body, encoding="utf-8")
    return p


def write_default_policy(
    path: str | Path,
    *,
    local_endpoint: str = "http://localhost:11434",
    local_model: str = "qwen2.5:14b",
) -> Path:
    """Write the default policy to ``path``, creating parents. Never overwrites."""
    return write_policy_document(
        path,
        default_policy_document(local_endpoint=local_endpoint, local_model=local_model),
    )
