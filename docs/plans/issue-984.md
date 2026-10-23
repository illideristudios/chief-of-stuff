# Issue 984 — Flow bars are not tab stops

## Outcome

A Flow bar that only carries a label is not a keyboard tab stop. The number of tab stops in the Flow does not grow with the number of attempts. Hover text and the accessible name of each bar stay.

```
b := a Flow bar carries only a label (no action)
t := the bar is a tab stop (tabindex="0")
n := the number of bars on the page
k := the tab stops a keyboard user passes to leave the Flow

1.  b → ¬t                         [a control that does nothing is not a stop]
2.  t → k grows with n             [each stop costs one key press]
3.  observed: b ∧ t, and every tabindex="0" on the drago board is a gantt bar
4.  (3) contradicts (1)            [MT on 1: t → ¬b, but b]
∴   drop tabindex from the bars. Do not add a stop on the figure or the row: those do nothing on Enter either, so (1) forbids them too.
```

The chart stays one `<figure>` with its existing `aria-label`. Issue refs stay real links (`<a class="gantt-ref">`), one per row that has an issue, independent of how many attempts that row drew.

## Where it comes from

`scripts/gantt.py` `_bar` is the only `tabindex` in the repository. It emits:

```html
<span class="gantt-bar …" data-cat="…" tabindex="0" role="img" aria-label="…" title="…" style="…"></span>
```

`flow_chart.section` calls `gantt.render` once per window (24 hours, 7 days). `render_board` inserts that HTML after the Build columns and the decision forms. Legend swatches from `gantt.legend` already omit `tabindex`, `role`, and `aria-label`.

The row note is not the bar label. `flow_chart._standing` and `build_from_store` set one note per row (`merged HH:MM`, `needs input · cause`, `stage · clock`, `closed`). `_bar` sets the label to `category start–end · worker · kind`, and one row has one label per attempt. Below 520px, `flow_chart.css` hides `.gantt-note`. Copying bar text into the note is the wrong disjunct.

## Change

1. In `evals/test_gantt.py`, turn `test_a_bar_is_focusable_and_carries_its_detail` red: the bar element contains no `tabindex`, and it still has `data-cat`, `role="img"`, `aria-label`, and `title` with the same label text. Add one test that renders a row with one segment and a row with several, and asserts `tabindex` occurs zero times in both, while the bar counts differ. Assert `gantt.legend(...)` contains no `tabindex`.
2. Run `python3 -m unittest evals.test_gantt` and confirm that new assertion fails.
3. In `scripts/gantt.py` `_bar`, delete ` tabindex="0"`. Leave `role="img"`, `aria-label`, and `title`.
4. Delete `.gantt-bar:focus-visible{…}` from `BASE_CSS`. Nothing can focus the bar after step 3. That rule sets no `--gantt-*` colour token, so `DarkSchemeTest.test_the_board_sets_every_gantt_colour_for_dark` still matches.
5. Run `python3 -m unittest evals.test_gantt evals.test_flow_chart` and `python3 -m unittest evals.test_decision_page.DarkSchemeTest.test_the_board_sets_every_gantt_colour_for_dark`. Then `python3 -m unittest discover -s evals -p 'test_*.py'`.

No edit to `flow_chart.py` or `render_board.py`. Both windows and every board that embeds the chart pick up the change because they all call `_bar`.

## Checks

- A rendered chart with N in-window segments has zero `tabindex` attributes, for N = 1 and N > 1.
- The bar still exposes the same `aria-label` and `title` the weekday tests already lock (`BarWeekdayTest`).
- `gantt.legend` output has no `tabindex`.
- When a board is being served, `curl -s http://127.0.0.1:8790/ | grep -c 'tabindex="0"'` is 0, and the same for 8788 and 8789. That curl is a live check, not a fixture: the counts depend on whoever is serving.

## Risks

- Keyboard users no longer hear each attempt while tabbing. Pointer hover still shows `title`. A screen reader that walks images still meets `role="img"`. Stripping `role` and `aria-label` would delete the only per-attempt name; do not.
- Leaving `tabindex` on the `<figure>` or `.gantt-row` would add stops that do nothing on Enter. Do not.
- `test_a_bar_is_focusable_and_carries_its_detail` currently requires `tabindex="0"`. The suite stays red until that assertion flips. No other test mentions `tabindex`.
- Architecture does not specify bar focus. This change does not edit `architecture/`.

## Out of scope

One stop per chart or per row. Rewriting notes to repeat bar labels. Tabs, tiles, decision forms, and issue-ref links.
