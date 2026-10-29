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
[workers.stall]
after_minutes = 20
action = "wait" # "wait" or "restart"
max_restarts = 2
```

- The default reports a suspected stall and leaves the worker running. An automatic restart is opt-in, bounded, and verifies the original has stopped before launching another attempt.
- Repeated failures and retry limits are tracked separately from silence; exhaustion holds the job for a person and retains evidence.

The current engine measures repository progress rather than full worker activity. Automatic restart must remain unavailable until the activity signal and a verified stop-and-restart path are in place.

## Delivery

1. Capture real examples of stalls, repeated failures, and UI outages as regression scenarios.
2. Prove durable lifecycle, transcript capture, direct messaging, and recovery in one runtime and one job.
3. Deliver the Workers overview and session/recovery views in the approved design language.
4. Validate crash recovery, message delivery, retry limits, preservation of work, and UI behavior end to end before expanding runtime coverage and review automation.
