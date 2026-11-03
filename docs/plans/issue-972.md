# Issue 972 — unlinked board tabs read as inactive

Dispatch, Since you last looked, Epics and Limits stay spans. They do not gain pages. A span with no route is greyed, its tooltip says `not available yet`, and its count stays as text in the muted colour. Board, Workers and Decisions stay links, including their coloured badges.

```
L(T) := T is a link to a page that exists
A(T) := T is drawn inactive (grey, tooltip "not available yet")
C(T) := T still shows its count when it has one
K(T) := that count is coloured as a call to action
U    := {Dispatch, Since you last looked, Epics, Limits}

1. ∀T∈U ¬L(T)                         [observed spans; /dispatch /limits /epics /since are 404]
2. ¬L(T) → A(T)                       [expected]
3. □C(T)                              [#224: an unlinked tab still shows its count]
4. ¬L(T) → ¬K(T)                      [a badge on a dead tab is not a call to action]
∴ ∀T∈U A(T) ∧ C(T) ∧ ¬K(T)            [MP on 1–2; 3 and 4 keep the count and drop the colour]
```

Building the four pages would make `L(T)` true and would also satisfy the issue, and it is a different product. #224 already allows these tabs to be unlinked. This issue is the inactive drawing.

## Change

One function draws the bar on every page: `tab_bar` in `scripts/fragment.py`. `TAB_CSS` in the same file is inlined by the board (`scripts/render_board.py`) and by `PAGE_CSS` / `HEAD` in `scripts/decision_page.py`, which the workers, issue, source and decision pages use. No other copy of the bar exists. Do not add routes in `scripts/pages.py`; the 404s stay.

In `tab_bar`, a tab whose route is empty (`TABS` already uses `""`) renders as:

```html
<span title="not available yet">Dispatch<span class="badge" title="157 ready to dispatch">157</span></span>
```

- The tooltip `not available yet` is on the outer span, hardcoded, not on the badge. Badge titles stay exactly as they are (`N ready to dispatch`, `N went needs input since …`, `milestones: …`). `EpicsBadgeTest` asserts the Epics badge title verbatim.
- Do not emit `data-tone` unless the tab has a route. A count on Dispatch, Since you last looked or Epics still renders. Workers and Decisions still get `hot` and `pending`.
- Do not use `aria-disabled`. The tab-bar contract already rejects it on an `<a>` without an `href`, and these tabs stay spans with no `<a>`.
- Leave `aria-current` / `tab-parent` on whichever name the caller passes. No current caller passes an unbuilt name.

Append these two rules to `TAB_CSS`. The second one has to win the cascade: `nav.tabs .badge` is specificity (0, 2, 1) and paints the badge `var(--link, var(--brass))`, and `nav.tabs>[data-tone=ready] .badge` is (0, 3, 1) and paints it `var(--stage-review)`. `nav.tabs>span .badge` is only (0, 1, 2) and loses to the brass rule, so a weaker selector leaves Dispatch looking clickable.

```css
nav.tabs>span[title]{opacity:.55;cursor:default}
nav.tabs>span[title] .badge{color:var(--muted)}
```

`span[title] .badge` is (0, 3, 2), which beats both. Opacity greys the label as well as the number, so Limits and Epics (often no badge) still look quieter than a real link, which is already `color: var(--muted)` at full opacity. Keep `pointer-events` so the tooltip works. Leave the four `data-tone` colour rules in place for the linked tabs.

## Tests

TDD in `evals/test_tab_bar.py` only. This is the page renderer, so it is a unittest, not an `evals/cases` runtime case.

1. Add a failing test, then run it red, then edit `fragment.py`.
   - `fragment.tab_bar("Board", 2, workers=1, dispatch=3, since=(4, "09:00"), epics=(2, 1))`: Dispatch, Since you last looked, Epics and Limits are spans, `title` is exactly `not available yet`, they have no `data-tone`, no `href` and no child `<a>`. Dispatch's badge text is `3` and its title contains `ready`. Since's badge text is `4`. Epics' badge text is `2 at risk · 1 late` and its title is `milestones: 2 at risk · 1 late`. Limits has no badge. Board, Workers and Decisions do not have that title. Workers is `data-tone="hot"`. Decisions is `data-tone="pending"`.
   - `TAB_CSS` contains the two rules above, character for character.
   - The same title and span shape hold on every page `served()` already renders (board, decisions index, decision, task, source), and on the lead-count pages (board, task, source, workers) Dispatch's badge is still present with no `data-tone`.
2. Update `test_badge_colours_distinguish_attention_ready_pending_and_risk`. It currently requires Dispatch, Since you last looked and Epics to carry `data-tone`, which is the call-to-action this issue removes. Keep the assertion that the four tone rules exist in `TAB_CSS`, and that Workers and Decisions still wear distinct tones. Assert the three unbuilt counted tabs wear none.
3. Leave `test_built_tabs_link_and_unbuilt_tabs_do_not`, `test_counts_are_present_on_every_page_including_unbuilt_tabs` and `EpicsBadgeTest` green without weakening them. `evals/test_render_board.py` asserts the Decisions anchor string `<a href="/decisions" data-tone="pending">Decisions<span class="badge" title="1 pending">1</span></a>`; that string must not change.

Run:

```
python3 -m unittest evals.test_tab_bar evals.test_render_board
```

## Out of scope

- Pages or routes for `/dispatch`, `/since`, `/epics`, `/limits`.
- What the counts mean (#942) and a missing Decisions badge (#965).
- `architecture/ARCHITECTURE.md` and `architecture/compliance.md`.

## Risks

- A selector weaker than (0, 3, 2) loses to the brass badge rule, so the test locks the exact rule. Opacity `.55` is the grey; do not drop it to keep the number louder, or Limits looks like a link again.
- The badge's own `title` wins on hover over the number, so the count explanation still shows there. The unavailable tooltip shows on the label. Do not merge the two titles: Epics' badge title is an exact-match assertion.
- `data-tone` on an unbuilt span would still be muted by the new rule, and the test forbids emitting it anyway.

## Implementation file ownership

- write: `scripts/fragment.py`
- write: `evals/test_tab_bar.py`
- read: `scripts/render_board.py`
- read: `scripts/decision_page.py`
- read: `scripts/workers_page.py`
- read: `scripts/issue_page.py`
- read: `scripts/source_page.py`
- read: `evals/test_render_board.py`
