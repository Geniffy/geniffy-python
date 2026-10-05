"""The Geniffy client: Geniffy (blocking) and AsyncGeniffy (asyncio), over https://api.geniffy.com/v1.

    from geniffy import Geniffy
    g = Geniffy()                                   # reads GENIFFY_API_KEY
    g.memories.add("Priya Nair signs the Lumen renewal, and it comes up in March.")
    print(g.ask("Who signs the Lumen renewal?").answer)

Retries: reads and deletes are retried on network errors, 408, 429 and 5xx; adding is retried only when
the request never reached Geniffy (a failed connection) or on 429, so a retry never saves a note twice.
"""
from __future__ import annotations

import asyncio
import mimetypes
import os
import random
import time
from datetime import date, datetime
from typing import IO, Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import quote

import httpx

from ._errors import APIConnectionError, GeniffyError, NotFoundError, from_response
from ._types import Answer, Key, Kind, Memory, MemoryDetail, MemoryPage, Source, SourcePage

__version__ = "0.2.0"
DEFAULT_BASE_URL = "https://api.geniffy.com"
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
FileInput = Union[str, "os.PathLike[str]", bytes, IO[bytes]]


def _settings(api_key: Optional[str], base_url: Optional[str]) -> Tuple[str, str, Dict[str, str]]:
    key = (api_key or os.environ.get("GENIFFY_API_KEY") or "").strip()
    if not key:
        raise GeniffyError("No API key. Pass api_key=... or set GENIFFY_API_KEY. "
                           "Make one in the Geniffy app under Connect, API keys.")
    url = (base_url or os.environ.get("GENIFFY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    headers = {"Authorization": f"Bearer {key}", "User-Agent": f"geniffy-python/{__version__}",
               "Accept": "application/json"}
    return key, url, headers


def _space_name(space: Any, what: str = "space()") -> str:
    """One of your users, by your own name for them: a string, or an int id. A blank one is refused, not
    read as no space: a client with no space reads YOUR memory, so a user with no id would land in it."""
    if isinstance(space, bool) or not isinstance(space, (str, int)):
        raise TypeError(f"{what} takes your name for one of your users, a string or an int id, "
                        f"not {type(space).__name__}.")
    name = str(space).strip()
    if not name:
        raise ValueError(f"{what} got a blank space. A client with no space reads your own memory, so a user "
                         "with no id would land in it. Pass the user's id, or use the client itself for your "
                         "own memory.")
    return name


def _delay(attempt: int, response: Optional[httpx.Response]) -> float:
    if response is not None:
        try:
            wait = float(response.headers.get("retry-after", ""))
            if 0 <= wait <= 60:
                return wait
        except ValueError:
            pass
    return min(8.0, 0.5 * (2 ** attempt)) * (0.75 + random.random() / 2)


def _retryable(method: str, response: Optional[httpx.Response], error: Optional[Exception]) -> bool:
    if error is not None:
        # a connection that never opened never delivered the request: always safe to send again
        return method not in ("POST", "PATCH") or isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
    assert response is not None
    if response.status_code == 429:
        return True
    return method not in ("POST", "PATCH") and response.status_code in _RETRY_STATUS


def _body(response: httpx.Response) -> Dict[str, Any]:
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError:
        return {"error": {"code": "bad_response", "message": response.text[:300]}}
    return data if isinstance(data, dict) else {"data": data}


def _file(file: FileInput, filename: Optional[str]) -> Tuple[str, bytes]:
    """(name, bytes) for a path, raw bytes or an open binary file; read whole so a retry can resend it."""
    if isinstance(file, (str, os.PathLike)):
        path = os.fspath(file)
        with open(path, "rb") as fh:
            return filename or os.path.basename(path), fh.read()
    if isinstance(file, (bytes, bytearray)):
        return filename or "file", bytes(file)
    data = file.read()
    if isinstance(data, str):
        raise TypeError("Open the file in binary mode: open(path, 'rb').")
    return filename or os.path.basename(str(getattr(file, "name", "") or "")) or "file", data


def _note_or_link(text: Optional[str], url: Optional[str], title: Optional[str]) -> Dict[str, Any]:
    if (text is None) == (url is None):
        raise ValueError("Pass the text of a note, or url=... for a web page (one of them).")
    body: Dict[str, Any] = {"text": text} if text is not None else {"url": url}
    if title:
        body["title"] = title
    return body


SaidAt = Union[str, datetime, date, None]


def _add_body(text: Optional[str], url: Optional[str], messages: Optional[List[Dict[str, Any]]],
              title: Optional[str], said_at: SaidAt, external_id: Optional[str] = None) -> Dict[str, Any]:
    body = _note_or_link(text, url, title) if messages is None else {"messages": list(messages)}
    if external_id is not None:
        body["external_id"] = str(external_id)
    if messages is not None and title:
        body["title"] = title
    if said_at is not None:
        if url is not None:
            raise ValueError("said_at goes with a note or a conversation: a web page is read as it is today.")
        # a datetime with no time zone is read as UTC by the API
        body["said_at"] = said_at.isoformat() if isinstance(said_at, (datetime, date)) else str(said_at)
    return body


def _multipart(file: FileInput, filename: Optional[str], title: Optional[str], external_id: Optional[str] = None):
    name, content = _file(file, filename)
    files = {"file": (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")}
    data = {k: str(v) for k, v in (("title", title), ("external_id", external_id)) if v}
    return files, (data or None)


def _one_source(source_id: Optional[str], external_id: Optional[str]) -> None:
    if (source_id is None) == (external_id is None):
        raise TypeError("Name the source by its id or by your own external_id, one of the two.")


_NO_EXTERNAL = "No source in this space has that external_id."


def _hold(http: Any, left: float) -> float:
    """How long to ask Geniffy to hold a wait open: up to 30 seconds, never past the caller's deadline, and
    never so long that this client's own read timeout fires first. With a short timeout this comes to 0,
    and waiting falls back to asking every `interval` seconds."""
    read = getattr(getattr(http, "timeout", None), "read", None)
    cap = 30.0 if read is None else float(read) - 5.0
    return round(max(0.0, min(left, cap, 30.0)), 1)


def _page_params(kind: Optional[Kind], limit: int, cursor: int) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit, "cursor": cursor}
    if kind:
        params["kind"] = kind
    return params


def _search_body(q: str, limit: int, kind: Optional[Kind]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"q": q, "limit": limit}
    if kind:
        body["kind"] = kind
    return body


# ── blocking ──────────────────────────────────────────────────────────────────
class Geniffy:
    """A person's memory, from Python. Every call reaches only the memory of the key's owner.

    Building an app on this, give each of YOUR users a space and nothing else can read it:

        client = Geniffy(api_key=...)        # once, for the process
        mem = client.space(user.id)          # per request
        mem.memories.add("Prefers WhatsApp, not email.")
        print(mem.ask("How should we reach them?").answer)

    Name no space and the client reaches your own memory, the one the Geniffy app shows you.
    """

    def __init__(self, api_key: Optional[str] = None, *, base_url: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 2, http_client: Optional[httpx.Client] = None,
                 space: Union[str, int, None] = None):
        _, url, headers = _settings(api_key, base_url)
        self.max_retries = max(0, int(max_retries))
        self._http = http_client or httpx.Client(timeout=timeout)
        self.space_id = "" if space is None else _space_name(space, "space=")
        # One header carries the space, so every call a bound client makes is scoped without a
        # single method taking it, and a space can never be lost by forgetting an argument.
        if self.space_id:
            headers = {**headers, "X-Geniffy-Space": self.space_id}
        self._url, self._headers = url, headers
        self._api_key, self._base_url, self._timeout = api_key, base_url, timeout
        self.memories = Memories(self)
        self.sources = Sources(self)
        self.keys = Keys(self)

    def space(self, space: Union[str, int]) -> "Geniffy":
        """The same client, pointed at one of YOUR users. It shares this client's connection pool,
        so one per request is cheap. `space` is your own name for that user, the id you already give
        them: up to 128 letters, digits, dots, dashes or underscores, so an id rather than an email. A
        blank one (or None) is refused rather than read as your own memory."""
        return Geniffy(self._api_key, base_url=self._base_url, timeout=self._timeout,
                       max_retries=self.max_retries, http_client=self._http, space=_space_name(space))

    def _request(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            response, error = None, None
            try:
                response = self._http.request(method, self._url + path, headers=self._headers, **kw)
            except httpx.TransportError as e:
                error = e
            if attempt < self.max_retries and _retryable(method, response, error):
                time.sleep(_delay(attempt, response))
                continue
            if error is not None:
                raise APIConnectionError(f"Couldn't reach Geniffy: {type(error).__name__}.") from error
            assert response is not None
            body = _body(response)
            if response.status_code >= 400:
                raise from_response(response.status_code, body, response.headers.get("x-request-id"))
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    def ask(self, question: str) -> Answer:
        """An answer from your memory only. When nothing you added supports one, .answer is None."""
        return Answer.from_json(self._request("POST", "/v1/ask", json={"question": question}))

    def search(self, q: str, *, limit: int = 10, kind: Optional[Kind] = None) -> List[Memory]:
        """The memories that best match q, best first."""
        out = self._request("POST", "/v1/search", json=_search_body(q, limit, kind))
        return [Memory.from_json(m) for m in out.get("memories") or []]

    def context(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                with_sources: bool = True) -> str:
        """The memories that bear on a question, already written out for YOUR prompt:

            prompt = f"{mem.context(question)}

User: {question}"

        This is the ten lines of formatting every integration writes after calling search, so it is
        written here once. It is never empty: when nothing is held it says so in words, because an
        empty block reads to a model as permission to invent."""
        return str(self.context_full(question, limit=limit, kind=kind, with_sources=with_sources)["context"])

    def context_full(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                     with_sources: bool = True) -> Dict[str, Any]:
        """The same, with the memories behind it and whether anything was found."""
        return self._request("POST", "/v1/context", json={"question": question, "limit": limit, "kind": kind,
                                                          "with_sources": with_sources})

    def profile(self, subject: Optional[str] = None) -> Dict[str, Any]:
        """What stays true about someone, and what is going on with them now."""
        return self._request("GET", "/v1/profile", params={"subject": subject} if subject else None)

    def brief(self, subject: Optional[str] = None, *, limit: int = 60) -> Dict[str, Any]:
        """What to read before dealing with someone."""
        q: Dict[str, Any] = {"limit": limit}
        if subject:
            q["subject"] = subject
        return self._request("GET", "/v1/brief", params=q)

    def graph(self) -> Dict[str, Any]:
        """What the memory holds and what connects to what. Every line has a memory behind it."""
        return self._request("GET", "/v1/graph")

    def me(self) -> Dict[str, Any]:
        """Whose key this is, and which space this client is reading."""
        return self._request("GET", "/v1/me")

    def spaces(self) -> List[Dict[str, Any]]:
        """Which of your users have memory, busiest first."""
        return list(self._request("GET", "/v1/spaces").get("spaces") or [])

    def forget_space(self, space: Union[str, int]) -> Dict[str, Any]:
        """Everything one of your users ever said, gone: facts, sources, all of it. The call to make
        when they ask to be forgotten. It cannot be undone."""
        return self._request("DELETE", f"/v1/spaces/{quote(_space_name(space, 'forget_space()'), safe='')}")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Geniffy":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Memories:
    def __init__(self, client: Geniffy):
        self._c = client

    def add(self, text: Optional[str] = None, *, url: Optional[str] = None,
            messages: Optional[List[Dict[str, Any]]] = None, title: Optional[str] = None,
            said_at: SaidAt = None, external_id: Optional[str] = None) -> Source:
        """Add a note (text), a web page (url=...) that Geniffy reads once, or a conversation
        (messages=[{"role": ..., "content": ...}]) as your framework already holds it: content as a
        string, or as Anthropic's blocks or OpenAI's parts, or parts as Gemini holds them. Only text is
        kept, system and developer messages are skipped, and who said what is kept, so the user's words
        become facts about the user. Learning takes a moment: see sources.wait().

        said_at: when a note or conversation from the past was said (a datetime, a date, or an ISO 8601
        string), so what it teaches is dated by it. Left out, now.

        external_id: your own id for it (a ticket's, a document's, a conversation's). Send again under the
        same id and that source is updated rather than added twice: only what changed is learned, and
        what was removed is taken back. Find or delete it by the same id with sources.get / delete."""
        body = _add_body(text, url, messages, title, said_at, external_id)
        return Source.from_json(self._c._request("POST", "/v1/memories", json=body)["source"])

    def add_many(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Up to a hundred at once. One bad item does not take the rest down: every item comes back
        in the order you sent it, with either its source or why it was refused."""
        return self._c._request("POST", "/v1/memories/batch", json={"items": list(items)})

    def correct(self, memory_id: int, text: str) -> Dict[str, Any]:
        """Say what we got wrong, and what is right. The wrong memory is marked and never used
        again; what is right is learned as a new memory with its own source, so a correction leaves
        a trail rather than quietly overwriting."""
        return self._c._request("PATCH", f"/v1/memories/{int(memory_id)}", json={"text": text})

    def add_file(self, file: FileInput, *, filename: Optional[str] = None, title: Optional[str] = None,
                 external_id: Optional[str] = None) -> Source:
        """Add a PDF or Word (.docx) file: a path, bytes, or a file opened with 'rb'. Under an external_id,
        sending a new version updates the source that id names."""
        files, data = _multipart(file, filename, title, external_id)
        return Source.from_json(self._c._request("POST", "/v1/memories/file", files=files, data=data)["source"])

    def list(self, *, kind: Optional[Kind] = None, limit: int = 50, cursor: int = 0) -> MemoryPage:
        """One page of memories, newest first."""
        return MemoryPage.from_json(self._c._request("GET", "/v1/memories", params=_page_params(kind, limit, cursor)))

    def iter(self, *, kind: Optional[Kind] = None, page_size: int = 100) -> Iterator[Memory]:
        """Every memory, newest first, a page at a time."""
        cursor: Optional[int] = 0
        while cursor is not None:
            page = self.list(kind=kind, limit=page_size, cursor=cursor)
            yield from page.memories
            cursor = page.next

    def get(self, memory_id: int) -> MemoryDetail:
        """A memory, the sentence it came from (.memory.quote), and the values it held before."""
        return MemoryDetail.from_json(self._c._request("GET", f"/v1/memories/{int(memory_id)}"))

    def delete(self, memory_id: int) -> None:
        """Forget a memory for good."""
        self._c._request("DELETE", f"/v1/memories/{int(memory_id)}")


def _key_body(name: Optional[str], rpm: Optional[int]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if name:
        body["name"] = name
    if rpm is not None:
        body["rpm"] = int(rpm)
    return body


class Keys:
    """Keys limited to one of your users, on a client bound to that user: client.space(user_id).keys. Such a key
    reads and writes that user's memory and nothing else, so it is safe to hand to their own app or device."""

    def __init__(self, client: Geniffy):
        self._c = client

    def create(self, *, name: Optional[str] = None, rpm: Optional[int] = None) -> Key:
        """A new key limited to this client's user. `.key` is shown once; keep it where their app can read it.
        rpm: requests a minute it may make (up to 600, the default)."""
        return Key.from_json(self._c._request("POST", "/v1/keys", json=_key_body(name, rpm)))

    def list(self) -> List[Key]:
        """The keys limited to this client's user that still work."""
        return [Key.from_json(k) for k in self._c._request("GET", "/v1/keys").get("keys") or []]

    def revoke(self, key_id: int) -> None:
        """One of this user's keys stops at once."""
        self._c._request("DELETE", f"/v1/keys/{int(key_id)}")


class AsyncKeys:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def create(self, *, name: Optional[str] = None, rpm: Optional[int] = None) -> Key:
        return Key.from_json(await self._c._request("POST", "/v1/keys", json=_key_body(name, rpm)))

    async def list(self) -> List[Key]:
        return [Key.from_json(k) for k in (await self._c._request("GET", "/v1/keys")).get("keys") or []]

    async def revoke(self, key_id: int) -> None:
        await self._c._request("DELETE", f"/v1/keys/{int(key_id)}")


class Sources:
    def __init__(self, client: Geniffy):
        self._c = client

    def list(self, *, limit: int = 100, cursor: int = 0) -> SourcePage:
        return SourcePage.from_json(self._c._request("GET", "/v1/sources", params={"limit": limit, "cursor": cursor}))

    def get(self, source_id: Optional[str] = None, *, external_id: Optional[str] = None) -> Source:
        """A source, by its id or by the external_id you added it under (NotFoundError if none)."""
        _one_source(source_id, external_id)
        if external_id is None:
            return Source.from_json(self._c._request("GET", f"/v1/sources/{source_id}")["source"])
        found = self._c._request("GET", "/v1/sources", params={"external_id": str(external_id)}).get("sources") or []
        if not found:
            raise NotFoundError(_NO_EXTERNAL, status=404, code="not_found")
        return Source.from_json(found[0])

    def delete(self, source_id: Optional[str] = None, *, external_id: Optional[str] = None) -> None:
        """Delete a source and every memory learned only from it, by its id or by the external_id you
        added it under: the call for a record your app deleted."""
        _one_source(source_id, external_id)
        if external_id is None:
            self._c._request("DELETE", f"/v1/sources/{source_id}")
        else:
            self._c._request("DELETE", "/v1/sources", params={"external_id": str(external_id)})

    def wait(self, source_id: str, *, timeout: float = 120.0, interval: float = 2.0) -> Source:
        """Wait until Geniffy has learned from a source (or could not), then return it. Geniffy holds the
        call open until the source is done, so this returns the moment it is, usually in one call."""
        deadline = time.monotonic() + timeout
        while True:
            asked = time.monotonic()
            src = Source.from_json(self._c._request("GET", f"/v1/sources/{source_id}",
                                                    params={"wait": _hold(self._c._http, deadline - asked)})["source"])
            now = time.monotonic()
            if src.done or now >= deadline:
                return src
            if now - asked < 1.0:       # answered at once rather than held: don't spin
                time.sleep(min(interval, deadline - now))


# ── asyncio ───────────────────────────────────────────────────────────────────
class AsyncGeniffy:
    """The same client for asyncio: every method is awaited."""

    def __init__(self, api_key: Optional[str] = None, *, base_url: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 2, http_client: Optional[httpx.AsyncClient] = None,
                 space: Union[str, int, None] = None):
        _, url, headers = _settings(api_key, base_url)
        self.max_retries = max(0, int(max_retries))
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self.space_id = "" if space is None else _space_name(space, "space=")
        if self.space_id:
            headers = {**headers, "X-Geniffy-Space": self.space_id}
        self._url, self._headers = url, headers
        self._api_key, self._base_url, self._timeout = api_key, base_url, timeout
        self.memories = AsyncMemories(self)
        self.sources = AsyncSources(self)
        self.keys = AsyncKeys(self)

    def space(self, space: Union[str, int]) -> "AsyncGeniffy":
        """The same client, pointed at one of YOUR users. Shares this client's connection pool. A blank
        space (or None) is refused rather than read as your own memory."""
        return AsyncGeniffy(self._api_key, base_url=self._base_url, timeout=self._timeout,
                            max_retries=self.max_retries, http_client=self._http, space=_space_name(space))

    async def _request(self, method: str, path: str, **kw: Any) -> Dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            response, error = None, None
            try:
                response = await self._http.request(method, self._url + path, headers=self._headers, **kw)
            except httpx.TransportError as e:
                error = e
            if attempt < self.max_retries and _retryable(method, response, error):
                await asyncio.sleep(_delay(attempt, response))
                continue
            if error is not None:
                raise APIConnectionError(f"Couldn't reach Geniffy: {type(error).__name__}.") from error
            assert response is not None
            body = _body(response)
            if response.status_code >= 400:
                raise from_response(response.status_code, body, response.headers.get("x-request-id"))
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    async def ask(self, question: str) -> Answer:
        return Answer.from_json(await self._request("POST", "/v1/ask", json={"question": question}))

    async def search(self, q: str, *, limit: int = 10, kind: Optional[Kind] = None) -> List[Memory]:
        out = await self._request("POST", "/v1/search", json=_search_body(q, limit, kind))
        return [Memory.from_json(m) for m in out.get("memories") or []]

    # The async client lacked these until 5 Oct 2026, context() among them: the one call most apps want,
    # in the client most Python AI apps use (FastAPI, async agents). Each mirrors the sync method above.
    async def context(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                      with_sources: bool = True) -> str:
        """The memories that bear on a question, written out for your prompt; never empty."""
        out = await self.context_full(question, limit=limit, kind=kind, with_sources=with_sources)
        return str(out["context"])

    async def context_full(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                           with_sources: bool = True) -> Dict[str, Any]:
        """The same, with the memories behind it and whether anything was found."""
        return await self._request("POST", "/v1/context", json={"question": question, "limit": limit, "kind": kind,
                                                                "with_sources": with_sources})

    async def profile(self, subject: Optional[str] = None) -> Dict[str, Any]:
        """What stays true about someone, and what is going on with them now."""
        return await self._request("GET", "/v1/profile", params={"subject": subject} if subject else None)

    async def brief(self, subject: Optional[str] = None, *, limit: int = 60) -> Dict[str, Any]:
        """What to read before dealing with someone."""
        q: Dict[str, Any] = {"limit": limit}
        if subject:
            q["subject"] = subject
        return await self._request("GET", "/v1/brief", params=q)

    async def graph(self) -> Dict[str, Any]:
        """What the memory holds and what connects to what."""
        return await self._request("GET", "/v1/graph")

    async def me(self) -> Dict[str, Any]:
        return await self._request("GET", "/v1/me")

    async def spaces(self) -> List[Dict[str, Any]]:
        """Which of your users have memory, busiest first."""
        return list((await self._request("GET", "/v1/spaces")).get("spaces") or [])

    async def forget_space(self, space: Union[str, int]) -> Dict[str, Any]:
        """Everything one of your users ever said, gone. It cannot be undone."""
        return await self._request("DELETE", f"/v1/spaces/{quote(_space_name(space, 'forget_space()'), safe='')}")

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncGeniffy":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class AsyncMemories:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def add(self, text: Optional[str] = None, *, url: Optional[str] = None,
                  messages: Optional[List[Dict[str, Any]]] = None, title: Optional[str] = None,
                  said_at: SaidAt = None, external_id: Optional[str] = None) -> Source:
        body = _add_body(text, url, messages, title, said_at, external_id)
        out = await self._c._request("POST", "/v1/memories", json=body)
        return Source.from_json(out["source"])

    async def add_many(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._c._request("POST", "/v1/memories/batch", json={"items": list(items)})

    async def correct(self, memory_id: int, text: str) -> Dict[str, Any]:
        return await self._c._request("PATCH", f"/v1/memories/{int(memory_id)}", json={"text": text})

    async def add_file(self, file: FileInput, *, filename: Optional[str] = None, title: Optional[str] = None,
                       external_id: Optional[str] = None) -> Source:
        files, data = _multipart(file, filename, title, external_id)
        return Source.from_json((await self._c._request("POST", "/v1/memories/file", files=files, data=data))["source"])

    async def list(self, *, kind: Optional[Kind] = None, limit: int = 50, cursor: int = 0) -> MemoryPage:
        return MemoryPage.from_json(await self._c._request("GET", "/v1/memories", params=_page_params(kind, limit, cursor)))

    async def iter(self, *, kind: Optional[Kind] = None, page_size: int = 100) -> AsyncIterator[Memory]:
        cursor: Optional[int] = 0
        while cursor is not None:
            page = await self.list(kind=kind, limit=page_size, cursor=cursor)
            for m in page.memories:
                yield m
            cursor = page.next

    async def get(self, memory_id: int) -> MemoryDetail:
        return MemoryDetail.from_json(await self._c._request("GET", f"/v1/memories/{int(memory_id)}"))

    async def delete(self, memory_id: int) -> None:
        await self._c._request("DELETE", f"/v1/memories/{int(memory_id)}")


class AsyncSources:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def list(self, *, limit: int = 100, cursor: int = 0) -> SourcePage:
        return SourcePage.from_json(await self._c._request("GET", "/v1/sources", params={"limit": limit, "cursor": cursor}))

    async def get(self, source_id: Optional[str] = None, *, external_id: Optional[str] = None) -> Source:
        _one_source(source_id, external_id)
        if external_id is None:
            return Source.from_json((await self._c._request("GET", f"/v1/sources/{source_id}"))["source"])
        found = (await self._c._request("GET", "/v1/sources", params={"external_id": str(external_id)})).get("sources") or []
        if not found:
            raise NotFoundError(_NO_EXTERNAL, status=404, code="not_found")
        return Source.from_json(found[0])

    async def delete(self, source_id: Optional[str] = None, *, external_id: Optional[str] = None) -> None:
        _one_source(source_id, external_id)
        if external_id is None:
            await self._c._request("DELETE", f"/v1/sources/{source_id}")
        else:
            await self._c._request("DELETE", "/v1/sources", params={"external_id": str(external_id)})

    async def wait(self, source_id: str, *, timeout: float = 120.0, interval: float = 2.0) -> Source:
        deadline = time.monotonic() + timeout
        while True:
            asked = time.monotonic()
            src = Source.from_json((await self._c._request("GET", f"/v1/sources/{source_id}",
                                                           params={"wait": _hold(self._c._http, deadline - asked)}))["source"])
            now = time.monotonic()
            if src.done or now >= deadline:
                return src
            if now - asked < 1.0:
                await asyncio.sleep(min(interval, deadline - now))
