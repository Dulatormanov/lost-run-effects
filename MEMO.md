# Memo: lost run effects in LiveKit Agents 2.0

Dulat Ormanov, November 2026. https://github.com/Dulatormanov/lost-run-effects

## What I found

I tested the agents-2.0 stack at 317f826 (#8200 to #8256) inside LiveKit's test suite, on its fake store, with scripted models.

- A `messageId` resent while its first send holds the lane makes that send yield, and is acked under an id `tasks/get` cannot find. The first batch learns the message is a duplicate, but a batch that finds the lease held throws that answer away (statements.rs:622-624), and the retry carries no row (run.rs:473-474). My patch keeps the answer and lets a known duplicate wait unmarked.
- A run dropped without a commit keeps renewing its lease, though run.rs:437-439 says it lapses. That is why #8200's B22 test fails on the fake.
- After a worker death or a failed save, the same `messageId` returns the failed task and runs nothing. Under a new `messageId` the message runs as a new task, with a new `call_id` and `ctx.task_id` and no record of the lost run's tool calls. So a charge keyed as tools.md:190-192 and agents.md:89 advise can be made twice.
- A worker that dies answering an `AgentTask`'s question leaves the task `working`.

## Why it matters

A booking or payments agent keys its outside call on `call_id` or `ctx.task_id`. After a crash the docs say to resend; the same `messageId` runs nothing, and a new one brings new keys. My models book whenever they read no booking: the tests show that a second charge can happen, not that a real model will make it.

## A design, built and measured

[DESIGN.md](DESIGN.md) and [patches/0002](patches/0002-calls-ahead-and-stable-keys.patch): a run writes a step's tool calls to the thread before their tools run, outside the fence, the way `Thread.cancel` commits unfenced (storage/resolve.py:215-242), and the next run's model reads a lost run's call as unfinished. `ctx.idempotency_key(ref)` hashes the thread, the tool and a ref the tool picks, so a model that books again anyway charges once. On the fake it costs one batch per step that calls tools.

## Questions

- Should tools.md say that these keys hold only within one run?
- Which `messageId` should a client resend with after `worker-lost`?
- Should a dead waiter's `submitted` row fail `lease-timeout`, as lanes.md:59-61 says?
- Is a `Drop` for `Run` the right fix for the lease?
