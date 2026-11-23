from __future__ import annotations

import asyncio
import json
import math
import shutil
import time
from pathlib import Path

from deutschcoach_phase0.domain import (
    ALLOWED_MIME_TYPES,
    MAX_AUDIO_DURATION_SEC,
    MAX_AUDIO_SIZE_BYTES,
    MIN_AUDIO_DURATION_SEC,
    MSG_AUDIO_INVALID_FORMAT,
    MSG_AUDIO_TOO_LARGE,
    MSG_AUDIO_TOO_LONG,
    MSG_AUDIO_TOO_SHORT,
    AudioRejectionCode,
    AudioValidationResult,
    IncomingAudioMeta,
)

ALLOWED_FFPROBE_FORMATS: frozenset[str] = frozenset({"ogg", "opus", "webm", "mp4", "m4a", "mp3"})
SAFE_FORMAT_WHITELIST = "ogg,matroska,webm,mov,mp4,m4a,3gp,3g2,mj2,mp3"


def normalize_mime_type(mime_type: str | None) -> str | None:
    if mime_type is None:
        return None
    base = mime_type.split(";", 1)[0].strip().lower()
    return base or None


def validate_pre_download(meta: IncomingAudioMeta) -> AudioValidationResult:
    """Fast pre-download metadata guard before reserving/downloading bytes."""
    if meta.file_size is not None:
        if meta.file_size <= 0:
            return AudioValidationResult(
                ok=False,
                size_bytes=meta.file_size,
                rejection_code=AudioRejectionCode.EMPTY_FILE,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )
        if meta.file_size > MAX_AUDIO_SIZE_BYTES:
            return AudioValidationResult(
                ok=False,
                size_bytes=meta.file_size,
                rejection_code=AudioRejectionCode.FILE_TOO_LARGE,
                user_message_fa=MSG_AUDIO_TOO_LARGE,
            )

    normalized_mime = normalize_mime_type(meta.mime_type)
    if normalized_mime is not None and normalized_mime not in ALLOWED_MIME_TYPES:
        return AudioValidationResult(
            ok=False,
            size_bytes=meta.file_size,
            rejection_code=AudioRejectionCode.INVALID_MIME,
            user_message_fa=MSG_AUDIO_INVALID_FORMAT,
        )

    if meta.reported_duration_sec is not None:
        dur = float(meta.reported_duration_sec)
        if not math.isfinite(dur) or dur <= 0:
            return AudioValidationResult(
                ok=False,
                duration_sec=meta.reported_duration_sec,
                size_bytes=meta.file_size,
                rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )
        if dur < MIN_AUDIO_DURATION_SEC:
            return AudioValidationResult(
                ok=False,
                duration_sec=dur,
                size_bytes=meta.file_size,
                rejection_code=AudioRejectionCode.TOO_SHORT,
                user_message_fa=MSG_AUDIO_TOO_SHORT,
            )
        if dur > MAX_AUDIO_DURATION_SEC:
            return AudioValidationResult(
                ok=False,
                duration_sec=dur,
                size_bytes=meta.file_size,
                rejection_code=AudioRejectionCode.TOO_LONG,
                user_message_fa=MSG_AUDIO_TOO_LONG,
            )

    return AudioValidationResult(
        ok=True,
        duration_sec=meta.reported_duration_sec,
        size_bytes=meta.file_size,
    )


class FfprobeAudioInspector:
    """Verify actual container format, audio stream, file size, and decoded duration via ffmpeg."""

    def __init__(
        self,
        ffprobe_bin: str | None = None,
        timeout_sec: float = 10.0,
        ffmpeg_bin: str | None = None,
        ffmpeg_timeout_sec: float = 20.0,
    ) -> None:
        self._ffprobe_bin = ffprobe_bin or shutil.which("ffprobe") or "ffprobe"
        self._timeout_sec = timeout_sec
        self._ffmpeg_bin = ffmpeg_bin or shutil.which("ffmpeg") or "ffmpeg"
        self._ffmpeg_timeout_sec = ffmpeg_timeout_sec

    def validate_pre_download(self, meta: IncomingAudioMeta) -> AudioValidationResult:
        return validate_pre_download(meta)

    async def _run_subprocess(self, cmd: list[str], timeout_sec: float) -> tuple[int | None, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return None, b""
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        return proc.returncode, stdout

    async def _probe_file(
        self, path: Path
    ) -> tuple[dict[str, object] | None, list[dict[str, object]], list[dict[str, object]], str]:
        cmd = [
            self._ffprobe_bin,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            SAFE_FORMAT_WHITELIST,
            "-show_entries",
            "format=duration,format_name:stream=codec_type,codec_name,duration",
            "-of",
            "json",
            f"file:{path.resolve()}",
        ]
        rc, stdout = await self._run_subprocess(cmd, self._timeout_sec)
        if rc != 0:
            return None, [], [], ""
        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None, [], [], ""
        if not isinstance(payload, dict):
            return None, [], [], ""

        streams = payload.get("streams")
        stream_dicts = (
            [s for s in streams if isinstance(s, dict)] if isinstance(streams, list) else []
        )
        audio_streams = [s for s in stream_dicts if s.get("codec_type") == "audio"]
        video_streams = [s for s in stream_dicts if s.get("codec_type") == "video"]
        fmt = payload.get("format")
        fmt_dict = fmt if isinstance(fmt, dict) else {}
        format_name = str(fmt_dict.get("format_name") or "").lower()
        return fmt_dict, audio_streams, video_streams, format_name

    async def inspect_file(self, path: Path, claimed_mime: str | None) -> AudioValidationResult:
        if not path.is_file():
            return AudioValidationResult(
                ok=False,
                rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )

        actual_size = path.stat().st_size
        if actual_size <= 0:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                rejection_code=AudioRejectionCode.EMPTY_FILE,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )
        if actual_size > MAX_AUDIO_SIZE_BYTES:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                rejection_code=AudioRejectionCode.FILE_TOO_LARGE,
                user_message_fa=MSG_AUDIO_TOO_LARGE,
            )

        normalized_mime = normalize_mime_type(claimed_mime)
        if normalized_mime is not None and normalized_mime not in ALLOWED_MIME_TYPES:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                rejection_code=AudioRejectionCode.INVALID_MIME,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )

        fmt_dict, audio_streams, video_streams, format_name = await self._probe_file(path)
        if fmt_dict is None:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )
        if video_streams:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                detected_format=format_name or None,
                rejection_code=AudioRejectionCode.INVALID_MIME,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )
        if not audio_streams:
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                detected_format=format_name or None,
                rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )

        format_tokens = {tok.strip() for tok in format_name.split(",") if tok.strip()}
        if not (format_tokens & ALLOWED_FFPROBE_FORMATS):
            return AudioValidationResult(
                ok=False,
                size_bytes=actual_size,
                detected_format=format_name or None,
                rejection_code=AudioRejectionCode.INVALID_MIME,
                user_message_fa=MSG_AUDIO_INVALID_FORMAT,
            )

        normalized_path = path.with_name(f"{path.stem}.normalized.ogg")
        try:
            ffmpeg_cmd = [
                self._ffmpeg_bin,
                "-nostdin",
                "-hide_banner",
                "-v",
                "error",
                "-y",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                SAFE_FORMAT_WHITELIST,
                "-i",
                f"file:{path.resolve()}",
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-t",
                "126",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                "-f",
                "ogg",
                str(normalized_path),
            ]
            ffmpeg_rc, _ = await self._run_subprocess(ffmpeg_cmd, self._ffmpeg_timeout_sec)
            if ffmpeg_rc != 0 or not normalized_path.is_file():
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    size_bytes=actual_size,
                    detected_format=format_name or None,
                    rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                    user_message_fa=MSG_AUDIO_INVALID_FORMAT,
                )

            normalized_size = normalized_path.stat().st_size
            if normalized_size <= 0:
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    size_bytes=normalized_size,
                    detected_format="ogg",
                    rejection_code=AudioRejectionCode.EMPTY_FILE,
                    user_message_fa=MSG_AUDIO_INVALID_FORMAT,
                )
            if normalized_size > MAX_AUDIO_SIZE_BYTES:
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    size_bytes=normalized_size,
                    detected_format="ogg",
                    rejection_code=AudioRejectionCode.FILE_TOO_LARGE,
                    user_message_fa=MSG_AUDIO_TOO_LARGE,
                )

            norm_fmt, norm_audio, norm_video, norm_format_name = await self._probe_file(
                normalized_path
            )
            if norm_fmt is None or norm_video or not norm_audio:
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    size_bytes=normalized_size,
                    detected_format=norm_format_name or None,
                    rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                    user_message_fa=MSG_AUDIO_INVALID_FORMAT,
                )

            decoded_duration_sec = _extract_duration(norm_fmt, norm_audio)
            if (
                decoded_duration_sec is None
                or not math.isfinite(decoded_duration_sec)
                or decoded_duration_sec <= 0
            ):
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    size_bytes=normalized_size,
                    detected_format="ogg",
                    rejection_code=AudioRejectionCode.CORRUPT_OR_NO_AUDIO,
                    user_message_fa=MSG_AUDIO_INVALID_FORMAT,
                )

            if decoded_duration_sec < MIN_AUDIO_DURATION_SEC:
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    duration_sec=decoded_duration_sec,
                    size_bytes=normalized_size,
                    detected_format="ogg",
                    rejection_code=AudioRejectionCode.TOO_SHORT,
                    user_message_fa=MSG_AUDIO_TOO_SHORT,
                )

            if decoded_duration_sec > MAX_AUDIO_DURATION_SEC:
                normalized_path.unlink(missing_ok=True)
                return AudioValidationResult(
                    ok=False,
                    duration_sec=decoded_duration_sec,
                    size_bytes=normalized_size,
                    detected_format="ogg",
                    rejection_code=AudioRejectionCode.TOO_LONG,
                    user_message_fa=MSG_AUDIO_TOO_LONG,
                )

            return AudioValidationResult(
                ok=True,
                duration_sec=decoded_duration_sec,
                size_bytes=normalized_size,
                detected_format="ogg",
                normalized_path=normalized_path,
            )
        except BaseException:
            normalized_path.unlink(missing_ok=True)
            raise


def _extract_duration(
    fmt_dict: dict[str, object], audio_streams: list[dict[str, object]]
) -> float | None:
    candidates: list[object] = [fmt_dict.get("duration")]
    candidates.extend(stream.get("duration") for stream in audio_streams)
    for raw in candidates:
        if raw is None:
            continue
        try:
            return float(str(raw))
        except ValueError:
            continue
    return None


class FilesystemAudioCleaner:
    """Deletes temporary audio files immediately and purges stale files over 24 hours."""

    def delete_now(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def purge_older_than(self, directory: Path, max_age_hours: int = 24) -> int:
        if not directory.is_dir():
            return 0
        cutoff_epoch = time.time() - (max_age_hours * 3600)
        removed = 0
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime < cutoff_epoch:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
        return removed
