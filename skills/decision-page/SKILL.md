---
name: decision-page
description: Load before writing a decision page for a genuine user decision or escalation, to use the Board JSON schema or backlog comment format with full context.
---

# Decision pages

When the block has a `Board:` line, the page is served from the pages dir: write the decision as JSON to `<pages dir>/decision-<slug>.json` with Write, not through the shell; the server renders it, and the ask carries its URL, `<board url>decisions/<slug>`, with no command to run (the user, 2026-09-25: "don't serve it as an artifact but serve it using the localhost server thing"). Never publish a decision page as an artifact, and never open it.

The page carries the canonical page theme, and none of the JSON does: the server reads it from `${CLAUDE_PLUGIN_ROOT}/assets/page-theme.css`, the vendored copy `scripts/sync_page_theme.py` keeps byte-identical to the pinned source. Never style a page from the JSON — no CSS, no colours of your own: the timeline's `kind`s and the node and edge `state`s are the theme's tokens, and a page that looks different reads as a different kind of document.

The JSON holds `headline`, `topic` (the area it is in, in a word or two), `ask`, `options` (`key`, `title`, `text`: what the choice costs), `recommended` (a key), `why`, `default` (what happens with no answer, and when), `timeline` (`when`, `text`, `kind`: the lane stage the step happened in, or `hot` for what went wrong): each step that led to the ask, from the Log, the messages and the forge; and for the background any of `sections` (`heading`, `text`: a list of paragraphs), `sides` (`label`, `status` open or done, `text`) and `references` (`kind`: issue, PR, MR, code, doc, note, pipeline or commit, `value`, `label`), with a link to each piece of evidence. When the ask concerns components, `architecture` draws them: `nodes` (`id`, `name`, `state` deployed, inflight, designed or external, `detail`: its file or fact, `hot` on what waits on this decision), `edges` (`from`, `to`, `label`, `state`, `hot`), `summary` and `not_drawn`. Leave out what the page reads for itself: when it was asked, the answer, and a reference's status come from the tracker and the forge. Without a `Board:` line, the page is a comment on the issue or pull request the decision is about (`chief-of-stuff backlog --comment N --body <page>`), or a new issue when nothing backs the decision (`chief-of-stuff backlog --create <the question> --body <page>`), with five labelled parts, in this order:

- `Decision:` the question, in one sentence.
- `Background:` what happened and what led here, with a link to each piece of evidence.
- `Options:` each choice and what it costs.
- `Recommendation:` yours, and why.
- `If no answer:` what happens by default, and when.

When carrying out an answer is refused, record the wait before re-rendering the board: `chief-of-stuff decision-page --root . --name <slug> --blocked "<what it waits on>"`, or a Log line `- HH:MM decision-<slug> blocked: <what it waits on>`. `--unblock` once the action runs.
