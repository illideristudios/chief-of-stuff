"""No work without an issue (design decision 3): work with no forge issue files one first, then writes its one store row.

A workspace with no `Backlog:` line has no forge to file in, so its work is refused and the setup is offered. The forge and the
store arrive as arguments (forge.Forge and store.Store in the engine), so this rule names neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ownership

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
          ask_id: int | None = None, answer: str = "") -> Started:
    """`forge` is None when the workspace has no `Backlog:` line. With no `issue`, one is filed first; a supplied one must exist on
    the forge and be open. Filing requires a valid File ownership section; an existing undeclared issue waits in readiness.
    The row takes the forge's title and follows the issue; nothing is written unless `commit`."""
    if forge is None:
        return Started(refused="no `Backlog:` line, so no issue can be filed and no work starts", setup=SETUP)
    if ask_id is not None and (issue is None or ask_id <= 0 or not answer.strip()):
        return Started(refused="answering an ask requires an existing --issue, a positive --answer-ask and a nonempty --answer")
    if issue is None:
        declared = ownership.issue_scopes(body)
        if declared is None:
            return Started(refused="needs file ownership: add a valid ## File ownership section with write or read-only paths")
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
        body = found.issue.body
        declared = ownership.issue_scopes(body)
        if not commit:
            return Started(preview=f"would answer ask {ask_id} on {forge.key(issue)}: {answer}" if ask_id is not None
                           else f"would start: {forge.key(issue)} {title}")
    key = forge.key(issue)
    if ask_id is not None:
        try:
            store.answer_ask(key, ask_id, answer)
        except (KeyError, ValueError) as exc:
            return Started(refused=str(exc))
    store.upsert_item(key, title=title)
    # The store atomically retains the launch scope while any segment is open.
    store.replace_file_ownership(key, declared or [])
    return Started(issue_key=key)
