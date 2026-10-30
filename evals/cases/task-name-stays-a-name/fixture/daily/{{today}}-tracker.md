# Tracker {{today}}

Coordinator: gauntlet-85. Board: board-known.

## Resume

- As of: 09:40 - gauntlet-85 [111111]. THE AUDIT REPLIED AT 09:31 and its finding is that the upload endpoint accepts a content-length it never checks, which is the third time a limit has gone unchecked on that path this week. The ingest path was measured at 09:12 and again at 09:35 and the second reading was slower, though the sample is one run each so the difference means nothing yet and should not be reported as a regression until it is measured properly. Robin has been asked twice about the release notes owner and has not answered either time, the second ask at 09:20 and the first at 08:05.
- In flight: nothing; the audit poll is out
- Next: read the audit reply, then decide the release notes owner
- Waiting on: Robin - the release notes owner
- Re-arm: the 30-minute check
- Verified 09:40: main 7daf71f, 449 OK on it at 09:38, board republished 09:39

## Tasks

| name | item | owner | state | since | due | size | checklist |
|---|---|---|---|---|---|---|---|
| Upload audit | Security audit of the upload endpoint | 4821-audit | running 09:30 | 09:00 |  | M | Checklist: Security audit of the upload endpoint |
| Release notes | Draft release notes for the 0.4 cut | Robin | open | 09:00 |  | S | Checklist: Draft release notes |
| Ingest load test | Load test the ingest path | unassigned | open | 09:00 |  | M | Checklist: Load test the ingest path |
| Credential rotation | Rotate the staging credentials | unassigned | open | 09:00 |  | S | Checklist: Rotate the staging credentials |

## Decisions

| time | item | Robin's words |
|---|---|---|

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| 4821 | 4821-audit | working | the upload audit | | 11:00 | | none | 09:31 |

## Log

- 09:00 opened the day
- 09:05 upload audit task opened
- 09:10 release notes assigned to Robin
- 09:30 upload audit running
