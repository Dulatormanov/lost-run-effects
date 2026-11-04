"""The prototype of patches/0002 against the charges of tests/test_lost_run_effects.py: a run
writes a step's tool calls to the thread before their tools run, outside the fence, so a run lost
after a charge leaves its call, which the next run's model reads as unfinished; and
``ctx.idempotency_key(ref)`` is the same on every run of a thread for one tool and ``ref``, so a
model that books again anyway charges once. The tests need the patch."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from livekit.agents import Agent, RunContext, utils
from livekit.agents.a2a import A2ASessionContext
from livekit.agents.llm import ChatContext, ChatItem, FunctionCallOutput, LLMStream
from livekit.agents.storage.resolve import Thread
from livekit.agents.voice.agent_run import UNFINISHED_CALL

from . import fake_agentstore, test_lost_run_effects as lost
from .a2a_driver import (
    AGENT,
    Door,
    Reply,
    ScriptedLLM,
    call,
    no_project_env,  # noqa: F401  (autouse: the demo environment must not shape the cards)
)
from .agentdb_fixture import ON_PROJECT, let_the_lease_lapse
from .test_a2a_failure import ModelDownOnce, reason_of
from .test_lost_run_effects import (
    BOOKED,
    BOOKING,
    NIGHTS,
    QUESTION,
    AsksThenBooks,
    HandsOffToTheStay,
    booked,
    charged_once,
    refuse_a_commit,
    resend,
    sent_to_a_dying_worker,
    stay_desk,
)

pytestmark = [pytest.mark.unit]


class BooksEveryTime(ModelDownOnce):
    """A model that reads no history: it books room 12 for every message, and says the room is
    booked once its request ends in the booking's output. Each call gets an id of its own, as a
    provider mints one; ``down`` is the request it goes down on, once."""

    def __init__(self, *, down: Callable[[ChatContext], bool] = lambda ctx: False) -> None:
        super().__init__(down=down)

    def chat(self, *, chat_ctx: ChatContext, **kwargs: Any) -> LLMStream:  # type: ignore[override]
        last = chat_ctx.items[-1] if chat_ctx.items else None
        done = isinstance(last, FunctionCallOutput) and last.name == "book"
        book = call("book", utils.shortuuid("call_"), room="12")
        self.replies = [Reply(BOOKED) if done else Reply(tool_calls=[book])]
        return super().chat(chat_ctx=chat_ctx, **kwargs)


def keyed_desk(
    charges: Path, model: ScriptedLLM, charged: Callable[[], Awaitable[None]] | None = None
) -> Callable[[A2ASessionContext[Any]], Awaitable[str | None]]:
    """``front_desk`` with the prototype's key: ``book`` charges for the room in the outside
    system under ``ctx.idempotency_key``, with the room as its ref. ``charged`` runs once the
    line is written."""
    front = Agent(instructions="front desk", llm=model)

    @front.tool
    async def book(ctx: RunContext, room: str) -> str:
        """Book a room."""
        with charges.open("a") as outside:
            outside.write(f"{ctx.idempotency_key(f'room {room}')} {room}\n")
        if charged is not None:
            await charged()
        return f"booked room {room}"

    async def handler(ctx: A2ASessionContext[Any]) -> str | None:
        return (await front.run(ctx.message, message_history=ctx.message_history)).output

    return handler


def unfinished(request: ChatContext) -> list[str]:
    """The calls a model's request reads as made by a run that did not finish, by name."""
    return [
        i.name
        for i in request.items
        if isinstance(i, FunctionCallOutput) and i.output == UNFINISHED_CALL
    ]


async def stored(desk: Door, thread: str) -> list[ChatItem]:
    """The door's lane of ``thread`` as the store holds it."""
    return (await Thread(desk.storage, thread, agent_name=AGENT).chat_history()).items


# ---------------------------------------------------------------------------------------------
# a charge made again by a model that reads no history
# ---------------------------------------------------------------------------------------------

DYING_WORKER = f"""
import asyncio, os, sys
from pathlib import Path

from tests import fake_agentstore
from tests.a2a_driver import Door
from tests.agentdb_fixture import project_storage
from {__name__} import BooksEveryTime, keyed_desk

if url := os.environ.get("FAKE_AGENTSTORE_URL"):
    fake_agentstore.serve = lambda: url  # the test's fake, not one of this process's own


async def main() -> None:
    desk, text, thread, message_id, charges, key = sys.argv[1:]

    async def charged() -> None:
        print("charged", flush=True)
        await asyncio.Event().wait()

    handler = keyed_desk(Path(charges), BooksEveryTime(), charged)

    async def dying(ctx):
        print(ctx.task_id, flush=True)
        return await handler(ctx)

    await Door(project_storage("dying-worker"), dying).send(
        text, context_id=thread, message_id=message_id
    )
    await asyncio.Event().wait()


asyncio.run(main())
"""
"""tests/test_lost_run_effects.py's dying worker with ``keyed_desk`` and ``BooksEveryTime``: it
says its task's id, then ``charged`` once the room is charged, and hangs until it is killed."""


@pytest.mark.no_concurrent  # the worker is swapped on the module that every dying-worker test uses
async def test_a_model_that_books_again_after_the_worker_died_charges_once(
    door: Callable[..., Door], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lost, "DYING_WORKER", DYING_WORKER)
    charges = tmp_path / "charges"
    model = BooksEveryTime()
    desk = door(keyed_desk(charges, model))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    task_id, said = await sent_to_a_dying_worker("keyed_desk", BOOKING, thread, first, charges, "")
    assert said == "charged", "the worker ended before it charged"
    await let_the_lease_lapse()

    same, asked = await resend(desk, model, thread, first)
    assert same.task["id"] == task_id and reason_of(same.task) == "worker-lost"
    # the dead run's call was in the thread before its tool ran; the resent message's model
    # read it as unfinished, and booked again anyway
    assert unfinished(asked) == ["book"]
    assert len(charges.read_text().splitlines()) == 2
    # both under one key, which the outside system charges once
    charged_once(charges, asked)


@pytest.mark.skipif(ON_PROJECT, reason="the commit is refused on the fake")
@pytest.mark.no_concurrent  # refuse_a_commit patches the fake that every test shares
async def test_a_model_that_books_again_after_the_save_failed_charges_once(
    door: Callable[..., Door], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    charges = tmp_path / "charges"
    model = BooksEveryTime()
    # the charge is made, then the run's commit is refused
    desk = door(keyed_desk(charges, model, refuse_a_commit(monkeypatch)))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    failed = await desk.send(BOOKING, context_id=thread, message_id=first)
    assert reason_of(failed.task) == "storage-unavailable"
    await let_the_lease_lapse()

    same, asked = await resend(desk, model, thread, first)
    assert same.task["id"] == failed.task["id"]
    # the refused commit wrote nothing, but the call was written before its tool ran
    assert unfinished(asked) == ["book"]
    assert len(charges.read_text().splitlines()) == 2
    charged_once(charges, asked)


@pytest.mark.rule("B3")
async def test_a_model_that_books_again_after_the_model_went_down_charges_once(
    door: Callable[..., Door], tmp_path: Path
) -> None:
    charges = tmp_path / "charges"
    model = BooksEveryTime(down=booked)
    desk = door(keyed_desk(charges, model))
    thread = await desk.thread()
    first = uuid.uuid4().hex
    failed = await desk.send(BOOKING, context_id=thread, message_id=first)
    assert failed.state == "failed"

    same, asked = await resend(desk, model, thread, first)
    assert same.task["id"] == failed.task["id"]
    # the failed run kept its call and output (B3), so nothing reads as unfinished
    assert booked(asked) and unfinished(asked) == []
    assert len(charges.read_text().splitlines()) == 2
    charged_once(charges, asked)
    # the write ahead and the failed run's commit wrote each item once
    items = await stored(desk, thread)
    assert len({i.id for i in items}) == len(items)


def is_write_ahead(msg: Any) -> bool:
    """Whether a message to the fake is a batch that writes history items with no fence, which
    under ``interrupt`` only a write ahead sends."""
    batch = msg.batch.statements if msg.WhichOneof("message") == "batch" else []
    return any(s.sql.startswith("INSERT INTO items") and "version" not in s.sql for s in batch)


@pytest.mark.skipif(ON_PROJECT, reason="the write is refused on the fake")
@pytest.mark.no_concurrent  # the refusal patches the fake that every test shares
async def test_a_call_whose_write_ahead_is_refused_is_not_made(
    door: Callable[..., Door], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = fake_agentstore.Database.answer

    def refused(self: fake_agentstore.Database, msg: Any) -> list[Any]:
        if is_write_ahead(msg):
            error = fake_agentstore.Wire.Error(code="unavailable", message="the database went away")
            return [fake_agentstore.Wire.ServerMessage(error=error)]
        return answer(self, msg)

    monkeypatch.setattr(fake_agentstore.Database, "answer", refused)
    charges = tmp_path / "charges"
    desk = door(keyed_desk(charges, BooksEveryTime()))
    failed = await desk.send(BOOKING, context_id=await desk.thread())
    # a tool runs only once its call is in the thread: the task failed, and nothing was charged
    assert failed.state == "failed" and reason_of(failed.task) == "storage-unavailable"
    assert not charges.exists()


# ---------------------------------------------------------------------------------------------
# runs that end
# ---------------------------------------------------------------------------------------------


async def test_a_call_is_in_the_thread_before_its_tool_runs_and_once_after_the_commit(
    door: Callable[..., Door],
) -> None:
    model = ScriptedLLM(Reply(tool_calls=[call("book", "c1", room="12")]), Reply(BOOKED))
    front = Agent(instructions="front desk", llm=model)
    threads: list[Thread] = []
    held: list[list[ChatItem]] = []

    @front.tool
    async def book(ctx: RunContext, room: str) -> str:
        """Book a room."""
        held.append((await threads[0].chat_history()).items)
        return f"booked room {room}"

    async def handler(ctx: A2ASessionContext[Any]) -> str | None:
        threads.append(ctx.thread)
        return (await front.run(ctx.message, message_history=ctx.message_history)).output

    desk = door(handler)
    thread = await desk.thread()
    sent = await desk.send(BOOKING, context_id=thread)
    # the fenced commit still held after the write ahead, which bumped no version
    assert sent.state == "completed" and sent.answer == BOOKED
    # while the tool ran, the thread held the person's message and the call
    assert [i.type for i in held[0]] == ["message", "function_call"]
    items = await stored(desk, thread)
    types = ["message", "function_call", "function_call_output", "message"]
    assert [i.type for i in items] == types
    # the commit wrote the same two items again, as upserts on their ids: no item twice
    assert [i.id for i in items[:2]] == [i.id for i in held[0]]
    assert len({i.id for i in items}) == len(items)
    # one start for the call in the journal: written ahead, and not again by the commit
    journal = await threads[0].journal()
    tools = [e.payload for e in journal if e.type == "tool_execution_updated"]
    assert [(p["phase"], p["callId"]) for p in tools] == [("started", "c1"), ("ended", "c1")]


async def test_two_rooms_are_two_charges_and_one_room_in_two_threads_two_keys(
    door: Callable[..., Door], tmp_path: Path
) -> None:
    charges = tmp_path / "charges"
    both = ScriptedLLM(
        Reply(tool_calls=[call("book", "c1", room="12"), call("book", "c2", room="14")]),
        Reply(BOOKED),
    )
    desk = door(keyed_desk(charges, both))
    sent = await desk.send("book rooms 12 and 14", context_id=await desk.thread())
    assert sent.answer == BOOKED
    one = ScriptedLLM(Reply(tool_calls=[call("book", "c3", room="12")]), Reply(BOOKED))
    other = door(keyed_desk(charges, one))
    assert (await other.send(BOOKING, context_id=await other.thread())).answer == BOOKED

    made = [line.split() for line in charges.read_text().splitlines()]
    assert sorted(room for _, room in made) == ["12", "12", "14"]
    # a key per booking: two refs in one thread, and one ref in two threads
    assert len({key for key, _ in made}) == 3


async def test_a_call_that_waits_on_a_task_is_not_read_as_unfinished(
    door: Callable[..., Door], tmp_path: Path
) -> None:
    front, stay = HandsOffToTheStay(), AsksThenBooks()
    desk = door(stay_desk(tmp_path / "charges", "task_id", front, stay))
    thread = await desk.thread()
    asked = await desk.send(BOOKING, context_id=thread)
    assert asked.state == "input_required" and asked.status_text == QUESTION
    answered = await desk.send(NIGHTS, context_id=thread)
    assert answered.answer == BOOKED
    # the front desk's call waited on the task with no output, and no request read it as lost
    assert not [name for r in [*front.requests, *stay.requests] for name in unfinished(r)]


# ---------------------------------------------------------------------------------------------
# what the write ahead costs
# ---------------------------------------------------------------------------------------------


def count_writes_ahead(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """How many write ahead batches the fake has answered since this call."""
    answered: list[None] = []
    answer = fake_agentstore.Database.answer

    def counted(self: fake_agentstore.Database, msg: Any) -> list[Any]:
        out = answer(self, msg)
        if is_write_ahead(msg):
            answered.append(None)
        return out

    monkeypatch.setattr(fake_agentstore.Database, "answer", counted)
    return lambda: len(answered)


@pytest.mark.skipif(ON_PROJECT, reason="the test counts the batches the fake answers")
@pytest.mark.no_concurrent  # count_writes_ahead patches the fake that every test shares
async def test_a_step_that_calls_tools_writes_ahead_once(
    door: Callable[..., Door], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ScriptedLLM(
        Reply(tool_calls=[call("book", "c1", room="12")]),
        Reply(tool_calls=[call("book", "c2", room="14"), call("book", "c3", room="16")]),
        Reply(BOOKED),
        Reply("Hello."),
    )
    desk = door(keyed_desk(tmp_path / "charges", model))
    thread = await desk.thread()
    writes = count_writes_ahead(monkeypatch)
    assert (await desk.send("book rooms 12, 14 and 16", context_id=thread)).answer == BOOKED
    # two steps called tools, the second two of them in parallel: one write for each step
    assert writes() == 2
    assert (await desk.send("hi", context_id=thread)).answer == "Hello."
    # a turn that calls no tool writes nothing ahead
    assert writes() == 2
