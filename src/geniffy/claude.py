"""Claude's memory tool, stored in Geniffy.

Claude's memory tool (type memory_20250818) keeps notes as files under /memories and needs each one back exactly as
it wrote it. GeniffyMemoryTool keeps them as files in a Geniffy memory: the text comes back character for
character, and Geniffy also learns from it like a note, so context() and ask() recall what Claude wrote anywhere
else in your app. Deleting a file takes back what only it taught.

    import anthropic
    from geniffy import Geniffy
    from geniffy.claude import GeniffyMemoryTool

    memory = GeniffyMemoryTool(Geniffy().space("customer_1042"))      # one of your users
    runner = anthropic.Anthropic().beta.messages.tool_runner(
        model="claude-opus-5-5", max_tokens=16000, tools=[memory],
        messages=[{"role": "user", "content": "Remember that I prefer email follow-ups."}])
    print(runner.until_done().content)

Needs the anthropic package: pip install "geniffy[claude]". Every file the tool writes carries LABELS. A directory
is every file whose path starts with it, and its size is what they hold; sizes are in characters. Each command
answers with the sentences Anthropic's memory tool documentation gives; where those leave a detail open, it does what
Anthropic's own BetaLocalFilesystemMemoryTool does, except that a file's last newline ends its last line rather than
starting an empty one, so view and insert count lines the same way.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote

try:
    from anthropic.lib.tools import ToolError
    from anthropic.tools import BetaAbstractMemoryTool, BetaAsyncAbstractMemoryTool
except ImportError as e:  # pragma: no cover - the message is the point
    raise ImportError('geniffy.claude needs the anthropic package, 1.0 or later: pip install "geniffy[claude]"') from e

from ._client import AsyncGeniffy, Geniffy
from ._errors import BadRequestError, NotFoundError
from ._types import File, FileInfo

if TYPE_CHECKING:
    from anthropic.types.beta import (BetaCacheControlEphemeralParam, BetaMemoryTool20250818CreateCommand,
                                      BetaMemoryTool20250818DeleteCommand, BetaMemoryTool20250818InsertCommand,
                                      BetaMemoryTool20250818RenameCommand, BetaMemoryTool20250818StrReplaceCommand,
                                      BetaMemoryTool20250818ViewCommand)

__all__ = ["GeniffyMemoryTool", "AsyncGeniffyMemoryTool", "ROOT", "LABELS"]

ROOT = "/memories"
LABELS: Dict[str, str] = {"channel": "claude-memory"}   # on every file the tool writes
_PAGE = 200            # the most files one listing returns
_PAGES = 1000          # the most pages one view reads, so a listing that keeps going cannot loop for ever
_MAX_PATH = 255        # the longest path Geniffy keeps
_MAX_LINES = 999_999
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# ── paths ─────────────────────────────────────────────────────────────────────
def _outside(path: Any) -> ToolError:
    return ToolError(f"Error: The path {path} is outside /memories. Use a path under it, such as /memories/notes.md.")


def _path(path: Any) -> str:
    """The path as Geniffy keeps it: /memories or a path under it, with doubled and trailing slashes dropped.
    Anything that could lead out of /memories is refused, typed plainly or URL-encoded, however often."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise _outside(path)
    forms, decoded = [path], unquote(path)
    while decoded != forms[-1]:              # %2e%2e, and %252e%252e encoded again; each decoding is shorter
        forms.append(decoded)
        decoded = unquote(decoded)
    for form in forms:
        if "\\" in form or _CONTROL.search(form) or any(part in (".", "..") for part in form.split("/")):
            raise ToolError(f"Error: The path {path} is not allowed: a memory path has no '.' or '..' parts, "
                            "backslashes or control characters. Use a plain path such as /memories/notes.md.")
    clean = "/" + "/".join(part for part in path.split("/") if part)
    if clean != ROOT and not clean.startswith(ROOT + "/"):
        raise _outside(path)
    return clean


@contextmanager
def _refused() -> Iterator[None]:
    """A request Geniffy refused as sent (a path or a text too long, say) goes back to Claude in Geniffy's words."""
    try:
        yield
    except BadRequestError as e:
        raise ToolError(f"Error: {e.message}") from e


# ── what each command says ────────────────────────────────────────────────────
def _size(chars: int) -> str:
    """A size as the memory tool shows one: 0B, 512B, 1.5K, 2M."""
    if chars <= 0:
        return "0B"
    unit = min((chars.bit_length() - 1) // 10, 3)
    n = chars / 1024 ** unit
    return (str(int(n)) if n == int(n) else f"{n:.1f}") + "BKMG"[unit]


def _lines(text: str) -> List[str]:
    """A text's lines, split at each newline. The newline that ends the last line starts no new one."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _numbered(number: int, line: str) -> str:
    return f"{number:6d}\t{line}"


def _hidden(name: str) -> bool:
    return name.startswith(".") or name == "node_modules"


def _directory(path: str, files: List[FileInfo]) -> str:
    """A directory as view shows one: its own size and path, then everything in it two levels deep, by name,
    directories ending in "/", hidden items and node_modules left out. A directory's size is all it holds."""
    inside = path + "/"
    total = 0
    sizes: Dict[Tuple[Tuple[str, ...], bool], int] = {}     # (names, is a directory) -> size
    for f in files:
        if not f.path.startswith(inside):
            continue
        total += f.size
        names = f.path[len(inside):].split("/")
        for depth in range(min(2, len(names))):
            if _hidden(names[depth]):
                break
            key = (tuple(names[:depth + 1]), depth + 1 < len(names))
            sizes[key] = sizes.get(key, 0) + f.size
    lines = [f"Here're the files and directories up to 2 levels deep in {path}, excluding hidden items and "
             f"node_modules:", f"{_size(total)}\t{path}"]
    lines += [f"{_size(size)}\t{inside}{'/'.join(names)}{'/' if is_dir else ''}"
              for (names, is_dir), size in sorted(sizes.items())]
    return "\n".join(lines)


def _file(path: str, text: str, view_range: Any) -> str:
    """A file as view shows one: each line numbered from 1, or only the lines view_range names ([start, -1] for
    the rest of the file)."""
    lines = _lines(text)
    if len(lines) > _MAX_LINES:
        raise ToolError(f"File {path} exceeds maximum line limit of 999,999 lines.")
    first = 1
    if view_range and len(view_range) == 2:
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in view_range):
            raise ToolError("Error: view_range is [start_line, end_line], such as [1, 20], or [start_line, -1] "
                            "for the rest of the file.")
        start, end = view_range
        first = max(1, start)
        lines = lines[first - 1:len(lines) if end == -1 else end]
    return "\n".join([f"Here's the content of {path} with line numbers:"]
                     + [_numbered(first + i, line) for i, line in enumerate(lines)])


def _replaced(path: str, text: str, old: Any, new: Any) -> Tuple[str, str]:
    """str_replace on a file's text: the text after it, and what to tell Claude. new_str left out deletes old_str."""
    if not isinstance(old, str) or not old:
        raise ToolError("Error: old_str is missing or empty. Give the exact text to replace, as it appears in the "
                        "file.")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"No replacement was performed, old_str `{old}` did not appear verbatim in {path}.")
    if count > 1:
        found, at = [], text.find(old)
        while at != -1:
            found.append(text.count("\n", 0, at) + 1)
            at = text.find(old, at + 1)
        raise ToolError(f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
                        f"{', '.join(map(str, found))}. Please ensure it is unique")
    at = text.find(old)
    edited = text[:at] + (new if isinstance(new, str) else "") + text[at + len(old):]
    changed, lines = text.count("\n", 0, at), _lines(edited)
    shown = range(max(0, changed - 2), min(len(lines), changed + 3))
    return edited, "\n".join(["The memory file has been edited. Here is the snippet showing the change "
                              "(with line numbers):"] + [_numbered(n + 1, lines[n]) for n in shown])


def _inserted(text: str, line: Any, insert_text: Any) -> str:
    """insert on a file's text: insert_text as lines of its own after line `line` (0: before the first)."""
    lines = _lines(text)
    if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line <= len(lines):
        raise ToolError(f"Error: Invalid `insert_line` parameter: {line}. It should be within the range of lines "
                        f"of the file: [0, {len(lines)}]")
    if not isinstance(insert_text, str):
        raise ToolError("Error: insert needs insert_text, the text to insert.")
    lines.insert(line, insert_text[:-1] if insert_text.endswith("\n") else insert_text)
    edited = "\n".join(lines)
    return edited if edited.endswith("\n") else edited + "\n"


def _holds_files(path: str) -> bool:
    """Whether any file can be under path: one under it is longer by "/" and a name, at least two characters."""
    return len(path) + 2 <= _MAX_PATH


def _no_file(path: str) -> ToolError:
    return ToolError(f"Error: The path {path} does not exist")


def _taken(path: str) -> ToolError:
    return ToolError(f"Error: The destination {path} already exists")


def _inside_itself(old: str, new: str) -> ToolError:
    return ToolError(f"Error: Cannot rename {old} to {new}, a path inside it")


# ── blocking ──────────────────────────────────────────────────────────────────
class GeniffyMemoryTool(BetaAbstractMemoryTool):
    """Claude's memory tool, kept in a Geniffy memory. Pass it in tools= to client.beta.messages.tool_runner.

    memory is a Geniffy client bound to one of your users with client.space(user_id), so each user's Claude
    keeps its own notes, or the client itself for your own memory. Claude's files are that memory's files under
    /memories, each labelled {"channel": "claude-memory"}: mem.files.list("/memories/") lists them, and
    context() and ask() recall what they say."""

    def __init__(self, memory: Geniffy, *, cache_control: Optional[BetaCacheControlEphemeralParam] = None) -> None:
        if isinstance(memory, AsyncGeniffy):
            raise TypeError("GeniffyMemoryTool takes a Geniffy client. With AsyncGeniffy, use AsyncGeniffyMemoryTool.")
        super().__init__(cache_control=cache_control)
        self.memory = memory

    def _get(self, path: str) -> Optional[File]:
        if path == ROOT:
            return None
        try:
            return self.memory.files.get(path)
        except NotFoundError:
            return None

    def _under(self, path: str) -> List[FileInfo]:
        files: List[FileInfo] = []
        cursor = 0
        for _ in range(_PAGES if _holds_files(path) else 0):
            page = self.memory.files.list(path + "/", limit=_PAGE, cursor=cursor)
            files += page.files
            if page.next is None:
                break
            cursor = page.next
        return files

    def _put(self, path: str, text: str) -> None:
        self.memory.files.put(path, text, labels=dict(LABELS))

    def _exists(self, path: str) -> bool:
        """A file at path, or a directory: files under it."""
        if path == ROOT or self._get(path) is not None:
            return True
        return _holds_files(path) and bool(self.memory.files.list(path + "/", limit=1).files)

    def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = self._get(path)
            if found is not None:
                return _file(path, found.text, command.view_range)
            files = self._under(path)
        if not files and path != ROOT:
            raise ToolError(f"The path {path} does not exist. Please provide a valid path.")
        return _directory(path, files)

    def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        path = _path(command.path)
        if path == ROOT:
            raise ToolError(f"Error: {ROOT} is the memory directory itself. Create files inside it, such as "
                            f"{ROOT}/notes.md.")
        if not isinstance(command.file_text, str):
            raise ToolError("Error: create needs file_text, the whole text of the file.")
        with _refused():
            self._put(path, command.file_text)
        return f"File created successfully at: {path}"

    def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = self._get(path)
            if found is None:
                raise ToolError(f"Error: The path {path} does not exist. Please provide a valid path.")
            text, said = _replaced(path, found.text, command.old_str, getattr(command, "new_str", None))
            self._put(path, text)
        return said

    def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = self._get(path)
            if found is None:
                raise _no_file(path)
            self._put(path, _inserted(found.text, command.insert_line, command.insert_text))
        return f"The file {path} has been edited."

    def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        path = _path(command.path)
        if path == ROOT:
            raise ToolError(f"Error: Cannot delete the {ROOT} directory itself")
        with _refused():
            try:
                self.memory.files.delete(path)
                found = True
            except NotFoundError:
                found = False
            if _holds_files(path):          # a directory too: nothing is left at that path
                found = self.memory.files.delete_prefix(path + "/") > 0 or found
        if not found:
            raise _no_file(path)
        return f"Successfully deleted {path}"

    def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        old, new = _path(command.old_path), _path(command.new_path)
        if old == ROOT:
            raise ToolError(f"Error: Cannot rename the {ROOT} directory itself")
        if new.startswith(old + "/"):
            raise _inside_itself(old, new)
        with _refused():
            if self._exists(new):
                raise _taken(new)
            try:
                self.memory.files.move(old, new)
            except NotFoundError:
                raise _no_file(old) from None
            except BadRequestError as e:
                if e.status != 409:
                    raise
                raise _taken(new) from None
        return f"Successfully renamed {old} to {new}"

    def clear_all_memory(self) -> str:
        """Delete every file under /memories, and what only they taught."""
        with _refused():
            self.memory.files.delete_prefix(ROOT + "/")
        return "All memory cleared"


# ── asyncio ───────────────────────────────────────────────────────────────────
class AsyncGeniffyMemoryTool(BetaAsyncAbstractMemoryTool):
    """The same tool for asyncio, on an AsyncGeniffy client: pass it in tools= to the AsyncAnthropic client's
    beta.messages.tool_runner."""

    def __init__(self, memory: AsyncGeniffy, *,
                 cache_control: Optional[BetaCacheControlEphemeralParam] = None) -> None:
        if isinstance(memory, Geniffy):
            raise TypeError("AsyncGeniffyMemoryTool takes an AsyncGeniffy client. With Geniffy, use GeniffyMemoryTool.")
        super().__init__(cache_control=cache_control)
        self.memory = memory

    async def _get(self, path: str) -> Optional[File]:
        if path == ROOT:
            return None
        try:
            return await self.memory.files.get(path)
        except NotFoundError:
            return None

    async def _under(self, path: str) -> List[FileInfo]:
        files: List[FileInfo] = []
        cursor = 0
        for _ in range(_PAGES if _holds_files(path) else 0):
            page = await self.memory.files.list(path + "/", limit=_PAGE, cursor=cursor)
            files += page.files
            if page.next is None:
                break
            cursor = page.next
        return files

    async def _put(self, path: str, text: str) -> None:
        await self.memory.files.put(path, text, labels=dict(LABELS))

    async def _exists(self, path: str) -> bool:
        if path == ROOT or await self._get(path) is not None:
            return True
        return _holds_files(path) and bool((await self.memory.files.list(path + "/", limit=1)).files)

    async def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = await self._get(path)
            if found is not None:
                return _file(path, found.text, command.view_range)
            files = await self._under(path)
        if not files and path != ROOT:
            raise ToolError(f"The path {path} does not exist. Please provide a valid path.")
        return _directory(path, files)

    async def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        path = _path(command.path)
        if path == ROOT:
            raise ToolError(f"Error: {ROOT} is the memory directory itself. Create files inside it, such as "
                            f"{ROOT}/notes.md.")
        if not isinstance(command.file_text, str):
            raise ToolError("Error: create needs file_text, the whole text of the file.")
        with _refused():
            await self._put(path, command.file_text)
        return f"File created successfully at: {path}"

    async def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = await self._get(path)
            if found is None:
                raise ToolError(f"Error: The path {path} does not exist. Please provide a valid path.")
            text, said = _replaced(path, found.text, command.old_str, getattr(command, "new_str", None))
            await self._put(path, text)
        return said

    async def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        path = _path(command.path)
        with _refused():
            found = await self._get(path)
            if found is None:
                raise _no_file(path)
            await self._put(path, _inserted(found.text, command.insert_line, command.insert_text))
        return f"The file {path} has been edited."

    async def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        path = _path(command.path)
        if path == ROOT:
            raise ToolError(f"Error: Cannot delete the {ROOT} directory itself")
        with _refused():
            try:
                await self.memory.files.delete(path)
                found = True
            except NotFoundError:
                found = False
            if _holds_files(path):
                found = await self.memory.files.delete_prefix(path + "/") > 0 or found
        if not found:
            raise _no_file(path)
        return f"Successfully deleted {path}"

    async def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        old, new = _path(command.old_path), _path(command.new_path)
        if old == ROOT:
            raise ToolError(f"Error: Cannot rename the {ROOT} directory itself")
        if new.startswith(old + "/"):
            raise _inside_itself(old, new)
        with _refused():
            if await self._exists(new):
                raise _taken(new)
            try:
                await self.memory.files.move(old, new)
            except NotFoundError:
                raise _no_file(old) from None
            except BadRequestError as e:
                if e.status != 409:
                    raise
                raise _taken(new) from None
        return f"Successfully renamed {old} to {new}"

    async def clear_all_memory(self) -> str:
        """Delete every file under /memories, and what only they taught."""
        with _refused():
            await self.memory.files.delete_prefix(ROOT + "/")
        return "All memory cleared"
