"""Geniffy for Python: add notes, files and links to a memory, then search it and ask it.

    from geniffy import Geniffy

    g = Geniffy()                     # reads GENIFFY_API_KEY
    g.memories.add("Priya Nair signs the Lumen renewal, and it comes up in March.")
    print(g.ask("Who signs the Lumen renewal?").answer)
"""
from ._client import DEFAULT_BASE_URL, AsyncGeniffy, Geniffy, __version__
from ._errors import (APIConnectionError, AuthenticationError, BadRequestError, GeniffyError, InternalServerError,
                      NotFoundError, RateLimitError, UnreadableError)
from ._types import Answer, Key, Memory, MemoryDetail, MemoryPage, Source, SourcePage, SourceRef

__all__ = ["Geniffy", "AsyncGeniffy", "DEFAULT_BASE_URL", "__version__",
           "Answer", "Key", "Memory", "MemoryDetail", "MemoryPage", "Source", "SourcePage", "SourceRef",
           "GeniffyError", "APIConnectionError", "AuthenticationError", "BadRequestError", "UnreadableError",
           "NotFoundError", "RateLimitError", "InternalServerError"]
