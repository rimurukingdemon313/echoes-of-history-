"""Behaviour version stamps.

Every finished video records the stamp of the code that made it. Performance
is then grouped by stamp, so "did the new scorer help" is answerable. Bump the
matching constant whenever you change what that component *does* -- not when
you rename a variable. A behaviour change under an unchanged stamp makes every
past number uninterpretable, because two different systems are averaged
together as though they were one.
"""

from __future__ import annotations

TOPIC_ENGINE_VERSION = "topic-1.0.0"
RESEARCH_ENGINE_VERSION = "research-1.0.0"
SCRIPT_ENGINE_VERSION = "script-1.0.0"
FACTCHECK_ENGINE_VERSION = "factcheck-1.0.0"
NARRATION_ENGINE_VERSION = "narration-1.0.0"
VISUAL_ENGINE_VERSION = "visual-1.0.0"
RENDER_ENGINE_VERSION = "render-1.0.0"
METADATA_ENGINE_VERSION = "metadata-1.0.0"
PROMPT_VERSION = "prompt-1.0.0"


def version_stamp() -> str:
    """One string that identifies the whole production behaviour."""
    return "|".join(
        (
            TOPIC_ENGINE_VERSION,
            RESEARCH_ENGINE_VERSION,
            SCRIPT_ENGINE_VERSION,
            FACTCHECK_ENGINE_VERSION,
            NARRATION_ENGINE_VERSION,
            VISUAL_ENGINE_VERSION,
            RENDER_ENGINE_VERSION,
            METADATA_ENGINE_VERSION,
            PROMPT_VERSION,
        )
    )
