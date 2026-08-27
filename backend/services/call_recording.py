"""Optional per-call 16 kHz mono WAV (inbound + outbound + mixed) for Vobiz WebSocket calls."""

from __future__ import annotations

import audioop
import threading
import wave
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from config import settings


import time

def _safe_stem(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:180]


def _day_dir(base_dir: Optional[str] = None) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = Path(base_dir or settings.call_recording_dir).resolve()
    return base / day


class CallRecorder:
    """Appends 16 kHz s16le mono PCM to in-memory buffers; writes WAV/MP3 on close.
    This prevents synchronous file-system writes from blocking the main event loop
    and causing audio packet delays (voice cracking) on low-resource hosts.
    """

    def __init__(self, session_id: str, *, channel: str, base_dir: Optional[str] = None) -> None:
        self._session_id = session_id
        self._channel = channel
        self._lock = threading.Lock()
        self._in_path: Optional[str] = None
        self._out_path: Optional[str] = None
        self._in_buffer = bytearray()
        self._out_buffer = bytearray()
        self._start_time: float = time.time()
        self._in_written = 0
        self._out_written = 0
        self._in_first_write_t: Optional[float] = None
        self._out_first_write_t: Optional[float] = None

        if not settings.call_recording_enabled:
            return
        d = _day_dir(base_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Call recording: cannot create dir {}: {}", d, e)
            return
        stem = _safe_stem(session_id)
        self._in_path = str(d / f"{stem}_inbound.wav")
        self._out_path = str(d / f"{stem}_outbound.wav")
        self._start_time = time.time()
        
        logger.info(
            "Call recording: initialized memory buffers for channel={} session={} in={} out={}",
            channel,
            session_id,
            self._in_path,
            self._out_path,
        )

    def add_inbound(self, pcm_s16le_mono: bytes) -> None:
        """Append inbound PCM. Lock-free for CPython (bytearray.extend is thread-safe
        at the C level). Silence padding is deferred to close() to avoid per-chunk overhead."""
        if not self._in_path or not pcm_s16le_mono:
            return
        if self._in_first_write_t is None:
            self._in_first_write_t = time.time()
        self._in_buffer.extend(pcm_s16le_mono)
        self._in_written += len(pcm_s16le_mono)

    def add_outbound(self, pcm_s16le_mono: bytes) -> None:
        """Append outbound PCM. Lock-free for CPython."""
        if not self._out_path or not pcm_s16le_mono:
            return
        if self._out_first_write_t is None:
            self._out_first_write_t = time.time()
        self._out_buffer.extend(pcm_s16le_mono)
        self._out_written += len(pcm_s16le_mono)

    def close(self) -> None:
        if self._in_path or self._out_path:
            logger.info(
                "Call recording: closed channel={} session={} (flushing buffers to background thread)",
                self._channel,
                self._session_id,
            )
            threading.Thread(target=self._write_mixed_wav, daemon=True).start()

    def _write_mixed_wav(self) -> None:
        """Mix inbound (caller) + outbound (AI) into a single ``*_mixed.wav`` playback file.
        Writes WAV files for inbound, outbound and mixed channels to disk, then compresses to MP3."""
        if not self._in_path and not self._out_path:
            return
        
        with self._lock:
            in_frames = bytes(self._in_buffer)
            out_frames = bytes(self._out_buffer)
            # Clear buffers to free memory
            self._in_buffer.clear()
            self._out_buffer.clear()

        if (in_frames and out_frames
                and self._in_first_write_t is not None
                and self._out_first_write_t is not None):
            offset_sec = self._out_first_write_t - self._in_first_write_t
            _SR = 16000
            _BPS = 2
            if offset_sec > 0.01:
                pad_bytes = int(offset_sec * _SR * _BPS)
                out_frames = b"\x00" * pad_bytes + out_frames
                logger.info(
                    "Call recording: aligned outbound with {}ms silence prefix "
                    "(outbound started {:.1f}s after inbound)",
                    int(offset_sec * 1000), offset_sec,
                )
            elif offset_sec < -0.01:
                pad_bytes = int(abs(offset_sec) * _SR * _BPS)
                in_frames = b"\x00" * pad_bytes + in_frames
                logger.info(
                    "Call recording: aligned inbound with {}ms silence prefix "
                    "(inbound started {:.1f}s after outbound)",
                    int(abs(offset_sec) * 1000), abs(offset_sec),
                )

        # Write inbound WAV file
        if self._in_path and in_frames:
            try:
                with wave.open(self._in_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16_000)
                    w.writeframes(in_frames)
            except Exception as e:
                logger.warning("Call recording mix: failed to write inbound WAV: {}", e)

        # Write outbound WAV file
        if self._out_path and out_frames:
            try:
                with wave.open(self._out_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16_000)
                    w.writeframes(out_frames)
            except Exception as e:
                logger.warning("Call recording mix: failed to write outbound WAV: {}", e)

        mixed_path = None
        try:
            if not in_frames and not out_frames:
                return
            # Pad shorter stream with silence so mix length = max(len(in), len(out)).
            ln = max(len(in_frames), len(out_frames))
            if len(in_frames) < ln:
                in_frames = in_frames + b"\x00" * (ln - len(in_frames))
            if len(out_frames) < ln:
                out_frames = out_frames + b"\x00" * (ln - len(out_frames))
            
            mixed = audioop.add(in_frames, out_frames, 2) if (in_frames and out_frames) else (in_frames or out_frames)
            
            stem = Path(self._in_path or self._out_path).stem
            for suffix in ("_inbound", "_outbound"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            
            mixed_path = str(Path((self._in_path or self._out_path)).parent / f"{stem}_mixed.wav")
            with wave.open(mixed_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16_000)
                w.writeframes(mixed)
            logger.info("Call recording: mixed WAV written {} ({} B)", mixed_path, len(mixed))

            # Compress to MP3
            mp3_path = mixed_path.replace(".wav", ".mp3")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", mixed_path, "-acodec", "libmp3lame", "-b:a", "32k", mp3_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                logger.info("Call recording: compressed to MP3 {}", mp3_path)
                Path(mixed_path).unlink(missing_ok=True)
            except Exception as ffmpeg_err:
                logger.warning("Call recording: MP3 compression failed: {}", ffmpeg_err)
        except Exception as e:
            logger.warning("Call recording mix failed: {}", e)

    def meta(self) -> dict[str, Any]:
        return {
            "inbound_wav": self._in_path,
            "outbound_wav": self._out_path,
            "call_recording": bool(self._in_path or self._out_path),
        }

def _parse_log_id_date(session_id: str) -> str | None:
    """Extract YYYY-MM-DD from log_id patterns like camp-xxx-20260513T07291 or vobiz-live-20260518T161022-xxx."""
    import re
    m = re.search(r"(\d{4})(\d{2})(\d{2})T", session_id)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _search_recording_dirs(
    stem: str,
    roots: list[Path],
    date_hint: str | None = None,
    scan_recent_days: int = 31,
) -> Path | None:
    """Search multiple recording roots for a matching WAV file."""
    suffixes = ("_mixed", "_outbound", "_inbound")
    for root in roots:
        if not root.is_dir():
            continue
        # Check flat files directly in root (legacy recordings without date subdirectories)
        for sfx in suffixes:
            for ext in (".mp3", ".wav"):
                cand = root / f"{stem}{sfx}{ext}"
                if cand.is_file():
                    return cand
        # If we have a date hint, search that exact day first in every root
        if date_hint:
            day_dir = root / date_hint
            if day_dir.is_dir():
                for sfx in suffixes:
                    for ext in (".mp3", ".wav"):
                        cand = day_dir / f"{stem}{sfx}{ext}"
                        if cand.is_file():
                            return cand
        # Fall back to scanning recent days
        dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and len(p.name) == 10),
            key=lambda p: p.name,
            reverse=True,
        )
        for day in dirs[: max(7, scan_recent_days)]:
            for sfx in suffixes:
                for ext in (".mp3", ".wav"):
                    cand = day / f"{stem}{sfx}{ext}"
                    if cand.is_file():
                        return cand
    return None


def recording_search_roots(base_dir: Optional[str | Path] = None) -> list[Path]:
    """All WAV directories to search (live DataEdge + historical agent/vernika trees)."""

    import os

    roots: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path) -> None:
        try:
            key = str(p.resolve())
        except OSError:
            return
        if key in seen:
            return
        try:
            if p.is_dir():
                seen.add(key)
                roots.append(p)
        except (PermissionError, OSError):
            pass

    if base_dir:
        _add(Path(base_dir))
    else:
        _add(Path(settings.call_recording_dir))
        for extra in (os.getenv("CALL_RECORDING_EXTRA_DIRS") or "").split(","):
            extra = extra.strip()
            if extra:
                _add(Path(extra))
        for candidate in (
            "/root/vernika/backend/data/call_recordings",
            "/root/vernika/agent/data/call_recordings",
            "/root/DataEdge/backend/data/call_recordings",
        ):
            _add(Path(candidate))
        # Also search the local data/recordings directory for legacy flat files
        _recordings_dir = Path(__file__).resolve().parent.parent / "data" / "recordings"
        _add(_recordings_dir)
    return roots


def resolve_session_recording_path(
    session_id: str,
    base_dir: Optional[str | Path] = None,
    *,
    scan_recent_days: int = 60,
) -> Path | None:
    """Locate ``*_mixed.wav`` (preferred) or outbound/inbound WAV for a CallRecorder ``session_id``."""

    stem = _safe_stem(session_id.strip())
    if not stem:
        return None

    date_hint = _parse_log_id_date(session_id)
    return _search_recording_dirs(
        stem, recording_search_roots(base_dir), date_hint, scan_recent_days
    )


def list_recording_days(base_dir: Optional[str] = None) -> list[str]:
    base = Path(base_dir or settings.call_recording_dir).resolve()
    if not base.is_dir():
        return []
    return sorted(
        [p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10],
        reverse=True,
    )


def list_recordings_wavs(day: str, base_dir: Optional[str] = None) -> list[str]:
    d = Path(base_dir or settings.call_recording_dir).resolve() / day
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.glob("*.wav"))


def resolve_recording_file(day: str, filename: str, base_dir: Optional[str] = None) -> Optional[Path]:
    if not day or len(day) != 10 or ".." in day or "/" in day or "\\" in day:
        return None
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    safe = Path(filename).name
    if safe != filename or not (safe.lower().endswith(".wav") or safe.lower().endswith(".mp3")):
        return None
    
    base_root = Path(base_dir or settings.call_recording_dir).resolve()
    p = (base_root / day / safe).resolve()
    # If exact file missing, try _mixed
    if not p.is_file():
        stem = safe.rsplit(".", 1)[0]
        mixed_mp3 = (base_root / day / f"{stem}_mixed.mp3").resolve()
        if mixed_mp3.is_file():
            p = mixed_mp3
        else:
            mixed_wav = (base_root / day / f"{stem}_mixed.wav").resolve()
            if mixed_wav.is_file():
                p = mixed_wav
            
    root = (base_root / day).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p if p.is_file() else None
