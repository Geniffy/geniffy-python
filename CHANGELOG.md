# Changelog

## Unreleased

- `memories.add(..., said_at=...)`: when a note or conversation from the past was said (a datetime, a date or
  an ISO 8601 string), so what it teaches is dated by it. Needs the API with `said_at` (October 2026).
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
