"""FFmpeg, wrapped.

Every measured decision from benchmarking this pipeline lives here.

**No ``zoompan``, ever.** It was measured at over nine minutes to produce
twenty seconds of 1080p output -- a 90-minute documentary would take longer
to encode than to watch. Motion is produced instead by a ``crop`` window
moving across a pre-scaled plate, which measured 2.17x real time for the
same visual effect.

**Pre-scale once.** Rendering motion directly from a 12-megapixel archive
scan rescales the full image on every output frame (1.37x real time).
Resampling once to a 2304px plate at ingest and panning across that runs 60%
faster for identical output.

**One encode.** Segments are rendered with identical parameters and joined
with the concat demuxer using ``-c copy``, so the joined file is never
re-encoded. Re-encoding a 90-minute concatenation would double the render
budget and lose quality for nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..errors import Permanent, Retryable
from ..logging import get_logger

log = get_logger(__name__)


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def require_ffmpeg() -> None:
    if not have_ffmpeg():
        raise Permanent(
            "ffmpeg and ffprobe are required but not on PATH. The container "
            "image installs them; a local run needs 'apt-get install ffmpeg'."
        )


def _run(args: Sequence[str], *, timeout: int = 7200, what: str = "ffmpeg") -> str:
    """Run a command, raising with the tail of stderr on failure.

    FFmpeg's diagnostics are the last few lines of stderr; the rest is banner
    noise. Truncating to the tail keeps the error readable in a log line.
    """
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise Retryable(f"{what} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise Permanent(f"{what} is not installed: {exc}") from exc

    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        raise Retryable(f"{what} failed (exit {proc.returncode}): {tail}")
    return proc.stdout


@dataclass
class MediaInfo:
    duration_s: float
    width: int | None = None
    height: int | None = None
    has_audio: bool = False
    has_video: bool = False
    size_bytes: int = 0


def probe(path: Path) -> MediaInfo:
    """Read real properties from the file. Never trusts what we meant to make."""
    path = Path(path)
    if not path.exists():
        raise Permanent(f"cannot probe missing file: {path}")
    out = _run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        timeout=120, what="ffprobe",
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise Permanent(f"ffprobe returned unreadable output for {path}") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = fmt.get("duration")
    if duration is None and video:
        duration = video.get("duration")
    try:
        duration_s = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        duration_s = 0.0

    return MediaInfo(
        duration_s=duration_s,
        width=int(video["width"]) if video and video.get("width") else None,
        height=int(video["height"]) if video and video.get("height") else None,
        has_audio=audio is not None,
        has_video=video is not None,
        size_bytes=path.stat().st_size,
    )


def concat_audio(parts: Sequence[Path], out_path: Path, *, sample_rate: int = 48000,
                 tolerance_s: float = 0.5) -> float:
    """Join audio parts into one file, re-encoding once to a uniform format.

    **Every part must already share a sample rate.** The concat demuxer takes
    its stream parameters from the *first* file and applies them to all of
    them: a 0.45-second gap written at 48 kHz, concatenated behind 16 kHz
    narration, is read as 21,600 frames at 16 kHz and plays for 1.35 seconds.
    Across one documentary's gaps that silently added 51 seconds of audio the
    picture track knew nothing about.

    So this uses ``-filter_complex concat``, which resamples each input to a
    common rate rather than reinterpreting it, and then checks the result
    against the sum of the parts. A mismatch raises instead of being
    discovered later as a picture/audio drift.
    """
    if not parts:
        raise Permanent("refusing to concatenate zero audio parts")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    paths = [Path(p) for p in parts]
    expected = sum(probe(p).duration_s for p in paths)

    args: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    for path in paths:
        args += ["-i", str(path)]
    # aresample on every input, then concat: each part is converted to the
    # target rate, so none of them can be reinterpreted at another's.
    chain = "".join(
        f"[{i}:a]aresample={sample_rate},aformat=sample_fmts=s16:channel_layouts=mono[a{i}];"
        for i in range(len(paths))
    )
    inputs = "".join(f"[a{i}]" for i in range(len(paths)))
    args += [
        "-filter_complex", f"{chain}{inputs}concat=n={len(paths)}:v=0:a=1[out]",
        "-map", "[out]", "-ar", str(sample_rate), "-ac", "1",
        "-c:a", "pcm_s16le", str(out_path),
    ]
    _run(args, what="audio concat")

    actual = probe(out_path).duration_s
    drift = abs(actual - expected)
    if drift > max(tolerance_s, expected * 0.002):
        raise Permanent(
            f"audio concatenation produced {actual:.2f}s from parts totalling "
            f"{expected:.2f}s ({drift:.2f}s apart). The parts do not agree on "
            f"a format."
        )
    return actual


def normalise_loudness(
    src: Path, out_path: Path, *, lufs: float = -14.0, true_peak: float = -1.5,
    sample_rate: int = 48000, codec: str = "aac", bitrate: str = "192k",
) -> float:
    """Apply EBU R128 loudness normalisation.

    -14 LUFS is the level YouTube normalises toward. Delivering at that level
    means the platform leaves the audio alone; delivering louder means it is
    turned down, and quieter means it stays quiet next to everything else.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
        "-af", f"loudnorm=I={lufs}:TP={true_peak}:LRA=11",
        # Two channels even though the narration is mono. The music-bed path
        # already delivers stereo; matching it here means a documentary's
        # channel layout does not depend on whether a bed happened to be
        # present, which is not something a viewer should be able to notice.
        "-ar", str(sample_rate), "-ac", "2",
        "-c:a", codec, "-b:a", bitrate,
        str(out_path),
    ], what="loudness normalisation")
    return probe(out_path).duration_s


def mix_narration_with_bed(
    narration: Path, bed: Path, out_path: Path, *, bed_gain_db: float = -26.0,
    duck_db: float = -6.0, sample_rate: int = 48000, codec: str = "aac",
    bitrate: str = "192k", lufs: float = -14.0, true_peak: float = -1.5,
) -> float:
    """Lay a music bed under narration, ducked and looped, then normalise.

    ``sidechaincompress`` is what does the ducking: the narration drives the
    compressor on the music, so the bed drops whenever the narrator speaks
    and comes back in the gaps. A fixed low gain alone does not work -- it is
    either audible over speech or inaudible in the pauses.

    ``-shortest`` with a looped bed means the music ends exactly with the
    narration, however long that turns out to be.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ratio = max(1.0, abs(duck_db))
    filtergraph = (
        f"[1:a]volume={bed_gain_db}dB,aloop=loop=-1:size=2e9[bed];"
        f"[0:a]asplit=2[narr][key];"
        f"[bed][key]sidechaincompress=threshold=0.03:ratio={ratio}:"
        f"attack=20:release=900[ducked];"
        f"[narr][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
        f"loudnorm=I={lufs}:TP={true_peak}:LRA=11[out]"
    )
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(narration), "-i", str(bed),
        "-filter_complex", filtergraph, "-map", "[out]",
        "-ar", str(sample_rate), "-ac", "2",
        "-c:a", codec, "-b:a", bitrate, "-shortest",
        str(out_path),
    ], what="narration/music mix")
    return probe(out_path).duration_s


# The crop window's position as a function of time, per motion style. The
# plate is larger than the frame, so (iw-ow) and (ih-oh) are the travel.
_MOTION = {
    "pan_right": "'(iw-ow)*(t/{d})':'(ih-oh)/2'",
    "pan_left": "'(iw-ow)*(1-t/{d})':'(ih-oh)/2'",
    "pan_down": "'(iw-ow)/2':'(ih-oh)*(t/{d})'",
    "pan_up": "'(iw-ow)/2':'(ih-oh)*(1-t/{d})'",
    "drift_ne": "'(iw-ow)*(t/{d})':'(ih-oh)*(1-t/{d})'",
    "drift_sw": "'(iw-ow)*(1-t/{d})':'(ih-oh)*(t/{d})'",
    "still": "'(iw-ow)/2':'(ih-oh)/2'",
}
MOTIONS = tuple(k for k in _MOTION if k != "still")


def render_segment(
    plate: Path, out_path: Path, *, duration_s: float, motion: str = "pan_right",
    width: int = 1920, height: int = 1080, fps: int = 25,
    codec: str = "libx264", preset: str = "veryfast", crf: int = 21,
    pixel_format: str = "yuv420p",
) -> float:
    """Render one still plate into a moving video segment.

    Every segment must be encoded with identical parameters, because the
    concat demuxer stream-copies them into one file. A segment that differs
    in resolution, frame rate or pixel format will either be rejected or
    produce a file that plays incorrectly after the join.
    """
    if duration_s <= 0:
        raise Permanent(f"segment duration must be positive, got {duration_s}")
    expression = _MOTION.get(motion, _MOTION["still"]).format(d=max(duration_s, 0.001))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = (
        f"crop={width}:{height}:{expression},"
        f"format={pixel_format}"
    )
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-t", f"{duration_s:.3f}",
        "-i", str(plate),
        "-vf", vf, "-r", str(fps),
        "-c:v", codec, "-preset", preset, "-crf", str(crf),
        "-pix_fmt", pixel_format,
        # A keyframe every second keeps concat joins clean and makes the
        # finished video seekable, which matters for a 90-minute film.
        "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
        "-an", str(out_path),
    ], what=f"segment render ({motion})")
    return probe(out_path).duration_s


def concat_video(parts: Sequence[Path], out_path: Path) -> float:
    """Join segments without re-encoding."""
    if not parts:
        raise Permanent("refusing to concatenate zero video segments")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = out_path.with_suffix(".concat.txt")
    listing.write_text(
        "\n".join(f"file '{Path(p).resolve()}'" for p in parts) + "\n",
        encoding="utf-8",
    )
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", str(out_path),
    ], what="video concat")
    listing.unlink(missing_ok=True)
    return probe(out_path).duration_s


def mux(video: Path, audio: Path, out_path: Path, *, audio_codec: str = "aac",
        audio_bitrate: str = "192k", faststart: bool = True) -> float:
    """Combine the finished picture and the finished audio.

    The video is stream-copied: it was encoded once, at segment level, and
    must not be touched again. ``+faststart`` moves the index to the front of
    the file so playback can begin before the whole thing has downloaded.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", audio_codec, "-b:a", audio_bitrate,
        "-shortest",
    ]
    if faststart:
        args += ["-movflags", "+faststart"]
    args.append(str(out_path))
    _run(args, what="mux")
    return probe(out_path).duration_s


def silence(out_path: Path, duration_s: float, *, sample_rate: int = 48000) -> Path:
    """A block of silence, used to pad gaps between narration chunks."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{duration_s:.3f}", "-c:a", "pcm_s16le", str(out_path),
    ], what="silence")
    return out_path
