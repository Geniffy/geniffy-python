"""The client against a fake API on an httpx mock transport: requests, results, errors and retries."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import geniffy
from geniffy import (AsyncGeniffy, AuthenticationError, Geniffy, GeniffyError, InternalServerError, NotFoundError,
                     UnreadableError)

KEY = "gnf_live_" + "k" * 43
SOURCE = {"id": "a" * 32, "kind": "note", "title": "Priya Nair signs the Lumen renewal.", "status": "reading"}
MEM = {"id": 7, "text": "Priya Nair signs the Lumen renewal.", "kind": "people", "about": "Priya Nair", "status": "current",
       "source": {"id": "a" * 32, "kind": "note", "title": "Call with Priya"}}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(geniffy._client.time, "sleep", lambda s: None)
    monkeypatch.delenv("GENIFFY_BASE_URL", raising=False)


def make(handler, **kw):
    return Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_a_key_is_required_and_the_message_says_where_to_get_one(monkeypatch):
    monkeypatch.delenv("GENIFFY_API_KEY", raising=False)
    with pytest.raises(GeniffyError, match="GENIFFY_API_KEY"):
        Geniffy()
    monkeypatch.setenv("GENIFFY_API_KEY", KEY)
    monkeypatch.setenv("GENIFFY_BASE_URL", "https://api.example.test/")
    seen = []
    g = Geniffy(http_client=httpx.Client(transport=httpx.MockTransport(
        lambda r: seen.append(r) or httpx.Response(200, json={"name": "Omkar", "memory": "personal"}))))
    assert g.me()["name"] == "Omkar"
    assert str(seen[0].url) == "https://api.example.test/v1/me" and seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert seen[0].headers["user-agent"].startswith("geniffy-python/")


def test_adding_a_note_a_link_and_a_file():
    sent = []

    def handler(r):
        sent.append(r)
        return httpx.Response(201, json={"source": dict(SOURCE, kind="file" if r.url.path.endswith("/file") else "note")})
    g = make(handler)
    src = g.memories.add("Priya Nair signs the Lumen renewal.", title="Call with Priya")
    assert isinstance(src, geniffy.Source) and src.status == "reading" and not src.done
    assert json.loads(sent[0].content) == {"text": "Priya Nair signs the Lumen renewal.", "title": "Call with Priya"}
    g.memories.add(url="https://acme.test/team")
    assert json.loads(sent[1].content) == {"url": "https://acme.test/team"}
    with pytest.raises(ValueError):
        g.memories.add("text", url="https://acme.test")
    g.memories.add_file(b"%PDF-1.7 ...", filename="Pricing.pdf", title="Pricing")
    body = sent[2].content
    assert sent[2].url.path == "/v1/memories/file" and b'filename="Pricing.pdf"' in body and b"application/pdf" in body
    assert b'name="title"' in body


def test_something_said_in_the_past_carries_its_date():
    from datetime import date, datetime, timezone
    sent = []
    g = make(lambda r: sent.append(json.loads(r.content)) or httpx.Response(201, json={"source": SOURCE}))
    g.memories.add(messages=[{"role": "user", "content": "We moved the launch to May."}],
                   said_at=datetime(2025, 3, 4, 9, 30, tzinfo=timezone.utc))
    assert sent[0]["said_at"] == "2025-03-04T09:30:00+00:00"
    g.memories.add("Priya signs the renewal.", said_at=date(2025, 3, 4))
    assert sent[1] == {"text": "Priya signs the renewal.", "said_at": "2025-03-04"}
    g.memories.add("Tea, not coffee.", said_at="2025-03-04T15:00:00+05:30")
    assert sent[2]["said_at"] == "2025-03-04T15:00:00+05:30"
    g.memories.add("No date.")
    assert "said_at" not in sent[3]
    with pytest.raises(ValueError, match="web page"):
        g.memories.add(url="https://acme.test", said_at="2025-03-04")
    a = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: sent.append(json.loads(r.content)) or httpx.Response(201, json={"source": SOURCE}))))
    asyncio.run(a.memories.add("Async too.", said_at=date(2024, 1, 2)))
    assert sent[-1]["said_at"] == "2024-01-02"


def test_your_own_id_goes_with_every_add_and_finds_and_deletes_the_source():
    """Sending again under the same external_id updates that source on Geniffy's side; the client's part is
    to send the id with a note, a conversation, a link and a file, and to find and delete by it."""
    sent = []
    held = dict(SOURCE, external_id="ticket-42")

    def handler(r: httpx.Request) -> httpx.Response:
        sent.append((r.method, r.url.path, dict(r.url.params), r.content))
        if r.method == "GET" and r.url.path == "/v1/sources":
            found = [held] if r.url.params.get("external_id") == "ticket-42" else []
            return httpx.Response(200, json={"sources": found, "total": len(found), "next": None})
        if r.method == "DELETE":
            return httpx.Response(200, json={"id": held["id"], "external_id": "ticket-42", "deleted": True})
        return httpx.Response(201, json={"source": held})

    g = make(handler)
    src = g.memories.add("Customer asked about invoice 7.", title="Ticket 42", external_id="ticket-42")
    assert src.external_id == "ticket-42"
    assert json.loads(sent[-1][3]) == {"text": "Customer asked about invoice 7.", "title": "Ticket 42",
                                       "external_id": "ticket-42"}
    g.memories.add(messages=[{"role": "user", "content": "I moved to Pune."}], external_id="chat-7")
    assert json.loads(sent[-1][3])["external_id"] == "chat-7"
    g.memories.add("No id.")
    assert "external_id" not in json.loads(sent[-1][3])
    g.memories.add_file(b"%PDF-1.4", filename="plan.pdf", external_id="plan-pdf")
    assert b'name="external_id"' in sent[-1][3] and b"plan-pdf" in sent[-1][3]

    assert g.sources.get(external_id="ticket-42").id == SOURCE["id"]
    assert sent[-1][:3] == ("GET", "/v1/sources", {"external_id": "ticket-42"})
    with pytest.raises(NotFoundError):
        g.sources.get(external_id="ticket-43")
    g.sources.delete(external_id="ticket-42")
    assert sent[-1][:3] == ("DELETE", "/v1/sources", {"external_id": "ticket-42"})
    g.sources.delete(SOURCE["id"])
    assert sent[-1][:2] == ("DELETE", f"/v1/sources/{SOURCE['id']}")
    for wrong in ({}, {"source_id": "a" * 32, "external_id": "ticket-42"}):
        with pytest.raises(TypeError, match="one of the two"):
            g.sources.get(**wrong)
        with pytest.raises(TypeError, match="one of the two"):
            g.sources.delete(**wrong)

    a = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def main():
        await a.memories.add("Async too.", external_id="ticket-42")
        assert json.loads(sent[-1][3])["external_id"] == "ticket-42"
        assert (await a.sources.get(external_id="ticket-42")).external_id == "ticket-42"
        with pytest.raises(NotFoundError):
            await a.sources.get(external_id="nope")
        await a.sources.delete(external_id="ticket-42")
        assert sent[-1][:3] == ("DELETE", "/v1/sources", {"external_id": "ticket-42"})
    asyncio.run(main())


def test_listing_pages_through_every_memory_and_opening_one():
    def handler(r):
        if r.url.path == "/v1/memories":
            cursor = int(r.url.params["cursor"])
            nxt = 2 if cursor == 0 else None
            return httpx.Response(200, json={"memories": [dict(MEM, id=cursor + 1), dict(MEM, id=cursor + 2)],
                                             "counts": {"all": 4}, "total": 4, "next": nxt})
        return httpx.Response(200, json={"memory": dict(MEM, quote="Priya Nair signs the Lumen renewal."), "history": [MEM]})
    g = make(handler)
    assert [m.id for m in g.memories.iter(page_size=2)] == [1, 2, 3, 4]
    page = g.memories.list(kind="people")
    assert page.total == 4 and page.memories[0].source.title == "Call with Priya"
    detail = g.memories.get(7)
    assert detail.memory.quote == "Priya Nair signs the Lumen renewal." and len(detail.history) == 1


def test_asking_returns_the_answer_or_says_nothing_supports_one():
    answers = iter([
        {"question": "Who signs the Lumen renewal?", "answer": "Priya Nair.", "memories": [MEM], "message": None, "clash": False},
        {"question": "When did we sign Acme?", "answer": None, "memories": [],
         "message": "Nothing you have added says that yet, so Geniffy won't guess."}])
    g = make(lambda r: httpx.Response(200, json=next(answers)))
    a = g.ask("Who signs the Lumen renewal?")
    assert a.answer == "Priya Nair." and str(a) == "Priya Nair." and a.memories[0].id == 7
    b = g.ask("When did we sign Acme?")
    assert b.answer is None and "won't guess" in str(b)


def test_errors_carry_the_apis_own_sentence():
    replies = iter([
        httpx.Response(401, json={"error": {"code": "bad_key", "message": "This key doesn't work. It may have been revoked."}}),
        httpx.Response(404, json={"error": {"code": "not_found", "message": "That memory wasn't found."}}),
        httpx.Response(422, json={"error": {"code": "unreadable", "message": "This PDF is a scan with no text in it.",
                                            "source": dict(SOURCE, status="failed")}})])
    g = make(lambda r: next(replies))
    with pytest.raises(AuthenticationError, match="revoked"):
        g.me()
    with pytest.raises(NotFoundError):
        g.memories.get(1)
    with pytest.raises(UnreadableError) as e:
        g.memories.add_file(b"%PDF-", filename="scan.pdf")
    assert e.value.source["status"] == "failed" and e.value.status == 422


def test_reads_are_retried_but_a_failed_add_is_not_sent_twice():
    calls = []

    def handler(r):
        calls.append(r.method)
        if r.method == "GET" and len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": "memory_unavailable", "message": "busy"}})
        if r.method == "POST" and r.url.path == "/v1/memories":
            return httpx.Response(502, json={"error": {"code": "memory_unavailable", "message": "Your memory didn't answer."}})
        return httpx.Response(200, json={"sources": [], "total": 0, "next": None})
    g = make(handler)
    assert g.sources.list().total == 0 and calls == ["GET", "GET"]
    with pytest.raises(InternalServerError):
        g.memories.add("once only")
    assert calls.count("POST") == 1


def test_a_rate_limited_add_and_a_refused_connection_are_retried():
    state = {"n": 0}

    def handler(r):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ConnectError("refused")
        if state["n"] == 2:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"code": "busy", "message": "slow"}})
        return httpx.Response(201, json={"source": SOURCE})
    assert make(handler).memories.add("hello").id == SOURCE["id"] and state["n"] == 3


def test_waiting_until_a_source_is_learned():
    stages = iter(["reading", "reading", "learned"])
    g = make(lambda r: httpx.Response(200, json={"source": dict(SOURCE, status=next(stages), facts=6)}))
    src = g.sources.wait(SOURCE["id"], interval=0)
    assert src.status == "learned" and src.facts == 6 and src.done


def test_waiting_asks_geniffy_to_hold_the_call_so_one_call_is_enough():
    asked = []

    def handler(r):
        asked.append(r.url.params.get("wait"))
        return httpx.Response(200, json={"source": dict(SOURCE, status="learned", facts=6)})
    g = Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(handler), timeout=60))
    assert g.sources.wait(SOURCE["id"]).done
    assert asked == ["30.0"], "held up to 30 seconds by Geniffy, not asked again every two"
    asked.clear()
    g.sources.wait(SOURCE["id"], timeout=3)
    assert asked == ["3.0"], "and never past the caller's own deadline"


def test_the_async_client_does_the_same():
    def handler(r):
        if r.url.path == "/v1/ask":
            return httpx.Response(200, json={"question": "q", "answer": "Priya Nair.", "memories": [MEM]})
        return httpx.Response(201, json={"source": SOURCE})

    async def main():
        async with AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as g:
            src = await g.memories.add("Priya Nair signs the Lumen renewal.")
            ans = await g.ask("Who signs the Lumen renewal?")
            return src, ans
    src, ans = asyncio.run(main())
    assert src.id == SOURCE["id"] and ans.answer == "Priya Nair."


def test_the_async_client_has_every_add_and_correct_the_sync_one_has():
    """The docs say every call has an awaited twin. A conversation, a batch and a correction were missing
    from AsyncGeniffy, so async code could not save a chat history at all."""
    sent = []

    def handler(r):
        sent.append((r.method, r.url.path, json.loads(r.content or b"{}")))
        if r.url.path == "/v1/memories/batch":
            return httpx.Response(202, json={"results": [], "added": 1, "failed": 0})
        if r.method == "PATCH":
            return httpx.Response(200, json={"id": 42, "corrected": True, "source": SOURCE})
        return httpx.Response(201, json={"source": SOURCE})

    async def main():
        async with AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as g:
            await g.memories.add(messages=[{"role": "user", "content": "I prefer mornings."}], title="Chat")
            await g.memories.add_many([{"text": "Pilots run for 6 weeks."}])
            await g.memories.correct(42, "Priya signs it.")
    asyncio.run(main())
    assert sent[0] == ("POST", "/v1/memories", {"messages": [{"role": "user", "content": "I prefer mornings."}], "title": "Chat"})
    assert sent[1] == ("POST", "/v1/memories/batch", {"items": [{"text": "Pilots run for 6 weeks."}]})
    assert sent[2] == ("PATCH", "/v1/memories/42", {"text": "Priya signs it."})


# ── spaces: one of YOUR users ─────────────────────────────────────────────────
def test_a_bound_client_carries_its_space_on_every_call_and_the_plain_one_carries_none():
    """The space rides a header rather than an argument, so no method can lose it by forgetting to
    pass it on, and binding once per request is the whole ceremony."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("x-geniffy-space")))
        return httpx.Response(200, json={"name": "Omkar", "memory": "personal", "space": None})

    client = make(handler)
    mem = client.space("customer_1042")

    assert client.space_id == ""
    assert mem.space_id == "customer_1042"
    assert mem._http is client._http, "a bound client shares the pool, so one per request is cheap"

    client.me()
    mem.me()
    mem.search("renewal")
    mem.memories.list()
    assert seen[0] == ("/v1/me", None), "the plain client reaches your own memory"
    assert [s for _, s in seen[1:]] == ["customer_1042"] * 3, "every call a bound client makes is scoped"


def test_two_bound_clients_do_not_share_a_space():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-geniffy-space"))
        return httpx.Response(200, json={"memories": [], "counts": {}, "total": 0})

    client = make(handler)
    client.space("customer_1042").search("x")
    client.space("customer_1043").search("x")
    assert seen == ["customer_1042", "customer_1043"]


def test_a_blank_space_is_refused_never_read_as_your_own_memory():
    """A blank space is what a missing user id looks like by the time it reaches space(): read as no
    space, that user's words would land in your own memory, with every other user missing an id."""
    sent = []
    client = make(lambda request: sent.append(request) or httpx.Response(200, json={}))
    for blank in ("", "   ", "	"):
        with pytest.raises(ValueError, match="blank"):
            client.space(blank)
        with pytest.raises(ValueError, match="blank"):
            client.forget_space(blank)
        with pytest.raises(ValueError, match="blank"):
            make(lambda request: httpx.Response(200, json={}), space=blank)
    for wrong in (None, True, 3.5, ["customer_1042"]):
        with pytest.raises(TypeError, match="string or an int id"):
            client.space(wrong)
        with pytest.raises(TypeError, match="string or an int id"):
            client.forget_space(wrong)
    assert sent == [], "nothing was asked of the API"

    assert client.space(1042).space_id == "1042", "an int id is the same user as its digits"
    assert client.space(" customer_1042 ").space_id == "customer_1042"
    assert make(lambda request: httpx.Response(200, json={}), space=None).space_id == "",         "space=None, or none at all, is your own memory"


def test_the_async_client_refuses_a_blank_space_too():
    client = AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={}))))
    with pytest.raises(ValueError, match="blank"):
        client.space("")
    with pytest.raises(TypeError):
        client.space(None)
    with pytest.raises(ValueError, match="blank"):
        asyncio.run(client.forget_space(" "))
    assert client.space(1042).space_id == "1042"


def test_listing_and_forgetting_one_of_your_users():
    rows = [{"space": "customer_1042", "sources": 3, "memories": 11, "last_added_at": "2026-10-04T06:00:00+00:00"}]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path, not path: httpx decodes `path` for display, and what matters is the wire
        seen.append((request.method, request.url.raw_path.decode()))
        if request.method == "DELETE":
            return httpx.Response(200, json={"space": "customer_1042", "erased": True, "sources_removed": 3})
        return httpx.Response(200, json={"spaces": rows, "total": 1})

    client = make(handler)
    assert client.spaces()[0]["memories"] == 11
    assert client.forget_space("customer 1042/../x")["erased"] is True
    assert seen[1] == ("DELETE", "/v1/spaces/customer%201042%2F..%2Fx"), "the name is escaped, never pasted into the path"


# ── the shapes a developer already has ────────────────────────────────────────
def test_a_conversation_goes_in_as_the_framework_holds_it():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"source": SOURCE})

    make(handler).memories.add(messages=[{"role": "user", "content": "I prefer mornings."},
                                         {"role": "assistant", "content": "Noted."}], title="Session 4")
    assert sent[0] == {"messages": [{"role": "user", "content": "I prefer mornings."},
                                    {"role": "assistant", "content": "Noted."}], "title": "Session 4"}


def test_context_comes_back_as_one_string_to_paste():
    body = {"question": "Devika", "context": "- Never calls before ten.  [Call notes, 2026-10-04]",
            "memories": [MEM], "used": 1, "empty": False}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=body)

    c = make(handler)
    assert c.context("Devika").startswith("- Never calls before ten.")
    assert c.context_full("Devika")["empty"] is False
    assert seen[0][0] == "/v1/context"
    assert seen[0][1] == {"question": "Devika", "limit": 12, "kind": None, "with_sources": True}


def test_a_batch_reports_each_item_and_a_correction_is_a_patch():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": 7, "corrected": True, "source": SOURCE})
        return httpx.Response(202, json={"results": [{"index": 0, "source": SOURCE, "error": None},
                                                     {"index": 1, "source": None,
                                                      "error": {"code": "bad_space", "message": "no"}}],
                                         "added": 1, "failed": 1})

    c = make(handler)
    out = c.memories.add_many([{"text": "Good."}, {"text": "Bad.", "space": "a:b"}])
    assert out["added"] == 1 and out["failed"] == 1
    assert out["results"][1]["error"]["code"] == "bad_space", "one bad item does not take the rest down"
    assert c.memories.correct(7, "Never before ten.")["corrected"] is True
    assert seen == [("POST", "/v1/memories/batch"), ("PATCH", "/v1/memories/7")]


def test_profile_brief_and_graph_reach_their_own_endpoints():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.path}?{request.url.query.decode()}".rstrip("?"))
        return httpx.Response(200, json={"nodes": [], "edges": [], "lasting": [], "current": [],
                                         "grouped": {}, "memories": 0, "total": 0})

    c = make(handler)
    c.profile()
    c.profile("Devika Rao")
    c.brief("Devika Rao", limit=10)
    c.graph()
    assert seen == ["/v1/profile", "/v1/profile?subject=Devika+Rao",
                    "/v1/brief?limit=10&subject=Devika+Rao", "/v1/graph"]


def test_the_async_client_has_context_and_the_people_calls_too():
    """Until 5 Oct 2026 the async client had no context(), the call most apps want, in the client most
    Python AI apps use."""
    seen = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append((r.method, r.url.path, dict(r.url.params), r.headers.get("x-geniffy-space")))
        if r.url.path == "/v1/context":
            return httpx.Response(200, json={"question": "q", "context": "- Never calls before ten.  [Call notes, 2026-10-04]",
                                             "memories": [MEM], "used": 1, "empty": False})
        return httpx.Response(200, json={"ok": True})

    async def main():
        async with AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as g:
            mem = g.space("customer_1042")
            block = await mem.context("When can I call Devika?")
            full = await mem.context_full("When can I call Devika?", limit=3)
            await mem.profile("Devika")
            await mem.brief("Devika", limit=10)
            await mem.graph()
            return block, full
    block, full = asyncio.run(main())
    assert block.startswith("- Never calls before ten.") and full["used"] == 1
    assert [x[1] for x in seen] == ["/v1/context", "/v1/context", "/v1/profile", "/v1/brief", "/v1/graph"]
    assert seen[2][2] == {"subject": "Devika"} and seen[3][2] == {"limit": "10", "subject": "Devika"}
    assert all(x[3] == "customer_1042" for x in seen), "a bound async client carries its space on every call"


def test_a_failed_calls_error_carries_the_id_geniffy_gave_it():
    rid = "e15cf212-341b-4e36-859e-fed587b87eca"
    c = make(lambda r: httpx.Response(404, json={"error": {"code": "not_found", "message": "No memory with that id."}},
                                      headers={"x-request-id": rid}))
    try:
        c.memories.get(1)
    except NotFoundError as e:
        assert e.request_id == rid
        assert str(e) == f"No memory with that id. (request {rid})", "it shows wherever the error is logged"
    else:
        raise AssertionError("expected NotFoundError")

