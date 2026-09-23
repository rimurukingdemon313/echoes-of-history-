"""The LLM boundary.

Everything the pipeline needs from a language model goes through this
protocol, so swapping Gemini for another provider is one class and one
settings value rather than a rewrite. Two rules hold for every implementation:

* A malformed response is an error, never something to repair. If the model
  returns prose where JSON was demanded, that is a failure the caller handles
  -- silently "fixing" it produces a documentary whose content nobody chose.
* The model is never the source of historical fact. It writes prose from
  retrieved sources and it judges whether a claim is supported by an excerpt.
  It is not asked "what happened in 1453".
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol


class LLMProvider(Protocol):
    name: str

    def generate(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None, temperature: float | None = None,
    ) -> str:
        """Return the model's text. Raises on transport or refusal."""

    def generate_json(
        self, prompt: str, *, system: str | None = None, heavy: bool = False,
        max_tokens: int | None = None,
    ) -> Any:
        """Return parsed JSON. Raises if the response is not valid JSON."""

    def available(self) -> bool:
        ...


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json_strict(text: str) -> Any:
    """Parse JSON from a model response.

    Stripping a ``` fence is not repair -- it is undoing a formatting habit
    that carries no content. Anything beyond that (balancing brackets,
    trimming to the first object) *is* repair, and is deliberately absent: a
    truncated response means the answer is incomplete, and guessing the rest
    invents content.
    """
    cleaned = _FENCE.sub("", text).strip()
    return json.loads(cleaned)
