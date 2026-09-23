"""Piper: local neural TTS, no API, no per-character cost, no rate limit.

Measured on a 4-core container at roughly 34x real time, so a 90-minute
documentary narrates in about three minutes. That ratio is why narration is
not the bottleneck and why a failed chunk can simply be redone.

Piper's stock speaking rate measured at ~211 wpm, which is wrong for a
documentary meant to be listened to slowly. ``length_scale`` above 1 slows it;
1.4 lands near 150 wpm. The pipeline still measures the rendered audio rather
than trusting that number.
"""

from __future__ import annotations

import wave
from pathlib import Path

from ...errors import ConfigError, ProviderUnavailable
from ...logging import get_logger

log = get_logger(__name__)


class PiperProvider:
    name = "piper"

    def __init__(
        self, voice: str, voice_dir: Path, *, length_scale: float = 1.4,
        noise_scale: float = 0.667, noise_w: float = 0.8,
    ) -> None:
        self.voice = voice
        self._dir = Path(voice_dir)
        self._length_scale = length_scale
        self._noise_scale = noise_scale
        self._noise_w = noise_w
        self._loaded = None
        self.sample_rate = 22050

    def model_path(self) -> Path | None:
        """Find the voice on disk, tolerating both naming conventions.

        Voices published on the Piper model hub are ``en_GB-alan-medium.onnx``;
        the older release tarballs use ``en-gb-alan-low.onnx``. Accepting both
        avoids a deployment that fails only because a file was named the way
        the other half of the documentation says.
        """
        candidates = [
            self._dir / f"{self.voice}.onnx",
            self._dir / self.voice / f"{self.voice}.onnx",
        ]
        for path in candidates:
            if path.exists():
                return path
        matches = sorted(self._dir.glob("*.onnx")) if self._dir.exists() else []
        return matches[0] if matches else None

    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return self.model_path() is not None

    def _voice(self):
        if self._loaded is None:
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise ConfigError(
                    "piper-tts is not installed but TTS_PROVIDER=piper"
                ) from exc
            path = self.model_path()
            if path is None:
                raise ConfigError(
                    f"No Piper voice found. Looked for {self.voice!r} in "
                    f"{self._dir}. Download one into PIPER_VOICE_DIR."
                )
            config = path.with_suffix(path.suffix + ".json")
            log.info("loading piper voice", extra={"model": path.name})
            self._loaded = PiperVoice.load(
                str(path), config_path=str(config) if config.exists() else None
            )
            rate = getattr(getattr(self._loaded, "config", None), "sample_rate", None)
            if rate:
                self.sample_rate = int(rate)
        return self._loaded

    def synthesize(self, text: str, out_path: Path) -> float:
        if not text.strip():
            raise ValueError("refusing to synthesize empty text")
        voice = self._voice()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        syn_config = self._syn_config()
        try:
            with wave.open(str(out_path), "wb") as wf:
                if syn_config is not None:
                    voice.synthesize_wav(text, wf, syn_config=syn_config)
                else:
                    voice.synthesize_wav(text, wf)
        except Exception as exc:  # noqa: BLE001 - provider faults are retryable
            raise ProviderUnavailable(f"piper synthesis failed: {exc}") from exc

        with wave.open(str(out_path)) as wf:
            frames, rate = wf.getnframes(), wf.getframerate()
            self.sample_rate = rate
        if frames == 0:
            raise ProviderUnavailable("piper produced an empty audio file")
        return frames / float(rate)

    def _syn_config(self):
        """Build a SynthesisConfig if this Piper build supports one.

        The knobs moved between Piper 1.x releases. Probing rather than
        assuming means a version bump degrades to default prosody instead of
        crashing a production run.
        """
        try:
            from piper import SynthesisConfig
        except ImportError:
            return None
        try:
            return SynthesisConfig(
                length_scale=self._length_scale,
                noise_scale=self._noise_scale,
                noise_w_scale=self._noise_w,
            )
        except TypeError:
            try:
                return SynthesisConfig(length_scale=self._length_scale)
            except TypeError:
                return None
