# lost-run-effects

Tests and a patch for LiveKit Agents 2.0 at 317f826, the head of the agents-2.0 PR stack ([#8200](https://github.com/livekit/agents/pull/8200) to [#8256](https://github.com/livekit/agents/pull/8256)). They run inside LiveKit's own test suite, on its fake store, with scripted models: no API keys or real model calls. [MEMO.md](MEMO.md): one page, with a design sketch.

Independent project; not affiliated with LiveKit.

## Findings (code at 317f826)

1. **A resend while the lane is held.** The same `messageId`, sent while its first send holds the lane, makes the first send yield at its next model call (`canceled`, `new-message`) and is acked under a task id that `tasks/get` cannot find. The first batch learns the message is a duplicate, but a batch that finds the lease held throws that answer away ([statements.rs:622-624](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L622-L624)), and the retry carries no row ([run.rs:473-474](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L473-L474)). The duplicate also marks itself the lane's waiter ([run.rs:481-486](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L481-L486)). Commit a4cc2d783d moved the `messageId` check after the lease. [The patch](patches/0001-resend-while-the-lane-is-held.patch) keeps the first answer, and a known duplicate waits unmarked.
2. **A dropped run keeps renewing its lease.** [run.rs:437-439](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L437-L439) says its lease lapses. But while renewals succeed, only `finish()` stops the timer ([run.rs:757-766](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L757-L766)), and the timer task holds its own `Arc` ([statements.rs:687-706](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L687-L706)), so `Drop for LeaseInner` never runs. That is why the test #8200 marks B22 ([test_storage.py:441-477](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_storage.py#L441-L477)) fails on the fake. #8200 blames the test; I think the test is right.
3. **Two doc promises that cannot both hold.** After `worker-lost` or `storage-unavailable`, the docs say to resend the message ([a2a.md:253-255](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/a2a.md#L253-L255)), and to key outside calls on `call_id` or `ctx.task_id` so a repeated call is a no-op ([tools.md:190-192](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/tools.md#L190-L192), [agents.md:89](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/agents.md#L89)). But the same `messageId` returns the failed task and runs nothing ([storage.md:138-141](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L138-L141)). Under a new one, `AgentClient`'s default ([client.py:304](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/client.py#L304)), the message is a new task with new keys and no record of the lost run's tool calls. So a charge can be made twice. The planned `ctx.task_id` change ([TODO.md:13](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/TODO.md#L13)) keeps this.
4. **A dead `AgentTask` answer stays `working`.** When the worker dies on the turn that answers a task's question, `fail_orphans` skips the row, because it still holds its ask ([turn.rs:865-866](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/turn.rs#L865-L866), from #8255). The resent answer runs as a new task and charges again.
5. **A stale assertion.** [test_task_in_text.py:147](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_task_in_text.py#L147), which #8200 also lists as failing, reads `parts[0]` of an empty last chunk ([pump.py:316-325](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/pump.py#L316-L325)). Joining the parts makes it pass.

One failure, as `--runxfail` shows it:

    ChargedAgain: one booking, charged under 2 keys: ['call_fbb8d80a314a 12', 'call_b6f123e822ee 12']

## Results

| test | finding | 317f826 | patched |
|---|---|---|---|
| model down after the charge (`call_id`, `task_id`) | 3, control | 2 passed | 2 passed |
| worker killed after the charge (`call_id`, `task_id`) | 3 | 2 xfailed | 2 xfailed |
| commit refused after the charge (`call_id`, `task_id`) | 3 | 2 xfailed | 2 xfailed |
| worker killed answering an `AgentTask`'s question | 4 | xfailed | xfailed |
| worker dies while the question waits | 4, control | passed | passed |
| same `messageId` while the lane is held | 1 | xfailed | passes (XPASS strict) |

Each xfail is strict, with its own exception. The markers reuse ids from LiveKit's tests: R3, "a resend runs nothing new" ([test_a2a_server.py:386](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_a2a_server.py#L386)); R16, "a dead worker's task fails `worker-lost`" ([test_a2a_failure.py:218](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_a2a_failure.py#L218)); B3, #8202's fix. LiveKit's checklist is not public, so the ids are my reading; finding 3's xfails carry none.

## Run it

In a LiveKit Agents checkout at 317f826:

    cp tests/test_lost_run_effects.py <checkout>/tests/
    cd <checkout> && uv run pytest tests/test_lost_run_effects.py -p no:cacheprovider

Expected: "3 passed, 6 xfailed", about 31 s; `--runxfail` shows each failure. With the patch applied and built (`git apply`, `uv sync --all-extras --dev`), `-k resent_while --runxfail` gives "1 passed".

From zero, `./run.sh` clones and builds 317f826 into vendor/agents, then runs that command; pytest arguments follow. It needs git 2.49+, uv, Rust and network (about 14 min here the first time). `./run.sh --patched -k resent_while --runxfail` does the same in vendor/agents-patched, with the patch. After a failed build, delete that checkout's .venv.

## What it doesn't show

- The models book whenever they read no booking, and the outside system dedupes on the key alone: finding 3 shows a charge can be made twice, not that a real model will do it.
- Only the fake store and text over A2A: no staging, no voice calls.
- The patch leaves two cases ([issues/02](issues/02-resend-while-the-lane-is-held.md)): a resend that waits over 60 s fails under a fresh id, and a resend of a dead waiter gets no response.

License: Apache-2.0
