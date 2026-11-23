from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from deutschcoach_phase0.audio_guard import (
    SAFE_FORMAT_WHITELIST,
    FfprobeAudioInspector,
    FilesystemAudioCleaner,
    validate_pre_download,
)
from deutschcoach_phase0.domain import (
    MAX_AUDIO_SIZE_BYTES,
    MSG_AUDIO_TOO_LONG,
    MSG_AUDIO_TOO_SHORT,
    AudioRejectionCode,
    IncomingAudioMeta,
)


def _make_ogg_opus(dest: Path, duration_sec: int) -> Path:
    ffmpeg_bin = shutil.which("ffmpeg")
    assert ffmpeg_bin is not None, "ffmpeg must be installed for audio guard tests"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_sec}",
            "-c:a",
            "libopus",
            "-b:a",
            "16k",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest


def test_validate_pre_download_rejects_short_long_oversized_and_bad_mime() -> None:
    short = validate_pre_download(IncomingAudioMeta("f1", "u1", "audio/ogg", 12_000, 10.0))
    assert short.ok is False
    assert short.rejection_code == AudioRejectionCode.TOO_SHORT
    assert short.user_message_fa == MSG_AUDIO_TOO_SHORT

    long_audio = validate_pre_download(IncomingAudioMeta("f2", "u2", "audio/ogg", 250_000, 130.0))
    assert long_audio.ok is False
    assert long_audio.rejection_code == AudioRejectionCode.TOO_LONG
    assert long_audio.user_message_fa == MSG_AUDIO_TOO_LONG

    huge = validate_pre_download(
        IncomingAudioMeta("f3", "u3", "audio/ogg", MAX_AUDIO_SIZE_BYTES + 1, 60.0)
    )
    assert huge.ok is False
    assert huge.rejection_code == AudioRejectionCode.FILE_TOO_LARGE

    bad_mime = validate_pre_download(IncomingAudioMeta("f4", "u4", "video/mp4", 120_000, 60.0))
    assert bad_mime.ok is False
    assert bad_mime.rejection_code == AudioRejectionCode.INVALID_MIME

    valid = validate_pre_download(
        IncomingAudioMeta("f5", "u5", "audio/ogg; codecs=opus", 120_000, 60.0)
    )
    assert valid.ok is True


@pytest.mark.asyncio
async def test_ffprobe_inspector_accepts_20s_ogg_and_rejects_10s_and_130s(tmp_path: Path) -> None:
    inspector = FfprobeAudioInspector()

    ogg_10s = _make_ogg_opus(tmp_path / "short_10s.ogg", 10)
    res_10s = await inspector.inspect_file(ogg_10s, "audio/ogg")
    assert res_10s.ok is False
    assert res_10s.rejection_code == AudioRejectionCode.TOO_SHORT
    assert res_10s.user_message_fa == MSG_AUDIO_TOO_SHORT

    ogg_20s = _make_ogg_opus(tmp_path / "valid_20s.ogg", 20)
    res_20s = await inspector.inspect_file(ogg_20s, "audio/ogg")
    assert res_20s.ok is True
    assert res_20s.duration_sec is not None
    assert 19.0 <= res_20s.duration_sec <= 21.0
    assert res_20s.detected_format == "ogg"
    assert res_20s.normalized_path is not None
    assert res_20s.normalized_path.is_file()
    assert res_20s.normalized_path.name == "valid_20s.normalized.ogg"

    ogg_130s = _make_ogg_opus(tmp_path / "long_130s.ogg", 130)
    res_130s = await inspector.inspect_file(ogg_130s, "audio/ogg")
    assert res_130s.ok is False
    assert res_130s.rejection_code == AudioRejectionCode.TOO_LONG
    assert res_130s.user_message_fa == MSG_AUDIO_TOO_LONG
    assert not (tmp_path / "short_10s.normalized.ogg").exists()
    assert not (tmp_path / "long_130s.normalized.ogg").exists()


@pytest.mark.asyncio
async def test_ffprobe_inspector_rejects_corrupt_empty_and_oversized_files(tmp_path: Path) -> None:
    inspector = FfprobeAudioInspector()

    empty = tmp_path / "empty.ogg"
    empty.write_bytes(b"")
    res_empty = await inspector.inspect_file(empty, "audio/ogg")
    assert res_empty.ok is False
    assert res_empty.rejection_code == AudioRejectionCode.EMPTY_FILE

    corrupt = tmp_path / "corrupt.ogg"
    corrupt.write_bytes(b"not-real-opus-data" * 50)
    res_corrupt = await inspector.inspect_file(corrupt, "audio/ogg")
    assert res_corrupt.ok is False
    assert res_corrupt.rejection_code == AudioRejectionCode.CORRUPT_OR_NO_AUDIO

    oversized = tmp_path / "oversized.ogg"
    oversized.write_bytes(b"\x00" * (MAX_AUDIO_SIZE_BYTES + 10))
    res_huge = await inspector.inspect_file(oversized, "audio/ogg")
    assert res_huge.ok is False
    assert res_huge.rejection_code == AudioRejectionCode.FILE_TOO_LARGE


def test_filesystem_audio_cleaner_deletes_immediately_and_purges_stale(tmp_path: Path) -> None:
    import os
    import time

    cleaner = FilesystemAudioCleaner()
    target = tmp_path / "temp.ogg"
    target.write_bytes(b"abc")
    cleaner.delete_now(target)
    assert not target.exists()
    cleaner.delete_now(target)  # idempotent missing_ok

    old_file = tmp_path / "old.ogg"
    fresh_file = tmp_path / "fresh.ogg"
    old_file.write_bytes(b"old")
    fresh_file.write_bytes(b"fresh")

    stale_ts = time.time() - (25 * 3600)
    os.utime(old_file, (stale_ts, stale_ts))

    purged = cleaner.purge_older_than(tmp_path, max_age_hours=24)
    assert purged == 1
    assert not old_file.exists()
    assert fresh_file.exists()


def _ogg_crc32(page: bytes) -> int:
    crc = 0
    for byte in page:
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


@pytest.mark.asyncio
async def test_ffprobe_and_ffmpeg_receive_strict_ssrf_whitelist_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded_cmds: list[tuple[str, ...]] = []
    real_exec = asyncio.create_subprocess_exec

    async def spy_exec(*args: object, **kwargs: object):
        recorded_cmds.append(tuple(str(a) for a in args))
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

    ogg_20s = _make_ogg_opus(tmp_path / "flags_20s.ogg", 20)
    inspector = FfprobeAudioInspector()
    res = await inspector.inspect_file(ogg_20s, "audio/ogg")

    assert res.ok is True
    assert len(recorded_cmds) == 3  # input ffprobe, ffmpeg normalize, normalized ffprobe
    for cmd in recorded_cmds:
        assert "-protocol_whitelist" in cmd
        assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
        assert "-format_whitelist" in cmd
        assert cmd[cmd.index("-format_whitelist") + 1] == SAFE_FORMAT_WHITELIST

    assert f"file:{ogg_20s.resolve()}" in recorded_cmds[0]
    assert f"file:{ogg_20s.resolve()}" in recorded_cmds[1]
    assert "-t" in recorded_cmds[1]
    assert recorded_cmds[1][recorded_cmds[1].index("-t") + 1] == "126"
    assert res.normalized_path is not None
    assert f"file:{res.normalized_path.resolve()}" in recorded_cmds[2]


@pytest.mark.asyncio
async def test_ffprobe_inspector_rejects_forged_ogg_granule_duration(tmp_path: Path) -> None:
    forged = _make_ogg_opus(tmp_path / "forged_5s.ogg", 5)
    data = bytearray(forged.read_bytes())
    last_idx = data.rfind(b"OggS")
    assert last_idx > 0
    struct.pack_into("<q", data, last_idx + 6, 60 * 48000)
    data[last_idx + 22 : last_idx + 26] = b"\x00\x00\x00\x00"
    n_segs = data[last_idx + 26]
    page_len = 27 + n_segs + sum(data[last_idx + 27 : last_idx + 27 + n_segs])
    page_crc = _ogg_crc32(bytes(data[last_idx : last_idx + page_len]))
    struct.pack_into("<I", data, last_idx + 22, page_crc)
    forged.write_bytes(bytes(data))

    inspector = FfprobeAudioInspector()
    res = await inspector.inspect_file(forged, "audio/ogg")

    assert res.ok is False
    assert res.rejection_code == AudioRejectionCode.TOO_SHORT
    assert res.duration_sec is not None
    assert res.duration_sec < 10.0
    assert not (tmp_path / "forged_5s.normalized.ogg").exists()


@pytest.mark.asyncio
async def test_ffprobe_inspector_rejects_hls_m3u8_saved_as_ogg_and_mp4_with_video(
    tmp_path: Path,
) -> None:
    inspector = FfprobeAudioInspector()

    hls_path = tmp_path / "ssrf.ogg"
    hls_path.write_text(
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXTINF:20.0,\n"
        "http://169.254.169.254/latest/meta-data/\n"
        "#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    res_hls = await inspector.inspect_file(hls_path, "audio/ogg")
    assert res_hls.ok is False
    assert res_hls.normalized_path is None
    assert not (tmp_path / "ssrf.normalized.ogg").exists()

    ffmpeg_bin = shutil.which("ffmpeg")
    assert ffmpeg_bin is not None
    mp4_video = tmp_path / "video_and_audio.mp4"
    subprocess.run(
        [
            ffmpeg_bin,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:d=20",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            "20",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(mp4_video),
        ],
        check=True,
        capture_output=True,
    )
    res_video = await inspector.inspect_file(mp4_video, "audio/mp4")
    assert res_video.ok is False
    assert res_video.rejection_code == AudioRejectionCode.INVALID_MIME
    assert not (tmp_path / "video_and_audio.normalized.ogg").exists()
