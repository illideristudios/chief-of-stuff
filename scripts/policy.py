"""Policy and workflow as data (design decisions 12 and 13): personas, rooms, stages, workflow graphs, and the
effective policy for a persona at a stage in a room.

the owner, 2026-10-10: "everything is policy driven. We have default policies, and based on personas. This is all
configurable at a stage / room level." Every policy field starts from the base `[policy]` table and is overridden, in
order, by the persona's, the stage's and the room's own `policy` table, so a value set for one stage or room holds only
there. A field is replaced whole, never merged. This module reads no file: `settings.load_policy` reads the TOML.

A workspace may name an overlay (#827): a policy TOML whose keys are merged over the shipped policy's, by `overlay`.
It changes keys of tables the shipped policy has; it adds no persona, stage, room or workflow.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from math import isfinite
import warnings

from runtimes import EFFORTS

SEVERITIES = ("critical", "important", "minor")  # extras:code-review's, most severe first
LAUNCHES = ("adapter", "session")


class PolicyError(ValueError):
    """Policy data that is there and cannot be read as written."""


@dataclass(frozen=True)
class Approval:
    """A standing approval: a policy exception that waives `gate` until `expires`. There is no permanent one."""
    gate: str
    expires: datetime


@dataclass(frozen=True)
class Policy:
    blocking_severity: str = "minor"  # a finding at or above it blocks merge; minor is "burn down to no open findings"
    architect: bool = False  # the architect is consulted where this holds
    retry_limit: int = 3  # fix rounds before an unrecoverable-failure ping (the owner, 2026-10-11)
    launch: str = "adapter"  # a worker runs through the model adapter, or as a full Claude Code session
    trio: tuple[str, ...] = ()  # the decision trio when no topic tag maps to one (decision 5a)
    trios: dict[str, tuple[str, ...]] = field(default_factory=dict)  # decision topic tag -> three personas
    approvals: tuple[Approval, ...] = ()
    # No default limits: they are set in 1.1 from what 1.0 measures (the owner, 2026-10-10). None is no limit.
    spend_usd: float | None = None
    quota_percent: float | None = None
    time_minutes: float | None = None


def breach(resolved: Policy, usage) -> str | None:
    """The first configured limit that measured usage has reached. An unset limit, or usage not yet measured, is not a breach."""
    # shortcut: ping when usage reaches the limit, until a warn fraction is configured
    if not usage:
        return None
    for field, kind in (("spend_usd", "spend"), ("quota_percent", "quota"), ("time_minutes", "time")):
        limit, used = getattr(resolved, field), usage.get(field)
        if limit is not None and type(used) in (int, float) and used >= limit:
            return kind
    return None


@dataclass(frozen=True)
class Persona:
    """A reviewer or actor profile. With `agent` it is an agent: a full Claude Code agent that may use personas."""
    name: str
    role: str
    model: str
    effort: str | None = None
    rules: tuple[str, ...] = ()
    agent: str | None = None
    uses: tuple[str, ...] = ()
    policy: dict = field(default_factory=dict)

    @property
    def agent_type(self) -> str | None:
        return self.agent


@dataclass(frozen=True)
class Place:
    """A stage, or a room (a board section and a persona's space)."""
    name: str
    persona: str | None = None
    policy: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    gate: str | None = None


@dataclass(frozen=True)
class Workflow:
    """A directed graph of stages with a gate on any edge; an issue of one of `issue_types` runs it."""
    name: str
    start: str
    edges: tuple[Edge, ...] = ()
    issue_types: tuple[str, ...] = ()

    def next(self, stage: str) -> tuple[Edge, ...]:
        return tuple(e for e in self.edges if e.source == stage)


@dataclass(frozen=True)
class Policies:
    base: Policy
    personas: dict[str, Persona]
    stages: dict[str, Place]
    rooms: dict[str, Place]
    workflows: dict[str, Workflow]
    default_workflow: str

    def resolve(self, persona: str | None = None, stage: str | None = None, room: str | None = None) -> Policy:
        """The effective policy: the base, then the persona's, the stage's and the room's overrides."""
        layers = []
        for kind, table, name in (("persona", self.personas, persona), ("stage", self.stages, stage), ("room", self.rooms, room)):
            if name is not None and name not in table:
                raise PolicyError(f"no {kind} named {name!r}")
            layers.append(table[name].policy if name is not None else {})
        return replace(self.base, **{k: v for layer in layers for k, v in layer.items()})

    def workflow_for(self, issue_type: str | None) -> Workflow:
        return next((w for w in self.workflows.values() if issue_type in w.issue_types), self.workflows[self.default_workflow])


FIELDS = {f.name for f in fields(Policy)} | {"zach_gates"}  # legacy input only, never runtime policy


def _table(where: str, value, allowed: set[str] | None = None) -> dict:
    """`value` as a table of only `allowed` keys; None allows any key."""
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be a table")
    if allowed is not None and (extra := sorted(set(value) - allowed)):
        raise PolicyError(f"{where}: unknown key {', '.join(extra)}")
    return value


def _names(where: str, value) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise PolicyError(f"{where} must be a list of names")
    return tuple(value)


def _text(where: str, value, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{where} must be a non-empty string")
    return value


def _limit(where: str, value) -> float:
    if type(value) not in (int, float) or not isfinite(value) or value < 0:
        raise PolicyError(f"{where} must be a finite number of at least 0")
    return value


def _approval(where: str, value) -> Approval:
    t = _table(where, value, {"gate", "expires"})
    expires = t.get("expires")
    # A standing approval with no expiry is refused (decision 12), and so is one whose zone is not stated.
    if not isinstance(expires, datetime) or expires.tzinfo is None:
        raise PolicyError(f"{where}: expires must be a date and time with its offset, like 2026-10-12T00:00:00-07:00")
    return Approval(_text(f"{where} gate", t.get("gate")), expires)


def _trio(where: str, value, personas: set[str]) -> tuple[str, ...]:
    trio = _names(where, value)
    if len(trio) != 3 or len(set(trio)) != 3 or not set(trio) <= personas:
        raise PolicyError(f"{where} must be three distinct personas of {', '.join(sorted(personas))}")
    if "architect" in trio:
        raise PolicyError(f"{where}: architect cannot be a decision-trio member")
    return trio


def _policy(where: str, value, personas: set[str], gates: set[str]) -> dict:
    """The fields a policy table sets, checked and in the form `Policy` holds them."""
    t, out = _table(where, value, FIELDS), {}
    for key, v in t.items():
        at = f"{where} {key}"
        if key == "blocking_severity" and v not in SEVERITIES:
            raise PolicyError(f"{at} must be one of {', '.join(SEVERITIES)}")
        if key == "launch" and v not in LAUNCHES:
            raise PolicyError(f"{at} must be one of {', '.join(LAUNCHES)}")
        if key == "architect" and not isinstance(v, bool):
            raise PolicyError(f"{at} must be true or false")
        if key == "retry_limit" and (type(v) is not int or v < 0):
            raise PolicyError(f"{at} must be a whole number of at least 0")
        if key in ("spend_usd", "quota_percent", "time_minutes"):
            v = _limit(at, v)
        elif key == "zach_gates":
            warnings.warn(f"{at} is retired and ignored; workflow gates advance automatically", stacklevel=2)
            continue
        elif key == "trio":
            v = _trio(at, v, personas)
        elif key == "trios":
            v = {tag: _trio(f"{at} {tag}", x, personas) for tag, x in _table(at, v).items()}
        elif key == "approvals":
            if not isinstance(v, list):
                raise PolicyError(f"{at} must be a list of tables")
            v = tuple(_approval(f"{at}[{i}]", x) for i, x in enumerate(v))
        # Legacy gate names are inert and may outlive the workflow edges that named them.
        out[key] = v
    return out


def _persona(name: str, value, personas: set[str], gates: set[str]) -> Persona:
    where = f"[personas.{name}]"
    t = _table(where, value, {"role", "model", "effort", "rules", "agent", "agent_type", "uses", "policy"})
    effort = t.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise PolicyError(f"{where} effort must be one of {', '.join(EFFORTS)}")
    raw_agent = t.get("agent") if t.get("agent") is not None else t.get("agent_type")
    agent, uses = _text(f"{where} agent", raw_agent, required=False), _names(f"{where} uses", t.get("uses", []))
    if uses and not agent:
        raise PolicyError(f"{where} uses: only an agent uses personas")
    if unknown := sorted(set(uses) - (personas - {name})):
        raise PolicyError(f"{where} uses: {', '.join(unknown)} is no other persona")
    return Persona(name, _text(f"{where} role", t.get("role")), _text(f"{where} model", t.get("model")), effort,
                   _names(f"{where} rules", t.get("rules", [])), agent, uses,
                   _policy(f"[personas.{name}.policy]", t.get("policy", {}), personas, gates))


def _place(kind: str, name: str, value, personas: set[str], gates: set[str]) -> Place:
    where = f"[{kind}.{name}]"
    t = _table(where, value, {"persona", "policy"})
    persona = _text(f"{where} persona", t.get("persona"), required=False)
    if persona is not None and persona not in personas:
        raise PolicyError(f"{where} persona: {persona} is no persona")
    return Place(name, persona, _policy(f"[{kind}.{name}.policy]", t.get("policy", {}), personas, gates))


def _workflow(name: str, value, stages: set[str]) -> Workflow:
    where = f"[workflows.{name}]"
    t = _table(where, value, {"start", "edges", "issue_types"})
    start = _text(f"{where} start", t.get("start"))
    if not isinstance(t.get("edges", []), list):
        raise PolicyError(f"{where} edges must be a list of tables")
    edges = []
    for i, e in enumerate(t.get("edges", [])):
        at = f"{where} edges[{i}]"
        e = _table(at, e, {"from", "to", "gate"})
        edges.append(Edge(_text(f"{at} from", e.get("from")), _text(f"{at} to", e.get("to")),
                          _text(f"{at} gate", e.get("gate"), required=False)))
    named = {start} | {e.source for e in edges} | {e.target for e in edges}
    if unknown := sorted(named - stages):
        raise PolicyError(f"{where}: {', '.join(unknown)} is no stage")
    reached, todo = {start}, [start]
    while todo:
        stage = todo.pop()
        for e in (e for e in edges if e.source == stage):
            if e.target not in reached:
                reached.add(e.target)
                todo.append(e.target)
    if unreachable := sorted(named - reached):
        raise PolicyError(f"{where}: {', '.join(unreachable)} cannot be reached from {start}")
    if not reached - {e.source for e in edges}:
        raise PolicyError(f"{where} has no end: every stage has an edge out")
    return Workflow(name, start, tuple(edges), _names(f"{where} issue_types", t.get("issue_types", [])))


_NAMED = ("personas", "stages", "rooms", "workflows")


def overlay(shipped: dict, over: dict, where: str = "overlay") -> dict:
    """The shipped policy's tables with an overlay's keys merged over them (#827). A table merges key by key and a
    field is replaced whole; a section the shipped policy does not have is refused with its name. `parse` then
    refuses any unknown key, as it does in the shipped file."""
    merged = deepcopy(shipped)
    for section, value in over.items():
        if section == "default_workflow":
            merged[section] = value
            continue
        if section != "policy" and section not in _NAMED:
            raise PolicyError(f"{where}: unknown section [{section}]")
        _table(f"{where} [{section}]", value)
        if section == "policy":
            merged["policy"] = {**merged.get("policy", {}), **value}
            continue
        for name, table in value.items():
            entry = merged.get(section, {}).get(name)
            if entry is None:
                raise PolicyError(f"{where}: unknown section [{section}.{name}]")
            for key, field_value in _table(f"{where} [{section}.{name}]", table).items():
                if key == "policy" and section != "workflows":
                    entry["policy"] = {**entry.get("policy", {}), **_table(f"{where} [{section}.{name}.policy]", field_value)}
                else:
                    entry[key] = field_value
    return merged


def parse(data: dict) -> Policies:
    """Policies from the policy file's tables, refused whole on the first fault."""
    _table("the policy file", data, {"default_workflow", "policy", "personas", "stages", "rooms", "workflows"})
    tables = {key: _table(f"[{key}]", data.get(key, {})) for key in ("personas", "stages", "rooms", "workflows")}
    personas, stages = set(tables["personas"]), set(tables["stages"])
    workflows = {name: _workflow(name, t, stages) for name, t in tables["workflows"].items()}
    gates = {e.gate for w in workflows.values() for e in w.edges if e.gate}
    claimed = Counter(kind for w in workflows.values() for kind in w.issue_types)
    if twice := sorted(kind for kind, n in claimed.items() if n > 1):
        raise PolicyError(f"[workflows]: issue type {', '.join(twice)} selects more than one workflow")
    default = _text("default_workflow", data.get("default_workflow"))
    if default not in workflows:
        raise PolicyError("default_workflow must name one of the [workflows]")
    return Policies(
        base=replace(Policy(), **_policy("[policy]", data.get("policy", {}), personas, gates)),
        personas={n: _persona(n, t, personas, gates) for n, t in tables["personas"].items()},
        stages={n: _place("stages", n, t, personas, gates) for n, t in tables["stages"].items()},
        rooms={n: _place("rooms", n, t, personas, gates) for n, t in tables["rooms"].items()},
        workflows=workflows, default_workflow=default)
