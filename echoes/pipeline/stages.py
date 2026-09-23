"""The stages, wired to the database.

Each stage reads what it needs from Postgres rather than from the previous
stage's return value. That is what makes resumption work: a stage restarted
in a fresh process finds the same inputs the original had, because they were
written down rather than passed in memory.

Stage return values are small summaries for the log and the dashboard. They
are never the mechanism by which data moves between stages.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..db import repo
from ..errors import Permanent, QualityGateFailed
from ..logging import get_logger
from ..media import ffmpeg
from ..providers.llm.offline import SYNTHETIC_GENERATORS
from ..providers.notify.base import Level
from ..version import version_stamp
from . import factcheck, metadata as meta_mod, narration, qc, render, research
from . import script as script_mod
from . import thumbnail as thumb_mod
from . import visuals as visuals_mod
from .runner import Context, notify
from .upload import UploadResult, YouTubeClient

log = get_logger(__name__)


def _wpm_key(voice: str, length_scale: float) -> str:
    return f"measured_wpm:{voice}:{length_scale:.3f}"


def effective_wpm(ctx: Context) -> float:
    """Words per minute to size the script with.

    The configured ``NARRATION_WPM`` is a starting estimate. The rate a voice
    actually delivers on documentary prose is lower than a short calibration
    passage suggests -- measured at 133 against a configured 150, because
    real prose has far more sentence-final pauses, and the inter-chunk and
    inter-chapter gaps are narration time too.

    Sizing a 90-minute script at the optimistic figure produces a 100-minute
    documentary. So once a production has measured the real rate for this
    voice and speed, that measurement is used instead, and the length error
    closes after the first run rather than persisting for every future one.
    """
    settings = ctx.settings
    measured = repo.get_setting(
        _wpm_key(ctx.providers.tts.voice, settings.piper_length_scale))
    if isinstance(measured, (int, float)) and 60 <= float(measured) <= 260:
        return float(measured)
    return settings.duration.words_per_minute


class ResearchStage:
    name = "research"

    def run(self, ctx: Context) -> dict[str, Any]:
        result = research.run(
            ctx.providers.llm, ctx.providers.research, ctx.topic,
            min_sources=ctx.settings.min_sources_per_documentary,
        )
        # Sources are written first so facts can point at their row ids.
        source_ids: list[int] = []
        for doc in result.sources:
            row = repo.upsert_source(
                url=doc.url, title=doc.title, provider=doc.provider,
                source_type=doc.source_type, author=doc.author,
                published=doc.published, licence=doc.licence,
                quality=doc.quality, rationale=doc.rationale,
            )
            source_ids.append(int(row["id"]))

        package = repo.create_research_package(
            job_id=ctx.job_id, topic_id=ctx.topic_id, summary=result.summary,
            source_count=len(result.sources), conflicts=result.conflicts,
        )
        facts = []
        for fact in result.facts:
            index = fact.pop("source_index", None)
            facts.append({**fact,
                          "source_id": source_ids[index]
                          if index is not None and index < len(source_ids) else None})
        repo.add_research_facts(int(package["id"]), facts)
        repo.set_topic_status(ctx.topic_id, "RESEARCHED")

        return {"sources": len(result.sources), "facts": len(facts),
                "conflicts": len(result.conflicts), "summary": result.summary}


class ScriptStage:
    name = "script"

    def run(self, ctx: Context) -> dict[str, Any]:
        package = repo.research_package_for_job(ctx.job_id)
        if not package:
            raise Permanent("script stage ran before research was stored")
        facts = repo.research_facts(int(package["id"]))
        conflicts = package.get("conflicts") or []
        if isinstance(conflicts, str):
            conflicts = json.loads(conflicts)

        duration = ctx.settings.duration
        wpm = effective_wpm(ctx)
        if abs(wpm - duration.words_per_minute) > 1:
            log.info("sizing the script from a measured speaking rate",
                     extra={"measured_wpm": round(wpm, 1),
                            "configured_wpm": duration.words_per_minute})
        result = script_mod.generate(
            ctx.providers.llm, str(ctx.topic["title"]), facts, conflicts,
            minutes=duration.target_minutes,
            wpm=wpm,
            min_words=int(duration.min_minutes * wpm),
        )
        if result.shortfall_words > 0:
            raise Permanent(
                f"the script is {result.word_count} words, "
                f"{result.shortfall_words} short of the "
                f"{duration.min_minutes}-minute floor at {wpm:.0f} words per "
                f"minute. Padding it would repeat material; widen the "
                f"research instead."
            )

        # Which provider wrote this is recorded as a column, never as a
        # marker inside the prose -- the prose is the narration script, and a
        # marker in it would be read aloud by the voice.
        script_row = repo.create_script(job_id=ctx.job_id, topic_id=ctx.topic_id,
                                        version=1,
                                        generator=ctx.providers.llm.name)
        repo.replace_chapters(int(script_row["id"]), [
            {"heading": c.heading, "body": c.body} for c in result.chapters
        ])
        repo.set_script_duration(int(script_row["id"]), result.estimated_s,
                                 "DRAFT")
        repo.set_topic_status(ctx.topic_id, "SCRIPTING")

        return {"chapters": len(result.chapters), "words": result.word_count,
                "estimated_minutes": round(result.estimated_minutes, 1)}


class FactCheckStage:
    name = "factcheck"

    def run(self, ctx: Context) -> dict[str, Any]:
        script_row = repo.get_script(ctx.job_id)
        package = repo.research_package_for_job(ctx.job_id)
        if not script_row or not package:
            raise Permanent("fact-check ran before the script was stored")

        chapters = repo.chapters(int(script_row["id"]))
        facts = repo.research_facts(int(package["id"]))
        bodies = [str(c["body"]) for c in chapters]

        result = factcheck.run(
            ctx.providers.llm, bodies, facts,
            max_unsupported_ratio=ctx.settings.max_unsupported_claim_ratio,
        )
        if result.revised_chapters:
            for index, body in result.revised_chapters.items():
                bodies[index] = body
            repo.replace_chapters(int(script_row["id"]), [
                {"heading": chapters[i]["heading"], "body": bodies[i]}
                for i in range(len(chapters))
            ])

        source_ids = [f["source_id"] for f in facts]
        stored = repo.replace_claims(int(script_row["id"]), [
            {"text": c.text, "kind": c.kind, "verdict": c.verdict, "note": c.note}
            for c in result.claims
        ])
        for claim_id, claim in zip(stored, result.claims):
            linked = [source_ids[i] for i in claim.source_indices
                      if i < len(source_ids) and source_ids[i]]
            repo.set_claim_verdict(claim_id, claim.verdict, note=claim.note,
                                   source_ids=linked)

        repo.set_script_duration(
            int(script_row["id"]),
            float(script_row.get("estimated_s") or 0), "FACT_CHECKED")
        repo.set_topic_status(ctx.topic_id, "READY_FOR_PRODUCTION")

        return {"claims": len(result.claims),
                "unsupported_ratio": round(result.unsupported_ratio, 4),
                "revised_chapters": len(result.revised_chapters),
                **{f"verdict_{k}": v for k, v in result.counts().items()}}


class NarrationStage:
    name = "narration"

    def run(self, ctx: Context) -> dict[str, Any]:
        script_row = repo.get_script(ctx.job_id)
        if not script_row:
            raise Permanent("narration ran before the script was stored")
        chapters = repo.chapters(int(script_row["id"]))
        settings = ctx.settings
        tts = ctx.providers.tts

        audio_job = repo.create_audio_job(
            job_id=ctx.job_id, script_id=int(script_row["id"]),
            provider=tts.name, voice=tts.voice, chunk_total=0,
        )
        audio_job_id = int(audio_job["id"])
        existing = {int(r["ordinal"]): r for r in repo.audio_chunks(audio_job_id)}

        def on_chunk(chunk, path, duration, digest) -> None:
            row = repo.upsert_audio_chunk(
                audio_job_id=audio_job_id, ordinal=chunk.ordinal,
                chapter_id=chunk.chapter_id, text_hash=digest,
            )
            repo.complete_audio_chunk(int(row["id"]), str(path), duration)

        result = narration.run(
            tts, chapters, ctx.stage_dir("narration"),
            max_chars=settings.tts_chunk_chars,
            voice=tts.voice,
            length_scale=settings.piper_length_scale,
            sample_rate=settings.render.audio_sample_rate,
            existing=existing, on_chunk=on_chunk,
        )

        for timing in result.timings:
            if timing.chapter_id:
                repo.set_chapter_timing(int(timing.chapter_id),
                                        timing.start_s, timing.duration_s)
        repo.finish_audio_job(audio_job_id, result.duration_s,
                              str(result.audio_path))
        # Feed the measured rate forward so the next script is sized from
        # evidence rather than from the configured estimate.
        if 60 <= result.measured_wpm <= 260:
            repo.set_setting(_wpm_key(tts.voice, settings.piper_length_scale),
                             round(result.measured_wpm, 2))
        repo.set_topic_status(ctx.topic_id, "PRODUCING")

        return {"duration_s": round(result.duration_s, 1),
                "minutes": round(result.duration_s / 60.0, 1),
                "synthesised": result.chunks_synthesised,
                "reused": result.chunks_reused,
                "measured_wpm": round(result.measured_wpm, 1),
                "audio_path": str(result.audio_path)}


class VisualsStage:
    name = "visuals"

    def run(self, ctx: Context) -> dict[str, Any]:
        script_row = repo.get_script(ctx.job_id)
        if not script_row:
            raise Permanent("visuals ran before the script was stored")
        chapters = repo.chapters(int(script_row["id"]))

        timings = [
            narration.ChapterTiming(
                chapter_id=int(c["id"]), ordinal=int(c["ordinal"]),
                heading=str(c["heading"]),
                start_s=float(c["start_s"] or 0.0),
                duration_s=float(c["duration_s"] or 0.0),
            )
            for c in chapters if c.get("duration_s")
        ]
        if not timings:
            raise Permanent(
                "no chapter timings are recorded; narration must run before "
                "visuals so the picture can be cut to the measured audio"
            )

        planned = visuals_mod.plan(
            ctx.providers.images, str(ctx.topic["title"]), chapters, timings,
            ctx.stage_dir("visuals"),
            plate_width=ctx.settings.render.plate_width,
            plate_height=ctx.settings.render.plate_height,
            contact_email=ctx.settings.contact_email,
        )
        repo.replace_visual_assets(ctx.job_id, [p.to_row() for p in planned])

        return {"segments": len(planned),
                "unique_images": len({p.perceptual_hash for p in planned}),
                "covers_s": round(sum(p.duration_s for p in planned), 1)}


class RenderStage:
    name = "render"

    def run(self, ctx: Context) -> dict[str, Any]:
        ffmpeg.require_ffmpeg()
        assets = repo.visual_assets(ctx.job_id)
        audio_job = repo.get_audio_job(ctx.job_id)
        if not assets:
            raise Permanent("render ran before the visual plan was stored")
        if not audio_job or not audio_job.get("storage_key"):
            raise Permanent("render ran before narration produced audio")

        narration_path = Path(str(audio_job.get("local_path")
                                  or audio_job.get("storage_key") or ""))
        if not narration_path.exists():
            raise Permanent(f"narration audio is missing: {narration_path}")

        policy = ctx.settings.render
        work = ctx.stage_dir("render")
        audio_for_mux = self._prepare_audio(ctx, narration_path, work)

        render_job = repo.create_render_job(
            job_id=ctx.job_id, width=policy.width, height=policy.height,
            fps=policy.fps, segment_total=len(assets),
        )
        render_job_id = int(render_job["id"])

        specs: list[render.SegmentSpec] = []
        for asset in assets:
            plate = asset.get("plate_path")
            if not plate or not Path(plate).exists():
                raise Permanent(
                    f"segment {asset['ordinal']} has no prepared plate on disk; "
                    f"re-run the visuals stage"
                )
            row = repo.upsert_render_segment(
                render_job_id=render_job_id, ordinal=int(asset["ordinal"]),
                asset_id=int(asset["id"]),
                duration_s=float(asset["duration_s"] or 0.0),
            )
            specs.append(render.SegmentSpec(
                ordinal=int(asset["ordinal"]), plate=Path(plate),
                duration_s=float(asset["duration_s"] or 0.0),
                motion=str(asset.get("motion") or "pan_right"),
                asset_id=int(row["id"]),
            ))

        def on_done(spec: render.SegmentSpec, path: Path) -> None:
            if spec.asset_id:
                repo.complete_render_segment(spec.asset_id, str(path))

        paths, rendered, reused = render.render_segments(
            specs, work / "segments", policy, on_done=on_done)

        out_path = work / f"echoes-{ctx.job_id}.mp4"
        result = render.assemble(paths, audio_for_mux, out_path, policy,
                                 work_dir=work)

        storage = ctx.providers.storage
        stored_key = storage.put(out_path, f"videos/{ctx.job_id}/final.mp4")
        repo.finish_render_job(
            render_job_id, duration_s=result.duration_s,
            size_bytes=result.size_bytes, local_path=str(out_path),
            # Only claim a durable copy when the storage actually is durable.
            # Recording a key for a volume that vanishes on redeploy would
            # send the recovery path looking for a file that is not there.
            storage_key=stored_key if storage.durable() else None,
        )

        return {"duration_s": round(result.duration_s, 1),
                "minutes": round(result.duration_s / 60.0, 1),
                "mb": round(result.size_bytes / 1_048_576, 1),
                "segments_rendered": rendered, "segments_reused": reused,
                "video_path": str(out_path), "storage_key": stored_key}

    @staticmethod
    def _prepare_audio(ctx: Context, narration_path: Path, work: Path) -> Path:
        """Normalise loudness, adding a music bed when one is available."""
        policy = ctx.settings.render
        beds_dir = ctx.settings.beds_dir
        beds = sorted(beds_dir.glob("*.*")) if beds_dir.is_dir() else []
        beds = [b for b in beds
                if b.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".flac")]
        out = work / "audio-final.m4a"

        if beds:
            bed = beds[ctx.job_id % len(beds)]
            log.info("mixing narration with a music bed",
                     extra={"bed": bed.name})
            ffmpeg.mix_narration_with_bed(
                narration_path, bed, out,
                bed_gain_db=policy.music_gain_db, duck_db=policy.ducking_db,
                sample_rate=policy.audio_sample_rate, codec=policy.audio_codec,
                bitrate=policy.audio_bitrate, lufs=policy.loudness_lufs,
                true_peak=policy.loudness_true_peak,
            )
        else:
            log.info("no music bed available; normalising narration only",
                     extra={"looked_in": str(beds_dir)})
            ffmpeg.normalise_loudness(
                narration_path, out, lufs=policy.loudness_lufs,
                true_peak=policy.loudness_true_peak,
                sample_rate=policy.audio_sample_rate,
                codec=policy.audio_codec, bitrate=policy.audio_bitrate,
            )
        return out


class ThumbnailStage:
    name = "thumbnail"

    def run(self, ctx: Context) -> dict[str, Any]:
        assets = repo.visual_assets(ctx.job_id)
        plates = [Path(a["plate_path"]) for a in assets
                  if a.get("plate_path") and Path(a["plate_path"]).exists()]
        if not plates:
            raise Permanent("no plates on disk to build a thumbnail from")
        # Skip the very first plate: it is the opening image and tends to be
        # the most generic of the set.
        chosen = plates[1:4] or plates[:1]
        concept = thumb_mod.generate(chosen, str(ctx.topic["title"]),
                                     ctx.stage_dir("thumbnail"))
        return {"path": str(concept.path), "concept": concept.name,
                "contrast": round(concept.contrast, 3)}


class MetadataStage:
    name = "metadata"

    def run(self, ctx: Context) -> dict[str, Any]:
        script_row = repo.get_script(ctx.job_id)
        package = repo.research_package_for_job(ctx.job_id)
        render_job = repo.get_render_job(ctx.job_id)
        if not (script_row and package and render_job):
            raise Permanent("metadata ran before script, research or render")

        chapters = repo.chapters(int(script_row["id"]))
        timings = [
            narration.ChapterTiming(
                chapter_id=int(c["id"]), ordinal=int(c["ordinal"]),
                heading=str(c["heading"]),
                start_s=float(c["start_s"] or 0.0),
                duration_s=float(c["duration_s"] or 0.0),
            ) for c in chapters if c.get("duration_s")
        ]
        chapter_rows = meta_mod.build_chapters(timings)

        facts = repo.research_facts(int(package["id"]))
        sources: list[dict[str, Any]] = []
        seen: set[str] = set()
        for fact in facts:
            url = fact.get("source_url")
            if url and url not in seen:
                seen.add(url)
                sources.append({"title": fact.get("source_title"), "url": url})

        assets = repo.visual_assets(ctx.job_id)
        attributions = [str(a["attribution"]) for a in assets if a.get("attribution")]
        # One credit per distinct image, not per segment.
        attributions = list(dict.fromkeys(attributions))

        duration_s = float(render_job.get("duration_s") or 0.0)
        built = meta_mod.generate(
            ctx.providers.llm, str(ctx.topic["title"]), chapter_rows, sources,
            attributions, minutes=duration_s / 60.0,
            channel=ctx.settings.channel_name,
            category_id=ctx.settings.youtube_category_id,
            language=ctx.settings.youtube_language,
        )

        repo.upsert_video(
            job_id=ctx.job_id, topic_id=ctx.topic_id, title=built.title,
            description=built.description, tags=built.tags,
            chapters_json=built.chapters, duration_s=duration_s,
            video_path=str(render_job.get("local_path")
                           or render_job.get("storage_key") or ""),
            thumbnail_path=str(ctx.results.get("thumbnail", {}).get("path") or ""),
            version_stamp=version_stamp(),
        )
        return {"title": built.title, "description_chars": len(built.description),
                "tags": len(built.tags), "chapters": len(built.chapters)}


class QualityControlStage:
    name = "qc"

    def run(self, ctx: Context) -> dict[str, Any]:
        video = repo.get_video(ctx.job_id)
        script_row = repo.get_script(ctx.job_id)
        package = repo.research_package_for_job(ctx.job_id)
        audio_job = repo.get_audio_job(ctx.job_id)
        if not (video and script_row and package and audio_job):
            raise Permanent("quality control ran before the video was assembled")

        chapters = repo.chapters(int(script_row["id"]))
        facts = repo.research_facts(int(package["id"]))
        sources = [{"url": f.get("source_url")} for f in facts if f.get("source_url")]
        counts = repo.claim_verdict_counts(int(script_row["id"]))
        total_claims = sum(counts.values())
        unsupported = counts.get("unsupported", 0) + counts.get("contradicted", 0)
        ratio = unsupported / total_claims if total_claims else 0.0

        chapters_json = video.get("chapters") or []
        if isinstance(chapters_json, str):
            chapters_json = json.loads(chapters_json)
        tags = video.get("tags") or []
        if isinstance(tags, str):
            tags = json.loads(tags)

        report = qc.run(
            settings=ctx.settings,
            script_generator=str(script_row.get("generator") or "unknown"),
            video_path=Path(str(video["video_path"])) if video.get("video_path") else None,
            thumbnail_path=Path(str(video["thumbnail_path"]))
            if video.get("thumbnail_path") else None,
            narration_duration_s=float(audio_job.get("duration_s") or 0.0),
            chapters=chapters_json,
            chapter_bodies=[str(c["body"]) for c in chapters],
            title=str(video["title"]), description=str(video["description"]),
            tags=tags,
            sources=[{"url": u["url"]} for u in {s["url"]: s for s in sources}.values()],
            visual_assets=repo.visual_assets(ctx.job_id),
            unsupported_ratio=ratio,
            dry_run=ctx.dry_run,
        )
        repo.set_qc_report(int(video["id"]), report.to_dict())

        if not report.passed:
            raise QualityGateFailed(
                "quality control refused this documentary: " + report.summary(),
                [c.name for c in report.blocking_failures],
            )
        if report.advisories:
            log.warning("quality control passed with advisories",
                        extra={"advisories": [c.name for c in report.advisories]})
        repo.set_topic_status(ctx.topic_id, "READY_FOR_UPLOAD")
        return report.to_dict()


class UploadStage:
    name = "upload"

    def run(self, ctx: Context) -> dict[str, Any]:
        video = repo.get_video(ctx.job_id)
        if not video:
            raise Permanent("upload ran before the video record existed")

        path = Path(str(video["video_path"]))
        if not path.exists():
            raise Permanent(f"the rendered file is gone: {path}")
        size = path.stat().st_size

        privacy, publish_at = self._publish_plan(ctx)
        # The reservation is written before any bytes move. See upload.py.
        upload_row, reserved = repo.reserve_upload(
            job_id=ctx.job_id, video_id=int(video["id"]),
            idempotency_key=f"{ctx.job['idempotency_key']}:upload",
            privacy_status=privacy, publish_at=publish_at, bytes_total=size,
        )
        upload_id = int(upload_row["id"])

        if upload_row.get("youtube_video_id"):
            log.info("this job already uploaded; not sending it again",
                     extra={"youtube_video_id": upload_row["youtube_video_id"]})
            return {"youtube_video_id": upload_row["youtube_video_id"],
                    "already_uploaded": True}

        if ctx.dry_run:
            repo.set_upload_status(upload_id, "SKIPPED_DRY_RUN")
            log.info("dry run: everything is ready, nothing was uploaded",
                     extra={"bytes": size, "privacy": privacy})
            return {"skipped": "dry_run", "would_upload_bytes": size,
                    "privacy_status": privacy,
                    "publish_at": publish_at.isoformat() if publish_at else None}

        client = YouTubeClient(ctx.settings.youtube_client_id,
                               ctx.settings.youtube_client_secret,
                               ctx.settings.youtube_refresh_token)
        result = self._send(ctx, client, upload_row, upload_id, path, size,
                            video, privacy, publish_at)

        thumb = video.get("thumbnail_path")
        if thumb and Path(str(thumb)).exists():
            client.set_thumbnail(result.youtube_video_id, Path(str(thumb)))

        repo.set_upload_status(upload_id, "SUCCEEDED",
                               youtube_video_id=result.youtube_video_id,
                               bytes_sent=result.bytes_sent)
        repo.set_topic_status(ctx.topic_id, "UPLOADED")
        notify(ctx, Level.INFO, "upload_succeeded",
               f"{video['title']}\nhttps://youtu.be/{result.youtube_video_id} "
               f"({privacy})")
        return {"youtube_video_id": result.youtube_video_id,
                "privacy_status": privacy, "bytes": result.bytes_sent,
                "resumed": result.resumed}

    @staticmethod
    def _publish_plan(ctx: Context) -> tuple[str, datetime | None]:
        mode = ctx.job.get("publish_mode") or ctx.settings.publish_mode
        if mode != "scheduled":
            return mode, None
        tz = ZoneInfo(ctx.settings.timezone)
        local = ctx.clock.now().astimezone(tz)
        target = local.replace(hour=ctx.settings.publish_hour_local, minute=0,
                               second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        # A scheduled publish is uploaded private with a publishAt; see
        # upload.py for why the two must go together.
        return "private", target.astimezone(timezone.utc)

    def _send(self, ctx, client, upload_row, upload_id, path, size, video,
              privacy, publish_at) -> UploadResult:
        session_url = upload_row.get("upload_url")
        start_at = 0
        resumed = False

        if session_url:
            state, received, resource = client.session_progress(session_url, size)
            if state == "complete" and resource:
                video_id = resource.get("id")
                if video_id:
                    log.info("the previous attempt had already finished",
                             extra={"youtube_video_id": video_id})
                    return UploadResult(video_id, privacy, publish_at, size, True)
            elif state == "incomplete":
                start_at, resumed = received, True
                log.info("resuming an interrupted upload",
                         extra={"from_byte": start_at, "of": size})
            else:
                session_url = None

        if not session_url:
            session_url = client.start_session(
                title=str(video["title"]), description=str(video["description"]),
                tags=list(video.get("tags") or []),
                category_id=ctx.settings.youtube_category_id,
                language=ctx.settings.youtube_language,
                privacy_status=privacy, publish_at=publish_at, size_bytes=size,
            )
        repo.set_upload_status(upload_id, "IN_FLIGHT", upload_url=session_url,
                               bytes_sent=start_at)

        def progress(sent: int, total: int) -> None:
            repo.set_upload_status(upload_id, "IN_FLIGHT", bytes_sent=sent)

        resource = client.upload(session_url, path, size_bytes=size,
                                 start_at=start_at, on_progress=progress)
        video_id = (resource or {}).get("id")
        if not video_id:
            raise Permanent("YouTube accepted the upload but returned no video id")
        return UploadResult(video_id, privacy, publish_at, size, resumed)


class RecordStage:
    name = "record"

    def run(self, ctx: Context) -> dict[str, Any]:
        upload = repo.get_upload(ctx.job_id)
        video = repo.get_video(ctx.job_id)
        youtube_id = (upload or {}).get("youtube_video_id")

        if youtube_id:
            url = f"https://www.youtube.com/watch?v={youtube_id}"
            repo.mark_topic_published(ctx.topic_id, url, ctx.clock.now())
            notify(ctx, Level.INFO, "published",
                   f"{(video or {}).get('title', 'Documentary')}\n{url}")
            published = True
        else:
            # A dry run reaches here having produced everything but the
            # upload. The topic stays claimed so it is not produced twice.
            repo.set_topic_status(ctx.topic_id, "READY_FOR_UPLOAD")
            url = None
            published = False

        # The render is only safe to tidy once the outcome is recorded.
        cleaned = 0
        if published or ctx.dry_run:
            cleaned = render.cleanup_segments(
                ctx.work_dir / "render" / "segments")

        return {"published": published, "youtube_url": url,
                "segments_cleaned": cleaned,
                "version_stamp": version_stamp()}


ALL_STAGES = (
    ResearchStage(), ScriptStage(), FactCheckStage(), NarrationStage(),
    VisualsStage(), RenderStage(), ThumbnailStage(), MetadataStage(),
    QualityControlStage(), UploadStage(), RecordStage(),
)
