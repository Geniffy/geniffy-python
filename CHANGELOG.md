# Changelog

## 0.4.0

- Briefings. `briefing(project=, cue=)` is what a session opens with, written out for your prompt: where the
  project stands (goal, focus, open items, decisions, next steps), what is due or was promised, the rules and
  lessons that apply, what happened, then the memories, each dated. `briefing_full()` has its parts. `now()`,
  `episodes()`, `lessons()` and `intentions()` read each part on its own, and `set_intention(id, "done")` marks a
  promise kept. `memory_health()`: how well the memory answers about its own work, from the questions it asks
  itself each night. On `AsyncGeniffy` too. Needs the API with briefings (October 2026).
- `memories.add(messages=...)` takes a whole session: the assistant's tool calls and what came back go in too, in
  the shapes OpenAI, Anthropic, the Vercel AI SDK, Gemini and LangChain hold them. Facts come only from what was
  said; the tool turns tell Geniffy what happened. Secrets are removed before anything is kept.
- Sessions saved as they go. `session(id).save(messages)`, after every turn, sends only the messages after the last
  one it sent, 500 a call, into one memory for the whole session however long it gets; if the agent rewrites its
  history, what it holds now is saved again rather than lost. `memories.add(messages=new_turns, session=id)` does
  the same for turns you track yourself. Needs the API with sessions (October 2026).
- `usage()`: this month's use for the whole account, what it comes to in dollars, what your plan includes, the
  most the month can come to, and when it resets. `UsageLimitError` (402, code `allowance_used`): raised when this
  month's use is up and what waits to be learned has reached its limit; search and recall keep working; not
  retried. A source saved past this month's use comes back with `status` `"waiting"`, learned once there is room.

## 0.3.0

- `files`: files kept exactly as they were written, each under a path such as `/memories/notes.md`.
  `files.put(path, text)` creates or replaces one, and `files.get(path).text` is that text character for
  character, spaces and line endings included. Geniffy also learns from each file like a note, so `context()` and
  `ask()` recall what it says; a replace learns only what changed. `files.list(prefix)` lists them by path,
  `files.move(from_path, to_path)` moves a file or every file in a folder without learning anything again, and
  `files.delete(path)` and `files.delete_prefix(prefix)` delete them, with what only they taught. `export()` lists
  every file by path under `"files"`, without its text: `files.get(path)` reads each one. On `AsyncGeniffy` too.
  Needs the API with `/v1/files` (October 2026).
- `geniffy.claude.GeniffyMemoryTool`: Claude's memory tool (`memory_20250818`), stored in Geniffy. Pass it in
  `tools=` to Anthropic's `client.beta.messages.tool_runner`, on a client bound to one of your users, and their
  Claude keeps its notes as files in their memory under `/memories`, labelled `{"channel": "claude-memory"}`. Every
  command answers in the words of Anthropic's memory tool documentation, and a path that could lead out of
  `/memories` is refused, typed or URL-encoded. `AsyncGeniffyMemoryTool` does the same for `AsyncAnthropic`. Install
  it with `pip install "geniffy[claude]"`; `import geniffy` still needs nothing but httpx.
- Putting a file is retried like a read: the same text put again changes nothing.

## 0.2.0

- `memories.add(..., said_at=...)`: when a note or conversation from the past was said (a datetime, a date or
  an ISO 8601 string), so what it teaches is dated by it. Needs the API with `said_at` (October 2026).
- `external_id=` on `memories.add` and `add_file`: your own id for a source. Sent again under the same id,
  the source is updated rather than added twice, and only what changed is learned. `sources.get` and
  `sources.delete` take `external_id=` too. `Source.external_id` says which id a source was added under.
- `labels=` on `memories.add` and `add_file`: up to 20 of your own name/value pairs on a source
  (`{"channel": "email"}`), and as a filter on `search`, `context`, `ask`, `memories.list`, `memories.iter` and
  `brief`: every name must match, and a list of values is any one of them. `Source.labels` shows a source's.
  `sources.list(labels=...)` lists the sources carrying them, and `sources.delete_labelled(labels)` deletes
  them all, with what only they taught (for a data source your user disconnected); with `keep=` (the
  external_ids a sync still has) it deletes only the rest, such as what is gone from the source. Needs the API
  with labels (October 2026).
- `export()`: everything held, as the user's own copy (on `client.space(id)`, for a user who asks what you hold).
- `sections`: the sections profiles are grouped into. `client.sections.create(name, keywords=...)` adds one
  for every one of your users; on `client.space(id)`, for that user only. `list()` and `delete(id)` too.
- `keys` on a client bound to one of your users: `client.space(id).keys.create(name=..., rpm=...)` makes a key
  limited to that user (it reads and writes their memory and nothing else), and `keys.list()` and
  `keys.revoke(id)` manage them. `expires_at=` makes one that stops by itself. Needs the API with `/v1/keys`
  (October 2026).
- `space()`, `forget_space()` and `space=` refuse a blank space (`ValueError`) and anything that isn't a
  string or an int (`TypeError`). A blank space used to mean your own memory, so a user with a missing id
  landed in it. Your own memory is still the client with no space. An int id is taken as its digits.

## 0.1.1

- The package page links to the source on GitHub, the docs and the issue tracker.
- The examples in the docstrings use the same sample note as the docs.

## 0.1.0 (5 October 2026)

The first release.

- `Geniffy` and `AsyncGeniffy`, reading `GENIFFY_API_KEY` and, to point at another server, `GENIFFY_BASE_URL`.
- Spaces: `space()` binds a client to one of your users; `spaces()` lists them; `forget_space()` erases one.
- Memories: add a note, a web page, a conversation, a PDF or Word file, or up to a hundred at once; list,
  iterate, get with the sentence each came from, correct and delete.
- Sources: list, get, wait until learned, and delete with everything only they taught.
- Recall: `context()` for your prompt, `ask()` for an answer or an honest "nothing stored", and `search()`.
- `profile()`, `brief()`, `graph()` and `me()`.
- Typed errors that carry the API's own sentence and the request id. Reads are retried; adding is retried
  only when it cannot save a note twice.
