---
name: review-pipeline
description: Use before moving a task's stage, sending its pull request to the reviewer, handling a Reviewer pass report or an over budget line, running the kanban command, checking review state, or merging.
---

### Pipeline

Advance stages when their work and automated checks finish. No lane gate, plan approval, interactive launch yes or owner merge approval is required. Keep Tasks and the Log current using `chief-of-stuff log --stage`. Record an automatic action as the coordinator's action, never as invented owner words.

### Triage

Use the policy review panel. A configured reviewer session receives the request URL and head sha; otherwise launch reviewers under the policy without asking the owner how to review. Reviewers report findings and never fix. Read `review-report.md` for the report shape. In one-shot mode use scoped review and fix Tasks rows and fresh workers; include the request, findings and dispositions.

Burn findings down to none. Send concrete fixes to the owning worker immediately, including critical and security defects; no owner triage yes is needed. The architect disposes of findings when policy requires it. Architectural decisions go to the owner and the architect; when unsure whether a choice is architectural, ask the architect. Answer scheduling, priority and dispatch as coordinator; other non-architectural choices go to the engine's independent read-only decision trio. A split trio or exhausted trio retry waits on the owner. Use a sensible cheap-to-undo default on separable work and log the assumption.

After every fix round review the new head. Three fix rounds ending with findings still open park the issue, carrying the findings, research, options and a recommendation. Two worker failures across the issue also park it; the first retry goes to the next agent in the rotation. A relaunch after a stall counts as a failure.

A verify pass with nothing open moves the task to `merge`.

### Kanban

After recording a permitted stage transition in the tracker, sync its one issue with `chief-of-stuff kanban --root <workspace> --date <today> --issue <issue> --from-stage "<previous stage>" --commit`. For a new issue, preview and use `--initialize`; a `workflow` row has no issue and takes no sync. Configured legacy hold stages do not create owner waits. An explicit problem hold still preserves its evidence; resolve the problem using Autonomy and continue independent work. Reconcile manual board changes without overwriting them. An audit reports drift and never moves cards.

### Merge

The engine owns merges, protected pushes and stage writes. Workers never bypass that guard. Run `chief-of-stuff merge-ready --root .` for head pipeline and unresolved-thread evidence; only its `ready` verdict permits readiness language, quoting its pipeline id and sha. `merge-approved` is a compatibility listing of the same evidence, with no owner approval requirement. Run the engine to execute its merge gate: it merges when review matches the current head, CI is green, and the architect approved that head where policy requires it. Legacy `merge_owner = "user"` and `"approval"` never restore owner waits. Verify main and its suite before marking code work done.

### Review threads

Run `chief-of-stuff review-threads --root .` when checking review state. Treat unresolved concrete findings as fix work, send them to the worker, and verify the new head. The coordinator verifies fixes against the reviewed head and resolves addressed threads through the forge; no owner yes is added between fixes and merge. Workers report their fixes and leave resolution to the coordinator.

### Budgets

Poll an over-budget task to learn its state and preserve its work. Poll that task's session and tell the owner; never stop or kill it just for exceeding a time budget. Recover a stall through the next worker and count the failure; park only under Autonomy. Report progress and problems while continuing other work. To poll it, Read ../coordinator-sessions/SKILL.md next to this file first.
