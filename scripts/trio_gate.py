"""The decision trio (design 5a): a Use Cases module with the model and store passed in."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from decision_actions import DecisionUpdate, exhaust_retry, identity, preserve_ask_link, retryable_failure
from model_port import Failure, Model, Reply, Request
from policy import Persona, Policy, PolicyError

TIMEOUT_S = 60


@dataclass(frozen=True)
class Decision:
    issue_key: str
    topic: str
    question: str
    options: tuple[str, ...]
    tags: tuple[str, ...] = ()
    context: str = ""
    instance: str = ""
    stage: str = ""
    attempt: int = 0
    gate: bool = False

    def validate(self):
        if (len(self.options) < 2 or not all(isinstance(option, str) and option.strip() for option in self.options)
                or len(set(self.options)) != len(self.options)):
            raise ValueError("decision options must be at least two distinct non-empty strings")


@dataclass(frozen=True)
class Outcome:
    recommendation: str | None
    unanimous: bool
    reason: str = ""

    @property
    def escalated(self) -> bool:
        return not self.unanimous


def select(policy: Policy, personas: Mapping[str, Persona], tags: Sequence[str]) -> tuple[Persona, ...]:
    """The first matching decision tag selects a trio; no match uses the resolved policy's default."""
    names = next((policy.trios[tag] for tag in tags if tag in policy.trios), policy.trio)
    if (len(names) != 3 or not all(isinstance(name, str) and name in personas for name in names)
            or len(set(names)) != 3):
        raise PolicyError("trio must be three distinct known personas")
    if "architect" in names:
        raise PolicyError("architect cannot be a decision-trio member")
    return tuple(personas[name] for name in names)


def _request(decision: dict, persona: Persona) -> Request:
    prompt = (f"You are {persona.name}: {persona.role}.\nRules: {json.dumps(persona.rules)}\n"
              "Review this decision independently. Vote adopt only if you recommend adopting it without concerns; "
              "otherwise vote concern. Choose one of the options as your choice and give non-empty reasoning.\n"
              f"Options: {json.dumps(decision['options'])}\n"
              f"Decision: {json.dumps(decision)}")
    return Request(prompt, model=persona.model, effort=persona.effort or "", timeout_s=TIMEOUT_S, schema={
        "type": "object",
        "properties": {
            "vote": {"type": "string", "enum": ["adopt", "concern"]},
            "choice": {"type": "string", "enum": decision["options"]},
            "reasoning": {"type": "string", "minLength": 1},
        },
        "required": ["vote", "choice", "reasoning"],
        "additionalProperties": False,
    })


def _verdict(persona: Persona, got: Reply | Failure, options: tuple[str, ...]) -> dict:
    if isinstance(got, Failure):
        reasoning = f"model failure ({got.kind}): {got.message}"
    else:
        data = got.data if isinstance(got, Reply) else None
        if (isinstance(data, dict) and set(data) == {"vote", "choice", "reasoning"}
                and all(isinstance(value, str) and value.strip() for value in data.values())
                and data["vote"] in ("adopt", "concern") and data["choice"] in options):
            return {"persona": persona.name, **data}
        reasoning = "malformed verdict: expected adopt or concern with a choice from the options and non-empty reasoning"
    return {"persona": persona.name, "vote": "concern", "choice": None, "reasoning": reasoning}


def record(store: Any, key: str, update: DecisionUpdate) -> None:
    """One writer for a trio result, including an unavailable consultation."""
    store.add_decision(key, update.topic, update.question, update.answer, update.decided_by, update.record)


def consult(decision: Decision, policy: Policy, personas: Mapping[str, Persona], model: Model, store: Any,
            failures: int = 0) -> Outcome:
    """Consult all three under a resolved policy, then record adoption or escalation on the existing issue.

    `failures` is the count of earlier outages; a retryable result carries the new count, and the cap, in its row."""
    decision.validate()
    trio = select(policy, personas, decision.tags)
    described = {**asdict(decision), "options": list(decision.options), "tags": list(decision.tags)}
    verdicts = [_verdict(persona, model(_request(described, persona)), decision.options) for persona in trio]
    unanimous = (all(v["vote"] == "adopt" for v in verdicts)
                 and len({v["choice"] for v in verdicts}) == 1)
    recommendation = verdicts[0]["choice"] if unanimous else None
    hold = unanimous and recommendation == "hold" and decision.gate
    update = preserve_ask_link(store.decisions(decision.issue_key), DecisionUpdate(
        decision.topic, decision.question, recommendation if not hold else None,
        "trio" if unanimous and not hold else "zach",
        {"decision": described, "verdicts": verdicts,
         "recommendation": recommendation, "unanimous": unanimous}))
    if retryable_failure(update.record):
        update = DecisionUpdate(update.topic, update.question, update.answer, "",
                                {**update.record, "failures": failures + 1})
        update = exhaust_retry(update, failures + 1) or update
    record(store, decision.issue_key, update)
    return Outcome(update.answer, unanimous and not hold,
                   "" if unanimous and not hold else str(update.record.get("error") or decision.question))


def _settled(decision: Decision, earlier: Sequence[Any]) -> Outcome | None:
    latest = next((row for row in reversed(earlier)
                   if identity(row) == (decision.topic, decision.question, decision.instance)), None)
    if latest is None:
        return None
    record = latest.record if isinstance(getattr(latest, "record", None), dict) else {}
    if record.get("reversed") or record.get("restarted"):
        return None
    prior = record.get("decision") if isinstance(record.get("decision"), dict) else None
    if prior is not None:
        if "options" in prior and tuple(prior["options"]) != decision.options:
            return None
        if ((prior.get("stage", ""), prior.get("attempt", 0)) != (decision.stage, decision.attempt)
                or prior.get("context", "") != decision.context):
            return None
    answer = latest.answer if isinstance(latest.answer, str) and latest.answer.strip() else None
    if answer is not None:
        return Outcome(answer, True) if answer in decision.options else None
    if retryable_failure(record):
        return None
    return Outcome(None, False, str(record.get("error") or decision.question))


def decide(decision: Decision, earlier: Sequence[Any], policy: Policy, personas: Mapping[str, Persona],
           model: Model | None, store: Any, failures: int = 0) -> Outcome:
    """Reuse a settled decision for these options and context, or consult the trio once."""
    try:
        decision.validate()
        settled = _settled(decision, earlier)
        if settled is not None:
            return settled
        if model is None:
            raise ValueError(decision.question)
        return consult(decision, policy, personas, model, store, failures)
    except (PolicyError, ValueError) as exc:
        described = {**asdict(decision), "options": list(decision.options), "tags": list(decision.tags)}
        update = preserve_ask_link(earlier, DecisionUpdate(
            decision.topic, decision.question, None, "zach",
            {"decision": described, "error": f"{decision.question}: {exc}", "unanimous": False}))
        prior = next((row for row in reversed(earlier) if identity(row) == identity(update)), None)
        if prior is None or prior.record != update.record:
            record(store, decision.issue_key, update)
        return Outcome(None, False, str(exc))
