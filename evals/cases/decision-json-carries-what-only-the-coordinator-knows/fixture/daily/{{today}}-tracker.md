# Tracker {{today}}

## Tasks

| name | item | owner | state | since | due | size | lane | stage | issue | checklist |
|---|---|---|---|---|---|---|---|---|---|---|
| Cache warmup | Keep a warm pool of cache connections so the first request after a deploy is fast. PR https://github.com/o/app/pull/58 | impl-cache | running 06:00 | 06:00 |  | M | build | review | #110 | Checklist: Cache warmup |
| Connection budget | Cap each service's database connections as docs/architecture.md §4.2 designs. | unassigned | open |  |  | M | build | triage | #111 |  |

## Sessions

| ref | name | state | doing | waiting on | free at | constraints | children | last reply |
|---|---|---|---|---|---|---|---|---|
| d4e5f6 | impl-cache | busy | cache warmup | none | unknown | none | none | 08:40 |

## Decisions

| time | item | Robin's words |
|---|---|---|
| 06:00 | Cache warmup | "yes, dispatch it" |

## Log

- 06:00 opened the day
- 06:00 Cache warmup dispatched to impl-cache
- 07:10 stage: Cache warmup → pr
- 08:05 stage: Cache warmup → review
- 08:40 stage: Connection budget → triage

## Resume

- As of: 08:40 — coordinator
- In flight: nothing
- Next: wait for impl-cache
- Waiting on: nothing
- Re-arm: none
- Verified 08:40: main not read this move
