# [agents-2.0] A worker that dies answering an AgentTask's question leaves the task working, and the resent answer charges again

R16, in #8202's test commit f154102a, is "a killed worker's task failed worker-lost with nothing run again" (also [lanes.md:64-65](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/lanes.md#L64-L65)). This skip breaks it when the worker dies on the turn that answers an `AgentTask`'s question. The pickup in the acquire batch touches only `status` and `updated_at` ([statements/sql.rs:78-80](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements/sql.rs#L78-L80)), so the dead continuation is a `working` row that still holds its ask, and this line skips it; the skip is meant for queued questions, `working` rows whose ask has `queued_at` ([run/tasks.rs:277-285](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run/tasks.rs#L277-L285)). Under `interrupt` and `queue`, nothing else fails the row.

A strict xfail, [`test_a_worker_that_dies_answering_a_tasks_question_leaves_it_failed_worker_lost`](https://github.com/Dulatormanov/lost-run-effects/blob/main/tests/test_lost_run_effects.py#L448-L483), kills the worker after the task's `book` tool charged, lets the lease lapse, and resends the answer with the same `messageId` (`./run.sh --runxfail -k dies_answering` in https://github.com/Dulatormanov/lost-run-effects):

```
StillWorking: AST_5be8cc25051e is working with its question 'How many nights?'; the resent answer ran as AST_8f78c9a5b1d5 (completed: 'Room 12 is booked.'), charges: 2
```

1. Should this skip only an ask with `queued_at`, so that a dead continuation fails `worker-lost`?
2. That alone would not stop the second charge. A continued task keeps its first `messageId` ([turn/step.rs:115-122](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn/step.rs#L115-L122)), and the answer reaches the history only in the commit. When I failed the row by hand before the resend, the resent answer still ran as a new task. Should the answer's `messageId` be findable before the commit?
