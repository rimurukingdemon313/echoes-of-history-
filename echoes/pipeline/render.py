"""Rendering.

The expensive stage, and the one most worth making resumable: a 90-minute
documentary is roughly 400 segments and forty minutes of encoding. Losing
that to a restart is the difference between a system that runs unattended and
one that does not.

So each segment is a database row with its own file. A resumed render skips
every segment whose file is already on disk and the right length. Segments
are encoded with identical parameters and joined with ``-c copy``, so the
join is nearly free and the picture is encoded exactly once.

Parallelism is modest on purpose. x264 is already threaded; running more
segment workers than about half the cores makes them contend and finishes
slower than running fewer.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import RenderPolicy
from ..errors import Permanent
from ..logging import get_logger
from ..media import ffmpeg

log = get_logger(__name__)


@dataclass
class SegmentSpec:
    ordinal: int
    plate: Path
    duration_s: float
    motion: str
    asset_id: int | None = None


@dataclass
class RenderResult:
    video_path: Path
    duration_s: float
    size_bytes: int
    segments_rendered: int
    segments_reused: int


def default_workers(policy: RenderPolicy) -> int:
    """How many segments to encode at once.

    Capped at half the visible cores: x264 uses several threads per encode,
    so more workers than this makes them fight for the same cores and the
    total gets slower, not faster.
    """
    cores = os.cpu_count() or 2
    return max(1, min(policy.segment_workers, max(1, cores // 2)))


def _segment_path(segments_dir: Path, spec: SegmentSpec) -> Path:
    return segments_dir / f"seg-{spec.ordinal:05d}.mp4"


def _is_usable(path: Path, expected_s: float, *, tolerance: float = 0.15) -> bool:
    """True if this segment file is already correct.

    The length is checked, not just existence: a file written by a process
    that was killed mid-encode exists and is the wrong length, and
    stream-copying it into the final cut would silently shorten the video.
    """
    if not path.exists() or path.stat().st_size < 1024:
        return False
    try:
        info = ffmpeg.probe(path)
    except Exception:  # noqa: BLE001
        return False
    return info.has_video and abs(info.duration_s - expected_s) <= tolerance


def render_segments(
    specs: Sequence[SegmentSpec], segments_dir: Path, policy: RenderPolicy,
    *, workers: int | None = None, on_done: Callable[[SegmentSpec, Path], None] | None = None,
) -> tuple[list[Path], int, int]:
    """Encode every segment, skipping any already on disk and correct."""
    segments_dir.mkdir(parents=True, exist_ok=True)
    count = workers or default_workers(policy)

    pending: list[SegmentSpec] = []
    paths: dict[int, Path] = {}
    reused = 0

    for spec in specs:
        path = _segment_path(segments_dir, spec)
        if _is_usable(path, spec.duration_s):
            paths[spec.ordinal] = path
            reused += 1
            if on_done:
                on_done(spec, path)
        else:
            pending.append(spec)

    if reused:
        log.info("reusing segments from an earlier run",
                 extra={"reused": reused, "pending": len(pending)})

    def work(spec: SegmentSpec) -> tuple[SegmentSpec, Path]:
        path = _segment_path(segments_dir, spec)
        ffmpeg.render_segment(
            spec.plate, path, duration_s=spec.duration_s, motion=spec.motion,
            width=policy.width, height=policy.height, fps=policy.fps,
            codec=policy.video_codec, preset=policy.preset, crf=policy.crf,
            pixel_format=policy.pixel_format,
        )
        return spec, path

    rendered = 0
    if pending:
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = {pool.submit(work, spec): spec for spec in pending}
            for future in as_completed(futures):
                spec, path = future.result()
                paths[spec.ordinal] = path
                rendered += 1
                if on_done:
                    on_done(spec, path)
                if rendered % 25 == 0:
                    log.info("render progress",
                             extra={"done": rendered, "of": len(pending)})

    ordered = [paths[spec.ordinal] for spec in specs if spec.ordinal in paths]
    if len(ordered) != len(specs):
        missing = [s.ordinal for s in specs if s.ordinal not in paths]
        raise Permanent(f"segments failed to render: {missing[:20]}")
    return ordered, rendered, reused


def assemble(
    segment_paths: Sequence[Path], audio_path: Path, out_path: Path,
    policy: RenderPolicy, *, work_dir: Path,
) -> RenderResult:
    """Join the picture, then marry it to the finished audio."""
    picture = work_dir / "picture.mp4"
    picture_s = ffmpeg.concat_video(segment_paths, picture)
    audio_s = ffmpeg.probe(audio_path).duration_s

    # The visual timeline is built from measured narration, so these should
    # agree closely. A large gap means the timeline and the audio came from
    # different runs, and muxing would silently truncate to the shorter.
    drift = abs(picture_s - audio_s)
    if drift > 2.0:
        log.warning("picture and audio lengths disagree",
                    extra={"picture_s": round(picture_s, 2),
                           "audio_s": round(audio_s, 2),
                           "drift_s": round(drift, 2)})
    if drift > 30.0:
        raise Permanent(
            f"picture track is {picture_s:.0f}s but narration is {audio_s:.0f}s "
            f"({drift:.0f}s apart). Muxing would truncate the documentary; "
            f"the visual plan and the narration are out of step."
        )

    final_s = ffmpeg.mux(picture, audio_path, out_path,
                         audio_codec=policy.audio_codec,
                         audio_bitrate=policy.audio_bitrate)
    info = ffmpeg.probe(out_path)
    if not (info.has_video and info.has_audio):
        raise Permanent(
            f"rendered file is incomplete (video={info.has_video}, "
            f"audio={info.has_audio})"
        )
    picture.unlink(missing_ok=True)

    log.info("render assembled",
             extra={"duration_s": round(final_s, 1),
                    "minutes": round(final_s / 60.0, 1),
                    "mb": round(info.size_bytes / 1_048_576, 1)})
    return RenderResult(out_path, final_s, info.size_bytes, 0, 0)


def cleanup_segments(segments_dir: Path) -> int:
    """Remove segment files after a confirmed assembly.

    Called only once the final file has been probed and stored. A 90-minute
    documentary's segments are roughly the size of the film itself, and
    leaving them fills the volume by the third production.
    """
    removed = 0
    if not segments_dir.exists():
        return 0
    for path in segments_dir.glob("seg-*.mp4"):
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            log.warning("could not remove segment",
                        extra={"path": str(path), "error": str(exc)})
    return removed
