"""An agent's session saved as it goes, and this month's use: sessions send only what is new, into one memory for the
whole session, sync and async; usage reads the month, and a used-up month is its own error."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import geniffy
from geniffy import AsyncGeniffy, Geniffy, GeniffyError, UsageLimitError

KEY = "gnf_live_" + "k" * 43
SOURCE = {"id": "a" * 32, "kind": "note", "title": "Refund agent", "status": "reading"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(geniffy._client.time, "sleep", lambda s: None)
    monkeypatch.delenv("GENIFFY_BASE_URL", raising=False)


def recorder(sent, reply=None):
    def handler(r: httpx.Request) -> httpx.Response:
        sent.append((r.method, r.url.path, json.loads(r.content or b"{}")))
        return reply(r) if reply else httpx.Response(201, json={"source": SOURCE})
    return handler


def make(handler):
    return Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def turn(i):
    return [{"role": "user", "content": f"Question {i}"}, {"role": "assistant", "content": f"Answer {i}"}]


# ── sessions ─────────────────────────────────────────────────────────────────────────────────────────────
def test_a_session_turn_goes_to_one_memory_for_the_whole_session():
    sent = []
    g = make(recorder(sent))
    src = g.memories.add(messages=turn(1), session="run-42", title="Refund agent")
    assert src.id == "a" * 32
    assert sent == [("POST", "/v1/memories", {"messages": turn(1), "session": "run-42", "title": "Refund agent"})]


def test_what_does_not_go_with_a_session_is_refused_before_anything_is_sent():
    sent = []
    g = make(recorder(sent))
    for kw, why in (({"text": "hi"}, "goes with messages"), ({"external_id": "e"}, "not both"),
                    ({"said_at": "2026-10-01"}, "dated as it arrives")):
        args = dict(kw)
        if "text" not in args:
            args["messages"] = turn(1)
        with pytest.raises(ValueError, match=why):
            g.memories.add(session="s", **args)
    assert sent == []


def test_save_sends_only_what_is_new_since_the_last_save():
    sent = []
    run = make(recorder(sent)).session("run-42", title="Refund agent", labels={"project": "refunds"})
    history = turn(1)
    run.save(history)
    history += turn(2)
    run.save(history)
    assert run.save(history) is None, "nothing new: nothing sent"
    assert [b["messages"] for _, _, b in sent] == [turn(1), turn(2)]
    assert all(b["session"] == "run-42" and b["labels"] == {"project": "refunds"} for _, _, b in sent)


def test_a_rewritten_conversation_is_saved_again_rather_than_lost():
    """An agent that compacts its history drops the turn last saved: what it holds now goes in whole."""
    sent = []
    run = make(recorder(sent)).session("run-7")
    run.save(turn(1) + turn(2))
    compacted = [{"role": "user", "content": "Summary of turns 1 and 2"}] + turn(3)
    run.save(compacted)
    assert [b["messages"] for _, _, b in sent][1] == compacted


def test_a_long_session_goes_five_hundred_messages_a_call():
    sent = []
    run = make(recorder(sent)).session("big")
    history = [m for i in range(400) for m in turn(i)]        # 800 messages
    run.save(history)
    assert [len(b["messages"]) for _, _, b in sent] == [500, 300]
    history += turn(400)
    run.save(history)
    assert len(sent[-1][2]["messages"]) == 2


def test_the_async_session_does_the_same():
    sent = []

    def handler(r):
        sent.append(json.loads(r.content))
        return httpx.Response(201, json={"source": SOURCE})

    async def go():
        a = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        run = a.session("run-9")
        await run.save(turn(1))
        await run.save(turn(1) + turn(2))
        await a.memories.add(messages=turn(3), session="run-9")
    asyncio.run(go())
    assert [b["messages"] for b in sent] == [turn(1), turn(2), turn(3)]
    assert all(b["session"] == "run-9" for b in sent)


# ── usage ────────────────────────────────────────────────────────────────────────────────────────────────
MONTH = {"plan": "pro", "trial": False, "period_start": "2026-10-07T00:00:00+00:00",
         "period_end": "2026-11-07T00:00:00+00:00", "learned_tokens": 600_000, "included_tokens": 2_000_000,
         "waiting_tokens": 3_000, "answers": 120, "included_answers": 1_000, "extra_on": True, "extra_cap_usd": 20.0,
         "extra_used_usd": 0.0, "state": "ok"}


def test_usage_says_what_the_month_comes_to_and_a_used_up_month_is_its_own_error():
    replies = iter([httpx.Response(200, json=MONTH),
                    httpx.Response(402, json={"error": {"code": "allowance_used",
                                                        "message": "This month's learning is used."}})])
    paths = []
    g = make(lambda r: paths.append(r.url.path) or next(replies))
    assert g.usage() == MONTH and paths == ["/v1/usage"]
    with pytest.raises(UsageLimitError) as e:
        g.memories.add("One more thing to remember.")
    assert (e.value.status, e.value.code) == (402, "allowance_used") and isinstance(e.value, GeniffyError)
    assert paths.count("/v1/memories") == 1, "a used-up month is not retried"


def test_the_async_client_reads_the_month_too():
    async def go():
        a = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=MONTH))))
        return await a.usage()
    assert asyncio.run(go()) == MONTH
