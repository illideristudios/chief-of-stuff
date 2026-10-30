"""No work without an issue (design decision 3): work with no forge issue files one first, then writes its one store row.

A workspace with no `Backlog:` line has no forge to file in, so its work is refused and the setup is offered. The forge and the
store arrive as arguments (forge.Forge and store.Store in the engine), so this rule names neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import decision_actions
import worker_health

SETUP = ("Name the forge with a `Backlog:` line under `## Coordinator` in the workspace's CLAUDE.md, one of:\n"
         "- Backlog: GitHub issues; repo OWNER/NAME\n"
         "- Backlog: GitLab; host https://HOST; project GROUP/NAME; token env CHIEF_OF_STUFF_GITLAB_TOKEN\n"
         "A workspace with no `## Coordinator` block yet: chief-of-stuff init --root . --user NAME --timezone ZONE "
         "--github-repo OWNER/NAME (or --gitlab-host https://HOST --gitlab-project GROUP/NAME)")


@dataclass(frozen=True)
class Started:
    issue_key: str = ""   # the store row the work is under; "" when it did not start
    preview: str = ""     # what a commit would file
    refused: str = ""     # why the work did not start
    setup: str = ""       # the setup offered with a refusal


def start(forge: Any, store: Any, title: str, body: str = "", issue: int | None = None, commit: bool = False,
          ask_id: int | None = None, answer: str = "", recovery: bool = False) -> Started:
    """`forge` is None when the workspace has no `Backlog:` line. With no `issue`, one is filed first; a supplied one must exist on
    the forge and be open. The row takes the forge's title and follows the issue; nothing is written unless `commit`."""
    if forge is None:
        return Started(refused="no `Backlog:` line, so no issue can be filed and no work starts", setup=SETUP)
    if recovery and (issue is None or ask_id is not None):
        return Started(refused="recovery requires an existing --issue and cannot answer an ask")
    if ask_id is not None and (issue is None or ask_id <= 0 or not answer.strip()):
        return Started(refused="answering an ask requires an existing --issue, a positive --answer-ask and a nonempty --answer")
    if issue is None:
        made = forge.create_issue(title, body=body, commit=commit)
        if made.error or (made.done and not made.iid):
            return Started(refused=f"issue not filed: {made.error or 'the forge named no issue number'}")
        if not made.done:
            return Started(preview=f"would create: {title}")
        issue = made.iid
    else:
        found = forge.issue(issue)  # a supplied number is work only once the forge confirms the issue
        if not found.ok:
            return Started(refused=f"issue #{issue} not confirmed by the forge: {found.error}")
        if found.issue.state == "closed":
            return Started(refused=f"issue #{issue} is closed; reopen it before starting work")
        title = found.issue.title
        if not commit:
            return Started(preview=f"would recover: {forge.key(issue)} (acknowledge previous failures; preserve attempts and work)" if recovery
                           else f"would answer ask {ask_id} on {forge.key(issue)}: {answer}" if ask_id is not None
                           else f"would start: {forge.key(issue)} {title}")
    key = forge.key(issue)
    if recovery:
        try:
            recover(forge, store, key)
        except (ValueError, RuntimeError) as exc:
            return Started(refused=str(exc))
    if ask_id is not None:
        try:
            store.answer_ask(key, ask_id, answer)
        except (KeyError, ValueError) as exc:
            return Started(refused=str(exc))
    store.upsert_item(key, title=title)
    return Started(issue_key=key)


def recover(forge: Any, store: Any, key: str) -> None:
    """Explicit operator recovery acknowledges existing failures; history and work are never reset.

    The forge hold is released first. A crash before acknowledgement leaves the failure
    gate in force, so it cannot accidentally authorize another attempt.
    """
    item = store.item(key)
    if item is None or item.forge_state != "open":
        raise ValueError("recovery requires an open stored issue")
    history = store.segments(key)
    if any(s.ended_at is None for s in history):
        raise ValueError("cannot recover while a worker attempt is still running")
    data = store.resume(key) or {}
    if not data.get("park") and (data.get("interrupt") or {}).get("kind") != "exhausted":
        raise ValueError("issue is not parked or exhausted")
    if getattr(forge, "kanban", None) is not None:
        result = forge.write_stage(int(key.rpartition("#")[2]), item.stage, expected=item.stage,
                                   commit=True, release_hold=True)
        if not result.ok:
            raise RuntimeError(f"recovery hold release failed: {result.error}")
    with store.transaction():
        # A concurrent engine may have claimed a worker while the forge was being contacted.
        # Refuse under the same write transaction that acknowledges the failure budget.
        if any(s.ended_at is None for s in store.segments(key)):
            raise ValueError("cannot recover while a worker attempt is still running")
        def acknowledge(d):
            d["failure_acknowledged"] = sorted(set(d.get("failure_acknowledged", ())) | {
                f"{s.stage}-{s.attempt}" for s in history if s.ended_at and s.outcome in worker_health.FAILURES})
            for name in ("park", "interrupt", "launcher_hold", "pending_hold"):
                d.pop(name, None)
        store.update_resume(key, acknowledge)
        store.set_human_review(key, False)
        intent = decision_actions.current_intent(store.decisions(key))
        if intent is not None and intent.status == "blocked" and intent.intent == "run":
            update = decision_actions.reroute(intent, intent.rerouted_to, answer_text="Explicit failure recovery",
                                              decided_by="owner").update
            store.add_decision(key, update.topic, update.question, update.answer, update.decided_by, update.record)
        store.add_log(key, "explicit recovery: previous failures acknowledged; attempts and work preserved", store.clock().isoformat())
