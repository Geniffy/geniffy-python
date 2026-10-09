"""The briefing doors from Python: the briefing and its parts, where things stand, episodes, lessons and intentions,
sync and async, each asking for exactly what the API takes, on a bound space too."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from geniffy import AsyncGeniffy, Geniffy, NotFoundError

KEY = "gnf_live_" + "k" * 43
STATE = {"project": "checkout", "goal": "Ship the new checkout", "focus": "Payments", "open": [{"text": "Refunds", "since": None}],
         "decisions": [], "next_steps": ["Test refunds"], "blockers": [], "done": [], "updated_at": None}
EPISODE = {"id": 5, "project": "checkout", "title": "Refund test", "what_happened": "Refunds failed.", "outcome": "Fixed",
           "led_to": None, "people": [], "decisions": [], "started_at": None, "ended_at": None,
           "source": {"id": "a" * 32, "kind": "note", "title": "Session"}}


def fake(seen):
    def handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content or b"{}")
        seen.append((r.method, r.url.path, dict(r.url.params), body, r.headers.get("x-geniffy-space")))
        if r.url.path == "/v1/briefing":
            return httpx.Response(200, json={"project": body.get("project"), "briefing": "Where things stand (checkout):",
                                             "now": [STATE], "due": [], "lessons": [], "episodes": [EPISODE], "memories": []})
        if r.url.path == "/v1/now":
            return httpx.Response(200, json={"now": [STATE]})
        if r.url.path == "/v1/episodes":
            return httpx.Response(200, json={"episodes": [EPISODE]})
        if r.url.path == "/v1/lessons":
            return httpx.Response(200, json={"lessons": [{"id": 3, "statement": "Test refunds before release"}]})
        if r.url.path == "/v1/intentions":
            return httpx.Response(200, json={"intentions": [{"id": 7, "what": "Revoke the test key", "status": "open"}]})
        if r.url.path == "/v1/memory-health":
            return httpx.Response(200, json={"health": 0.75, "tested_at": "2026-10-09T03:00:00+00:00", "asked": 2,
                                             "score": 1.5, "items": []})
        if r.url.path == "/v1/intentions/7":
            return httpx.Response(200, json={"intention": {"id": 7, "what": "Revoke the test key", "status": body["status"]}})
        return httpx.Response(404, json={"error": {"code": "not_switched_on", "message": "Briefings aren't switched on."}})
    return handler


def test_the_briefing_and_every_part_ask_for_what_the_api_takes():
    seen = []
    g = Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(fake(seen))))
    assert g.briefing(project="checkout", cue="refunds fail") == "Where things stand (checkout):"
    assert seen[-1][:2] == ("POST", "/v1/briefing")
    assert seen[-1][3] == {"project": "checkout", "cue": "refunds fail", "budget_chars": 6000}
    full = g.briefing_full(budget_chars=2000)
    assert full["episodes"][0]["title"] == "Refund test" and seen[-1][3] == {"cue": "", "budget_chars": 2000}
    assert g.now("checkout")[0]["goal"] == "Ship the new checkout" and seen[-1][2] == {"project": "checkout"}
    assert g.episodes(limit=5)[0]["outcome"] == "Fixed" and seen[-1][2] == {"limit": "5"}
    assert g.lessons("checkout")[0]["statement"] == "Test refunds before release"
    assert seen[-1][2] == {"project": "checkout", "limit": "50"}
    assert g.intentions()[0]["what"] == "Revoke the test key" and seen[-1][2] == {"status": "open", "limit": "50"}
    assert g.set_intention(7)["status"] == "done" and seen[-1][3] == {"status": "done"}
    assert g.memory_health()["health"] == 0.75 and seen[-1][1] == "/v1/memory-health"
    g.space("customer_1042").now()
    assert seen[-1][4] == "customer_1042", "a bound client reads its user's own"


def test_briefings_switched_off_say_so():
    g = Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(fake([]))))
    with pytest.raises(NotFoundError, match="aren't switched on"):
        g.set_intention(8)


def test_the_async_client_has_briefings_too():
    seen = []

    async def go():
        g = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake(seen))))
        assert await g.briefing(project="checkout") == "Where things stand (checkout):"
        assert (await g.now())[0]["focus"] == "Payments"
        assert (await g.episodes("checkout"))[0]["id"] == 5
        assert (await g.lessons())[0]["id"] == 3
        assert (await g.intentions(status="done"))[0]["id"] == 7
        assert (await g.set_intention(7, "dropped"))["status"] == "dropped"
    asyncio.run(go())
    assert [x[1] for x in seen] == ["/v1/briefing", "/v1/now", "/v1/episodes", "/v1/lessons", "/v1/intentions",
                                    "/v1/intentions/7"]
    assert seen[4][2] == {"status": "done", "limit": "50"}


def test_a_whole_session_goes_in_as_the_framework_holds_it():
    seen = []

    def handler(r):
        seen.append(json.loads(r.content))
        return httpx.Response(201, json={"source": {"id": "a" * 32, "kind": "note", "title": "Session", "status": "reading"}})
    g = Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    session = [{"role": "user", "content": "Refund order 42."},
               {"role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "refund", "arguments": '{"order":42}'}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "refunded"},
               {"role": "assistant", "content": "Order 42 is refunded."}]
    g.memories.add(messages=session, labels={"project": "checkout"})
    assert seen[0]["messages"] == session, "tool calls and results go as they are; the API reads every shape"
    assert seen[0]["labels"] == {"project": "checkout"}
