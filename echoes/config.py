"""Configuration, read once from the environment and then immutable.

Two rules hold throughout:

* A secret is never given a default. If ``GEMINI_API_KEY`` is absent the
  system says so by name; it does not fall back to a free tier that does not
  exist or silently produce an empty script.
* Publishing widens only on an explicit instruction. ``PUBLISH_MODE`` defaults
  to ``private`` and ``DRY_RUN`` defaults to true, so a fresh deployment that
  is misconfigured produces a private draft rather than a public video.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from .errors import ConfigError

PublishMode = Literal["private", "unlisted", "public", "scheduled"]
_PUBLISH_MODES = ("private", "unlisted", "public", "scheduled")


def _str(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _str(name)
    if raw is None:
        return list(default or [])
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class DurationPolicy:
    """How long a documentary should be, and how that is enforced.

    ``words_per_minute`` is not a style preference: it is the conversion the
    script engine uses to decide whether a draft is long enough *before*
    spending render time on it. It must match the speaking rate the TTS
    provider is actually configured to produce, which is why the narration
    stage re-measures the rendered audio rather than trusting this number.
    """

    target_minutes: int = 90
    min_minutes: int = 60
    max_minutes: int = 120
    words_per_minute: float = 150.0
    # How far the finished narration may drift from target before QC objects.
    tolerance_minutes: int = 8

    def target_words(self) -> int:
        return int(self.target_minutes * self.words_per_minute)

    def min_words(self) -> int:
        return int(self.min_minutes * self.words_per_minute)

    def validate(self) -> None:
        if not (self.min_minutes <= self.target_minutes <= self.max_minutes):
            raise ConfigError(
                "Duration policy is inconsistent: require MIN_VIDEO_MINUTES "
                f"({self.min_minutes}) <= TARGET_VIDEO_MINUTES "
                f"({self.target_minutes}) <= MAX_VIDEO_MINUTES ({self.max_minutes})"
            )
        if self.words_per_minute <= 0:
            raise ConfigError("NARRATION_WPM must be greater than zero")


@dataclass(frozen=True)
class RenderPolicy:
    """Encoder settings, chosen from measured throughput rather than taste.

    ``plate_width``/``plate_height`` are the size every source image is
    resampled to exactly once, at ingest. Motion is then a crop window moving
    across that plate. Rendering motion straight from a 12-megapixel archive
    scan, or via ffmpeg's ``zoompan`` filter, was measured on this hardware at
    well under real time and would make a 90-minute documentary take longer to
    encode than to watch. Do not reintroduce either.
    """

    width: int = 1920
    height: int = 1080
    fps: int = 25
    plate_width: int = 2304
    plate_height: int = 1296
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 21
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 48000
    # Loudness target. -14 LUFS is what YouTube normalises toward, so
    # delivering at -14 means the platform leaves the track alone.
    loudness_lufs: float = -14.0
    loudness_true_peak: float = -1.5
    music_gain_db: float = -26.0
    ducking_db: float = -6.0
    segment_workers: int = 2

    def validate(self) -> None:
        if self.width % 2 or self.height % 2:
            raise ConfigError("Render dimensions must be even for yuv420p")
        # Strictly larger, in both dimensions. Equal is not "no zoom", it is
        # a crop window with zero travel: every pan silently renders as a
        # still and the documentary looks like a slideshow of frozen images.
        if self.plate_width <= self.width or self.plate_height <= self.height:
            raise ConfigError(
                f"The plate ({self.plate_width}x{self.plate_height}) must be "
                f"strictly larger than the output frame "
                f"({self.width}x{self.height}) in both dimensions, or the crop "
                f"window has no room to move and every pan renders as a still"
            )


@dataclass(frozen=True)
class Settings:
    # ---- control surface (spec section 23) -------------------------------
    dry_run: bool = True
    publish_mode: PublishMode = "private"
    max_concurrent_jobs: int = 1
    scheduler_enabled: bool = False
    schedule_cron: str = "0 3 * * *"
    timezone: str = "Asia/Baghdad"
    retry_max_attempts: int = 3
    stage_timeout_s: int = 3600

    # ---- storage ---------------------------------------------------------
    database_url: str | None = None
    data_dir: Path = Path("/app/data")
    storage_provider: str = "local"
    s3_bucket: str | None = None
    s3_endpoint: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "auto"

    # ---- providers -------------------------------------------------------
    llm_provider: str = "gemini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_model_heavy: str = "gemini-2.5-pro"
    llm_max_output_tokens: int = 8192
    llm_temperature: float = 0.7

    tts_provider: str = "piper"
    piper_voice: str = "en_GB-alan-medium"
    piper_voice_dir: Path = Path("/app/voices")
    # length_scale > 1 slows the voice down. Piper's stock rate measured at
    # ~211 wpm, far too brisk for a documentary; 1.4 lands near 150 wpm.
    piper_length_scale: float = 1.4
    piper_noise_scale: float = 0.667
    piper_noise_w: float = 0.8
    tts_chunk_chars: int = 1800

    research_providers: list[str] = field(
        default_factory=lambda: ["wikipedia", "loc", "met", "crossref"]
    )
    image_providers: list[str] = field(
        default_factory=lambda: ["wikimedia", "met", "loc"]
    )
    search_api_key: str | None = None
    contact_email: str | None = None

    notify_provider: str = "noop"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # ---- youtube ---------------------------------------------------------
    youtube_channel_id: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None
    youtube_category_id: str = "27"  # Education
    youtube_language: str = "en"
    publish_hour_local: int = 18

    # ---- editorial -------------------------------------------------------
    channel_name: str = "Echoes of History"
    channel_handle: str = "@echoesofhistory-v"
    banned_topics: list[str] = field(default_factory=list)
    preferred_eras: list[str] = field(default_factory=list)
    similarity_threshold: float = 0.72
    min_sources_per_documentary: int = 8
    max_unsupported_claim_ratio: float = 0.05

    duration: DurationPolicy = field(default_factory=DurationPolicy)
    render: RenderPolicy = field(default_factory=RenderPolicy)

    # ---- api -------------------------------------------------------------
    api_port: int = 8080
    api_token: str | None = None

    # -----------------------------------------------------------------
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def validate(self) -> None:
        """Fail loudly, naming the variable an operator has to change.

        A failure here costs a deploy cycle only if the message is vague, so
        every branch names the environment variable at fault.
        """
        if self.publish_mode not in _PUBLISH_MODES:
            raise ConfigError(
                f"PUBLISH_MODE must be one of {', '.join(_PUBLISH_MODES)}; "
                f"got {self.publish_mode!r}"
            )
        self.duration.validate()
        self.render.validate()

        if self.max_concurrent_jobs < 1:
            raise ConfigError("MAX_CONCURRENT_JOBS must be at least 1")
        if not 0 < self.similarity_threshold < 1:
            raise ConfigError("SIMILARITY_THRESHOLD must be between 0 and 1")

        # Anything that leaves the building needs real credentials. A dry run
        # is exempt because its whole purpose is to exercise the pipeline
        # without them.
        if not self.dry_run:
            if self.llm_provider == "gemini" and not self.gemini_api_key:
                raise ConfigError(
                    "GEMINI_API_KEY is required when LLM_PROVIDER=gemini and "
                    "DRY_RUN=false"
                )
            missing = [
                name
                for name, value in (
                    ("YOUTUBE_CLIENT_ID", self.youtube_client_id),
                    ("YOUTUBE_CLIENT_SECRET", self.youtube_client_secret),
                    ("YOUTUBE_REFRESH_TOKEN", self.youtube_refresh_token),
                )
                if not value
            ]
            if missing:
                raise ConfigError(
                    "Uploading requires " + ", ".join(missing) + ". Set them, or "
                    "run with DRY_RUN=true to exercise everything up to upload."
                )

        if self.notify_provider == "telegram" and not (
            self.telegram_bot_token and self.telegram_chat_id
        ):
            raise ConfigError(
                "NOTIFY_PROVIDER=telegram requires TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_CHAT_ID"
            )
        if self.storage_provider == "s3" and not (
            self.s3_bucket and self.s3_access_key and self.s3_secret_key
        ):
            raise ConfigError(
                "STORAGE_PROVIDER=s3 requires S3_BUCKET, S3_ACCESS_KEY and "
                "S3_SECRET_KEY"
            )

    def with_(self, **kw: object) -> "Settings":
        """A copy with overrides. Tests use this instead of touching os.environ."""
        return replace(self, **kw)  # type: ignore[arg-type]


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment. Does not validate."""
    data_dir = Path(_str("DATA_DIR", "/app/data") or "/app/data")
    return Settings(
        dry_run=_bool("DRY_RUN", True),
        publish_mode=(_str("PUBLISH_MODE", "private") or "private").lower(),  # type: ignore[arg-type]
        max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 1),
        scheduler_enabled=_bool("SCHEDULER_ENABLED", False),
        schedule_cron=_str("SCHEDULE_CRON", "0 3 * * *") or "0 3 * * *",
        timezone=_str("TZ", "Asia/Baghdad") or "Asia/Baghdad",
        retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 3),
        stage_timeout_s=_int("STAGE_TIMEOUT_S", 3600),
        database_url=_str("DATABASE_URL"),
        data_dir=data_dir,
        storage_provider=_str("STORAGE_PROVIDER", "local") or "local",
        s3_bucket=_str("S3_BUCKET"),
        s3_endpoint=_str("S3_ENDPOINT"),
        s3_access_key=_str("S3_ACCESS_KEY"),
        s3_secret_key=_str("S3_SECRET_KEY"),
        s3_region=_str("S3_REGION", "auto") or "auto",
        llm_provider=_str("LLM_PROVIDER", "gemini") or "gemini",
        gemini_api_key=_str("GEMINI_API_KEY"),
        gemini_model=_str("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
        gemini_model_heavy=_str("GEMINI_MODEL_HEAVY", "gemini-2.5-pro")
        or "gemini-2.5-pro",
        llm_max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 8192),
        llm_temperature=_float("LLM_TEMPERATURE", 0.7),
        tts_provider=_str("TTS_PROVIDER", "piper") or "piper",
        piper_voice=_str("PIPER_VOICE", "en_GB-alan-medium") or "en_GB-alan-medium",
        piper_voice_dir=Path(_str("PIPER_VOICE_DIR", "/app/voices") or "/app/voices"),
        piper_length_scale=_float("PIPER_LENGTH_SCALE", 1.4),
        piper_noise_scale=_float("PIPER_NOISE_SCALE", 0.667),
        piper_noise_w=_float("PIPER_NOISE_W", 0.8),
        tts_chunk_chars=_int("TTS_CHUNK_CHARS", 1800),
        research_providers=_list(
            "RESEARCH_PROVIDERS", ["wikipedia", "loc", "met", "crossref"]
        ),
        image_providers=_list("IMAGE_PROVIDERS", ["wikimedia", "met", "loc"]),
        search_api_key=_str("SEARCH_API_KEY"),
        contact_email=_str("CONTACT_EMAIL"),
        notify_provider=_str("NOTIFY_PROVIDER", "noop") or "noop",
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        youtube_channel_id=_str("YOUTUBE_CHANNEL_ID"),
        youtube_client_id=_str("YOUTUBE_CLIENT_ID"),
        youtube_client_secret=_str("YOUTUBE_CLIENT_SECRET"),
        youtube_refresh_token=_str("YOUTUBE_REFRESH_TOKEN"),
        youtube_category_id=_str("YOUTUBE_CATEGORY_ID", "27") or "27",
        youtube_language=_str("YOUTUBE_LANGUAGE", "en") or "en",
        publish_hour_local=_int("PUBLISH_HOUR_LOCAL", 18),
        channel_name=_str("CHANNEL_NAME", "Echoes of History") or "Echoes of History",
        channel_handle=_str("CHANNEL_HANDLE", "@echoesofhistory-v")
        or "@echoesofhistory-v",
        banned_topics=_list("BANNED_TOPICS"),
        preferred_eras=_list("PREFERRED_ERAS"),
        similarity_threshold=_float("SIMILARITY_THRESHOLD", 0.72),
        min_sources_per_documentary=_int("MIN_SOURCES_PER_DOCUMENTARY", 8),
        max_unsupported_claim_ratio=_float("MAX_UNSUPPORTED_CLAIM_RATIO", 0.05),
        duration=DurationPolicy(
            target_minutes=_int("TARGET_VIDEO_MINUTES", 90),
            min_minutes=_int("MIN_VIDEO_MINUTES", 60),
            max_minutes=_int("MAX_VIDEO_MINUTES", 120),
            words_per_minute=_float("NARRATION_WPM", 150.0),
            tolerance_minutes=_int("DURATION_TOLERANCE_MINUTES", 8),
        ),
        render=RenderPolicy(
            width=_int("RENDER_WIDTH", 1920),
            height=_int("RENDER_HEIGHT", 1080),
            fps=_int("RENDER_FPS", 25),
            preset=_str("RENDER_PRESET", "veryfast") or "veryfast",
            crf=_int("RENDER_CRF", 21),
            music_gain_db=_float("MUSIC_GAIN_DB", -26.0),
            segment_workers=_int("RENDER_SEGMENT_WORKERS", 2),
        ),
        api_port=_int("PORT", 8080),
        api_token=_str("API_TOKEN"),
    )
