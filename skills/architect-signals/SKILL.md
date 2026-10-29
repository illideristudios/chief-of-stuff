---
name: architect-signals
description: Turn unassigned architecture compliance gaps into forge issues through the issue-filing worker without editing compliance.md.
---

# Architect signals

The architect writes `unassigned` / `open` rows in `architecture/compliance.md`. Only the architect edits that file. You never edit it.

## Intake

On Resume and on each `[Scheduled check]`, after the audit and before ordinary one-shot dispatch:

1. Run `chief-of-stuff architect-signals --root . pending`.
2. Each printed gap id is a signal not yet in `.chief-of-stuff/architect-signals.json`.
3. For each pending id, spawn one short-lived `issue-filing` worker (one-shot) whose assignment names the gap id and the commit the compliance row was written against (`git log -1 --format=%H -- architecture/compliance.md` when the row has no other commit). Launch it the same way as any ready one-shot task: its own launcher call with `run_in_background: true`.
4. Do not file the forge issue yourself. Do not ask the owner to approve filing these.

## Ledger

The issue-filing worker records `.chief-of-stuff/architect-signals.json` after it files or finds an existing issue with the same `architect-signal:<id>@<sha>` key. You may read that ledger; you never edit `architecture/compliance.md` to mark a row assigned. A gap already in the ledger is not pending.

## Mismatch reports

When a worker or you see a decision and the code disagree, tell the architect (standing `frank-lloyd-aight` session) so it grades. You do not write compliance rows yourself.
