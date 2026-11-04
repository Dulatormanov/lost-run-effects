# Design: a lost run's charge is made once

A prototype for finding 3 of the [README](README.md), at 317f826: [patches/0002](patches/0002-calls-ahead-and-stable-keys.patch), tested in [tests/test_effect_prototype.py](tests/test_effect_prototype.py).

## The API

```python
@front.tool
async def book(ctx: RunContext, room: str, nights: int) -> str:
    """Book a room."""
    key = ctx.idempotency_key(f"room {room}, {nights} nights")
    await payments.charge(room, nights, idempotency_key=key)
    return f"booked room {room}"
```

That is the whole API: `ref` names one real effect, and the record needs no code.

## Underneath

1. **Write ahead.** Before a step's tools run, the door writes what the run added so far, the calls last, to the lane. It is one unfenced commit, like `Thread.cancel`'s ([resolve.py:215-242](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/storage/resolve.py#L215-L242)). It bumps no version ([statements.rs:798-799](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L798-L799)), so the run's fenced commit still holds. That commit writes the items again as upserts on `id`, which keep `seq` ([sql.rs:102-109](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements/sql.rs#L102-L109)). The call's `started` journal event moves to the write.
2. **Read.** Today a call with no output is left out of the request ([agent_run.py:1343-1356](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/voice/agent_run.py#L1343-L1356)), so the write alone would not reach the model. Now, in a door's turn, a call that no `AgentTask` waits on gets an output in the request only. The history stays as it is, as with `as_model_reads` ([tool_context.py:414-431](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/llm/tool_context.py#L414-L431)).
3. **Key.** `ctx.idempotency_key(ref)` is 32 hex digits of SHA-256 over the thread id, the tool's name and `ref`. No storage: tools reach the thread id through the run hooks ([context.py:203](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/storage/context.py#L203)), and a resend keeps its `contextId`.

## What a write costs

On the fake store over loopback, not agent-db. One batch per step that calls tools; parallel calls share it (a test counts them). 200 threads, each with two one-tool turns and one with no tool:

| median | 317f826 | with the patch |
|---|---|---|
| one-tool turn | 6.02 ms, 5 batches | 6.78 ms, 6 batches |
| turn with no tool | 4.96 ms | 4.98 ms |
| the write, open and close included | | 0.73 ms (p90 0.76 ms) |

A thread's first write in a process may dial a connection; on the fake that write took 0.76 ms. On agent-db each write adds a network round trip.

## What the next model reads

The lost run's message, its `book` call, then this output, marked as an error:

> (This call has no result: the run that made it did not finish, so whether it took effect is unknown. If that matters, find out before you make the call again.)

Then the resent message.

## What the docs can promise

- In text, a tool call is in the thread before its tool runs; if it cannot be written, the tool does not run.
- After a lost run, the next model reads each of its calls with no result as unfinished.
- `ctx.idempotency_key(ref)` is the same on every run of a thread for one tool and `ref`, also under a new `messageId`.

## What it does not cover

- A provider that ignores keys, or forgets them before the resend.
- Two real effects with one `ref`: the provider takes the second as a repeat. The ref must tell them apart.
- A death between the write and the charge: the model reads "unknown", though nothing was charged. The key makes a retry safe.
- Voice calls, where the key falls back to `ctx.task_id`; and an `AgentTask` row that stays `working` (finding 4).
- A run that lost its lease but still runs: its write still lands.
- Compaction that drops the call.

## Alternatives I rejected

- **Opt-in per tool**, the first sketch's `await ctx.effect(ref)`: read-only tools skip the write, but a tool that forgets it charges twice.
- **A stored key** that a retry reuses: the retry must still be matched to the lost call by tool and ref, so storage adds nothing.
- **Storing the "unfinished" output**: under the `concurrent` policy, a run still in flight would later write a second output for the call.
- **A Rust statement fenced on the lease token**: it would also stop a run that lost its lease, but adds storage surface and generated models.
