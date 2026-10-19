"""The decision trio (design 5a): a Use Cases module with the model and store passed in."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from decision_actions import DecisionUpdate, exhaust_retry, human_answer, identity, preserve_ask_link, retryable_failure
from model_port import Failure, Model, Reply, Request, draw
from policy import Persona, Policy, PolicyError

TIMEOUT_S = 300  # #928: Cursor composer took 222s on a one-line decision (Zach 2026-10-19: "we need to bump timeout")


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


def _request(decision: dict, persona: Persona, entry: Any = None, timeout_s: float = TIMEOUT_S) -> Request:
    prompt = (f"You are {persona.name}: {persona.role}.\nRules: {json.dumps(persona.rules)}\n"
              "Review this decision independently. Vote adopt only if you recommend adopting it without concerns; "
              "otherwise vote concern. Choose one of the options as your choice and give non-empty reasoning.\n"
              f"Options: {json.dumps(decision['options'])}\n"
              f"Decision: {json.dumps(decision)}")
    model, effort, runtime = ((entry.model, entry.effort or "", entry.runtime) if entry is not None
                              else (persona.model, persona.effort or "", ""))
    return Request(prompt, model=model, effort=effort, runtime=runtime, timeout_s=timeout_s, schema={
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


def record(store: Any, key: str, update: DecisionUpdate) -> Any | None:
    """The one writer of decision rows: a trio result, an unavailable consultation, a board intent and the engine's
    transitions. Human answer wins: a row a person did not write never lands over a person's answer, which is
    returned instead, the check and the append under one write lock."""
    won = []

    def person_first(rows: list) -> Any | None:
        won.append(human_answer(rows, update))
        return won[-1]

    store.add_decision(key, update.topic, update.question, update.answer, update.decided_by, update.record,
                       unless=person_first if update.decided_by != "user" else None)
    return won[-1] if won else None


def _members(model: Model, trio: Sequence[Persona]) -> tuple[list[Any], bool | None]:
    """Each member's `[models.trio]` entry (§6, #879): distinct entries, different runtimes first, reused in turn
    when fewer can launch. No rotation is the personas' own models (None entries, diversity unrecorded); a rotation
    with nothing launchable has no members."""
    rotation = tuple(getattr(model, "rotation", ()) or ())
    if not rotation:
        return [None] * len(trio), None
    chosen = draw(rotation, len(trio), launchable=getattr(model, "launchable", lambda runtime: True),
                  how=getattr(model, "pick", "first"))
    diverse = len(chosen) == len(trio) and len({entry.runtime for entry in chosen}) == len(trio)
    return [chosen[i % len(chosen)] for i in range(len(trio))] if chosen else [], diverse


def _ask(model: Model, persona: Persona, described: dict, entry: Any, stopped: list,
         others: Sequence[Any], remaining: Callable[[], float] | None = None) -> tuple[Any, Any, bool]:
    """One member's answer. A quota stop moves the member to the next entry, one no other member holds when there is
    one, so the trio stays diverse; any other model failure (a timeout, an unparsed answer) moves it once (#928), so
    an outage is not counted as a concern. Each call gets at most the pass budget's `remaining` seconds, and none
    starts once it is spent (#927); (answer, entry, exhausted)."""
    replaced = False
    while True:
        left = TIMEOUT_S if remaining is None else min(TIMEOUT_S, remaining())
        if left <= 0:
            return Failure("unknown", "the engine pass spent its trio budget; consulted again next pass"), entry, False
        got = model(_request(described, persona, entry, left))
        if entry is None or not isinstance(got, Failure) or (got.kind != "quota" and replaced):
            return got, entry, False
        stopped.append(entry)
        replaced = replaced or got.kind != "quota"
        drawn = dict(launchable=model.launchable, how=model.pick)
        following = (draw(model.rotation, 1, used=[*stopped, *others], **drawn)
                     or draw(model.rotation, 1, used=stopped, **drawn))
        if not following:
            return got, entry, got.kind == "quota"
        entry = following[0]


def consult(decision: Decision, policy: Policy, personas: Mapping[str, Persona], model: Model, store: Any,
            failures: int = 0, remaining: Callable[[], float] | None = None) -> Outcome:
    """Consult all three under a resolved policy, then record adoption or escalation on the existing issue.

    `failures` is the count of earlier outages; a retryable result carries the new count, and the cap, in its row."""
    decision.validate()
    trio = select(policy, personas, decision.tags)
    described = {**asdict(decision), "options": list(decision.options), "tags": list(decision.tags)}
    members, diverse = _members(model, trio)
    verdicts, stopped, exhausted = [], [], False
    for i, persona in enumerate(trio):
        entry = members[i] if members else None
        if not members:
            got = Failure("missing_program", "no [models.trio] entry can launch read-only on this host")
        else:
            got, entry, out = _ask(model, persona, described, entry, stopped, members[:i] + members[i + 1:],
                                     remaining)
            members[i], exhausted = entry, exhausted or out
        # Each vote names what cast it, so trio accuracy can be compared per model (§6, section 9).
        verdicts.append({**_verdict(persona, got, decision.options),
                         "runtime": entry.runtime if entry is not None else "claude",
                         "model": entry.model if entry is not None else persona.model})
    unanimous = (all(v["vote"] == "adopt" for v in verdicts)
                 and len({v["choice"] for v in verdicts}) == 1)
    recommendation = verdicts[0]["choice"] if unanimous else None
    hold = unanimous and recommendation == "hold" and decision.gate
    update = preserve_ask_link(store.decisions(decision.issue_key), DecisionUpdate(
        decision.topic, decision.question, recommendation if not hold else None,
        "trio" if unanimous and not hold else "zach",
        {"decision": described, "verdicts": verdicts, "recommendation": recommendation, "unanimous": unanimous,
         **({"diverse": diverse} if diverse is not None else {})}))
    if exhausted and retryable_failure(update.record):
        # Every entry a member could move to stopped on quota: the owner decides, as for an exhausted retry (§6).
        update = DecisionUpdate(update.topic, update.question, None, "zach", {
            **update.record, "retry_exhausted": failures + 1,
            "error": f"trio unavailable: every [models.trio] entry stopped on quota: {decision.question}"})
    elif retryable_failure(update.record):
        update = DecisionUpdate(update.topic, update.question, update.answer, "",
                                {**update.record, "failures": failures + 1})
        update = exhaust_retry(update, failures + 1) or update
    human = record(store, decision.issue_key, update)
    if human is not None:  # a person answered while the trio was consulting
        return Outcome(human.answer, True)
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
           model: Model | None, store: Any, failures: int = 0,
           remaining: Callable[[], float] | None = None) -> Outcome:
    """Reuse a settled decision for these options and context, or consult the trio once."""
    try:
        decision.validate()
        settled = _settled(decision, earlier)
        if settled is not None:
            return settled
        if model is None:
            raise ValueError(decision.question)
        return consult(decision, policy, personas, model, store, failures, remaining)
    except (PolicyError, ValueError) as exc:
        described = {**asdict(decision), "options": list(decision.options), "tags": list(decision.tags)}
        update = preserve_ask_link(earlier, DecisionUpdate(
            decision.topic, decision.question, None, "zach",
            {"decision": described, "error": f"{decision.question}: {exc}", "unanimous": False}))
        prior = next((row for row in reversed(earlier) if identity(row) == identity(update)), None)
        if prior is None or prior.record != update.record:
            record(store, decision.issue_key, update)
        return Outcome(None, False, str(exc))
