"""What a lost run already did: a charge keyed as docs/tools.md says, made before the model went
down, the save failed or the worker died, and resent as the docs say; a worker that dies on the
turn that answers an ``AgentTask``'s question; and a messageId resent while its first send holds
the lane. Each gap is a strict xfail that raises its own exception."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from livekit.agents import Agent, AgentTask, Completed, RunContext, function_tool, utils
from livekit.agents.a2a import A2ASessionContext
from livekit.agents.llm import ChatContext, ChatMessage, FunctionCallOutput, LLMStream

from . import fake_agentstore
from .a2a_driver import (
    Door,
    Reply,
    ScriptedLLM,
    Sent,
    call,
    no_project_env,  # noqa: F401  (autouse: the demo environment must not shape the cards)
    rpc,
    sse_results,
    wire,
)
from .agentdb_fixture import ON_PROJECT, PROJECT_URL, let_the_lease_lapse
from .test_a2a_failure import REPO, ModelDownOnce, reason_of
from .test_live_inbox import until

pytestmark = [pytest.mark.unit]

BOOKING = "book room 12"
BOOKED = "Room 12 is booked."

KEYS: dict[str, Callable[[RunContext], str]] = {
    "call_id": lambda ctx: ctx.function_call.call_id,
    "task_id": lambda ctx: ctx.task_id,
}
"""The documented keys: ``call_id``, which docs/tools.md tells a conversation tool such as
``book`` to key on, and ``task_id``, which docs/agents.md calls the idempotency key for outside
calls; tools.md names it for a tool inside a task, which in text reads the same id, today a fresh
``run_`` id per run (docs/TODO.md)."""


def booked(request: ChatContext) -> bool:
    """Whether a model's request holds a ``book`` output: the record of a booking it reads."""
    return any(isinstance(i, FunctionCallOutput) and i.name == "book" for i in request.items)


class BooksUnlessBooked(ModelDownOnce):
    """B3's model, made to read its history: it books room 12 unless the request holds a
    booking, and then says the room is booked. Each call gets an id of its own, as a provider
    mints one; ``down`` is the request it goes down on, once."""

    def __init__(self, *, down: Callable[[ChatContext], bool] = lambda ctx: False) -> None:
        super().__init__(down=down)

    def chat(self, *, chat_ctx: ChatContext, **kwargs: Any) -> LLMStream:  # type: ignore[override]
        book = call("book", utils.shortuuid("call_"), room="12")
        self.replies = [Reply(BOOKED) if booked(chat_ctx) else Reply(tool_calls=[book])]
        return super().chat(chat_ctx=chat_ctx, **kwargs)


def front_desk(
    charges: Path,
    key: str,
    model: BooksUnlessBooked,
    charged: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[A2ASessionContext[Any]], Awaitable[str | None]]:
    """A door's handler whose ``book`` charges for the room in an outside system: a ``key room``
    line appended to ``charges``, under the key ``KEYS[key]`` reads. ``charged`` runs once the
    line is written."""
    front = Agent(instructions="front desk", llm=model)

    @front.tool
    async def book(ctx: RunContext, room: str) -> str:
        """Book a room."""
        with charges.open("a") as outside:
            outside.write(f"{KEYS[key](ctx)} {room}\n")
        if charged is not None:
            await charged()
        return f"booked room {room}"

    async def handler(ctx: A2ASessionContext[Any]) -> str | None:
        return (await front.run(ctx.message, message_history=ctx.message_history)).output

    return handler


class ChargedAgain(AssertionError):
    """The outside system holds a second charge for one booking."""


def charged_once(charges: Path, asked: ChatContext) -> None:
    """One charge in the outside system, which dedupes on the idempotency key alone. A second
    one is the framework's only when ``asked``, the resent message's first request, held no
    record of the first: a model that read the booking and booked again is the test's fault."""
    made = charges.read_text().splitlines()
    keys = {line.split()[0] for line in made}
    assert keys, "no charge reached the outside system"
    if len(keys) > 1:
        assert not booked(asked), f"the model booked again although it read the booking: {made}"
        raise ChargedAgain(f"one booking, charged under {len(keys)} keys: {made}")


async def resend(
    desk: Door, model: BooksUnlessBooked, thread: str, message_id: str
) -> tuple[Sent, ChatContext]:
    """The client after a failed task, as the docs tell it: it resends the message. Once the
    lane's lease is free, the same messageId returns the failed task, which holds the person's
    message, and runs nothing (storage.md, "No double answers"); while the lease is held it is
    acked under a fresh id, a task that fails ``not-written`` and ``tasks/get`` cannot find (the
    last section). So the message runs again only under a new messageId. The same messageId's
    answer, and the first request the new message's model read."""
    asked = len(model.requests)
    same = await desk.send(BOOKING, context_id=thread, message_id=message_id)
    assert same.state == "failed" and len(model.requests) == asked
    assert [m["parts"][0]["text"] for m in same.task["history"]] == [BOOKING]
    again = await desk.send(BOOKING, context_id=thread)
    assert again.answer == BOOKED
    return same, model.requests[asked]


# ---------------------------------------------------------------------------------------------
# a worker that dies after the charge
# ---------------------------------------------------------------------------------------------

DYING_WORKER = f"""
import asyncio, os, sys
from pathlib import Path

from tests import fake_agentstore
from tests.a2a_driver import Door
from tests.agentdb_fixture import project_storage
from {__name__} import AsksThenBooks, BooksUnlessBooked, HandsOffToTheStay, front_desk, stay_desk

if url := os.environ.get("FAKE_AGENTSTORE_URL"):
    fake_agentstore.serve = lambda: url  # the test's fake, not one of this process's own


async def main() -> None:
    desk, text, thread, message_id, charges, key = sys.argv[1:]

    async def charged() -> None:
        print("charged", flush=True)
        await asyncio.Event().wait()

    if desk == "stay_desk":
        handler = stay_desk(Path(charges), key, HandsOffToTheStay(), AsksThenBooks(), charged)
    else:
        handler = front_desk(Path(charges), key, BooksUnlessBooked(), charged)

    async def dying(ctx):
        print(ctx.task_id, flush=True)
        return await handler(ctx)

    sent = await Door(project_storage("dying-worker"), dying).send(
        text, context_id=thread, message_id=message_id
    )
    print(sent.state, flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
"""
"""A worker in a process of its own, with this module's agents and models: it takes the lane for
one message on ``front_desk`` or ``stay_desk`` and says its task's id, then says ``charged`` once
the room is charged, or the state the task ended the turn in, and hangs until it is killed."""


async def sent_to_a_dying_worker(
    desk: str, text: str, thread: str, message_id: str, charges: Path, key: str
) -> list[str]:
    """``text`` sent to a ``DYING_WORKER`` on ``desk``, killed once it said its two lines."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    if ON_PROJECT:
        env["LIVEKIT_URL"] = PROJECT_URL
    else:
        env["FAKE_AGENTSTORE_URL"] = fake_agentstore.serve()
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        DYING_WORKER,
        desk,
        text,
        thread,
        message_id,
        str(charges),
        key,
        cwd=REPO,
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    said: list[str] = []
    try:
        assert worker.stdout is not None
        for _ in range(2):
            said.append((await asyncio.wait_for(worker.stdout.readline(), 60)).decode().strip())
    finally:
        worker.kill()
        await worker.wait()
    return said


@pytest.mark.xfail(
    strict=True,
    raises=ChargedAgain,
    reason="the documented keys change on the resend that worker-lost asks for: the dead run wrote none of its tool calls, so the resent message's model reads no booking, and this one books again under a fresh key",
)
@pytest.mark.parametrize("key", KEYS)
async def test_a_charge_made_before_the_worker_died_is_not_made_again(
    door: Callable[..., Door], tmp_path: Path, key: str
) -> None:
    charges = tmp_path / "charges"
    model = BooksUnlessBooked()
    desk = door(front_desk(charges, key, model))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    task_id, said = await sent_to_a_dying_worker("front_desk", BOOKING, thread, first, charges, key)
    assert task_id, "the worker ended before its turn started"
    assert said == "charged", "the worker ended before it charged"
    # the charge outlived the worker that made it, and nothing has charged again yet
    assert len(charges.read_text().splitlines()) == 1, "the dead worker's charge did not survive"
    # a resend sent while the dead worker's lease is held fails not-written under a fresh id
    await let_the_lease_lapse()

    same, asked = await resend(desk, model, thread, first)
    # R16 holds here: the task failed worker-lost, and the same messageId ran nothing
    assert same.task["id"] == task_id and reason_of(same.task) == "worker-lost"
    # no record of the first booking reaches the resent message's model; this one books
    # whenever it reads none
    charged_once(charges, asked)


# ---------------------------------------------------------------------------------------------
# a model that goes down after the charge
# ---------------------------------------------------------------------------------------------


@pytest.mark.rule("B3")
@pytest.mark.parametrize("key", KEYS)
async def test_a_charge_made_before_the_model_went_down_is_not_made_again(
    door: Callable[..., Door], tmp_path: Path, key: str
) -> None:
    charges = tmp_path / "charges"
    model = BooksUnlessBooked(down=booked)
    desk = door(front_desk(charges, key, model))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    failed = await desk.send(BOOKING, context_id=thread, message_id=first)
    assert failed.state == "failed"

    same, asked = await resend(desk, model, thread, first)
    assert same.task["id"] == failed.task["id"]
    # the resent message's model reads the booking that ran, so it does not book again
    assert booked(asked)
    charged_once(charges, asked)


# ---------------------------------------------------------------------------------------------
# a save that fails after the charge
# ---------------------------------------------------------------------------------------------

COMMIT = "UPDATE lanes SET version = version + 1"
"""How a run's fenced commit ends (lk-agents-storage, ``update_lane``)."""


def refuse_a_commit(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[None]]:
    """``arm()``: the fake refuses the next run commit it receives, once, as ``unavailable``,
    which the storage client reads as ``StorageUnavailable``."""
    armed, spent = threading.Event(), threading.Event()
    answer = fake_agentstore.Database.answer

    def refused(self: fake_agentstore.Database, msg: Any) -> list[Any]:
        batch = msg.batch.statements if msg.WhichOneof("message") == "batch" else []
        if armed.is_set() and not spent.is_set() and any(s.sql.startswith(COMMIT) for s in batch):
            spent.set()
            error = fake_agentstore.Wire.Error(code="unavailable", message="the database went away")
            return [fake_agentstore.Wire.ServerMessage(error=error)]
        return answer(self, msg)

    monkeypatch.setattr(fake_agentstore.Database, "answer", refused)

    async def arm() -> None:
        armed.set()

    return arm


@pytest.mark.skipif(ON_PROJECT, reason="the commit is refused on the fake")
@pytest.mark.no_concurrent  # refuse_a_commit patches the fake that every test shares
@pytest.mark.xfail(
    strict=True,
    raises=ChargedAgain,
    reason="the documented keys change on the resend that storage-unavailable asks for: the refused commit wrote none of the run's tool calls, so the resent message's model reads no booking, and this one books again under a fresh key",
)
@pytest.mark.parametrize("key", KEYS)
async def test_a_charge_made_before_the_save_failed_is_not_made_again(
    door: Callable[..., Door], tmp_path: Path, key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    charges = tmp_path / "charges"
    model = BooksUnlessBooked()
    # the charge is made, then the run's commit is refused
    desk = door(front_desk(charges, key, model, refuse_a_commit(monkeypatch)))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    failed = await desk.send(BOOKING, context_id=thread, message_id=first)
    # R15 holds here: the failed save is reported on the acked task
    assert reason_of(failed.task) == "storage-unavailable"
    # the refused commit left the lease held; no doc tells the client to wait, and a resend
    # sent at once fails not-written under a fresh id
    await let_the_lease_lapse()

    same, asked = await resend(desk, model, thread, first)
    assert same.task["id"] == failed.task["id"]
    # the run's tool calls were not written (its failed row, with the person's message, was), so
    # the resent message's model reads no booking; this one books whenever it reads none
    charged_once(charges, asked)


# ---------------------------------------------------------------------------------------------
# a worker that dies answering an AgentTask's question in text
# ---------------------------------------------------------------------------------------------

QUESTION = "How many nights?"
NIGHTS = "two nights"


@dataclass
class StayDesk:
    """What ``BookStay`` reaches on a worker: the outside system, the key it charges under, its
    model, and ``charged``, which runs once the line is written."""

    charges: Path
    key: str
    model: ScriptedLLM
    charged: Callable[[], Awaitable[None]] | None = None


DESKS: dict[str, StayDesk] = {}
"""Each worker's ``StayDesk``, by the outside system's path, which every worker of a test
knows."""


class HandsOffToTheStay(ScriptedLLM):
    """The front desk's model: it hands the booking to ``BookStay``, and says the room is booked
    once the task's value is the pending call's output."""

    def chat(self, *, chat_ctx: ChatContext, **kwargs: Any) -> LLMStream:  # type: ignore[override]
        done = any(
            isinstance(i, FunctionCallOutput) and i.name == "start_booking" for i in chat_ctx.items
        )
        start = call("start_booking", utils.shortuuid("call_"), room="12")
        self.replies = [Reply(BOOKED) if done else Reply(tool_calls=[start])]
        return super().chat(chat_ctx=chat_ctx, **kwargs)


def told(request: ChatContext) -> bool:
    """Whether a model's request holds the person's answer to the task's question."""
    return any(
        isinstance(i, ChatMessage) and i.role == "user" and i.text_content == NIGHTS
        for i in request.items
    )


class AsksThenBooks(ScriptedLLM):
    """``BookStay``'s model: it asks how many nights until its request holds the answer, then
    books unless it holds a booking."""

    def chat(self, *, chat_ctx: ChatContext, **kwargs: Any) -> LLMStream:  # type: ignore[override]
        book = call("book", utils.shortuuid("call_"), nights=2)
        if not told(chat_ctx):
            self.replies = [Reply(QUESTION)]
        else:
            self.replies = [Reply(BOOKED) if booked(chat_ctx) else Reply(tool_calls=[book])]
        return super().chat(chat_ctx=chat_ctx, **kwargs)


@dataclass
class BookStay(AgentTask[str]):
    """An ``AgentTask`` awaited in text, as in tests/test_task_in_text.py: it asks how many
    nights, and the answer books the room in its desk's outside system and completes it. Any
    worker rebuilds it from its fields."""

    room: str
    desk: str

    def __post_init__(self) -> None:
        super().__init__(instructions="Book the stay.", llm=DESKS[self.desk].model)

    @function_tool
    async def book(self, ctx: RunContext, nights: int) -> Completed[str]:
        """Book the room.

        Args:
            nights: how many nights
        """
        desk = DESKS[self.desk]
        with desk.charges.open("a") as outside:
            outside.write(f"{KEYS[desk.key](ctx)} {self.room}\n")
        if desk.charged is not None:
            await desk.charged()
        return Completed(f"booked room {self.room} for {nights} nights")


def stay_desk(
    charges: Path,
    key: str,
    front_model: ScriptedLLM,
    stay_model: ScriptedLLM,
    charged: Callable[[], Awaitable[None]] | None = None,
) -> Callable[[A2ASessionContext[Any]], Awaitable[Any]]:
    """A door's handler whose front desk awaits ``BookStay``, which charges in ``charges`` under
    the key ``KEYS[key]`` reads."""
    desk = str(charges)
    DESKS[desk] = StayDesk(charges, key, stay_model, charged)
    front = Agent(instructions="front desk", llm=front_model)

    @front.tool
    async def start_booking(ctx: RunContext, room: str) -> str:
        """Book a room.

        Args:
            room: the room
        """
        return await BookStay(room=room, desk=desk)  # turns pass here

    async def handler(ctx: A2ASessionContext[Any]) -> Any:
        return await front.run(ctx.message, message_history=ctx.message_history)

    return handler


class StillWorking(AssertionError):
    """A dead worker's task is still ``working`` after the next holder took the lane."""


@pytest.mark.rule("R16")
@pytest.mark.xfail(
    strict=True,
    raises=StillWorking,
    reason="R16 on a task that asked: the dead run left the row working with its ask, which fail_orphans skips, so it never fails worker-lost, and the resent answer runs as a new task that books again",
)
async def test_a_worker_that_dies_answering_a_tasks_question_leaves_it_failed_worker_lost(
    door: Callable[..., Door], tmp_path: Path
) -> None:
    charges = tmp_path / "charges"
    stay = AsksThenBooks()
    desk = door(stay_desk(charges, "task_id", HandsOffToTheStay(), stay))
    thread = await desk.thread()
    asked = await desk.send(BOOKING, context_id=thread)
    assert asked.state == "input_required" and asked.status_text == QUESTION
    task, answer = asked.task["id"], uuid.uuid4().hex
    # the answer continues the task on a worker that charges for the room and dies
    said = await sent_to_a_dying_worker("stay_desk", NIGHTS, thread, answer, charges, "task_id")
    assert said == [task, "charged"], "the worker died before it charged"
    assert len(charges.read_text().splitlines()) == 1, "the dead worker's charge did not survive"
    await let_the_lease_lapse()

    requests = len(stay.requests)
    same = await desk.send(NIGHTS, context_id=thread, message_id=answer)
    lost = await desk.get_task(task, thread)
    if lost["status"]["state"] == "TASK_STATE_WORKING":
        made = len(charges.read_text().splitlines())
        raise StillWorking(
            f"{task} is working with its question {Sent([], lost).status_text!r}; the resent "
            f"answer ran as {same.task['id']} ({same.state}: {same.answer!r}), charges: {made}"
        )
    # the task failed worker-lost when the next holder found it (docs/lanes.md), and the resent
    # answer returns it and runs nothing new (docs/storage.md, "No double answers")
    assert reason_of(lost) == "worker-lost"
    assert same.task["id"] == task and len(stay.requests) == requests
    assert len(charges.read_text().splitlines()) == 1


async def test_a_question_waits_on_the_person_when_the_worker_that_asked_it_dies(
    door: Callable[..., Door], tmp_path: Path
) -> None:
    # the control: a task that waits on the person is not the dead worker's to fail
    charges = tmp_path / "charges"
    stay = AsksThenBooks()
    desk = door(stay_desk(charges, "task_id", HandsOffToTheStay(), stay))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    # a worker asks the task's question, ends its turn and dies while the task waits
    task, ended = await sent_to_a_dying_worker(
        "stay_desk", BOOKING, thread, first, charges, "task_id"
    )
    assert ended == "input_required"
    held = Sent([], await desk.get_task(task, thread))
    assert held.state == "input_required" and held.status_text == QUESTION

    # the person's answer reaches this worker: it continues the task, which completes once
    answered = await desk.send(NIGHTS, context_id=thread)
    assert answered.task["id"] == task and answered.answer == BOOKED
    assert answered.task["history"][0]["messageId"] == first
    assert len(charges.read_text().splitlines()) == 1
    # asked once: this worker's model read the answer, and booked
    assert len(stay.requests) == 1 and told(stay.requests[0])


# ---------------------------------------------------------------------------------------------
# a resend while the first send holds the lane
# ---------------------------------------------------------------------------------------------


ACQUIRE = "UPDATE lanes SET lease_holder"
"""How an acquire batch takes the lease (lk-agents-storage, ``ACQUIRE``)."""


def count_acquires(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """How many acquire batches the fake has answered since this call."""
    answered: list[None] = []
    answer = fake_agentstore.Database.answer

    def counted(self: fake_agentstore.Database, msg: Any) -> list[Any]:
        out = answer(self, msg)
        batch = msg.batch.statements if msg.WhichOneof("message") == "batch" else []
        if any(s.sql.startswith(ACQUIRE) for s in batch):
            answered.append(None)
        return out

    monkeypatch.setattr(fake_agentstore.Database, "answer", counted)
    return lambda: len(answered)


class ResendInterfered(AssertionError):
    """A resend sent while its first send held the lane changed what one of the two got."""


@pytest.mark.rule("R3")
@pytest.mark.skipif(ON_PROJECT, reason="the test counts the acquires the fake answers")
@pytest.mark.no_concurrent  # count_acquires patches the fake that every test shares
@pytest.mark.xfail(
    strict=True,
    raises=ResendInterfered,
    reason="R3 while the lane is held: the resend marks itself the lane's waiter, so the first send yields at its model call, and the acquire's retry carries no row, so the resend loses the duplicate its first batch found and is acked under a fresh id that tasks/get cannot find",
)
async def test_a_message_id_resent_while_its_turn_runs_returns_the_existing_task(
    door: Callable[..., Door], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    model = ScriptedLLM(Reply(BOOKED))
    front = Agent(instructions="front desk", llm=model)
    runs: list[str] = []

    async def holds_the_lane(ctx: A2ASessionContext[Any]) -> str | None:
        runs.append(ctx.message.text_content or "")
        entered.set()
        await release.wait()
        return (await front.run(ctx.message, message_history=ctx.message_history)).output

    acquires = count_acquires(monkeypatch)
    desk = door(holds_the_lane)
    thread = await desk.thread()
    message_id = uuid.uuid4().hex
    first = asyncio.create_task(desk.send(BOOKING, context_id=thread, message_id=message_id))
    await asyncio.wait_for(entered.wait(), 5)
    held = acquires()
    # the same messageId again, on a run of its own: its acquire finds the lease held, and so
    # does its first retry, by when a run that marks itself the lane's waiter has done so
    message = wire(BOOKING, message_id, context_id=thread)
    resent = asyncio.create_task(rpc(desk.http, "message/stream", {"message": message}))

    async def retried() -> bool:
        return acquires() >= held + 2

    await until(retried, "the resend retrying the acquire")
    release.set()

    sent = await first
    frames = [r["result"] for r in sse_results(await resent) if "result" in r]
    acked = frames[0]["task"]["id"]
    got = (await rpc(desk.http, "tasks/get", {"id": acked, "contextId": thread})).json()
    wrong = []
    if sent.state != "completed":
        reason = sent.metadata.get("livekit/interruptionReason")
        wrong.append(f"the first send ended {sent.state} ({reason})")
    if "result" not in got:
        error = got["error"]["message"]
        wrong.append(f"the resend was acked under {acked}, which tasks/get cannot find ({error})")
    if wrong:
        raise ResendInterfered("; ".join(wrong))
    # the first send answered, and the resend answers with its task and runs nothing
    again = Sent(frames, got["result"])
    assert sent.answer == BOOKED
    assert again.task["id"] == sent.task["id"] and again.answer == sent.answer
    assert runs == [BOOKING] and len(model.requests) == 1, "nothing runs twice"
