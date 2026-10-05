# Geniffy for Python

[![CI](https://github.com/Geniffy/geniffy-python/actions/workflows/ci.yml/badge.svg)](https://github.com/Geniffy/geniffy-python/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/geniffy)](https://pypi.org/project/geniffy/) [![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![Docs](https://img.shields.io/badge/docs-docs.geniffy.com-1A1814)](https://docs.geniffy.com/sdks/python)

Give your app a memory. Write what each of your users tells you, and put what is known about them
in front of your model, with where every line came from. When nothing is known, it says so instead
of guessing.

```bash
pip install geniffy
```

Make a key in the Geniffy app under **API keys** and set it as `GENIFFY_API_KEY`.

```python
from geniffy import Geniffy

client = Geniffy()                       # reads GENIFFY_API_KEY
mem = client.space("customer_1042")      # one of your users; nothing else can read it

source = mem.memories.add("Priya Nair signs the Lumen renewal, and it comes up in March.")
mem.sources.wait(source.id)              # learning usually takes a few seconds

prompt = f"{mem.context('Who signs the Lumen renewal?')}\n\nUser: Who signs the Lumen renewal?"
```

`context()` returns the memories that bear on the question, one per line, each with where it came from:

```
- Priya Nair signs the Lumen renewal.  [note, 2026-10-05]
- The Lumen renewal comes up in March 2027.  [note, 2026-10-05]
```

Ask about something that was never stored and it returns one sentence, `There is nothing stored about
this yet. Say so rather than guessing.`, never an empty string: a model reads silence as permission to
invent.

## Three ways to recall

```python
mem.context("Who signs the renewal?")    # a block for your own prompt; the one most apps want
mem.ask("Who signs the renewal?")        # our answer in words, or answer=None and a message saying why
mem.search("renewal", limit=5)           # the raw memories, ranked, to do with as you like
```

`context()` and `ask()` judge whether anything bears on the question. `search()` ranks and does not
judge: it returns its best matches for any question at all.

## Spaces: one memory per user

A space is your own name for one of your users. Bind a client to it and every call stays inside it.
A space exists from the first time you write to it; there is nothing to create.

```python
mem = client.space(f"user_{user.id}")    # per request
client.memories.add("...")               # no space: your own memory, the one the Geniffy app shows
client.spaces()                          # which spaces hold anything, most recently written first
client.forget_space("user_8841")         # everything held for that user, gone, when they ask
```

## Add

```python
mem.memories.add("A note to remember", title="Call with Priya")
mem.memories.add(url="https://example.com")              # a web page, read once
mem.memories.add(messages=chat_history)                        # a conversation, as your framework holds it
mem.memories.add_file("Pricing.pdf")                           # PDF or Word (.docx): a path, bytes or a file opened "rb"
mem.memories.add_many([{"text": "..."}, {"url": "https://..."}])
```

A file or page that can't be read raises `UnreadableError`; `error.source` is the row it left, with the reason.

## Read and correct

```python
page = mem.memories.list(kind="people")  # all, people, plan, pref or detail; newest first
for m in mem.memories.iter():            # every memory, a page at a time
    ...
detail = mem.memories.get(42)            # .memory.quote is the sentence it came from; .history its older values
mem.memories.correct(42, "Priya signs it, with Arjun co-signing.")
mem.memories.delete(42)                  # forget it for good
mem.profile("Priya Nair")                # what is lastingly true about someone, and what is going on now
mem.brief("Priya Nair")                  # what to read before talking to them
mem.sources.list(); mem.sources.delete(source.id)   # a source, and what only it taught
```

## Async

```python
from geniffy import AsyncGeniffy

async with AsyncGeniffy() as client:
    mem = client.space("customer_1042")
    await mem.memories.add("Priya wants a demo on Tuesday.")
    print(await mem.context("When is Priya's demo?"))
```

## Errors, retries and request ids

Every error carries the API's own sentence: `AuthenticationError` (a wrong or revoked key),
`NotFoundError`, `BadRequestError`, `UnreadableError`, `RateLimitError`, `InternalServerError`,
`APIConnectionError`. Reads are retried twice on network errors, 408, 429 and 5xx. Adding is retried
only when the request never reached Geniffy, or on 429, so a retry never saves a note twice. Set
`max_retries=` and `timeout=` on the client to change that.

Every response carries an `X-Request-ID`, and every error carries it as `error.request_id`. Paste it into
**Requests** in the Geniffy app to see that exact call: what was asked, what came back, and how long it took.

A key reaches its owner's memory and every space beneath it. Keep it on your server.

Docs: https://docs.geniffy.com
