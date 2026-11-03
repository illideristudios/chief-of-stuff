---
name: architect-signals
description: Dispatch issue-filing workers for unassigned compliance gaps; never edit compliance.md.
---

# Architect signals

The architect writes `unassigned` / `open` compliance rows; only the architect edits them.

## Intake

On Resume and on each `[Scheduled check]`, after the audit and before ordinary one-shot dispatch:

1. Run `chief-of-stuff architect-signals --root . pending`.
2. For each pending id, spawn one short-lived `issue-filing` one-shot in its own launcher call with `run_in_background: true`. Assign the gap id, absolute coordinator root (shared source and ledger, not the worker tree), and commit the row was written against. If no commit is named, use `git log -1 --format=%H -- architecture/compliance.md` at that root.
3. The worker uses `architect-signals --root <coordinator-root> file <gap-id> --against <sha>`. Do not file issues yourself or ask the owner to approve filing. Never bypass an unconfirmed create.

## Ledger

Pending excludes gaps acted on in `.chief-of-stuff/architect-signals.json`. You may read it; you never edit `architecture/compliance.md` to mark a row assigned. Filing and recovery details: `chief-of-stuff architect-signals file --help`.

## Mismatch reports

When you or a worker see decision/code drift, tell the standing `frank-lloyd-aight` architect so it grades.
