"""Claude's memory tool on a fake /v1/files that keeps files in memory, as the API's contract describes it. Every
command goes in as the tool runner sends it (tool.call with Claude's input) and every answer is checked to the
character, on the blocking tool and the asyncio one alike."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import pytest

pytest.importorskip("anthropic")

from anthropic.lib.tools import ToolError  # noqa: E402

from geniffy import AsyncGeniffy, Geniffy  # noqa: E402
from geniffy.claude import LABELS, AsyncGeniffyMemoryTool, GeniffyMemoryTool, _size  # noqa: E402

KEY = "gnf_live_" + "k" * 43
HEADER = "Here're the files and directories up to 2 levels deep in {}, excluding hidden items and node_modules:"


def refused(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def bad_path(path: Any, *, prefix: bool = False) -> bool:
    """The API's rule: starts with "/", at most 255 characters, no empty, "." or ".." parts, no backslash or
    control characters; only a prefix may end with "/"."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 255:
        return True
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return True
    parts = path[1:].split("/")
    if prefix and parts[-1] == "":
        parts.pop()
    return any(p in ("", ".", "..") for p in parts)


class FakeFiles:
    """/v1/files kept in memory: each path's exact text and labels."""

    def __init__(self) -> None:
        self.held: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str, Dict[str, str], Any]] = []
        self.spaces: List[Optional[str]] = []
        self.limit = 200_000
        self.ticks = 0

    def __call__(self, r: httpx.Request) -> httpx.Response:
        params = dict(r.url.params)
        body = json.loads(r.content) if r.content else None
        self.calls.append((r.method, r.url.path, params, body))
        self.spaces.append(r.headers.get("x-geniffy-space"))
        if r.method == "POST" and r.url.path == "/v1/files/move":
            return self.move(body["from"], body["to"])
        if r.method == "PUT":
            return self.put(body)
        if ("path" in params) == ("prefix" in params):
            return refused(422, "invalid_request", "Name a path or a prefix, one of them.")
        if "prefix" in params:
            if bad_path(params["prefix"], prefix=True):
                return refused(422, "bad_path", "That prefix is not a path.")
            return self.list(params) if r.method == "GET" else self.delete_prefix(params["prefix"])
        if bad_path(params["path"]):
            return refused(422, "bad_path", "That is not a file path.")
        held = self.held.get(params["path"])
        if held is None:
            return refused(404, "not_found", "No file has that path.")
        if r.method == "GET":
            return httpx.Response(200, json={"path": params["path"], "text": held["text"], "size": len(held["text"]),
                                             "updated_at": held["updated_at"]})
        del self.held[params["path"]]
        return httpx.Response(200, json={"deleted": 1, "path": params["path"]})

    def put(self, body: Dict[str, Any]) -> httpx.Response:
        path, text = body.get("path"), body.get("text")
        if bad_path(path):
            return refused(422, "bad_path", "That is not a file path.")
        if len(text) > self.limit:
            return refused(413, "too_long", f"A file holds up to {self.limit:,} characters.")
        created = path not in self.held
        labels = body.get("labels", {} if created else self.held[path]["labels"])
        self.ticks += 1
        self.held[path] = {"text": text, "labels": dict(labels), "updated_at": f"2026-10-06T09:00:{self.ticks:02d}Z"}
        return httpx.Response(200, json={"path": path, "size": len(text), "updated_at": self.held[path]["updated_at"],
                                         "created": created,
                                         "source": {"id": "s" * 32, "kind": "note", "title": path, "labels": labels}})

    def list(self, params: Dict[str, str]) -> httpx.Response:
        paths = sorted(p for p in self.held if p.startswith(params["prefix"]))
        start, limit = int(params.get("cursor", 0)), min(int(params.get("limit", 100)), 200)
        page = paths[start:start + limit]
        return httpx.Response(200, json={
            "files": [{"path": p, "size": len(self.held[p]["text"]), "updated_at": self.held[p]["updated_at"]}
                      for p in page],
            "total": len(paths), "next": start + limit if start + limit < len(paths) else None})

    def delete_prefix(self, prefix: str) -> httpx.Response:
        gone = sorted(p for p in self.held if p.startswith(prefix))[:100]
        for p in gone:
            del self.held[p]
        return httpx.Response(200, json={"deleted": len(gone), "more": any(p.startswith(prefix) for p in self.held)})

    def move(self, old: str, new: str) -> httpx.Response:
        if bad_path(old) or bad_path(new):
            return refused(422, "bad_path", "That is not a file path.")
        moves = {old: new} if old in self.held else {
            p: new + p[len(old):] for p in self.held if p.startswith(old + "/")}
        if not moves:
            return refused(404, "not_found", "No file or folder has that path.")
        if any(to in self.held for to in moves.values()):
            return refused(409, "conflict", "Something is already at the destination.")
        for frm, to in moves.items():
            self.held[to] = self.held.pop(frm)
        return httpx.Response(200, json={"moved": len(moves)})

    def text(self, path: str) -> str:
        return self.held[path]["text"]


class Claude:
    """Claude's side of the tool: a command goes in as the tool runner sends it, and what comes back is the text of
    the tool result, or of the error the runner returns with is_error."""

    def __init__(self, fake: FakeFiles, call: Callable[[Dict[str, Any]], Any], clear: Callable[[], Any], tool: Any):
        self.fake, self._call, self.clear, self.tool = fake, call, clear, tool

    def ok(self, **command: Any) -> str:
        return self._call(command)

    def error(self, **command: Any) -> str:
        with pytest.raises(ToolError) as e:
            self._call(command)
        return e.value.content

    def put(self, files: Dict[str, str]) -> None:
        for path, text in files.items():
            self.fake.held[path] = {"text": text, "labels": {}, "updated_at": "2026-10-06T08:00:00Z"}


@pytest.fixture(params=["blocking", "asyncio"])
def claude(request) -> Claude:
    fake = FakeFiles()
    if request.param == "blocking":
        tool = GeniffyMemoryTool(Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(fake)))
                                 .space("customer_1042"))
        return Claude(fake, tool.call, tool.clear_all_memory, tool)
    atool = AsyncGeniffyMemoryTool(AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(fake))).space("customer_1042"))
    return Claude(fake, lambda command: asyncio.run(atool.call(command)),
                  lambda: asyncio.run(atool.clear_all_memory()), atool)


# ── view ──────────────────────────────────────────────────────────────────────
def test_an_empty_memory_is_a_directory_with_nothing_in_it(claude):
    assert claude.ok(command="view", path="/memories") == HEADER.format("/memories") + "\n0B\t/memories"
    assert claude.ok(command="view", path="/memories/") == HEADER.format("/memories") + "\n0B\t/memories"


def test_a_file_comes_back_exactly_as_claude_wrote_it(claude):
    text = "# Lumen\n- Priya Nair signs the renewal.\n\n  Indented, trailing spaces  \r\n"
    assert claude.ok(command="create", path="/memories/lumen.md", file_text=text) == \
        "File created successfully at: /memories/lumen.md"
    assert claude.fake.text("/memories/lumen.md") == text, "kept character for character"
    assert claude.fake.held["/memories/lumen.md"]["labels"] == {"channel": "claude-memory"} == LABELS
    assert claude.ok(command="view", path="/memories/lumen.md") == (
        "Here's the content of /memories/lumen.md with line numbers:\n"
        "     1\t# Lumen\n"
        "     2\t- Priya Nair signs the renewal.\n"
        "     3\t\n"
        "     4\t  Indented, trailing spaces  \r")


def test_create_overwrites_and_an_empty_file_is_a_file(claude):
    claude.ok(command="create", path="/memories/prefs.md", file_text="Tea.\n")
    assert claude.ok(command="create", path="/memories/prefs.md", file_text="Coffee, black.\n") == \
        "File created successfully at: /memories/prefs.md"
    assert claude.fake.text("/memories/prefs.md") == "Coffee, black.\n"
    assert claude.ok(command="create", path="/memories/empty.md", file_text="") == \
        "File created successfully at: /memories/empty.md"
    assert claude.fake.text("/memories/empty.md") == ""
    assert claude.ok(command="view", path="/memories/empty.md") == \
        "Here's the content of /memories/empty.md with line numbers:"
    assert claude.error(command="create", path="/memories", file_text="x") == \
        "Error: /memories is the memory directory itself. Create files inside it, such as /memories/notes.md."
    assert claude.error(command="create", path="/memories/x.md") == \
        "Error: create needs file_text, the whole text of the file."


def test_view_range_shows_only_those_lines(claude):
    claude.ok(command="create", path="/memories/n.md", file_text="one\ntwo\nthree\nfour\n")
    head = "Here's the content of /memories/n.md with line numbers:"
    assert claude.ok(command="view", path="/memories/n.md", view_range=[2, 3]) == f"{head}\n     2\ttwo\n     3\tthree"
    assert claude.ok(command="view", path="/memories/n.md", view_range=[3, -1]) == f"{head}\n     3\tthree\n     4\tfour"
    assert claude.ok(command="view", path="/memories/n.md", view_range=[0, 1]) == f"{head}\n     1\tone"
    assert claude.ok(command="view", path="/memories/n.md", view_range=[9, -1]) == head
    assert claude.error(command="view", path="/memories/n.md", view_range=["a", "b"]).startswith("Error: view_range is")


def test_viewing_what_is_not_there(claude):
    assert claude.error(command="view", path="/memories/nope.md") == \
        "The path /memories/nope.md does not exist. Please provide a valid path."
    assert claude.error(command="view", path="/memories/nope/") == \
        "The path /memories/nope does not exist. Please provide a valid path."


def test_a_directory_lists_two_levels_by_name_without_hidden_items(claude):
    claude.put({
        "/memories/notes.md": "x" * 1536,                     # 1.5K
        "/memories/projects/lumen.md": "y" * 100,             # 100B
        "/memories/projects/2026/q4.md": "z" * 2048,          # 2K
        "/memories/projects/2026/deep/plan.md": "w" * 10,     # three levels down: counted, not listed
        "/memories/.scratch.md": "h" * 5,                     # hidden
        "/memories/node_modules/pkg.md": "n" * 7,             # left out
        "/memories/projects/.cache/tmp.md": "c" * 3,          # hidden, two levels down
        "/elsewhere/other.md": "o" * 9,                       # not under /memories at all
    })
    assert claude.ok(command="view", path="/memories") == "\n".join([
        HEADER.format("/memories"),
        "3.6K\t/memories",                                    # 3709: everything it holds
        "1.5K\t/memories/notes.md",
        "2.1K\t/memories/projects/",                          # 2161
        "2.0K\t/memories/projects/2026/",                     # 2058
        "100B\t/memories/projects/lumen.md",
    ])
    projects = "\n".join([
        HEADER.format("/memories/projects"),
        "2.1K\t/memories/projects",
        "2.0K\t/memories/projects/2026/",
        "10B\t/memories/projects/2026/deep/",
        "2K\t/memories/projects/2026/q4.md",
        "100B\t/memories/projects/lumen.md",
    ])
    assert claude.ok(command="view", path="/memories/projects") == projects
    assert claude.ok(command="view", path="/memories/projects/") == projects


def test_a_big_directory_is_read_a_page_at_a_time(claude):
    claude.put({f"/memories/log/{i:03d}.md": "x" for i in range(450)})
    out = claude.ok(command="view", path="/memories")
    assert out.splitlines()[1:] == ["450B\t/memories", "450B\t/memories/log/"] + [
        f"1B\t/memories/log/{i:03d}.md" for i in range(450)]
    cursors = [c[2].get("cursor") for c in claude.fake.calls if c[0] == "GET" and "prefix" in c[2]]
    assert cursors == ["0", "200", "400"] and all(
        c[2]["limit"] == "200" for c in claude.fake.calls if "prefix" in c[2])


def test_sizes_read_like_the_memory_tools_own():
    assert [_size(n) for n in (0, 1, 1023, 1024, 1536, 5632, 2058, 1048576, 1258291, 3 * 1024 ** 3)] == [
        "0B", "1B", "1023B", "1K", "1.5K", "5.5K", "2.0K", "1M", "1.2M", "3G"]


# ── str_replace ───────────────────────────────────────────────────────────────
PREFS = "Prefers email.\nTimezone: IST\nPrefers email follow-ups on Fridays.\n"


def test_str_replace_edits_one_exact_occurrence_and_shows_the_change(claude):
    claude.ok(command="create", path="/memories/prefs.md", file_text=PREFS)
    assert claude.ok(command="str_replace", path="/memories/prefs.md", old_str="Timezone: IST",
                     new_str="Timezone: GMT") == (
        "The memory file has been edited. Here is the snippet showing the change (with line numbers):\n"
        "     1\tPrefers email.\n"
        "     2\tTimezone: GMT\n"
        "     3\tPrefers email follow-ups on Fridays.")
    assert claude.fake.text("/memories/prefs.md") == PREFS.replace("IST", "GMT")
    assert claude.fake.held["/memories/prefs.md"]["labels"] == LABELS


def test_str_replace_without_new_str_deletes_old_str(claude):
    claude.ok(command="create", path="/memories/prefs.md", file_text=PREFS)
    assert claude.ok(command="str_replace", path="/memories/prefs.md", old_str="\nTimezone: IST") == (
        "The memory file has been edited. Here is the snippet showing the change (with line numbers):\n"
        "     1\tPrefers email.\n"
        "     2\tPrefers email follow-ups on Fridays.")
    assert claude.fake.text("/memories/prefs.md") == "Prefers email.\nPrefers email follow-ups on Fridays.\n"


def test_the_snippet_is_two_lines_either_side(claude):
    claude.ok(command="create", path="/memories/n.md", file_text="".join(f"line {i}\n" for i in range(1, 11)))
    assert claude.ok(command="str_replace", path="/memories/n.md", old_str="line 6", new_str="line six") == (
        "The memory file has been edited. Here is the snippet showing the change (with line numbers):\n"
        "     4\tline 4\n     5\tline 5\n     6\tline six\n     7\tline 7\n     8\tline 8")


def test_str_replace_refuses_what_is_missing_absent_or_repeated(claude):
    claude.ok(command="create", path="/memories/prefs.md", file_text=PREFS)
    claude.put({"/memories/projects/a.md": "a"})
    assert claude.error(command="str_replace", path="/memories/nope.md", old_str="x", new_str="y") == \
        "Error: The path /memories/nope.md does not exist. Please provide a valid path."
    assert claude.error(command="str_replace", path="/memories/projects", old_str="x", new_str="y") == \
        "Error: The path /memories/projects does not exist. Please provide a valid path.", "a directory is no file"
    assert claude.error(command="str_replace", path="/memories", old_str="x", new_str="y") == \
        "Error: The path /memories does not exist. Please provide a valid path."
    assert claude.error(command="str_replace", path="/memories/prefs.md", old_str="Timezone: PST", new_str="y") == \
        "No replacement was performed, old_str `Timezone: PST` did not appear verbatim in /memories/prefs.md."
    assert claude.error(command="str_replace", path="/memories/prefs.md", old_str="Prefers email", new_str="y") == (
        "No replacement was performed. Multiple occurrences of old_str `Prefers email` in lines: 1, 3. "
        "Please ensure it is unique")
    assert claude.error(command="str_replace", path="/memories/prefs.md", old_str="", new_str="y").startswith(
        "Error: old_str is missing or empty.")
    assert claude.fake.text("/memories/prefs.md") == PREFS, "nothing refused was written"


# ── insert ────────────────────────────────────────────────────────────────────
def test_insert_puts_lines_after_the_line_named(claude):
    claude.ok(command="create", path="/memories/todo.md", file_text="- Call Priya\n- Send the deck\n")
    assert claude.ok(command="insert", path="/memories/todo.md", insert_line=0, insert_text="# To do\n") == \
        "The file /memories/todo.md has been edited."
    assert claude.ok(command="insert", path="/memories/todo.md", insert_line=3, insert_text="- Book the room") == \
        "The file /memories/todo.md has been edited."
    assert claude.ok(command="insert", path="/memories/todo.md", insert_line=2, insert_text="- Draft terms\n\n") == \
        "The file /memories/todo.md has been edited."
    assert claude.fake.text("/memories/todo.md") == \
        "# To do\n- Call Priya\n- Draft terms\n\n- Send the deck\n- Book the room\n"
    assert claude.fake.held["/memories/todo.md"]["labels"] == LABELS


def test_insert_into_a_file_with_no_last_newline_or_nothing_at_all(claude):
    claude.ok(command="create", path="/memories/a.md", file_text="a")
    claude.ok(command="insert", path="/memories/a.md", insert_line=1, insert_text="b")
    assert claude.fake.text("/memories/a.md") == "a\nb\n"
    claude.ok(command="create", path="/memories/empty.md", file_text="")
    claude.ok(command="insert", path="/memories/empty.md", insert_line=0, insert_text="first")
    assert claude.fake.text("/memories/empty.md") == "first\n"


def test_insert_refuses_a_missing_file_and_a_line_out_of_range(claude):
    claude.ok(command="create", path="/memories/todo.md", file_text="- Call Priya\n- Send the deck\n")
    claude.put({"/memories/projects/a.md": "a"})
    assert claude.error(command="insert", path="/memories/nope.md", insert_line=0, insert_text="x") == \
        "Error: The path /memories/nope.md does not exist"
    assert claude.error(command="insert", path="/memories/projects", insert_line=0, insert_text="x") == \
        "Error: The path /memories/projects does not exist"
    for line in (3, -1):
        assert claude.error(command="insert", path="/memories/todo.md", insert_line=line, insert_text="x") == (
            f"Error: Invalid `insert_line` parameter: {line}. It should be within the range of lines of the file: "
            "[0, 2]")
    assert claude.error(command="insert", path="/memories/todo.md", insert_line=1) == \
        "Error: insert needs insert_text, the text to insert."
    assert claude.fake.text("/memories/todo.md") == "- Call Priya\n- Send the deck\n"


# ── delete ────────────────────────────────────────────────────────────────────
def test_delete_takes_a_file_or_a_whole_directory(claude):
    claude.put({"/memories/notes.md": "n", "/memories/projects/a.md": "a", "/memories/projects/sub/b.md": "b",
                "/memories/projects-2.md": "p"})
    assert claude.ok(command="delete", path="/memories/notes.md") == "Successfully deleted /memories/notes.md"
    assert claude.ok(command="delete", path="/memories/projects") == "Successfully deleted /memories/projects"
    assert sorted(claude.fake.held) == ["/memories/projects-2.md"], "a sibling that only starts the same stays"
    assert claude.error(command="delete", path="/memories/nope") == "Error: The path /memories/nope does not exist"


def test_the_memory_directory_itself_cannot_be_deleted_or_renamed(claude):
    claude.put({"/memories/notes.md": "n"})
    for root in ("/memories", "/memories/", "//memories//"):
        assert claude.error(command="delete", path=root) == "Error: Cannot delete the /memories directory itself"
        assert claude.error(command="rename", old_path=root, new_path="/memories/x") == \
            "Error: Cannot rename the /memories directory itself"
    assert claude.fake.text("/memories/notes.md") == "n"


# ── rename ────────────────────────────────────────────────────────────────────
def test_rename_moves_a_file_without_learning_it_again(claude):
    claude.ok(command="create", path="/memories/draft.md", file_text="Lumen renews in March.\n")
    puts = sum(1 for c in claude.fake.calls if c[0] == "PUT")
    assert claude.ok(command="rename", old_path="/memories/draft.md", new_path="/memories/final.md") == \
        "Successfully renamed /memories/draft.md to /memories/final.md"
    assert claude.fake.text("/memories/final.md") == "Lumen renews in March.\n" and "/memories/draft.md" not in claude.fake.held
    assert sum(1 for c in claude.fake.calls if c[0] == "PUT") == puts, "moved, not written again"
    assert [c[3] for c in claude.fake.calls if c[0] == "POST"] == [{"from": "/memories/draft.md", "to": "/memories/final.md"}]


def test_rename_moves_a_whole_directory(claude):
    claude.put({"/memories/drafts/x.md": "x", "/memories/drafts/y/z.md": "z", "/memories/keep.md": "k"})
    assert claude.ok(command="rename", old_path="/memories/drafts", new_path="/memories/archive/2026") == \
        "Successfully renamed /memories/drafts to /memories/archive/2026"
    assert sorted(claude.fake.held) == ["/memories/archive/2026/x.md", "/memories/archive/2026/y/z.md",
                                        "/memories/keep.md"]


def test_rename_refuses_a_missing_source_and_a_taken_destination(claude):
    claude.put({"/memories/a.md": "a", "/memories/b.md": "b", "/memories/dir/c.md": "c"})
    assert claude.error(command="rename", old_path="/memories/nope.md", new_path="/memories/new.md") == \
        "Error: The path /memories/nope.md does not exist"
    assert claude.error(command="rename", old_path="/memories/a.md", new_path="/memories/b.md") == \
        "Error: The destination /memories/b.md already exists"
    assert claude.error(command="rename", old_path="/memories/a.md", new_path="/memories/dir") == \
        "Error: The destination /memories/dir already exists", "a directory is taken too"
    assert claude.error(command="rename", old_path="/memories/dir", new_path="/memories/a.md") == \
        "Error: The destination /memories/a.md already exists"
    assert claude.error(command="rename", old_path="/memories/a.md", new_path="/memories") == \
        "Error: The destination /memories already exists"
    assert claude.error(command="rename", old_path="/memories/dir", new_path="/memories/dir/inner") == \
        "Error: Cannot rename /memories/dir to /memories/dir/inner, a path inside it"
    assert sorted(claude.fake.held) == ["/memories/a.md", "/memories/b.md", "/memories/dir/c.md"], "nothing moved"


def test_a_destination_taken_meanwhile_is_still_reported_as_taken(claude):
    """Between the check and the move another writer can take the path: the API's conflict says the same."""
    claude.put({"/memories/a.md": "a"})
    real = claude.fake.move

    def taken(old: str, new: str) -> httpx.Response:
        claude.put({new: "theirs"})
        return real(old, new)
    claude.fake.move = taken
    assert claude.error(command="rename", old_path="/memories/a.md", new_path="/memories/b.md") == \
        "Error: The destination /memories/b.md already exists"
    assert claude.fake.text("/memories/b.md") == "theirs" and claude.fake.text("/memories/a.md") == "a"


# ── clear_all_memory, paths, and the rest ─────────────────────────────────────
def test_clearing_all_memory_leaves_files_outside_it(claude):
    claude.put({f"/memories/n{i}.md": "x" for i in range(250)})
    claude.put({"/docs/readme.md": "Your own file.", "/memories-old/x.md": "y"})
    assert claude.clear() == "All memory cleared"
    assert sorted(claude.fake.held) == ["/docs/readme.md", "/memories-old/x.md"]
    assert claude.ok(command="view", path="/memories") == HEADER.format("/memories") + "\n0B\t/memories"


TRAVERSAL = ["/memories/../secrets.env", "/memories/notes/../../etc/passwd", "/memories/../memories/notes.md",
             "/memories/%2e%2e/secrets.env", "/memories/%2E%2E%2Fsecrets.env", "/memories/%252e%252e/secrets.env",
             "/memories/..%5csecrets.env", "/memories\\..\\secrets.env", "/memories/./notes.md",
             "/memories/notes\x00.md", "/memories/notes%00.md", "/memories/a\nb.md"]
OUTSIDE = ["/etc/passwd", "/memoriesX/notes.md", "memories/notes.md", "", "/", "/memorie", None, 42]


@pytest.mark.parametrize("path", TRAVERSAL + OUTSIDE)
def test_no_command_reaches_outside_memories(claude, path):
    commands = [dict(command="view", path=path), dict(command="create", path=path, file_text="x"),
                dict(command="str_replace", path=path, old_str="a", new_str="b"),
                dict(command="insert", path=path, insert_line=0, insert_text="x"), dict(command="delete", path=path),
                dict(command="rename", old_path=path, new_path="/memories/x.md"),
                dict(command="rename", old_path="/memories/x.md", new_path=path)]
    for command in commands:
        said = claude.error(**command)
        assert said.startswith(f"Error: The path {path} ")
        assert ("outside /memories" in said) if path in OUTSIDE else ("is not allowed" in said)
    assert claude.fake.calls == [], "refused before anything was asked of Geniffy"


def test_the_refusal_messages_say_what_to_do():
    tool = GeniffyMemoryTool(Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(FakeFiles()))))
    with pytest.raises(ToolError) as e:
        tool.call({"command": "view", "path": "/memories/../secrets.env"})
    assert e.value.content == ("Error: The path /memories/../secrets.env is not allowed: a memory path has no '.' or "
                               "'..' parts, backslashes or control characters. Use a plain path such as "
                               "/memories/notes.md.")
    with pytest.raises(ToolError) as e:
        tool.call({"command": "view", "path": "/etc/passwd"})
    assert e.value.content == \
        "Error: The path /etc/passwd is outside /memories. Use a path under it, such as /memories/notes.md."


def test_a_path_as_long_as_paths_go_works_like_any_other(claude):
    """Nothing can be under a 255-character path, so it is never asked for as a prefix one character too long."""
    longest = "/memories/" + "a" * 245
    assert claude.ok(command="create", path=longest, file_text="x") == f"File created successfully at: {longest}"
    assert claude.ok(command="rename", old_path=longest, new_path="/memories/b.md").startswith("Successfully renamed")
    assert claude.ok(command="rename", old_path="/memories/b.md", new_path=longest).startswith("Successfully renamed")
    assert claude.ok(command="delete", path=longest) == f"Successfully deleted {longest}"
    assert claude.error(command="view", path=longest) == \
        f"The path {longest} does not exist. Please provide a valid path."
    assert all(len(c[2].get("prefix", "")) <= 255 for c in claude.fake.calls)


def test_what_geniffy_refuses_goes_back_to_claude_in_its_words(claude):
    claude.fake.limit = 20
    assert claude.error(command="create", path="/memories/long.md", file_text="x" * 21) == \
        "Error: A file holds up to 20 characters."
    long = "/memories/" + "a" * 250
    assert claude.error(command="create", path=long, file_text="x") == "Error: That is not a file path."
    assert "/memories/long.md" not in claude.fake.held


def test_every_call_stays_in_the_users_space_and_every_write_is_labelled(claude):
    claude.ok(command="create", path="/memories/a.md", file_text="a\n")
    claude.ok(command="str_replace", path="/memories/a.md", old_str="a", new_str="b")
    claude.ok(command="insert", path="/memories/a.md", insert_line=1, insert_text="c")
    claude.ok(command="view", path="/memories")
    claude.ok(command="rename", old_path="/memories/a.md", new_path="/memories/b.md")
    claude.ok(command="delete", path="/memories/b.md")
    assert claude.fake.spaces and set(claude.fake.spaces) == {"customer_1042"}
    puts = [body for method, _, _, body in claude.fake.calls if method == "PUT"]
    assert len(puts) == 3 and all(body["labels"] == {"channel": "claude-memory"} for body in puts)


def test_a_file_past_the_line_limit_is_not_shown(claude):
    claude.put({"/memories/huge.md": "\n" * 1_000_000})
    assert claude.error(command="view", path="/memories/huge.md") == \
        "File /memories/huge.md exceeds maximum line limit of 999,999 lines."


def test_it_is_claudes_memory_tool():
    sync = GeniffyMemoryTool(Geniffy(api_key=KEY, http_client=httpx.Client(transport=httpx.MockTransport(FakeFiles()))))
    assert sync.to_dict() == {"type": "memory_20250818", "name": "memory"} and sync.name == "memory"
    cached = GeniffyMemoryTool(sync.memory, cache_control={"type": "ephemeral"})
    assert cached.to_dict()["cache_control"] == {"type": "ephemeral"}
    a = AsyncGeniffyMemoryTool(AsyncGeniffy(api_key=KEY, http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(FakeFiles()))))
    assert a.to_dict() == {"type": "memory_20250818", "name": "memory"}
    with pytest.raises(TypeError, match="AsyncGeniffyMemoryTool"):
        GeniffyMemoryTool(a.memory)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="use GeniffyMemoryTool"):
        AsyncGeniffyMemoryTool(sync.memory)  # type: ignore[arg-type]
