# Design

The approved concepts for the board pages. The board's UI is built to match these screens.

Open `index.html` through a local server (`python3 -m http.server --directory design`) to see every screen on one sheet. Each screen is also its own page, for example `flow-laptop.html` or `issue-107-held.html`. The pages need network access: they load React 18.3.1 from jsDelivr and Cormorant SC from Google Fonts.

| Screen | File |
|---|---|
| Flow | `flow-laptop.html` |
| Decisions page | `decisions-page-laptop.html` |
| Tabs (shared) | `tabs-shared.html`, component `Tabs.dc.html` |
| Decision page | `decision-page-laptop.html` |
| Task page | `task-page-laptop.html` |
| Palette, light and dark | `palette-light-and-dark.html` |
| Epics | `epics-laptop.html` |
| Epic drilldown | `epic-drilldown-laptop.html` |
| Source and diff, decision | `source-diff-decision-58.html` |
| Source and diff, task | `source-diff-task-48.html` |
| Worker session, in trouble | `worker-session-in-trouble.html` |
| Worker session, healthy | `worker-session-healthy.html` |
| Limits | `limits-laptop.html` |
| Since you last looked | `since-you-last-looked-laptop.html` |
| Dispatch | `dispatch-laptop.html` |
| Workers | `workers-laptop.html` |
| Issue in flight, held, orphaned, merged | `issue-105-in-flight.html`, `issue-107-held.html`, `issue-124-orphaned.html`, `issue-98-merged.html`, component `Issue.dc.html` |

The files were unpacked from a bundled Claude Design export. `lib/` holds its `dc-runtime` script and `fonts/` the font faces it inlined, each stored once under its content hash. The data on the screens is sample data.

```
S := a board page has a screen here          M := the page matches its screen
A := the screens here are the approved concepts
1. A               [Zach, 2026-10-23: the approved concepts]
2. A ∧ S → M       [the board is built to match the approved concepts]
∴ every board page with a screen here is built to match it
```
