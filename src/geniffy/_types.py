"""What the API returns, as plain objects. Unknown fields are kept in `.raw`, so a newer API never breaks
an older client."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

Kind = Literal["all", "people", "plan", "pref", "detail"]


@dataclass
class SourceRef:
    """Where a memory came from: a note, a file or a link you added, or "other"."""
    id: Optional[str]
    kind: str
    title: str
    labels: Dict[str, str] = field(default_factory=dict)   # the labels on that source

    @classmethod
    def from_json(cls, d: Optional[Dict[str, Any]]) -> Optional["SourceRef"]:
        if not d:
            return None
        return cls(id=d.get("id"), kind=str(d.get("kind") or "other"), title=str(d.get("title") or ""),
                   labels=dict(d.get("labels") or {}))


@dataclass
class Memory:
    """One thing your memory knows."""
    id: int
    text: str
    kind: str                          # people, plan, pref or detail
    about: Optional[str] = None
    status: str = "current"            # current, or clash when two memories disagree
    learned_at: Optional[str] = None
    said_at: Optional[str] = None
    source: Optional[SourceRef] = None
    quote: Optional[str] = None        # the sentence it came from (on get())
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Memory":
        return cls(id=int(d["id"]), text=str(d.get("text") or ""), kind=str(d.get("kind") or "detail"),
                   about=d.get("about"), status=str(d.get("status") or "current"), learned_at=d.get("learned_at"),
                   said_at=d.get("said_at"), source=SourceRef.from_json(d.get("source")), quote=d.get("quote"),
                   raw=d)


@dataclass
class MemoryPage:
    memories: List[Memory]
    counts: Dict[str, int]
    total: int
    next: Optional[int] = None

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "MemoryPage":
        return cls(memories=[Memory.from_json(m) for m in d.get("memories") or []], counts=dict(d.get("counts") or {}),
                   total=int(d.get("total") or 0), next=d.get("next"))


@dataclass
class MemoryDetail:
    memory: Memory
    history: List[Memory]

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "MemoryDetail":
        return cls(memory=Memory.from_json(d["memory"]), history=[Memory.from_json(h) for h in d.get("history") or []])


@dataclass
class Source:
    """Something you added: a note, a file or a link, and how learning from it went."""
    id: str
    kind: str                          # note, file or link
    title: str
    status: str                        # reading, learned or failed
    error: Optional[str] = None        # why it failed, in plain words
    facts: Optional[int] = None
    url: Optional[str] = None
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    size_bytes: Optional[int] = None
    added_by: Optional[str] = None
    added_at: Optional[str] = None
    external_id: Optional[str] = None  # your own id for it, when you gave one
    labels: Dict[str, str] = field(default_factory=dict)  # your own name/value pairs on it
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def done(self) -> bool:
        return self.status in ("learned", "failed")

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Source":
        return cls(id=str(d["id"]), kind=str(d.get("kind") or ""), title=str(d.get("title") or ""),
                   status=str(d.get("status") or ""), error=d.get("error"), facts=d.get("facts"), url=d.get("url"),
                   file_name=d.get("file_name"), file_type=d.get("file_type"), size_bytes=d.get("size_bytes"),
                   added_by=d.get("added_by"), added_at=d.get("added_at"), external_id=d.get("external_id"),
                   labels=dict(d.get("labels") or {}), raw=d)


@dataclass
class Key:
    """A key limited to one of your users: it reads and writes their memory and nothing else."""
    id: int
    name: str
    space: str                         # the user it is limited to
    key: Optional[str] = None          # the key itself: only when it is made, never again
    starts_with: Optional[str] = None
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    expires_at: Optional[str] = None   # when it stops working by itself; None: when it is revoked
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Key":
        return cls(id=int(d["id"]), name=str(d.get("name") or ""), space=str(d.get("space") or ""),
                   key=d.get("key"), starts_with=d.get("starts_with"), created_at=d.get("created_at"),
                   last_used_at=d.get("last_used_at"), expires_at=d.get("expires_at"), raw=d)


@dataclass
class SourcePage:
    sources: List[Source]
    total: int
    next: Optional[int] = None

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "SourcePage":
        return cls(sources=[Source.from_json(s) for s in d.get("sources") or []], total=int(d.get("total") or 0),
                   next=d.get("next"))


@dataclass
class FileInfo:
    """A file held under a path, without its text: how long it is, when it last changed and, from put(), whether
    the path was new and the source Geniffy learns it from."""
    path: str
    size: int                          # characters of text
    updated_at: Optional[str] = None
    created: bool = False              # put(): True when nothing was held at the path before
    source: Optional[SourceRef] = None  # put(): the source Geniffy learns it as, a note titled by the path
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "FileInfo":
        return cls(path=str(d.get("path") or ""), size=int(d.get("size") or 0), updated_at=d.get("updated_at"),
                   created=bool(d.get("created")), source=SourceRef.from_json(d.get("source")), raw=d)


@dataclass
class File:
    """A file and its text, exactly as it was put."""
    path: str
    text: str
    size: int                          # characters of text
    updated_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "File":
        text = d.get("text")
        return cls(path=str(d.get("path") or ""), text="" if text is None else str(text),
                   size=int(d.get("size") or 0), updated_at=d.get("updated_at"), raw=d)


@dataclass
class FilePage:
    files: List[FileInfo]              # sorted by path
    total: int
    next: Optional[int] = None

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "FilePage":
        return cls(files=[FileInfo.from_json(f) for f in d.get("files") or []], total=int(d.get("total") or 0),
                   next=d.get("next"))


@dataclass
class Answer:
    """The answer, from your memory only. `answer` is None when nothing you added supports one; `message`
    then says so."""
    question: str
    answer: Optional[str]
    memories: List[Memory]
    message: Optional[str] = None
    clash: bool = False

    def __str__(self) -> str:
        return self.answer if self.answer is not None else (self.message or "")

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Answer":
        return cls(question=str(d.get("question") or ""), answer=d.get("answer"),
                   memories=[Memory.from_json(m) for m in d.get("memories") or []], message=d.get("message"),
                   clash=bool(d.get("clash")))
