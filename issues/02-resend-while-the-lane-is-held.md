# [agents-2.0] A messageId resent while its first send holds the lane cancels that send and is acked under an id tasks/get cannot find

docs/storage.md says a resend "returns the task the first send created, finished or still running, and runs nothing new" ([storage.md:138-141](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L138-L141); R3, which [test_a2a_server.py:386-401](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_a2a_server.py#L386-L401) pins for a first send that has finished). At 317f826 (the #8256 head), when the first send still holds the lane:

1. The resend marks itself the lane's waiter, so under `interrupt` (the A2A default) the first send yields at its next model call and ends `canceled` (`new-message`).
2. The resend is acked under a fresh `AST_` id, a task that ends `failed` (`not-written`) and that `tasks/get` cannot find. This also happens under `queue`, and while the first send is itself still waiting for the lane.

### Test

[`test_a_message_id_resent_while_its_turn_runs_returns_the_existing_task`](https://github.com/Dulatormanov/lost-run-effects/blob/main/tests/test_lost_run_effects.py#L541-L598) is a strict xfail. The first send's handler holds the lane on an `asyncio.Event`, then answers through an `Agent` with a scripted model. The same `messageId` goes out on a second request, and the handler is released once the resend has found the lease held twice (the test counts the acquire batches the fake answers).

```
git clone https://github.com/Dulatormanov/lost-run-effects && cd lost-run-effects
./run.sh --runxfail -k resent_while
```

The first run clones and builds 317f826 (about 14 min here). It printed, the same in 5 of 5 runs:

```
ResendInterfered: the first send ended canceled (new-message); the resend was acked under AST_da6c2c0dc883, which tasks/get cannot find (task AST_da6c2c0dc883 is not in session AT_fa6c4a5f94a9)
1 failed, 8 deselected, 2 warnings in 0.49s
```

### Cause (code at 317f826)

The first batch knows that the message is a duplicate: its `INSERT OR IGNORE` of the early row inserts nothing. But a batch that finds the lease held returns before that answer is read ([statements.rs:622-624](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L622-L624); only the success path reads it, at L645), and the retry carries no row ([run.rs:473-474](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L473-L474)). So `Run.submitted` is `None`, the resend branch at [turn.rs:297-309](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn.rs#L297-L309) is skipped, and the turn answers `not-written` under the request's own stream id ([turn/step.rs:561](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn/step.rs#L561), [a2a/methods.py:383](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/methods.py#L383)). The waiter mark ([run.rs:481-486](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L481-L486)) makes the holder yield ([turn/host.rs:285-289](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn/host.rs#L285-L289)).

The comment at run.rs:473 is right: a retry must not report a new message's own row as a duplicate. The answer just has to survive the retry. This came with a4cc2d783d, which moved the `messageId` check in turn.rs from before the lease to after it. None of #8200 to #8256 touches run.rs.

### Patch

[patches/0001-resend-while-the-lane-is-held.patch](https://github.com/Dulatormanov/lost-run-effects/blob/main/patches/0001-resend-while-the-lane-is-held.patch), 2 files, +40/-14:

- `acquire_batch` returns `AcquireOutcome::Loaded(Box<Loaded>)` or `AcquireOutcome::Held { submitted }`, through a crate-private `try_acquire`. The public `acquire` keeps its signature, so the Python binding and the crate tests do not change.
- `Lane::acquire` keeps the first batch's answer through the retries, and a known duplicate (`Some(false)`) polls without the waiter mark.
- A new message's first batch says `Some(true)`, so line 473's guarantee holds.

With it, the test passes in 5 of 5 runs (0.44-0.47 s). With the answer kept but the waiter mark unchanged, it still prints "the first send ended canceled (new-message)".

Checks with the patch, on the fake:

- tests/test_a2a_failure.py, test_a2a_server.py, test_live_inbox.py: 12, 17 and 32 passed. In probes, a new message that waits still runs as its own task, under `interrupt` and `queue`.
- tests/test_storage.py: 23 passed; B22's test fails as it does without the patch, for another reason (my comment on #8200). The whole unit suite has the same failures as without the patch.
- `cargo test -p lk-agents-storage` (from rust/): the 52 unit tests pass; the 25 staging tests return early without a project. `cargo fmt --check` passes, and `cargo clippy -p lk-agents-storage` warns only at statements.rs:531, which the patch does not touch.

You may prefer that a known duplicate not wait for the lane at all, as before a4cc2d783d, and await the existing task instead. That would also cover the first case below. I did not try it.

### Not fixed by the patch

- A resend that waits over `wait_timeout` (60 s) still fails `lease-timeout` under its fresh id ([turn.rs:282-293](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn.rs#L282-L293)).
- A waiter that dies leaves its `submitted` row for good. [lanes.md:59-61](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/lanes.md#L59-L61) says such a waiter fails `lease-timeout`, but `await_task` and `fail_orphans` fail only `working` rows ([turn.rs:506-509](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn.rs#L506-L509), [statements/sql.rs:91-92](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements/sql.rs#L91-L92)), and the door sends no ack for a task that has not ended ([a2a/pump.py:276-277](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/pump.py#L276-L277)). In a probe with `wait_timeout` at 3 s, a resend of its message got no response in 15 s. At 317f826 that happens when the lane is free at the resend; when it is held, 317f826 answers at once under a fresh id. With the patch, both get no response.
- The resend waits for the lane, as today, and then for the first task to end. The docs don't say a client may wait.

Should a dead waiter's `submitted` row fail `lease-timeout`, as lanes.md says? I marked the test R3, after test_a2a_server.py:386; if your checklist has a bug id for this, I'll use it.
