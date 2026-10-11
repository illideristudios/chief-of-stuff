"""The decision trio (design 5a): a Use Cases module with the model and store passed in."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from model_port import Failure, Model, Reply, Request
from policy import Persona, Policy, PolicyError


@dataclass(frozen=True)
class Decision:
    issue_key: str
    topic: str
    question: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outcome:
    recommendation: str | None
    unanimous: bool

    @property
    def escalated(self) -> bool:
        return not self.unanimous


def select(policy: Policy, personas: Mapping[str, Persona], tags: Sequence[str]) -> tuple[Persona, ...]:
    """The first matching decision tag selects a trio; no match uses the resolved policy's default."""
    names = next((policy.trios[tag] for tag in tags if tag in policy.trios), policy.trio)
    if (len(names) != 3 or not all(isinstance(name, str) and name in personas for name in names)
            or len(set(names)) != 3):
        raise PolicyError("trio must be three distinct known personas")
    return tuple(personas[name] for name in names)


def _request(decision: dict, persona: Persona) -> Request:
    prompt = (f"You are {persona.name}: {persona.role}.\nRules: {json.dumps(persona.rules)}\n"
              "Review this decision independently. Vote adopt only if you recommend adopting it without concerns; "
              "otherwise vote concern. State your exact recommendation and reasoning as non-empty strings.\n"
              f"Decision: {json.dumps(decision)}")
    return Request(prompt, model=persona.model, effort=persona.effort or "", schema={
        "type": "object",
        "properties": {
            "vote": {"type": "string", "enum": ["adopt", "concern"]},
            "recommendation": {"type": "string", "minLength": 1},
            "reasoning": {"type": "string", "minLength": 1},
        },
        "required": ["vote", "recommendation", "reasoning"],
        "additionalProperties": False,
    })


def _verdict(persona: Persona, got: Reply | Failure) -> dict:
    if isinstance(got, Failure):
        reasoning = f"model failure ({got.kind}): {got.message}"
    else:
        data = got.data if isinstance(got, Reply) else None
        if (isinstance(data, dict) and set(data) == {"vote", "recommendation", "reasoning"}
                and all(isinstance(value, str) and value.strip() for value in data.values())
                and data["vote"] in ("adopt", "concern")):
            return {"persona": persona.name, **data}
        reasoning = "malformed verdict: expected adopt or concern with non-empty recommendation and reasoning"
    return {"persona": persona.name, "vote": "concern", "recommendation": None, "reasoning": reasoning}


def consult(decision: Decision, policy: Policy, personas: Mapping[str, Persona], model: Model, store: Any) -> Outcome:
    """Consult all three under a resolved policy, then record adoption or escalation on the existing issue."""
    trio = select(policy, personas, decision.tags)
    described = {**asdict(decision), "tags": list(decision.tags)}
    verdicts = [_verdict(persona, model(_request(described, persona))) for persona in trio]
    unanimous = (all(v["vote"] == "adopt" for v in verdicts)
                 and len({v["recommendation"] for v in verdicts}) == 1)
    recommendation = verdicts[0]["recommendation"] if unanimous else None
    store.add_decision(decision.issue_key, topic=decision.topic, question=decision.question,
                       answer=recommendation, decided_by="trio" if unanimous else "zach",
                       record={"decision": described, "verdicts": verdicts,
                               "recommendation": recommendation, "unanimous": unanimous})
    return Outcome(recommendation, unanimous)
