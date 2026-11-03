# [agents-2.0] After worker-lost or storage-unavailable, the documented idempotency keys change on the resend the docs ask for

At 317f826 (the #8256 head), the docs make two promises that I think cannot both hold after a worker death or a failed save:

1. Resend the message. Both `worker-lost` and `storage-unavailable` say "resend the message" ([a2a.md:253-255](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/a2a.md#L253-L255)), and so do [storage.md:146-147](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L146-L147) and [lanes.md:64-65](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/lanes.md#L64-L65).
2. "Key every outside call on `ctx.function_call.call_id` inside a conversation tool and on `ctx.task_id` inside a task, so a repeated call is a no-op on the other side" ([tools.md:190-192](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/tools.md#L190-L192)). [agents.md:89](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/agents.md#L89) calls `ctx.task_id` "the idempotency key for outside calls".

The same `messageId` returns the failed task and runs nothing ([storage.md:138-141](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L138-L141)). So the message runs again only under a new `messageId`, which is `AgentClient`'s default ([a2a/client.py:304](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/client.py#L304)). That is a new task, with a new `ctx.task_id` (a fresh `run_` id today, [voice/agent_run.py:459](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/voice/agent_run.py#L459)), new call ids from the provider, and none of the lost run's tool calls in its history. The keys change on exactly the resend the docs ask for. The TODO at [voice/events.py:163-164](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/voice/events.py#L163-L164) would make `ctx.task_id` the A2A task id, but a new message is still a new task with a new id ([a2a/methods.py:383](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/a2a/methods.py#L383)).

I don't think the unit of work is wrong: a lost run writes nothing ([storage.md:121-122](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L121-L122)), and #8202 leaves a dying worker "as before". I'm asking which promise should give. A smaller clash on the way: [storage.md:135-137](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/docs/storage.md#L135-L137) says resending after a failed save "runs it again", and L138-141 that a resend "runs nothing new".

### What I ran

Four strict xfails: the worker is killed after the charge, or the run's commit is refused after it, with the charge keyed on `call_id` and on `ctx.task_id`. The model books whenever its request holds no booking, and the outside system dedupes on the key alone, so this shows that a charge can be made twice, not that a real model will make it. The model-down control, #8202's own path, passes with one charge.

```
git clone https://github.com/Dulatormanov/lost-run-effects && cd lost-run-effects
./run.sh --runxfail -k "worker_died or save_failed"
```

```
ChargedAgain: one booking, charged under 2 keys: ['call_27db51f2bf5b 12', 'call_fc7a27a6c09a 12']
ChargedAgain: one booking, charged under 2 keys: ['run_9b68d11e0b97 12', 'run_0806bb113f69 12']
ChargedAgain: one booking, charged under 2 keys: ['call_053b4aac271c 12', 'call_184caab13126 12']
ChargedAgain: one booking, charged under 2 keys: ['run_1d40aac81409 12', 'run_4493c853e40c 12']
4 failed, 5 deselected, 5 warnings in 24.90s
```

The first two are the worker death, the last two the refused commit. The tests wait for the lease to lapse before they resend; while it is held, the same `messageId` hits a separate bug, which I reported in [#8367](https://github.com/livekit/agents/issues/8367). In a checkout of 317f826, you can copy tests/test_lost_run_effects.py into tests/ instead of using run.sh.

### Questions

1. Should tools.md say that `call_id` and `ctx.task_id` hold only within one run, and point to a key the caller owns, such as an order number, for an outside call that a lost run may have made?
2. Or should a tool record its outside call before making it, outside the fence, the way `Thread.cancel` commits unfenced ([storage/resolve.py:215-242](https://github.com/livekit/agents/blob/317f826957239e255dd67dd162c83b4cac6b072b/livekit-agents/livekit/agents/storage/resolve.py#L215-L242)), so the next run's model reads it and can reuse its key?
3. Which `messageId` should a client resend with after `worker-lost` or `storage-unavailable`?
4. Which id should these tests carry? I left them unmarked.
