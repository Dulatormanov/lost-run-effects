# Comment on #8200: two of the four tests it lists as failing

Two of the four tests this PR's body lists as failing seem to fail for other reasons than the body gives: one for a lease that is never released, one for a stale assertion. Both at 317f826 (the #8256 head), on the fake.

**1. B22's test ([test_storage.py:441-477](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_storage.py#L441-L477)): a dropped run keeps renewing its lease.**

The body says the test "kills the worker the wrong way". I think the test is right. Its docstring (L445-446) expects what [run.rs:437-439](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L437-L439) promises: "a run dropped without either stops renewing and lets its lease lapse". But while renewals succeed, only `finish()` stops the renewal timer ([run.rs:757-766](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/run.rs#L757-L766)), and only `commit` and `abandon` call it. The timer task holds its own `Arc<LeaseInner>` and a `ThreadDb` clone ([statements.rs:687-706](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L687-L706)), so `Drop for LeaseInner` ([statements.rs:1136-1142](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L1136-L1142)) never runs. And `renew()` uses `self.conn`, not `live()` ([statements.rs:659-673](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/rust/crates/lk-agents-storage/src/statements.rs#L659-L673)), so `close()` does not stop it either.

- A probe of the test's setup read the `lanes` row 9 times in the 4 s after the cancel: the same holder and token each time, and `lease_until - now` at 1 or 2 s (the fake's TTL is 2 s). A resend with `wait_timeout` at 6 s then failed `lease-timeout` after 6.54 s, and nothing ran.
- The test itself fails at L468 with a fresh id instead of `AST_x`, after 64 s, and logs `turn.rs:283 the lane was not released in time; the task fails lease-timeout`.
- As an experiment, this `Drop` made the test pass in 3.3 s. The lease then counted down to -2 s, and the resend returned `AST_x` failed `worker-lost` at once. I have not run the full suites with it.

```rust
impl Drop for Run {
    fn drop(&mut self) {
        if let Some(lease) = &self.lease {
            lease.stop();
        }
    }
}
```

A timer that holds a `Weak`, or `renew()` through `live()`, might work too; I did not try them. In production a turn runs to its end whatever happens to its response ([a2a/pump.py:61-65](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/pump.py#L61-L65)), so from reading I expect the leak only when a turn's task is cancelled, at shutdown for example. #7963 sends `renew()` as a Batch but still on `self.conn`, so the leak would stay after it. I did not run staging. #7963's body says a renew sent as a Query was not durable there, so on staging the test may also fail for another reason.

**2. [test_task_in_text.py:147](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_task_in_text.py#L147) fails only on a stale assertion.**

The `AgentTask` round trip works. The third turn's answer streams one artifact chunk per token, then an empty last chunk, because the tokens already spelled the answer ([a2a/pump.py:316-325](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/pump.py#L316-L325)). An empty appended chunk gets no part ([a2a/codec.py:394](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/codec.py#L394)), so `parts[0]` raises `IndexError`. Joining the parts, as [test_graph.py:246-249](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/tests/test_graph.py#L246-L249) does, makes the whole test pass, at 317f826 and at this PR's head:

```diff
-    assert artifacts and artifacts[-1].artifact.parts[0].text == "Booked, HTL-22."
+    assert "".join(part.text for a in artifacts for part in a.artifact.parts) == "Booked, HTL-22."
```
