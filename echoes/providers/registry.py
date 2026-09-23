"""Assemble providers from settings.

One place decides which implementation backs each capability, so swapping
Gemini for another model, or Piper for a paid voice, is a settings change and
a new class -- never an edit spread across the pipeline.

The registry refuses unknown names rather than falling back to a default.
Silent substitution would make every recorded ``provider`` column a claim
about code that did not run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..errors import ConfigError
from ..logging import get_logger
from .images.base import ImageProvider
from .images.synthetic import SyntheticImageProvider
from .images.wikimedia import WikimediaCommonsProvider
from .llm.base import LLMProvider
from .llm.gemini import GeminiProvider
from .llm.offline import OfflineProvider
from .notify.base import NoopNotifier, NotifyProvider
from .notify.telegram import TelegramNotifier
from .research.base import ResearchProvider
from .research.crossref import CrossrefProvider
from .research.fixtures import FixturesProvider
from .research.loc import LibraryOfCongressProvider
from .research.met import MetMuseumProvider
from .research.wikipedia import WikipediaProvider
from .storage.base import StorageProvider
from .storage.local import LocalStorage
from .storage.s3 import S3Storage
from .tts.base import TTSProvider
from .tts.piper import PiperProvider
from .tts.silent import SilentProvider

log = get_logger(__name__)


@dataclass
class Providers:
    llm: LLMProvider
    tts: TTSProvider
    research: list[ResearchProvider]
    images: list[ImageProvider]
    storage: StorageProvider
    notify: NotifyProvider

    def describe(self) -> dict[str, Any]:
        return {
            "llm": self.llm.name,
            "tts": f"{self.tts.name}:{self.tts.voice}",
            "research": [p.name for p in self.research],
            "images": [p.name for p in self.images],
            "storage": self.storage.name,
            "notify": self.notify.name,
            "storage_durable": self.storage.durable(),
        }


def _llm(settings: Settings) -> LLMProvider:
    name = settings.llm_provider.lower()
    if name == "gemini":
        return GeminiProvider(
            settings.gemini_api_key, settings.gemini_model,
            settings.gemini_model_heavy,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
        )
    if name == "offline":
        return OfflineProvider()
    raise ConfigError(f"Unknown LLM_PROVIDER={settings.llm_provider!r} (gemini|offline)")


def _tts(settings: Settings) -> TTSProvider:
    name = settings.tts_provider.lower()
    if name == "piper":
        return PiperProvider(
            settings.piper_voice, settings.piper_voice_dir,
            length_scale=settings.piper_length_scale,
            noise_scale=settings.piper_noise_scale,
            noise_w=settings.piper_noise_w,
        )
    if name == "silent":
        return SilentProvider(settings.duration.words_per_minute)
    raise ConfigError(f"Unknown TTS_PROVIDER={settings.tts_provider!r} (piper|silent)")


def _research(settings: Settings) -> list[ResearchProvider]:
    email = settings.contact_email
    built: dict[str, Any] = {
        "wikipedia": lambda: WikipediaProvider(email),
        "loc": lambda: LibraryOfCongressProvider(email),
        "met": lambda: MetMuseumProvider(email),
        "crossref": lambda: CrossrefProvider(email),
        # Offline only. qc refuses to pass a documentary built on these
        # whenever DRY_RUN is false.
        "fixtures": lambda: FixturesProvider(),
    }
    out = []
    for name in settings.research_providers:
        factory = built.get(name.lower())
        if factory is None:
            raise ConfigError(
                f"Unknown research provider {name!r} in RESEARCH_PROVIDERS; "
                f"known: {', '.join(sorted(built))}"
            )
        out.append(factory())
    if not out:
        raise ConfigError("RESEARCH_PROVIDERS is empty; a documentary needs sources")
    return out


def _images(settings: Settings) -> list[ImageProvider]:
    built: dict[str, Any] = {
        "wikimedia": lambda: WikimediaCommonsProvider(
            settings.contact_email, settings.render.plate_width),
        "met": lambda: _MetImages(settings.contact_email),
        "loc": lambda: _LocImages(settings.contact_email),
        "synthetic": lambda: SyntheticImageProvider(
            settings.cache_dir / "synthetic",
            settings.render.plate_width, settings.render.plate_height),
    }
    out = []
    for name in settings.image_providers:
        factory = built.get(name.lower())
        if factory is None:
            raise ConfigError(
                f"Unknown image provider {name!r} in IMAGE_PROVIDERS; "
                f"known: {', '.join(sorted(built))}"
            )
        out.append(factory())
    return out


def _storage(settings: Settings) -> StorageProvider:
    name = settings.storage_provider.lower()
    if name == "local":
        return LocalStorage(settings.media_dir)
    if name == "s3":
        return S3Storage(
            settings.s3_bucket or "", endpoint=settings.s3_endpoint,
            access_key=settings.s3_access_key or "",
            secret_key=settings.s3_secret_key or "",
            region=settings.s3_region,
        )
    raise ConfigError(f"Unknown STORAGE_PROVIDER={settings.storage_provider!r} (local|s3)")


def _notify(settings: Settings) -> NotifyProvider:
    name = settings.notify_provider.lower()
    if name == "noop":
        return NoopNotifier()
    if name == "telegram":
        return TelegramNotifier(
            settings.telegram_bot_token or "", settings.telegram_chat_id or ""
        )
    raise ConfigError(f"Unknown NOTIFY_PROVIDER={settings.notify_provider!r} (noop|telegram)")


class _MetImages:
    """The Met's object records, surfaced as image candidates."""

    name = "met"

    def __init__(self, email: str | None) -> None:
        self._inner = MetMuseumProvider(email)

    def available(self) -> bool:
        return True

    def search(self, query: str, *, limit: int = 8):
        from .base_helpers import met_candidates
        return met_candidates(self._inner, query, limit)


class _LocImages:
    """Library of Congress items that carry image URLs."""

    name = "loc"

    def __init__(self, email: str | None) -> None:
        self._inner = LibraryOfCongressProvider(email)

    def available(self) -> bool:
        return True

    def search(self, query: str, *, limit: int = 8):
        from .base_helpers import loc_candidates
        return loc_candidates(self._inner, query, limit)


def build(settings: Settings) -> Providers:
    providers = Providers(
        llm=_llm(settings),
        tts=_tts(settings),
        research=_research(settings),
        images=_images(settings),
        storage=_storage(settings),
        notify=_notify(settings),
    )
    log.info("providers ready", extra=providers.describe())
    return providers
