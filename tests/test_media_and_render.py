"""Media handling. These run real ffmpeg: the encoder is the thing under test."""

from __future__ import annotations

import pytest

from echoes.config import RenderPolicy
from echoes.errors import Permanent
from echoes.media import ffmpeg
from echoes.pipeline.render import SegmentSpec, _is_usable, default_workers, render_segments
from echoes.pipeline.visuals import hamming, perceptual_hash, split_chapter
from echoes.providers.images.synthetic import SyntheticImageProvider

pytestmark = pytest.mark.skipif(not ffmpeg.have_ffmpeg(),
                                reason="ffmpeg is not installed")

POLICY = RenderPolicy(fps=25, preset="ultrafast", crf=28)


@pytest.fixture
def plates(tmp_path):
    provider = SyntheticImageProvider(tmp_path / "plates", POLICY.plate_width,
                                      POLICY.plate_height)
    import pathlib
    return [pathlib.Path(c.extra["local_path"])
            for c in provider.search("test plates", limit=4)]


# ----------------------------------------------------------------- timeline
@pytest.mark.parametrize("start,duration", [
    (0.0, 113.8), (61.6, 240.0), (0.0, 7.0), (12.5, 4107.2),
])
def test_segments_tile_a_chapter_exactly(start, duration):
    """Accumulated rounding is how a picture track ends up short of its audio."""
    spans = split_chapter(start, duration)
    assert abs(sum(d for _, d in spans) - duration) < 0.01
    assert spans[0][0] == pytest.approx(start, abs=0.01)
    for i in range(len(spans) - 1):
        assert spans[i][0] + spans[i][1] == pytest.approx(spans[i + 1][0], abs=0.01)


def test_perceptual_hash_separates_different_images(plates):
    a, b = perceptual_hash(plates[0]), perceptual_hash(plates[1])
    assert hamming(a, a) == 0
    assert hamming(a, b) >= 6


def test_perceptual_hash_survives_rescaling(tmp_path, plates):
    """Archives serve the same photograph at several sizes; those must collide."""
    from PIL import Image
    smaller = tmp_path / "small.jpg"
    with Image.open(plates[0]) as image:
        image.resize((image.width // 3, image.height // 3)).save(smaller, quality=70)
    assert hamming(perceptual_hash(plates[0]), perceptual_hash(smaller)) <= 6


# ------------------------------------------------------------------ encoder
def test_a_segment_is_the_length_it_was_asked_for(tmp_path, plates):
    out = tmp_path / "seg.mp4"
    got = ffmpeg.render_segment(plates[0], out, duration_s=3.0, motion="pan_right",
                                width=POLICY.width, height=POLICY.height,
                                fps=POLICY.fps, preset="ultrafast", crf=30)
    assert got == pytest.approx(3.0, abs=0.15)
    info = ffmpeg.probe(out)
    assert (info.width, info.height) == (POLICY.width, POLICY.height)
    assert info.has_video and not info.has_audio


def test_a_zero_length_segment_is_refused(tmp_path, plates):
    with pytest.raises(Permanent):
        ffmpeg.render_segment(plates[0], tmp_path / "x.mp4", duration_s=0.0)


def test_concatenation_preserves_total_duration(tmp_path, plates):
    parts = []
    for index in range(3):
        path = tmp_path / f"s{index}.mp4"
        ffmpeg.render_segment(plates[index], path, duration_s=2.0,
                              motion="pan_right", fps=POLICY.fps,
                              preset="ultrafast", crf=30)
        parts.append(path)
    joined = tmp_path / "joined.mp4"
    assert ffmpeg.concat_video(parts, joined) == pytest.approx(6.0, abs=0.2)


def test_render_resumes_and_replaces_only_damaged_segments(tmp_path, plates):
    specs = [SegmentSpec(i, plates[i % len(plates)], 2.0, "pan_right")
             for i in range(4)]
    segments = tmp_path / "segments"

    _, rendered, reused = render_segments(specs, segments, POLICY, workers=2)
    assert (rendered, reused) == (4, 0)

    paths, rendered, reused = render_segments(specs, segments, POLICY, workers=2)
    assert (rendered, reused) == (0, 4), "a resumed render must reuse its work"

    # A file left truncated by a killed process exists and is the wrong
    # length; stream-copying it would silently shorten the documentary.
    paths[2].write_bytes(b"truncated")
    _, rendered, reused = render_segments(specs, segments, POLICY, workers=2)
    assert (rendered, reused) == (1, 3)


def test_a_truncated_segment_is_not_considered_usable(tmp_path, plates):
    path = tmp_path / "seg.mp4"
    ffmpeg.render_segment(plates[0], path, duration_s=3.0, fps=POLICY.fps,
                          preset="ultrafast", crf=30)
    assert _is_usable(path, 3.0) is True
    assert _is_usable(path, 9.0) is False, "wrong length must be rejected"
    path.write_bytes(b"x")
    assert _is_usable(path, 3.0) is False


def test_worker_count_never_exceeds_half_the_cores():
    """More workers than this makes x264 threads contend and finishes slower."""
    import os
    assert default_workers(RenderPolicy(segment_workers=64)) <= max(1, (os.cpu_count() or 2) // 2)


# -------------------------------------------------------------------- audio
def test_narration_and_music_are_mixed_to_the_narration_length(tmp_path):
    from echoes.providers.tts.silent import SilentProvider
    narration = tmp_path / "n.wav"
    SilentProvider(150.0).synthesize(" ".join(["word"] * 300), narration)
    joined = tmp_path / "narr.wav"
    total = ffmpeg.concat_audio([narration], joined)

    bed = tmp_path / "bed.wav"
    ffmpeg._run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=frequency=110:duration=4", "-c:a", "pcm_s16le",
                 str(bed)])

    mixed = tmp_path / "mixed.m4a"
    # The bed is 4s and the narration is two minutes: it must loop, and the
    # result must end with the narration rather than with the music.
    got = ffmpeg.mix_narration_with_bed(narration, bed, mixed)
    assert got == pytest.approx(total, abs=1.0)


def test_mux_produces_a_file_with_both_streams(tmp_path, plates):
    from echoes.providers.tts.silent import SilentProvider
    picture = tmp_path / "p.mp4"
    ffmpeg.render_segment(plates[0], picture, duration_s=4.0, fps=POLICY.fps,
                          preset="ultrafast", crf=30)
    raw = tmp_path / "n.wav"
    SilentProvider(150.0).synthesize(" ".join(["word"] * 10), raw)
    audio = tmp_path / "a.m4a"
    ffmpeg.normalise_loudness(raw, audio)

    final = tmp_path / "final.mp4"
    ffmpeg.mux(picture, audio, final)
    info = ffmpeg.probe(final)
    assert info.has_video and info.has_audio


# ------------------------------------------------- regressions worth keeping
def test_parts_with_different_sample_rates_concatenate_to_the_right_length(tmp_path):
    """The concat demuxer applies the FIRST file's rate to every input.

    A 1.6-second gap written at 48 kHz, placed behind 16 kHz narration, was
    read as 16 kHz and played for 4.8 seconds. Across one documentary that
    silently added 51 seconds of audio the picture track knew nothing about,
    and the render refused to mux.
    """
    speech_a = tmp_path / "a16.wav"
    speech_b = tmp_path / "b16.wav"
    for path, freq in ((speech_a, 200), (speech_b, 300)):
        ffmpeg._run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"sine=frequency={freq}:duration=10:sample_rate=16000",
                     "-c:a", "pcm_s16le", str(path)])
    gap = tmp_path / "gap48.wav"
    ffmpeg.silence(gap, 1.6, sample_rate=48000)

    total = ffmpeg.concat_audio([speech_a, gap, speech_b], tmp_path / "joined.wav",
                                sample_rate=48000)
    assert total == pytest.approx(21.6, abs=0.05)


def test_chapter_spans_tile_the_whole_recording(tmp_path):
    """Every second of narration must belong to a chapter.

    The gaps between chapters and the tail after the last word are narration
    time. If no chapter owns them the visual timeline is built short and the
    picture ends before the voice does.
    """
    from echoes.pipeline import narration as narration_mod
    from echoes.providers.tts.silent import SilentProvider

    chapters = [{"id": i + 1, "ordinal": i, "heading": f"C{i}",
                 "body": " ".join(["word"] * 120)} for i in range(4)]
    result = narration_mod.run(
        SilentProvider(150.0), chapters, tmp_path, max_chars=300,
        voice="silent", length_scale=1.0,
    )
    timings = result.timings
    assert timings[0].start_s == 0.0
    for i in range(len(timings) - 1):
        assert timings[i].start_s + timings[i].duration_s == \
            pytest.approx(timings[i + 1].start_s, abs=0.01)
    covered = sum(t.duration_s for t in timings)
    assert covered == pytest.approx(result.duration_s, abs=0.05)


def test_the_visual_timeline_covers_the_whole_narration(tmp_path):
    """End to end: chapter spans -> segment spans -> total picture length."""
    from echoes.pipeline import narration as narration_mod
    from echoes.pipeline.visuals import split_chapter
    from echoes.providers.tts.silent import SilentProvider

    chapters = [{"id": i + 1, "ordinal": i, "heading": f"C{i}",
                 "body": " ".join(["word"] * 200)} for i in range(5)]
    result = narration_mod.run(
        SilentProvider(150.0), chapters, tmp_path, max_chars=400,
        voice="silent", length_scale=1.0,
    )
    picture = 0.0
    for timing in result.timings:
        picture += sum(d for _, d in split_chapter(timing.start_s, timing.duration_s))
    assert picture == pytest.approx(result.duration_s, abs=0.05)
