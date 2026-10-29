# Reliable autonomous workers

Provide autonomous jobs with durable progress, a usable Workers UI, transcripts, and direct messages. The first milestone proves one job end to end on one runtime before expanding to others.

Work is tracked in [the autonomous workers rework](https://github.com/illideristudios/chief-of-stuff/issues/1159) and its linked issues.

## Experience

- The Workers overview shows running, queued, attention, and ended jobs; a job opens to its attempt history and transcript.
- A person can message a running worker and see whether the message is queued, delivered, or failed. A finished session cannot receive a message.
- A suspected stall or repeated failure shows the evidence and the available recovery actions. Stopping or restarting preserves the prior transcript and worktree.

## Reliability

- Persist job and attempt state and reconcile actual process identities after a restart before launching replacements.
- Measure activity from worker events as well as process state; silence alone is a suspicion, not proof of failure.
- Configure the stall policy per workspace:

```toml
[workers]
failure_limit = 2

[workers.stall]
after_minutes = 3
action = "wait" # "wait" or "restart"
max_restarts = 2
```

- The default reports a suspected stall and leaves the worker running. An automatic restart is opt-in, bounded, and verifies the original has stopped before launching another attempt.
- Failures count per issue across stages and agents. At `failure_limit` the engine parks the issue with the attempts, reports, research, options and a recommendation. Merely surfacing a suspected stall spends no failure budget; a stall counts only after a replacement worker actually starts.

The engine reads T3 thread status, update time and pending requests using the launch's saved CLI and environment. For local launches it uses process identities, repository progress and transcript activity. Silence is a suspected stall. Pending requests wait for an answer before the silence threshold expires; architectural decisions and split trios keep waiting. Failed T3 reads remain unknown, even if the local polling process died.

Local automatic recovery records its stop intent before acting and verifies that the previous host, runner and session processes ended before releasing the attempt. The next launch uses the existing worktree and runtime rotation. Attempts, transcripts, restart counts and pending-request age survive engine restarts in the existing store and run directories.

T3 cancellation remains an open item for the owner: the CLI has no stop or interrupt command. A T3 launch configured with `action = "restart"` waits and surfaces `T3 cannot cancel a thread`. No T3 replacement is launched by stall recovery.

After investigating a parked issue, explicitly acknowledge its existing failures and permit another attempt with `chief-of-stuff work "Retry after investigation" --issue NUMBER --recover --commit --root /path/to/workspace`. Omit `--commit` to preview. This refuses a live worker, retains all attempts and work, and logs the acknowledgement. Raising `[workers] failure_limit` also permits another attempt, subject to any existing hold or decision.

## Delivery

1. Capture real examples of stalls, repeated failures, and UI outages as regression scenarios.
2. Prove durable lifecycle, transcript capture, direct messaging, and recovery in one runtime and one job.
3. Deliver the Workers overview and session/recovery views in the approved design language.
4. Validate crash recovery, message delivery, retry limits, preservation of work, and UI behavior end to end before expanding runtime coverage and review automation.
