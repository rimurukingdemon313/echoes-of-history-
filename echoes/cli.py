"""Command line entry points.

Everything the system does can be driven from here, which is what makes it
debuggable: the same code path n8n triggers over HTTP can be run in a
terminal with the output in front of you.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_settings
from .db import migrate, pool
from .logging import configure, get_logger
from .orchestrator import Orchestrator

log = get_logger(__name__)


def _orchestrator():
    settings = load_settings()
    pool.init_pool(settings.database_url)
    return Orchestrator(settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="echoes",
                                     description="Echoes of History production system")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("status", help="print system status")
    sub.add_parser("doctor", help="check configuration and providers")
    sub.add_parser("reconcile", help="resolve uploads whose outcome is unknown")

    calibrate = sub.add_parser(
        "calibrate",
        help="fit PIPER_LENGTH_SCALE so the voice hits NARRATION_WPM")
    calibrate.add_argument("--target-wpm", type=float)
    calibrate.add_argument("--save", action="store_true",
                           help="store the measured rate for the script engine")

    topics = sub.add_parser("topics", help="add candidate topics")
    topics.add_argument("--count", type=int, default=8)

    produce = sub.add_parser("produce", help="produce one documentary")
    produce.add_argument("--topic-id", type=int)
    produce.add_argument("--key", help="idempotency key")
    produce.add_argument("--live", action="store_true",
                         help="disable DRY_RUN for this run")
    produce.add_argument("--publish-mode")

    resume = sub.add_parser("resume", help="resume a job")
    resume.add_argument("job_id", type=int)

    args = parser.parse_args(argv)
    configure(args.log_level)

    if args.command == "migrate":
        settings = load_settings()
        pool.init_pool(settings.database_url)
        applied = migrate.migrate()
        print(json.dumps({"applied": applied}, indent=2))
        return 0

    if args.command == "doctor":
        return _doctor()

    if args.command == "calibrate":
        return _calibrate(args.target_wpm, args.save)

    orchestrator = _orchestrator()

    if args.command == "status":
        print(json.dumps(orchestrator.status(), indent=2, default=str))
        return 0

    if args.command == "reconcile":
        print(json.dumps(orchestrator.reconcile_uploads(), indent=2, default=str))
        return 0

    if args.command == "topics":
        print(json.dumps(orchestrator.replenish_topics(want=args.count),
                         indent=2, default=str))
        return 0

    if args.command == "produce":
        result = orchestrator.start(
            topic_id=args.topic_id, idempotency_key=args.key,
            dry_run=False if args.live else None,
            publish_mode=args.publish_mode,
        )
        print(json.dumps(result.summary(), indent=2, default=str))
        return 0 if result.succeeded else 1

    if args.command == "resume":
        result = orchestrator.resume(args.job_id)
        print(json.dumps(result.summary(), indent=2, default=str))
        return 0 if result.succeeded else 1

    return 2


def _calibrate(target_wpm: float | None, save: bool) -> int:
    """Find the length scale that makes this voice speak at the target rate.

    Worth doing once per voice. The relationship is not ``1 / length_scale``:
    sentence-final pauses do not stretch with the scale, so the fit has a
    non-zero intercept. Two probes are taken and a line solved through them.

    The result is a *starting* figure. The real rate on documentary prose is
    lower again -- measured at 133 words per minute against a configured 150,
    because real prose has far more sentence breaks than a calibration
    passage. ``--save`` records the measured rate so the script engine sizes
    its next script from it.
    """
    from .providers.tts.calibrate import measure_wpm, solve_length_scale
    from .providers.tts.piper import PiperProvider

    settings = load_settings()
    target = target_wpm or settings.duration.words_per_minute

    if settings.tts_provider.lower() != "piper":
        print(json.dumps({
            "error": f"calibration applies to TTS_PROVIDER=piper; this "
                     f"deployment uses {settings.tts_provider!r}"}, indent=2))
        return 1

    probe = PiperProvider(settings.piper_voice, settings.piper_voice_dir)
    if not probe.available():
        print(json.dumps({
            "error": f"no Piper voice found for {settings.piper_voice!r} in "
                     f"{settings.piper_voice_dir}"}, indent=2))
        return 1

    def make(scale: float):
        return PiperProvider(settings.piper_voice, settings.piper_voice_dir,
                             length_scale=scale)

    scale, predicted = solve_length_scale(make, target)
    actual = measure_wpm(make(scale))

    report = {
        "voice": settings.piper_voice,
        "target_wpm": round(target, 1),
        "length_scale": round(scale, 3),
        "predicted_wpm": round(predicted, 1),
        "measured_wpm": round(actual, 1),
        "set_this": f"PIPER_LENGTH_SCALE={scale:.3f}",
        "note": ("This is measured on a calibration passage. Real documentary "
                 "prose runs slower; the narration stage measures the true "
                 "rate and feeds it back automatically."),
    }

    if save:
        pool.init_pool(settings.database_url)
        from .db import repo
        repo.set_setting(f"measured_wpm:{settings.piper_voice}:{scale:.3f}",
                         round(actual, 2))
        report["saved"] = True

    print(json.dumps(report, indent=2))
    return 0


def _doctor() -> int:
    """Check everything that can be checked without producing anything."""
    from .media.ffmpeg import have_ffmpeg
    from .providers.registry import build

    problems: list[str] = []
    settings = load_settings()
    report: dict[str, object] = {}

    try:
        settings.validate()
        report["config"] = "ok"
    except Exception as exc:  # noqa: BLE001
        report["config"] = str(exc)
        problems.append("config")

    report["ffmpeg"] = "ok" if have_ffmpeg() else "MISSING"
    if not have_ffmpeg():
        problems.append("ffmpeg")

    try:
        pool.init_pool(settings.database_url)
        report["database"] = "ok" if pool.healthy() else "unreachable"
        if not pool.healthy():
            problems.append("database")
        report["pending_migrations"] = [p.name for p in migrate.pending()]
    except Exception as exc:  # noqa: BLE001
        report["database"] = str(exc)
        problems.append("database")

    try:
        providers = build(settings)
        report["providers"] = providers.describe()
        if not providers.tts.available():
            report["tts_warning"] = (
                f"no voice model found for {settings.piper_voice!r} in "
                f"{settings.piper_voice_dir}"
            )
            problems.append("tts")
        if not providers.storage.durable():
            report["storage_warning"] = (
                "storage is not durable across redeploys; mount a volume at "
                f"{settings.data_dir} or set STORAGE_PROVIDER=s3"
            )
    except Exception as exc:  # noqa: BLE001
        report["providers"] = str(exc)
        problems.append("providers")

    report["problems"] = problems
    print(json.dumps(report, indent=2, default=str))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
