"""The Geniffy client: Geniffy (blocking) and AsyncGeniffy (asyncio), over https://api.geniffy.com/v1.

    from geniffy import Geniffy
    g = Geniffy()                                   # reads GENIFFY_API_KEY
    g.memories.add("Priya Nair signs the Lumen renewal, and it comes up in March.")
    print(g.ask("Who signs the Lumen renewal?").answer)

Retries: reads, deletes and putting a file (the same text again changes nothing) are retried on network errors,
408, 429 and 5xx; adding and moving are retried only when the request never reached Geniffy (a failed
connection) or on 429, so a retry never saves a note twice.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import random
import re
import time
from datetime import date, datetime
from typing import IO, Any, AsyncIterator, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union
from urllib.parse import quote

import httpx

from ._errors import APIConnectionError, GeniffyError, NotFoundError, from_response
from ._types import (Answer, File, FileInfo, FilePage, Key, Kind, Memory, MemoryDetail, MemoryPage, Source,
                     SourcePage)

__version__ = "0.2.0"
DEFAULT_BASE_URL = "https://api.geniffy.com"
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
FileInput = Union[str, "os.PathLike[str]", bytes, IO[bytes]]
# A source's labels: your own name/value pairs. A filter by them: every name must match, and a list of values is
# any one of them ({"channel": ["email", "chat"]}).
Labels = Dict[str, str]
LabelFilter = Dict[str, Union[str, List[str]]]


_INTEGRATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._+-]{0,31}$")


def _settings(api_key: Optional[str], base_url: Optional[str],
              integration: Optional[str] = None) -> Tuple[str, str, Dict[str, str]]:
    key = (api_key or os.environ.get("GENIFFY_API_KEY") or "").strip()
    if not key:
        raise GeniffyError("No API key. Pass api_key=... or set GENIFFY_API_KEY. "
                           "Make one in the Geniffy app under Connect, API keys.")
    url = (base_url or os.environ.get("GENIFFY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    agent = f"geniffy-python/{__version__}"
    if integration:
        if not _INTEGRATION.match(integration):
            raise ValueError("integration is a name and a version, such as \"langchain-geniffy/0.1.0\".")
        agent = f"{agent} {integration}"          # the Requests page shows which integration made each call
    headers = {"Authorization": f"Bearer {key}", "User-Agent": agent, "Accept": "application/json"}
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
              title: Optional[str], said_at: SaidAt, external_id: Optional[str] = None,
              labels: Optional[Labels] = None) -> Dict[str, Any]:
    body = _note_or_link(text, url, title) if messages is None else {"messages": list(messages)}
    if external_id is not None:
        body["external_id"] = str(external_id)
    if labels is not None:
        body["labels"] = dict(labels)
    if messages is not None and title:
        body["title"] = title
    if said_at is not None:
        if url is not None:
            raise ValueError("said_at goes with a note or a conversation: a web page is read as it is today.")
        # a datetime with no time zone is read as UTC by the API
        body["said_at"] = said_at.isoformat() if isinstance(said_at, (datetime, date)) else str(said_at)
    return body


def _multipart(file: FileInput, filename: Optional[str], title: Optional[str], external_id: Optional[str] = None,
               labels: Optional[Labels] = None):
    name, content = _file(file, filename)
    files = {"file": (name, content, mimetypes.guess_type(name)[0] or "application/octet-stream")}
    data = {k: str(v) for k, v in (("title", title), ("external_id", external_id)) if v}
    if labels is not None:
        data["labels"] = json.dumps(dict(labels))
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


def _label_params(labels: Optional[LabelFilter]) -> Dict[str, Any]:
    """A filter in a query string: label=name:value, once for each value."""
    if not labels:
        return {}
    return {"label": [f"{k}:{v}" for k, vs in labels.items() for v in (vs if isinstance(vs, list) else [vs])]}


def _page_params(kind: Optional[Kind], limit: int, cursor: int, labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": limit, "cursor": cursor, **_label_params(labels)}
    if kind:
        params["kind"] = kind
    return params


def _with_labels(body: Dict[str, Any], labels: Optional[LabelFilter]) -> Dict[str, Any]:
    if labels:
        body["labels"] = dict(labels)
    return body


def _search_body(q: str, limit: int, kind: Optional[Kind], labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"q": q, "limit": limit}
    if kind:
        body["kind"] = kind
    return _with_labels(body, labels)


def _brief_params(subject: Optional[str], limit: int, labels: Optional[LabelFilter]) -> Dict[str, Any]:
    q: Dict[str, Any] = {"limit": limit, **_label_params(labels)}
    if subject:
        q["subject"] = subject
    return q


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
                 space: Union[str, int, None] = None, integration: Optional[str] = None):
        _, url, headers = _settings(api_key, base_url, integration)
        self.max_retries = max(0, int(max_retries))
        self._http = http_client or httpx.Client(timeout=timeout)
        self.space_id = "" if space is None else _space_name(space, "space=")
        # One header carries the space, so every call a bound client makes is scoped without a
        # single method taking it, and a space can never be lost by forgetting an argument.
        if self.space_id:
            headers = {**headers, "X-Geniffy-Space": self.space_id}
        self._url, self._headers = url, headers
        self._api_key, self._base_url, self._timeout, self._integration = api_key, base_url, timeout, integration
        self.memories = Memories(self)
        self.sources = Sources(self)
        self.files = Files(self)
        self.keys = Keys(self)
        self.sections = Sections(self)

    def space(self, space: Union[str, int]) -> "Geniffy":
        """The same client, pointed at one of YOUR users. It shares this client's connection pool,
        so one per request is cheap. `space` is your own name for that user, the id you already give
        them: up to 128 letters, digits, dots, dashes or underscores, so an id rather than an email. A
        blank one (or None) is refused rather than read as your own memory."""
        return Geniffy(self._api_key, base_url=self._base_url, timeout=self._timeout,
                       max_retries=self.max_retries, http_client=self._http, space=_space_name(space),
                       integration=self._integration)

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

    def ask(self, question: str, *, labels: Optional[LabelFilter] = None) -> Answer:
        """An answer from your memory only. When nothing you added supports one, .answer is None. With labels,
        only from what the sources carrying them said."""
        return Answer.from_json(self._request("POST", "/v1/ask", json=_with_labels({"question": question}, labels)))

    def search(self, q: str, *, limit: int = 10, kind: Optional[Kind] = None,
               labels: Optional[LabelFilter] = None) -> List[Memory]:
        """The memories that best match q, best first. labels={"channel": "email"} keeps to the sources
        carrying them: every name must match, and a list of values is any one of them."""
        out = self._request("POST", "/v1/search", json=_search_body(q, limit, kind, labels))
        return [Memory.from_json(m) for m in out.get("memories") or []]

    def context(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                with_sources: bool = True, labels: Optional[LabelFilter] = None) -> str:
        """The memories that bear on a question, already written out for YOUR prompt:

            prompt = f"{mem.context(question)}

User: {question}"

        This is the ten lines of formatting every integration writes after calling search, so it is
        written here once. It is never empty: when nothing is held it says so in words, because an
        empty block reads to a model as permission to invent. With labels, only what the sources carrying
        them said."""
        return str(self.context_full(question, limit=limit, kind=kind, with_sources=with_sources,
                                     labels=labels)["context"])

    def context_full(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                     with_sources: bool = True, labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
        """The same, with the memories behind it and whether anything was found."""
        return self._request("POST", "/v1/context", json=_with_labels(
            {"question": question, "limit": limit, "kind": kind, "with_sources": with_sources}, labels))

    def profile(self, subject: Optional[str] = None) -> Dict[str, Any]:
        """What stays true about someone, and what is going on with them now."""
        return self._request("GET", "/v1/profile", params={"subject": subject} if subject else None)

    def brief(self, subject: Optional[str] = None, *, limit: int = 60,
              labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
        """What to read before dealing with someone."""
        return self._request("GET", "/v1/brief", params=_brief_params(subject, limit, labels))

    def graph(self) -> Dict[str, Any]:
        """What the memory holds and what connects to what. Every line has a memory behind it."""
        return self._request("GET", "/v1/graph")

    def export(self) -> Dict[str, Any]:
        """Everything held, as the user's own copy: every memory, current or not, with its status and the
        sentence it came from, and every source. On client.space(id), for a user who asks what you hold."""
        return self._request("GET", "/v1/export")

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
            said_at: SaidAt = None, external_id: Optional[str] = None, labels: Optional[Labels] = None) -> Source:
        """Add a note (text), a web page (url=...) that Geniffy reads once, or a conversation
        (messages=[{"role": ..., "content": ...}]) as your framework already holds it: content as a
        string, or as Anthropic's blocks or OpenAI's parts, or parts as Gemini holds them. Only text is
        kept, system and developer messages are skipped, and who said what is kept, so the user's words
        become facts about the user. Learning takes a moment: see sources.wait().

        said_at: when a note or conversation from the past was said (a datetime, a date, or an ISO 8601
        string), so what it teaches is dated by it. Left out, now.

        external_id: your own id for it (a ticket's, a document's, a conversation's). Send again under the
        same id and that source is updated rather than added twice: only what changed is learned, and
        what was removed is taken back. Find or delete it by the same id with sources.get / delete.

        labels: up to 20 of your own name/value pairs ({"channel": "email", "project": "apollo"}) to filter
        search, context, ask, list and brief by. Sent again under the same external_id they replace the old
        ones, with nothing learned again; left out, they are kept; {} clears them."""
        body = _add_body(text, url, messages, title, said_at, external_id, labels)
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
                 external_id: Optional[str] = None, labels: Optional[Labels] = None) -> Source:
        """Add a file: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), or text (.txt, .md, .csv, .html),
        as a path, bytes, or a file opened with 'rb'. Under an external_id,
        sending a new version updates the source that id names."""
        files, data = _multipart(file, filename, title, external_id, labels)
        return Source.from_json(self._c._request("POST", "/v1/memories/file", files=files, data=data)["source"])

    def list(self, *, kind: Optional[Kind] = None, limit: int = 50, cursor: int = 0,
             labels: Optional[LabelFilter] = None) -> MemoryPage:
        """One page of memories, newest first; with labels, only what the sources carrying them said."""
        return MemoryPage.from_json(self._c._request("GET", "/v1/memories",
                                                     params=_page_params(kind, limit, cursor, labels)))

    def iter(self, *, kind: Optional[Kind] = None, page_size: int = 100,
             labels: Optional[LabelFilter] = None) -> Iterator[Memory]:
        """Every memory, newest first, a page at a time."""
        cursor: Optional[int] = 0
        while cursor is not None:
            page = self.list(kind=kind, limit=page_size, cursor=cursor, labels=labels)
            yield from page.memories
            cursor = page.next

    def get(self, memory_id: int) -> MemoryDetail:
        """A memory, the sentence it came from (.memory.quote), and the values it held before."""
        return MemoryDetail.from_json(self._c._request("GET", f"/v1/memories/{int(memory_id)}"))

    def delete(self, memory_id: int) -> None:
        """Forget a memory for good."""
        self._c._request("DELETE", f"/v1/memories/{int(memory_id)}")


def _kept_ids(keep: Iterable[str]) -> Set[str]:
    """The external_ids a delete by labels leaves. One id on its own is refused: as a string it would read as
    its letters, and keep nothing."""
    if isinstance(keep, (str, bytes)):
        raise TypeError("keep is a collection of external_ids, such as a set, not one id.")
    return {str(k) for k in keep}


def _key_body(name: Optional[str], rpm: Optional[int], expires_at: SaidAt = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if name:
        body["name"] = name
    if rpm is not None:
        body["rpm"] = int(rpm)
    if expires_at is not None:
        # a datetime with no time zone is read as UTC; a date means the key works through that day
        body["expires_at"] = expires_at.isoformat() if isinstance(expires_at, (datetime, date)) else str(expires_at)
    return body


class Keys:
    """Keys limited to one of your users, on a client bound to that user: client.space(user_id).keys. Such a key
    reads and writes that user's memory and nothing else, so it is safe to hand to their own app or device."""

    def __init__(self, client: Geniffy):
        self._c = client

    def create(self, *, name: Optional[str] = None, rpm: Optional[int] = None, expires_at: SaidAt = None) -> Key:
        """A new key limited to this client's user. `.key` is shown once; keep it where their app can read it.
        rpm: requests a minute it may make (up to 600, the default). expires_at: when it stops working by itself
        (a datetime, or a date it works through); left out, it works until revoked."""
        return Key.from_json(self._c._request("POST", "/v1/keys", json=_key_body(name, rpm, expires_at)))

    def list(self) -> List[Key]:
        """The keys limited to this client's user that still work."""
        return [Key.from_json(k) for k in self._c._request("GET", "/v1/keys").get("keys") or []]

    def revoke(self, key_id: int) -> None:
        """One of this user's keys stops at once."""
        self._c._request("DELETE", f"/v1/keys/{int(key_id)}")


def _section_body(name: str, description: str, keywords: Optional[List[str]], topics: Optional[List[str]]) -> Dict[str, Any]:
    return {"name": name, "description": description, "keywords": list(keywords or []), "topics": list(topics or [])}


class Sections:
    """The sections profiles are grouped into. On the plain client, for every one of your users; on a client
    bound to one user (client.space(id).sections), for that user only. A memory goes in a section when one of
    its keywords appears in it, or its topic is one of the section's."""

    def __init__(self, client: Geniffy):
        self._c = client

    def list(self) -> List[Dict[str, Any]]:
        """The sections, the app's own first, then the built-in ones; each says whom it applies to."""
        return list(self._c._request("GET", "/v1/profile/sections").get("sections") or [])

    def create(self, name: str, *, keywords: Optional[List[str]] = None, topics: Optional[List[str]] = None,
               description: str = "") -> Dict[str, Any]:
        """Add a section (or, under a name it already has, update it). Profiles regroup within a minute or so."""
        return self._c._request("POST", "/v1/profile/sections", json=_section_body(name, description, keywords, topics))

    def delete(self, section_id: int) -> None:
        self._c._request("DELETE", f"/v1/profile/sections/{int(section_id)}")


class AsyncSections:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def list(self) -> List[Dict[str, Any]]:
        return list((await self._c._request("GET", "/v1/profile/sections")).get("sections") or [])

    async def create(self, name: str, *, keywords: Optional[List[str]] = None, topics: Optional[List[str]] = None,
                     description: str = "") -> Dict[str, Any]:
        return await self._c._request("POST", "/v1/profile/sections",
                                      json=_section_body(name, description, keywords, topics))

    async def delete(self, section_id: int) -> None:
        await self._c._request("DELETE", f"/v1/profile/sections/{int(section_id)}")


class AsyncKeys:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def create(self, *, name: Optional[str] = None, rpm: Optional[int] = None,
                     expires_at: SaidAt = None) -> Key:
        return Key.from_json(await self._c._request("POST", "/v1/keys", json=_key_body(name, rpm, expires_at)))

    async def list(self) -> List[Key]:
        return [Key.from_json(k) for k in (await self._c._request("GET", "/v1/keys")).get("keys") or []]

    async def revoke(self, key_id: int) -> None:
        await self._c._request("DELETE", f"/v1/keys/{int(key_id)}")


def _need_labels(labels: Optional[LabelFilter]) -> None:
    if not labels:
        raise ValueError("Name the labels whose sources to delete, such as {\"channel\": \"gmail\"}.")


# Each call deletes up to 100 sources (or files) and says whether there are more; this many calls is the most one
# delete_labelled() or files.delete_prefix() makes, so a filter that somehow keeps matching cannot loop for ever.
_LABELLED_CALLS = 1000


class Sources:
    def __init__(self, client: Geniffy):
        self._c = client

    def list(self, *, limit: int = 100, cursor: int = 0, labels: Optional[LabelFilter] = None) -> SourcePage:
        """What was added, newest first; with labels, only the sources carrying them."""
        params = {"limit": limit, "cursor": cursor, **_label_params(labels)}
        return SourcePage.from_json(self._c._request("GET", "/v1/sources", params=params))

    def delete_labelled(self, labels: LabelFilter, *, keep: Optional[Iterable[str]] = None) -> int:
        """Delete every source carrying these labels, and every memory learned only from them: the call when
        your user disconnects a data source whose things you added under its label. Returns how many.

        keep: the external_ids to leave, for the end of a sync that read everything: every other source with
        these labels, such as what is gone from the data source, is deleted."""
        _need_labels(labels)
        if keep is not None:
            kept, gone, cursor = _kept_ids(keep), [], 0
            while cursor is not None:                 # the whole list first: deleting moves the pages
                page = self.list(labels=labels, cursor=cursor)
                gone += [s.id for s in page.sources if s.external_id not in kept]
                cursor = page.next
            deleted = 0
            for source_id in gone:
                try:
                    self.delete(source_id)
                    deleted += 1
                except NotFoundError:                 # deleted meanwhile: gone either way
                    pass
            return deleted
        total = 0
        for _ in range(_LABELLED_CALLS):
            out = self._c._request("DELETE", "/v1/sources", params=_label_params(labels))
            total += int(out.get("sources_deleted") or 0)
            if not out.get("more"):
                break
        return total

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


def _file_body(path: str, text: str, labels: Optional[Labels]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"path": path, "text": text}
    if labels is not None:
        body["labels"] = dict(labels)
    return body


class Files:
    """Files kept exactly as they were written, each under a path such as /memories/notes.md: what an agent keeps
    for itself, like the notes Claude's memory tool writes (see geniffy.claude). Geniffy also learns from each file
    like a note, so context() and ask() recall what it says, and deleting it takes back what only it taught."""

    def __init__(self, client: Geniffy):
        self._c = client

    def put(self, path: str, text: str, *, labels: Optional[Labels] = None) -> FileInfo:
        """Create a file, or replace its text. The text is kept exactly as sent, whitespace and line endings
        included, and learned like a note titled by its path; a replace learns only what changed. An empty file
        is kept too, with nothing learned from it. labels: as on memories.add."""
        return FileInfo.from_json(self._c._request("PUT", "/v1/files", json=_file_body(path, text, labels)))

    def get(self, path: str) -> File:
        """A file and its exact text (NotFoundError if no file has that path)."""
        return File.from_json(self._c._request("GET", "/v1/files", params={"path": path}))

    def list(self, prefix: str = "/", *, limit: int = 100, cursor: int = 0) -> FilePage:
        """The files whose paths start with prefix, by path, without their text ("/" for all of them). Up to 200
        a page; .next is the cursor of the page after, None at the end."""
        params = {"prefix": prefix, "limit": limit, "cursor": cursor}
        return FilePage.from_json(self._c._request("GET", "/v1/files", params=params))

    def delete(self, path: str) -> None:
        """Delete a file, its text and what only it taught (NotFoundError if no file has that path)."""
        self._c._request("DELETE", "/v1/files", params={"path": path})

    def delete_prefix(self, prefix: str) -> int:
        """Delete every file whose path starts with prefix ("/memories/" for everything in that folder), with
        what only they taught. Returns how many."""
        total = 0
        for _ in range(_LABELLED_CALLS):
            out = self._c._request("DELETE", "/v1/files", params={"prefix": prefix})
            total += int(out.get("deleted") or 0)
            if not out.get("more"):
                break
        return total

    def move(self, from_path: str, to_path: str) -> int:
        """Move a file, or, when from_path is a folder, every file in it, keeping the text and what was learned.
        NotFoundError when there is nothing to move; BadRequestError with code "conflict" when a destination is
        already taken, and then nothing moves. Returns how many files moved."""
        out = self._c._request("POST", "/v1/files/move", json={"from": from_path, "to": to_path})
        return int(out.get("moved") or 0)


# ── asyncio ───────────────────────────────────────────────────────────────────
class AsyncGeniffy:
    """The same client for asyncio: every method is awaited."""

    def __init__(self, api_key: Optional[str] = None, *, base_url: Optional[str] = None, timeout: float = 60.0,
                 max_retries: int = 2, http_client: Optional[httpx.AsyncClient] = None,
                 space: Union[str, int, None] = None, integration: Optional[str] = None):
        _, url, headers = _settings(api_key, base_url, integration)
        self.max_retries = max(0, int(max_retries))
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self.space_id = "" if space is None else _space_name(space, "space=")
        if self.space_id:
            headers = {**headers, "X-Geniffy-Space": self.space_id}
        self._url, self._headers = url, headers
        self._api_key, self._base_url, self._timeout, self._integration = api_key, base_url, timeout, integration
        self.memories = AsyncMemories(self)
        self.sources = AsyncSources(self)
        self.files = AsyncFiles(self)
        self.keys = AsyncKeys(self)
        self.sections = AsyncSections(self)

    def space(self, space: Union[str, int]) -> "AsyncGeniffy":
        """The same client, pointed at one of YOUR users. Shares this client's connection pool. A blank
        space (or None) is refused rather than read as your own memory."""
        return AsyncGeniffy(self._api_key, base_url=self._base_url, timeout=self._timeout,
                            max_retries=self.max_retries, http_client=self._http, space=_space_name(space),
                            integration=self._integration)

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

    async def ask(self, question: str, *, labels: Optional[LabelFilter] = None) -> Answer:
        return Answer.from_json(await self._request("POST", "/v1/ask", json=_with_labels({"question": question}, labels)))

    async def search(self, q: str, *, limit: int = 10, kind: Optional[Kind] = None,
                     labels: Optional[LabelFilter] = None) -> List[Memory]:
        out = await self._request("POST", "/v1/search", json=_search_body(q, limit, kind, labels))
        return [Memory.from_json(m) for m in out.get("memories") or []]

    # The async client lacked these until 5 Oct 2026, context() among them: the one call most apps want,
    # in the client most Python AI apps use (FastAPI, async agents). Each mirrors the sync method above.
    async def context(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                      with_sources: bool = True, labels: Optional[LabelFilter] = None) -> str:
        """The memories that bear on a question, written out for your prompt; never empty."""
        out = await self.context_full(question, limit=limit, kind=kind, with_sources=with_sources, labels=labels)
        return str(out["context"])

    async def context_full(self, question: str, *, limit: int = 12, kind: Optional[Kind] = None,
                           with_sources: bool = True, labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
        """The same, with the memories behind it and whether anything was found."""
        return await self._request("POST", "/v1/context", json=_with_labels(
            {"question": question, "limit": limit, "kind": kind, "with_sources": with_sources}, labels))

    async def profile(self, subject: Optional[str] = None) -> Dict[str, Any]:
        """What stays true about someone, and what is going on with them now."""
        return await self._request("GET", "/v1/profile", params={"subject": subject} if subject else None)

    async def brief(self, subject: Optional[str] = None, *, limit: int = 60,
                    labels: Optional[LabelFilter] = None) -> Dict[str, Any]:
        """What to read before dealing with someone."""
        return await self._request("GET", "/v1/brief", params=_brief_params(subject, limit, labels))

    async def graph(self) -> Dict[str, Any]:
        """What the memory holds and what connects to what."""
        return await self._request("GET", "/v1/graph")

    async def export(self) -> Dict[str, Any]:
        """Everything held, as the user's own copy."""
        return await self._request("GET", "/v1/export")

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
                  said_at: SaidAt = None, external_id: Optional[str] = None,
                  labels: Optional[Labels] = None) -> Source:
        body = _add_body(text, url, messages, title, said_at, external_id, labels)
        out = await self._c._request("POST", "/v1/memories", json=body)
        return Source.from_json(out["source"])

    async def add_many(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return await self._c._request("POST", "/v1/memories/batch", json={"items": list(items)})

    async def correct(self, memory_id: int, text: str) -> Dict[str, Any]:
        return await self._c._request("PATCH", f"/v1/memories/{int(memory_id)}", json={"text": text})

    async def add_file(self, file: FileInput, *, filename: Optional[str] = None, title: Optional[str] = None,
                       external_id: Optional[str] = None, labels: Optional[Labels] = None) -> Source:
        files, data = _multipart(file, filename, title, external_id, labels)
        return Source.from_json((await self._c._request("POST", "/v1/memories/file", files=files, data=data))["source"])

    async def list(self, *, kind: Optional[Kind] = None, limit: int = 50, cursor: int = 0,
                   labels: Optional[LabelFilter] = None) -> MemoryPage:
        return MemoryPage.from_json(await self._c._request("GET", "/v1/memories",
                                                           params=_page_params(kind, limit, cursor, labels)))

    async def iter(self, *, kind: Optional[Kind] = None, page_size: int = 100,
                   labels: Optional[LabelFilter] = None) -> AsyncIterator[Memory]:
        cursor: Optional[int] = 0
        while cursor is not None:
            page = await self.list(kind=kind, limit=page_size, cursor=cursor, labels=labels)
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

    async def list(self, *, limit: int = 100, cursor: int = 0, labels: Optional[LabelFilter] = None) -> SourcePage:
        params = {"limit": limit, "cursor": cursor, **_label_params(labels)}
        return SourcePage.from_json(await self._c._request("GET", "/v1/sources", params=params))

    async def delete_labelled(self, labels: LabelFilter, *, keep: Optional[Iterable[str]] = None) -> int:
        _need_labels(labels)
        if keep is not None:
            kept, gone, cursor = _kept_ids(keep), [], 0
            while cursor is not None:
                page = await self.list(labels=labels, cursor=cursor)
                gone += [s.id for s in page.sources if s.external_id not in kept]
                cursor = page.next
            deleted = 0
            for source_id in gone:
                try:
                    await self.delete(source_id)
                    deleted += 1
                except NotFoundError:
                    pass
            return deleted
        total = 0
        for _ in range(_LABELLED_CALLS):
            out = await self._c._request("DELETE", "/v1/sources", params=_label_params(labels))
            total += int(out.get("sources_deleted") or 0)
            if not out.get("more"):
                break
        return total

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


class AsyncFiles:
    def __init__(self, client: AsyncGeniffy):
        self._c = client

    async def put(self, path: str, text: str, *, labels: Optional[Labels] = None) -> FileInfo:
        return FileInfo.from_json(await self._c._request("PUT", "/v1/files", json=_file_body(path, text, labels)))

    async def get(self, path: str) -> File:
        return File.from_json(await self._c._request("GET", "/v1/files", params={"path": path}))

    async def list(self, prefix: str = "/", *, limit: int = 100, cursor: int = 0) -> FilePage:
        params = {"prefix": prefix, "limit": limit, "cursor": cursor}
        return FilePage.from_json(await self._c._request("GET", "/v1/files", params=params))

    async def delete(self, path: str) -> None:
        await self._c._request("DELETE", "/v1/files", params={"path": path})

    async def delete_prefix(self, prefix: str) -> int:
        total = 0
        for _ in range(_LABELLED_CALLS):
            out = await self._c._request("DELETE", "/v1/files", params={"prefix": prefix})
            total += int(out.get("deleted") or 0)
            if not out.get("more"):
                break
        return total

    async def move(self, from_path: str, to_path: str) -> int:
        out = await self._c._request("POST", "/v1/files/move", json={"from": from_path, "to": to_path})
        return int(out.get("moved") or 0)
