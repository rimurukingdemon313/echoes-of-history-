"""Quality control.

The last gate before anything leaves the building. It re-derives every fact
it checks from the artefacts themselves -- it probes the rendered file rather
than trusting the render stage's report, because the whole point of a final
check is to catch the case where an earlier stage was wrong.

Checks are either **blocking** or **advisory**. A blocking failure means no
upload, full stop. An advisory failure is recorded on the video row and shown
on the dashboard, because some things are worth an operator's attention
without being worth discarding ninety minutes of work.

Two blocking checks exist specifically to stop dry-run scaffolding reaching a
real channel: synthetic visuals and offline-generated scripts are refused
whenever ``DRY_RUN`` is false. Without them, a misconfigured deployment would
publish gradient images narrated by a simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..logging import get_logger
from ..media import ffmpeg
from ..providers.llm.offline import SYNTHETIC_MARKER
from .metadata import DESCRIPTION_MAX, TITLE_MAX, chapters_valid
from .script import internal_repetition, style_violations

log = get_logger(__name__)

# Above this, chapters are restating each other rather than progressing.
MAX_INTERNAL_REPETITION = 0.18
# Above this, the script reuses another documentary's material.
MAX_CROSS_REPETITION = 0.12
# Roughly 60 kbps averaged over the file. Deliberately far below what this
# renderer actually produces (~500 kbps on archive stills), so it flags a
# broken or black render without ever objecting to one that merely
# compressed well.
MIN_BYTES_PER_SECOND = 7_500


@dataclass
class Check:
    name: str
    passed: bool
    blocking: bool
    detail: str = ""


@dataclass
class QCReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, *, blocking: bool = True,
            detail: str = "") -> None:
        self.checks.append(Check(name, passed, blocking, detail))

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.blocking]

    @property
    def advisories(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.blocking]

    @property
    def passed(self) -> bool:
        return not self.blocking_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking_failures": [
                {"check": c.name, "detail": c.detail} for c in self.blocking_failures
            ],
            "advisories": [
                {"check": c.name, "detail": c.detail} for c in self.advisories
            ],
            "checks_run": len(self.checks),
        }

    def summary(self) -> str:
        if self.passed and not self.advisories:
            return f"all {len(self.checks)} checks passed"
        if self.passed:
            return (f"passed with {len(self.advisories)} advisory: "
                    + "; ".join(f"{c.name} ({c.detail})" for c in self.advisories[:4]))
        return "BLOCKED: " + "; ".join(
            f"{c.name} ({c.detail})" for c in self.blocking_failures
        )


def run(
    *,
    settings,
    video_path: Path | None,
    thumbnail_path: Path | None,
    narration_duration_s: float,
    chapters: Sequence[dict[str, Any]],
    chapter_bodies: Sequence[str],
    title: str,
    description: str,
    tags: Sequence[str],
    sources: Sequence[dict[str, Any]],
    visual_assets: Sequence[dict[str, Any]],
    unsupported_ratio: float,
    previous_scripts: Sequence[str] = (),
    dry_run: bool = True,
) -> QCReport:
    report = QCReport()
    duration = settings.duration

    # ---- script ---------------------------------------------------------
    full_script = "\n\n".join(chapter_bodies)
    words = len(full_script.split())
    report.add("script_exists", bool(full_script.strip()),
               detail=f"{words} words")
    report.add("script_long_enough", words >= duration.min_words(),
               detail=f"{words} words, floor {duration.min_words()}")

    violations = style_violations(full_script)
    report.add("script_style", not violations,
               detail=", ".join(violations) if violations else "clean")

    repetition = internal_repetition(
        [type("C", (), {"body": b})() for b in chapter_bodies]  # duck-typed
    ) if len(chapter_bodies) > 1 else 0.0
    report.add("script_not_repetitive", repetition <= MAX_INTERNAL_REPETITION,
               blocking=not dry_run,
               detail=f"max chapter overlap {repetition:.3f}, "
                      f"limit {MAX_INTERNAL_REPETITION}")

    worst_cross = 0.0
    for previous in previous_scripts:
        from .topics import text_overlap
        worst_cross = max(worst_cross, text_overlap(full_script, previous))
    report.add("script_original_vs_previous", worst_cross <= MAX_CROSS_REPETITION,
               detail=f"max overlap with an earlier script {worst_cross:.3f}")

    report.add("script_not_synthetic",
               SYNTHETIC_MARKER not in full_script or dry_run,
               detail="script was produced by the offline simulator"
                      if SYNTHETIC_MARKER in full_script else "genuine")

    # ---- research -------------------------------------------------------
    report.add("sources_present",
               len(sources) >= settings.min_sources_per_documentary,
               detail=f"{len(sources)} sources, "
                      f"minimum {settings.min_sources_per_documentary}")
    fixture_sources = [s for s in sources
                       if str(s.get("provider") or "") == "fixtures"
                       or str(s.get("url") or "").startswith("https://fixtures.invalid/")]
    report.add("sources_not_fixtures", not fixture_sources or dry_run,
               detail=f"{len(fixture_sources)} offline fixture sources"
                      if fixture_sources else "all sources are real")
    report.add("factcheck_within_limit",
               unsupported_ratio <= settings.max_unsupported_claim_ratio,
               detail=f"{unsupported_ratio:.1%} unsupported, limit "
                      f"{settings.max_unsupported_claim_ratio:.1%}")

    # ---- narration ------------------------------------------------------
    minutes = narration_duration_s / 60.0
    report.add("narration_exists", narration_duration_s > 0,
               detail=f"{minutes:.1f} min")
    within = duration.min_minutes <= minutes <= duration.max_minutes
    report.add("narration_duration_in_range", within,
               detail=f"{minutes:.1f} min, allowed "
                      f"{duration.min_minutes}-{duration.max_minutes}")
    near_target = abs(minutes - duration.target_minutes) <= duration.tolerance_minutes
    report.add("narration_near_target", near_target, blocking=False,
               detail=f"{minutes:.1f} min vs target {duration.target_minutes} "
                      f"+/- {duration.tolerance_minutes}")

    # ---- visuals --------------------------------------------------------
    report.add("visuals_exist", len(visual_assets) > 0,
               detail=f"{len(visual_assets)} segments")
    unlicensed = [a for a in visual_assets if not str(a.get("licence") or "").strip()]
    report.add("visuals_licensed", not unlicensed,
               detail=f"{len(unlicensed)} assets without a licence")

    synthetic = [a for a in visual_assets if a.get("provider") == "synthetic"]
    report.add("visuals_not_synthetic", not synthetic or dry_run,
               detail=f"{len(synthetic)} synthetic plates"
                      if synthetic else "all sourced")

    hashes = [a.get("perceptual_hash") for a in visual_assets if a.get("perceptual_hash")]
    unique_ratio = len(set(hashes)) / len(hashes) if hashes else 1.0
    report.add("visuals_varied", unique_ratio >= 0.5, blocking=False,
               detail=f"{unique_ratio:.0%} of segments use a distinct image")

    # ---- the rendered file ---------------------------------------------
    if video_path and Path(video_path).exists():
        try:
            info = ffmpeg.probe(Path(video_path))
            report.add("video_readable", True,
                       detail=f"{info.duration_s / 60:.1f} min, "
                              f"{info.size_bytes / 1_048_576:.0f} MB")
            report.add("video_has_audio", info.has_audio, detail="audio stream")
            report.add("video_has_picture", info.has_video, detail="video stream")
            report.add("video_resolution",
                       info.width == settings.render.width
                       and info.height == settings.render.height,
                       detail=f"{info.width}x{info.height}, expected "
                              f"{settings.render.width}x{settings.render.height}")
            # The rendered file must match the narration it was built for.
            drift = abs(info.duration_s - narration_duration_s)
            report.add("video_matches_narration", drift <= 5.0,
                       detail=f"{drift:.1f}s apart from the narration")
            # Judged against the file's own duration, not a fixed size. A
            # flat 1 MB floor passes a 90-minute file of black frames, which
            # is precisely the failure this check exists to catch.
            expected_min = max(200_000, int(info.duration_s * MIN_BYTES_PER_SECOND))
            report.add("video_not_empty", info.size_bytes >= expected_min,
                       detail=f"{info.size_bytes / 1_048_576:.1f} MB for "
                              f"{info.duration_s / 60:.1f} min; expected at "
                              f"least {expected_min / 1_048_576:.1f} MB")
        except Exception as exc:  # noqa: BLE001
            report.add("video_readable", False,
                       detail=f"could not probe the rendered file: {exc}")
    else:
        report.add("video_readable", False, detail="no rendered file")

    # ---- publishing metadata -------------------------------------------
    report.add("title_present", bool(title.strip()), detail=title[:80])
    report.add("title_length", len(title) <= TITLE_MAX,
               detail=f"{len(title)} chars, limit {TITLE_MAX}")
    report.add("description_present", len(description.strip()) > 50,
               detail=f"{len(description)} chars")
    report.add("description_length", len(description) <= DESCRIPTION_MAX,
               detail=f"{len(description)} chars, limit {DESCRIPTION_MAX}")
    if chapters:
        ok, why = chapters_valid(chapters)
        report.add("chapters_valid", ok, detail=why)
    else:
        # No markers is legal; broken markers are not.
        report.add("chapters_present", False, blocking=False,
                   detail="too few qualifying sections for chapter markers")
    report.add("tags_present", len(tags) > 0, blocking=False,
               detail=f"{len(tags)} tags")

    if thumbnail_path and Path(thumbnail_path).exists():
        size = Path(thumbnail_path).stat().st_size
        report.add("thumbnail_exists", True, detail=f"{size // 1024} KB")
        # YouTube refuses thumbnails over 2 MB.
        report.add("thumbnail_within_limit", size <= 2 * 1024 * 1024,
                   detail=f"{size // 1024} KB, limit 2048 KB")
    else:
        report.add("thumbnail_exists", False, detail="no thumbnail file")

    level = "info" if report.passed else "error"
    getattr(log, level)("quality control complete",
                        extra={"passed": report.passed,
                               "blocking": len(report.blocking_failures),
                               "advisories": len(report.advisories),
                               "summary": report.summary()[:400]})
    return report
