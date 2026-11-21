#!/usr/bin/env python3
"""Speed-Up Media Py - cross-platform audio/video speed-up utility."""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence, Set, Tuple

VERSION = "1.0.0"

MEDIA_EXTENSIONS: Set[str] = {
    # Video and container formats
    ".mp4", ".m4v", ".mov", ".qt", ".mkv", ".avi", ".webm", ".wmv",
    ".flv", ".f4v", ".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".m2ts",
    ".mts", ".ts", ".vob", ".ogv", ".3gp", ".3g2", ".asf", ".dv",
    ".mxf", ".mpv", ".y4m",
    # Audio formats
    ".mp3", ".mp2", ".m4a", ".m4b", ".m4p", ".mka", ".aac", ".flac",
    ".wav", ".wave", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif",
    ".ape", ".amr", ".ac3", ".dts", ".caf", ".au", ".ra",
}

SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}
ARTWORK_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".m4b", ".m4v", ".mov", ".mkv", ".mka", ".flac"}
FASTSTART_EXTENSIONS = {".mp4", ".m4v", ".mov", ".m4a", ".m4b", ".3gp", ".3g2"}


@dataclass
class Settings:
    quality: str = "Balanced"
    recursive: bool = False
    add_speed_suffix: bool = True
    skip_existing: bool = False
    external_subtitles: bool = True
    embedded_subtitles: bool = True
    scale_chapters: bool = True
    preserve_artwork: bool = True
    open_output_at_end: bool = False


@dataclass
class ConversionResult:
    cancelled: bool
    exit_code: Optional[int]
    duration: float
    speed: str
    error_lines: List[str]


class ConversionCancelled(Exception):
    """Raised when the user cancels a probe or conversion operation."""


class RunLogger:
    """Small UTF-8 file logger; console progress stays uncluttered."""

    def __init__(self, log_path: Path) -> None:
        self.path = log_path
        self.logger = logging.getLogger(f"speed_up_media_{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        handler = logging.FileHandler(str(log_path), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        self.logger.addHandler(handler)

    def info(self, message: str) -> None:
        self.logger.info(message)

    def warning(self, message: str) -> None:
        self.logger.warning(message)

    def error(self, message: str) -> None:
        self.logger.error(message)

    def close(self) -> None:
        for handler in list(self.logger.handlers):
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)


class Console:
    """Plain-text terminal UI designed to work without ANSI/VT support."""

    def __init__(self) -> None:
        self.is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self.last_progress_length = 0
        self.last_non_tty_update = 0.0

    def line(self, message: str = "") -> None:
        print(message, flush=True)

    def header(self, root: Path, output: Path, logs: Path) -> None:
        self.line("")
        self.line("  ==============================================")
        self.line("                 Speed-Up Media Py")
        self.line("        Cross-platform FFmpeg media utility")
        self.line("  ==============================================")
        self.line(f"  Version:      {VERSION}")
        self.line(f"  Input folder: {root}")
        self.line(f"  Output:       {output}")
        self.line(f"  Logs:         {logs}")
        self.line("")

    @staticmethod
    def _bar(percent: Optional[float], width: int = 8) -> str:
        if percent is None or not math.isfinite(percent):
            return "[--------]"
        clamped = max(0.0, min(100.0, percent))
        filled = int(round((clamped / 100.0) * width))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def progress(
        self,
        file_index: int,
        file_count: int,
        file_percent: Optional[float],
        total_percent: float,
        speed_text: str,
        file_elapsed: float,
        file_eta: Optional[float],
        total_elapsed: float,
        total_eta: Optional[float],
    ) -> None:
        columns = shutil.get_terminal_size((120, 24)).columns if self.is_tty else 160
        file_pct_text = "--.-%" if file_percent is None else f"{max(0.0, min(100.0, file_percent)):5.1f}%"
        speed = speed_text.strip() if speed_text.strip() else "--"
        elapsed_text = format_duration(file_elapsed)
        left_text = format_duration(file_eta) if file_eta is not None else "--:--:--"
        total_eta_text = format_duration(total_eta) if total_eta is not None else "--:--:--"

        if columns >= 125:
            message = (
                f"TOTAL {self._bar(total_percent)} {total_percent:5.1f}% | "
                f"FILE {file_index}/{file_count} | {speed} | {file_pct_text} {self._bar(file_percent)} | "
                f"elapsed {elapsed_text} | left {left_text} | total ETA {total_eta_text}"
            )
        else:
            message = (
                f"TOTAL {total_percent:5.1f}% {self._bar(total_percent)} | "
                f"FILE {file_index}/{file_count} | {speed} | {file_pct_text} {self._bar(file_percent)} | "
                f"{elapsed_text} left {left_text}"
            )

        if self.is_tty:
            available = max(20, columns - 1)
            message = message[:available]
            padding = max(0, self.last_progress_length - len(message))
            sys.stdout.write("\r" + message + (" " * padding))
            sys.stdout.flush()
            self.last_progress_length = len(message)
        else:
            now = time.monotonic()
            if now - self.last_non_tty_update >= 5.0 or total_percent >= 100.0:
                print(message, flush=True)
                self.last_non_tty_update = now

    def end_progress(self) -> None:
        if self.is_tty and self.last_progress_length:
            sys.stdout.write("\n")
            sys.stdout.flush()
        self.last_progress_length = 0


def configure_console_io() -> None:
    """Avoid UnicodeEncodeError on legacy Windows console code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (OSError, ValueError):
                pass


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def get_relative_display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def get_media_files(root: Path, settings: Settings, output_dir: Path, logs_dir: Path) -> List[Path]:
    found: List[Path] = []
    excluded = [output_dir.resolve(), logs_dir.resolve()]

    if settings.recursive:
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_file(follow_symlinks=False):
                        if entry_path.suffix.lower() in MEDIA_EXTENSIONS:
                            found.append(entry_path)
                    elif entry.is_dir(follow_symlinks=False):
                        resolved = entry_path.resolve()
                        if any(is_inside(resolved, excluded_dir) for excluded_dir in excluded):
                            continue
                        pending.append(entry_path)
                except OSError:
                    continue
    else:
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    if path.suffix.lower() in MEDIA_EXTENSIONS:
                        found.append(path)
                except OSError:
                    continue
        except OSError:
            pass

    return sorted(found, key=lambda p: str(p.relative_to(root)).casefold())


def parse_file_selection(selection: str, item_count: int) -> List[int]:
    if not selection.strip():
        raise ValueError("Enter at least one file number.")
    selected: Set[int] = set()
    for token in selection.split(","):
        part = token.strip()
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not match:
            raise ValueError(f"Invalid selection '{part}'. Use a format such as 1, 3, 6-11.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < 1 or start > item_count or end > item_count:
            raise ValueError(f"Numbers must be between 1 and {item_count}.")
        if end < start:
            raise ValueError(f"Invalid range '{part}'. The first number must be less than or equal to the last.")
        selected.update(range(start, end + 1))
    return sorted(selected)


def show_advanced_settings(settings: Settings, console: Console) -> None:
    while True:
        console.line("")
        console.line("  --- ADVANCED SETTINGS ---")
        console.line(f"  [1] Encoding quality              : {settings.quality}")
        console.line(f"  [2] Include subfolders            : {'ON' if settings.recursive else 'OFF'}")
        console.line(f"  [3] Add speed to output filename  : {'ON' if settings.add_speed_suffix else 'OFF'}")
        console.line(f"  [4] Skip existing outputs         : {'ON' if settings.skip_existing else 'OFF'}")
        subtitles_state = "ON" if settings.external_subtitles and settings.embedded_subtitles else "PARTIAL/OFF"
        console.line(f"  [5] Scale subtitle timing         : {subtitles_state}")
        console.line(f"  [6] Scale chapter timing          : {'ON' if settings.scale_chapters else 'OFF'}")
        console.line(f"  [7] Preserve artwork/attachments  : {'ON' if settings.preserve_artwork else 'OFF'}")
        console.line(f"  [8] Open output folder at end     : {'ON' if settings.open_output_at_end else 'OFF'}")
        console.line("  [9] Return to the main prompt")
        choice = input("  Choice [1-9]: ").strip()
        if choice == "1":
            console.line("")
            console.line("  Quality presets:")
            console.line("    [1] Compact  - smaller files, more compression")
            console.line("    [2] Balanced - recommended default")
            console.line("    [3] High     - larger files, better image quality")
            quality_choice = input("  Preset [1-3, Enter = Balanced]: ").strip()
            settings.quality = {"1": "Compact", "3": "High"}.get(quality_choice, "Balanced")
        elif choice == "2":
            settings.recursive = not settings.recursive
        elif choice == "3":
            settings.add_speed_suffix = not settings.add_speed_suffix
        elif choice == "4":
            settings.skip_existing = not settings.skip_existing
        elif choice == "5":
            new_value = not (settings.external_subtitles and settings.embedded_subtitles)
            settings.external_subtitles = new_value
            settings.embedded_subtitles = new_value
        elif choice == "6":
            settings.scale_chapters = not settings.scale_chapters
        elif choice == "7":
            settings.preserve_artwork = not settings.preserve_artwork
        elif choice == "8":
            settings.open_output_at_end = not settings.open_output_at_end
        elif choice == "9":
            return
        else:
            console.line("  Enter a number from 1 to 9.")


def read_speed_choice(settings: Settings, console: Console) -> float:
    while True:
        console.line("  1) Speed: [1] 1.5x   [2] 2x (default)")
        console.line("     Press A to open Advanced Settings.")
        choice = input("  Speed [1/2, Enter = 2x, A = Advanced]: ").strip().lower()
        if choice in {"", "2"}:
            return 2.0
        if choice == "1":
            return 1.5
        if choice == "a":
            show_advanced_settings(settings, console)
        else:
            console.line("  Enter 1 or 2, or A for Advanced Settings.")


def read_file_selection(
    available_files: List[Path], root: Path, settings: Settings, console: Console,
    output_dir: Path, logs_dir: Path,
) -> List[Path]:
    while True:
        if not available_files:
            console.line("  No media files were found with the current search settings.")
            return []
        console.line("")
        console.line("  2) Choose files to process:")
        console.line("     [Enter] All files (default)   [S] Select files   [A] Advanced Settings")
        choice = input("  File selection: ").strip().lower()
        if choice in {"", "1"}:
            return list(available_files)
        if choice == "a":
            show_advanced_settings(settings, console)
            available_files = get_media_files(root, settings, output_dir, logs_dir)
            continue
        if choice not in {"s", "2"}:
            console.line("  Press Enter, S, or A.")
            continue

        console.line("")
        console.line("  Enter file numbers. Examples: 1, 3, 6-11 or 1-4, 8.")
        for index, path in enumerate(available_files, start=1):
            console.line(f"    [{index:3}] {get_relative_display_path(path, root)}")
        while True:
            numbers = input(f"  File numbers [1-{len(available_files)}]: ").strip()
            try:
                indices = parse_file_selection(numbers, len(available_files))
                return [available_files[index - 1] for index in indices]
            except ValueError as exc:
                console.line(f"  {exc}")


def locate_tool(name: str, root: Path) -> Optional[str]:
    executable_names = [f"{name}.exe", name] if os.name == "nt" else [name]
    for executable_name in executable_names:
        candidate = root / executable_name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


class ProcessSupervisor:
    """Owns FFmpeg child processes and guarantees cleanup on supported hosts."""

    def __init__(self, logger: RunLogger) -> None:
        self.logger = logger
        self.job_handle: Any = None
        self.active_process: Optional[subprocess.Popen[str]] = None
        self.active_process_attached = False
        if os.name == "nt":
            self._create_windows_job()
        atexit.register(self.cleanup)

    def _create_windows_job(self) -> None:
        import ctypes
        from ctypes import wintypes

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._job_struct_type = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "Could not create a Windows Job Object")
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        limits.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "Could not enable automatic child-process cleanup")
        self.job_handle = job

    def attach(self, process: subprocess.Popen[str]) -> None:
        self.active_process = process
        self.active_process_attached = False
        if os.name == "nt":
            process_handle = int(process._handle)  # type: ignore[attr-defined]
            assigned = self._kernel32.AssignProcessToJobObject(self.job_handle, process_handle)
            if not assigned:
                # A very short FFprobe/FFmpeg process may have exited before assignment.
                if process.poll() is not None:
                    return
                error = self._ctypes.get_last_error()
                try:
                    process.kill()  # It is not in the Job Object, so terminate it directly.
                    process.wait(timeout=5)
                except Exception:
                    pass
                if self.active_process is process:
                    self.active_process = None
                raise OSError(error, "Could not attach FFmpeg to the Windows cleanup Job Object")
            self.active_process_attached = True

    def terminate(self, process: Optional[subprocess.Popen[str]] = None) -> None:
        target = process or self.active_process
        if target is None or target.poll() is not None:
            return
        if os.name == "nt":
            try:
                if self.job_handle and self.active_process is target and self.active_process_attached:
                    # The queue is cancelled as a whole, so terminating this job is safe.
                    if not self._kernel32.TerminateJobObject(self.job_handle, 1):
                        target.kill()
                else:
                    target.kill()
            except Exception:
                try:
                    target.kill()
                except Exception:
                    pass
        else:
            try:
                os.killpg(target.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    target.terminate()
                except Exception:
                    pass
            try:
                target.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(target.pid, signal.SIGKILL)
                except Exception:
                    try:
                        target.kill()
                    except Exception:
                        pass
        try:
            target.wait(timeout=5)
        except Exception:
            pass

    def cleanup(self) -> None:
        target = self.active_process
        if target is not None and target.poll() is None:
            self.logger.warning("Active child process was stopped during application cleanup.")
            self.terminate(target)
        self.active_process = None
        self.active_process_attached = False
        if os.name == "nt" and self.job_handle:
            try:
                # Closing the last Job Object handle kills any remaining assigned process.
                self._kernel32.CloseHandle(self.job_handle)
            except Exception:
                pass
            self.job_handle = None


class CancellationController:
    def __init__(self) -> None:
        self.requested = threading.Event()
        self.previous_handlers: Dict[int, Any] = {}

    def _handler(self, signum: int, frame: Any) -> None:
        self.requested.set()

    def install(self) -> None:
        candidate_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            candidate_signals.append(signal.SIGHUP)
        if hasattr(signal, "SIGBREAK"):
            candidate_signals.append(signal.SIGBREAK)
        for sig in candidate_signals:
            try:
                self.previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handler)
            except (OSError, ValueError):
                continue

    def restore(self) -> None:
        for sig, handler in self.previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
        self.previous_handlers.clear()


class KeyboardCancelPoller:
    """Detect Q/Esc without requiring ANSI terminal features."""

    def __init__(self) -> None:
        self.fd: Optional[int] = None
        self.old_settings: Any = None
        self.active = False

    def start(self) -> None:
        if not getattr(sys.stdin, "isatty", lambda: False)():
            return
        if os.name == "nt":
            self.active = True
            return
        try:
            import termios
            import tty

            self.fd = sys.stdin.fileno()
            self.old_settings = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.active = True
        except Exception:
            self.fd = None
            self.old_settings = None
            self.active = False

    def cancel_pressed(self) -> bool:
        if not self.active:
            return False
        if os.name == "nt":
            try:
                import msvcrt

                pressed = False
                while msvcrt.kbhit():
                    character = msvcrt.getwch()
                    if character in ("q", "Q", "\x1b"):
                        pressed = True
                return pressed
            except Exception:
                return False
        try:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if readable:
                character = sys.stdin.read(1)
                return character.lower() == "q" or character == "\x1b"
        except Exception:
            return False
        return False

    def restore(self) -> None:
        if self.fd is not None and self.old_settings is not None:
            try:
                import termios

                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass
        self.active = False
        self.fd = None
        self.old_settings = None


def run_capture(
    command: Sequence[str],
    supervisor: ProcessSupervisor,
    logger: RunLogger,
    timeout: Optional[float] = None,
    cancellation: Optional[CancellationController] = None,
    key_poller: Optional[KeyboardCancelPoller] = None,
) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "creationflags": creationflags,
        "start_new_session": os.name != "nt",
    }
    process = subprocess.Popen(list(command), **kwargs)
    try:
        supervisor.attach(process)  # type: ignore[arg-type]
        started = time.monotonic()
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancellation and cancellation.requested.is_set():
                    supervisor.terminate(process)  # type: ignore[arg-type]
                    process.communicate()
                    raise ConversionCancelled("The user cancelled media probing.")
                if key_poller and key_poller.cancel_pressed():
                    supervisor.terminate(process)  # type: ignore[arg-type]
                    process.communicate()
                    raise ConversionCancelled("The user cancelled media probing.")
                if timeout is not None and time.monotonic() - started > timeout:
                    supervisor.terminate(process)  # type: ignore[arg-type]
                    process.communicate()
                    raise RuntimeError("The media probe command timed out.")
    finally:
        if process.poll() is None:
            supervisor.terminate(process)  # type: ignore[arg-type]
        if supervisor.active_process is process:
            supervisor.active_process = None
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    if stderr:
        for line in stderr.splitlines():
            if line.strip():
                logger.warning(f"FFprobe: {line.strip()}")
    return subprocess.CompletedProcess(list(command), process.returncode or 0, stdout, stderr)


def get_probe_data(
    path: Path, ffprobe_path: str, supervisor: ProcessSupervisor, logger: RunLogger,
    cancellation: Optional[CancellationController] = None,
    key_poller: Optional[KeyboardCancelPoller] = None,
) -> Dict[str, Any]:
    command = [
        ffprobe_path, "-v", "error", "-show_format", "-show_streams", "-show_chapters",
        "-of", "json", str(path),
    ]
    result = run_capture(command, supervisor, logger, timeout=120, cancellation=cancellation, key_poller=key_poller)
    if result.returncode != 0:
        raise RuntimeError("FFprobe could not read this file. See the log for details.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe returned invalid JSON: {exc}") from exc


def get_source_duration(probe: Dict[str, Any]) -> float:
    format_info = probe.get("format") or {}
    candidates: List[Any] = [format_info.get("duration")]
    for stream in probe.get("streams", []):
        candidates.append(stream.get("duration"))
    for value in candidates:
        try:
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError):
            continue
    return 0.0


def escape_ffmetadata_value(value: Any) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def create_scaled_chapter_metadata(chapters: Sequence[Dict[str, Any]], speed: float, temp_dir: Path) -> Optional[Path]:
    if not chapters:
        return None
    lines = [";FFMETADATA1", ""]
    for chapter in chapters:
        try:
            start = float(chapter["start_time"])
            end = float(chapter["end_time"])
        except (KeyError, TypeError, ValueError):
            continue
        start_ms = int(math.floor(start * 1000.0 / speed + 0.5))
        end_ms = max(start_ms + 1, int(math.floor(end * 1000.0 / speed + 0.5)))
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}"])
        tags = chapter.get("tags") or {}
        for key, value in tags.items():
            key_text = str(key)
            if re.fullmatch(r"[A-Za-z0-9_-]+", key_text) and key_text not in {"TIMEBASE", "START", "END"}:
                lines.append(f"{key_text}={escape_ffmetadata_value(value)}")
        lines.append("")
    if len(lines) <= 2:
        return None
    temp_path = temp_dir / f"chapters_{uuid.uuid4().hex}.ffmeta"
    temp_path.write_text("\n".join(lines), encoding="utf-8")
    return temp_path


def convert_subtitle_timestamp(timestamp: str, speed: float, subtitle_format: str) -> str:
    if subtitle_format == "ASS":
        match = re.fullmatch(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})\.(?P<cs>\d{2})", timestamp)
        if not match:
            return timestamp
        total_cs = (
            (int(match.group("h")) * 3600 + int(match.group("m")) * 60 + int(match.group("s"))) * 100
            + int(match.group("cs"))
        )
        scaled = int(math.floor(total_cs / speed + 0.5))
        hours, remainder = divmod(scaled, 360000)
        minutes, remainder = divmod(remainder, 6000)
        seconds, centiseconds = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    match = re.fullmatch(r"(?:(?P<h>\d{2}):)?(?P<m>\d{2}):(?P<s>\d{2})(?P<sep>[,.])(?P<ms>\d{3})", timestamp)
    if not match:
        return timestamp
    hours = int(match.group("h") or 0)
    total_ms = ((hours * 3600 + int(match.group("m")) * 60 + int(match.group("s"))) * 1000) + int(match.group("ms"))
    scaled = int(math.floor(total_ms / speed + 0.5))
    new_hours, remainder = divmod(scaled, 3_600_000)
    new_minutes, remainder = divmod(remainder, 60_000)
    new_seconds, milliseconds = divmod(remainder, 1000)
    separator = match.group("sep")
    if match.group("h") or new_hours > 0:
        return f"{new_hours:02d}:{new_minutes:02d}:{new_seconds:02d}{separator}{milliseconds:03d}"
    return f"{new_minutes:02d}:{new_seconds:02d}{separator}{milliseconds:03d}"


def read_subtitle_text(path: Path) -> str:
    data = path.read_bytes()
    # Check longer BOMs before UTF-16, because UTF-32 LE also begins with FF FE.
    bom_encodings = (
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    )
    for marker, encoding in bom_encodings:
        if data.startswith(marker):
            return data.decode(encoding)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # CP1251 is common in older Russian-language subtitle files.
        try:
            return data.decode("cp1251")
        except UnicodeDecodeError:
            return data.decode("latin-1")


def scale_external_subtitle_file(source: Path, destination: Path, speed: float) -> None:
    extension = source.suffix.lower()
    subtitle_format = "ASS" if extension in {".ass", ".ssa"} else "SRT"
    content = read_subtitle_text(source)
    newline = "\r\n" if "\r\n" in content else ("\r" if "\r" in content and "\n" not in content else "\n")
    lines = re.split(r"\r\n|\n|\r", content)

    srt_time_pattern = re.compile(r"(?P<time>(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})")
    ass_dialogue_pattern = re.compile(
        r"^(?P<prefix>\s*Dialogue:\s*[^,]*,)(?P<start>\d+:\d{2}:\d{2}\.\d{2}),(?P<end>\d+:\d{2}:\d{2}\.\d{2}),(?P<rest>.*)$"
    )

    for index, line in enumerate(lines):
        if subtitle_format == "SRT" and "-->" in line:
            lines[index] = srt_time_pattern.sub(
                lambda match: convert_subtitle_timestamp(match.group("time"), speed, "SRT"), line
            )
        elif subtitle_format == "ASS" and line.lstrip().startswith("Dialogue:"):
            match = ass_dialogue_pattern.match(line)
            if match:
                start = convert_subtitle_timestamp(match.group("start"), speed, "ASS")
                end = convert_subtitle_timestamp(match.group("end"), speed, "ASS")
                lines[index] = f"{match.group('prefix')}{start},{end},{match.group('rest')}"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".tmp_{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as subtitle_handle:
            subtitle_handle.write(newline.join(lines))
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def get_subtitle_sidecars(media_file: Path) -> List[Path]:
    try:
        candidates = list(media_file.parent.iterdir())
    except OSError:
        return []
    result = []
    prefix = media_file.stem.casefold() + "."
    for candidate in candidates:
        if not candidate.is_file() or candidate.is_symlink() or candidate.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        name = candidate.name.casefold()
        if name.startswith(prefix) or candidate.stem.casefold() == media_file.stem.casefold():
            result.append(candidate)
    return sorted(result, key=lambda item: item.name.casefold())


def write_subtitle_sidecars(
    media_file: Path, output_media: Path, speed: float, settings: Settings, logger: RunLogger, console: Console,
) -> int:
    sidecars = get_subtitle_sidecars(media_file)
    processed = 0
    output_stem = output_media.stem
    for sidecar in sidecars:
        suffix = sidecar.name[len(media_file.stem):]
        subtitle_output = output_media.with_name(output_stem + suffix)
        try:
            if settings.external_subtitles:
                scale_external_subtitle_file(sidecar, subtitle_output, speed)
                console.line(f"  Subtitle timing scaled: {sidecar.name} -> {subtitle_output.name}")
                logger.info(f"Scaled external subtitle: {sidecar} -> {subtitle_output}")
            else:
                shutil.copy2(sidecar, subtitle_output)
                console.line(f"  Subtitle copied: {sidecar.name} -> {subtitle_output.name}")
                logger.info(f"Copied external subtitle: {sidecar} -> {subtitle_output}")
            processed += 1
        except Exception as exc:
            logger.warning(f"Could not process external subtitle '{sidecar}': {exc}")
            console.line(f"  Warning: could not process subtitle '{sidecar.name}': {exc}")
    return processed


def get_video_plan(extension: str, quality: str) -> Tuple[str, List[Tuple[str, str]]]:
    ext = extension.lower()
    if ext == ".webm":
        crf, cpu = {"Compact": ("35", "6"), "High": ("26", "2")}.get(quality, ("31", "4"))
        return "libvpx-vp9", [("-crf", crf), ("-b:v", "0"), ("-deadline", "good"), ("-cpu-used", cpu)]
    if ext == ".ogv":
        value = {"Compact": "4", "High": "9"}.get(quality, "6")
        return "libtheora", [("-q:v", value)]
    if ext == ".avi":
        value = {"Compact": "6", "High": "2"}.get(quality, "4")
        return "mpeg4", [("-q:v", value)]
    if ext in {".wmv", ".asf"}:
        value = {"Compact": "1200k", "High": "5000k"}.get(quality, "2500k")
        return "wmv2", [("-b:v", value)]
    if ext == ".flv":
        value = {"Compact": "1000k", "High": "4000k"}.get(quality, "2000k")
        return "flv", [("-b:v", value)]
    if ext in {".vob", ".mpeg", ".mpg", ".mpe", ".m1v", ".m2v", ".mpv", ".mxf"}:
        value = {"Compact": "7", "High": "2"}.get(quality, "4")
        return "mpeg2video", [("-q:v", value)]
    if ext == ".dv":
        return "dvvideo", []
    if ext == ".y4m":
        return "rawvideo", []
    crf, preset = {"Compact": ("28", "veryfast"), "High": ("18", "slow")}.get(quality, ("23", "medium"))
    return "libx264", [("-crf", crf), ("-preset", preset)]


def get_audio_plan(extension: str, quality: str) -> Tuple[Optional[str], List[Tuple[str, str]]]:
    ext = extension.lower()
    bitrate = {"Compact": "128k", "High": "256k"}.get(quality, "192k")
    if ext == ".mp3":
        return "libmp3lame", [("-b:a", bitrate)]
    if ext in {".mpg", ".mpeg", ".mpe", ".m1v", ".m2v", ".mp2"}:
        return "mp2", [("-b:a", bitrate)]
    if ext in {".m4a", ".m4b", ".m4p", ".aac", ".mp4", ".m4v", ".mov", ".3gp", ".3g2", ".mkv", ".mka", ".m2ts", ".mts", ".ts", ".mpv"}:
        return "aac", [("-b:a", bitrate)]
    if ext in {".ogg", ".oga", ".ogv"}:
        value = {"Compact": "3", "High": "8"}.get(quality, "5")
        return "libvorbis", [("-q:a", value)]
    if ext in {".opus", ".webm"}:
        value = {"Compact": "96k", "High": "256k"}.get(quality, "160k")
        return "libopus", [("-b:a", value)]
    if ext == ".flac":
        value = {"Compact": "3", "High": "8"}.get(quality, "5")
        return "flac", [("-compression_level", value)]
    if ext in {".wav", ".wave", ".caf"}:
        return "pcm_s16le", []
    if ext in {".aiff", ".aif", ".au"}:
        return "pcm_s16be", []
    if ext in {".wma", ".wmv", ".asf"}:
        return "wmav2", [("-b:a", bitrate)]
    if ext in {".avi", ".flv", ".f4v"}:
        return "libmp3lame", [("-b:a", bitrate)]
    if ext == ".ac3":
        return "ac3", [("-b:a", bitrate)]
    if ext in {".vob", ".mxf", ".dv"}:
        return "pcm_s16le", []
    if ext in {".ape", ".ra", ".dts", ".amr"}:
        return None, []
    return "aac", [("-b:a", bitrate)]


def scoped_option(option: str, media_type: str, stream_number: int) -> str:
    # Convert e.g. -crf to -crf:v:0 and -b:a to -b:a:0.
    if option.endswith(":v") or option.endswith(":a"):
        return f"{option}:{stream_number}"
    return f"{option}:{media_type}:{stream_number}"


def build_ffmpeg_arguments(
    media_file: Path,
    temporary_output: Path,
    probe: Dict[str, Any],
    speed: float,
    settings: Settings,
    chapter_metadata: Optional[Path],
) -> List[str]:
    streams = probe.get("streams", [])
    normal_videos: List[Dict[str, Any]] = []
    audio_streams: List[Dict[str, Any]] = []
    subtitle_streams: List[Dict[str, Any]] = []
    picture_streams: List[Dict[str, Any]] = []
    attachment_streams: List[Dict[str, Any]] = []

    can_preserve_artwork = settings.preserve_artwork and media_file.suffix.lower() in ARTWORK_EXTENSIONS
    for stream in streams:
        codec_type = stream.get("codec_type")
        disposition = stream.get("disposition") or {}
        attached_picture = int(disposition.get("attached_pic", 0) or 0) == 1
        if codec_type == "video" and attached_picture:
            if can_preserve_artwork:
                picture_streams.append(stream)
        elif codec_type == "video":
            normal_videos.append(stream)
        elif codec_type == "audio":
            audio_streams.append(stream)
        elif codec_type == "subtitle":
            subtitle_streams.append(stream)
        elif codec_type == "attachment" and settings.preserve_artwork and media_file.suffix.lower() in {".mkv", ".mka"}:
            attachment_streams.append(stream)

    if not normal_videos and not audio_streams:
        raise RuntimeError("No regular video or audio streams were found.")

    arguments: List[str] = [
        "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats",
        "-progress", "pipe:1", "-stats_period", "0.5", "-i", str(media_file),
    ]
    if chapter_metadata is not None:
        arguments.extend(["-f", "ffmetadata", "-i", str(chapter_metadata)])

    # Explicit maps keep all ordinary audio/video/subtitle streams in the output.
    for stream in normal_videos + audio_streams + subtitle_streams + picture_streams + attachment_streams:
        arguments.extend(["-map", f"0:{stream['index']}"])

    speed_text = f"{speed:.1f}"
    video_codec, video_options = get_video_plan(media_file.suffix, settings.quality)
    for ordinal, _stream in enumerate(normal_videos):
        arguments.extend([f"-filter:v:{ordinal}", f"setpts=PTS/{speed_text}"])
        arguments.extend([f"-c:v:{ordinal}", video_codec])
        for option, value in video_options:
            arguments.extend([scoped_option(option, "v", ordinal), value])

    audio_codec, audio_options = get_audio_plan(media_file.suffix, settings.quality)
    if audio_streams and not audio_codec:
        raise RuntimeError(
            f"No suitable audio encoder is configured for '{media_file.suffix}'. "
            "Choose a supported output format such as FLAC, WAV, or MP3 first."
        )
    for ordinal, _stream in enumerate(audio_streams):
        arguments.extend([f"-filter:a:{ordinal}", f"atempo={speed_text}"])
        arguments.extend([f"-c:a:{ordinal}", str(audio_codec)])
        for option, value in audio_options:
            arguments.extend([scoped_option(option, "a", ordinal), value])

    if subtitle_streams:
        arguments.extend(["-c:s", "copy"])
        if settings.embedded_subtitles:
            arguments.extend(["-bsf:s", f"setts=pts=PTS/{speed_text}:dts=DTS/{speed_text}:duration=DURATION/{speed_text}"])

    # Attached cover art is copied rather than accelerated.
    for picture_ordinal, _stream in enumerate(picture_streams):
        video_ordinal = len(normal_videos) + picture_ordinal
        arguments.extend([f"-c:v:{video_ordinal}", "copy", f"-disposition:v:{video_ordinal}", "attached_pic"])
    if attachment_streams:
        arguments.extend(["-c:t", "copy"])

    arguments.extend(["-map_metadata", "0"])
    if chapter_metadata is not None:
        arguments.extend(["-map_chapters", "1"])
    elif probe.get("chapters"):
        arguments.extend(["-map_chapters", "0"])
    else:
        arguments.extend(["-map_chapters", "-1"])

    if media_file.suffix.lower() in FASTSTART_EXTENSIONS:
        arguments.extend(["-movflags", "+faststart"])
    arguments.extend(["-y", str(temporary_output)])
    return arguments


def parse_progress_block(values: Dict[str, str], expected_output_duration: float) -> Optional[float]:
    if values.get("progress") == "end":
        return 100.0
    output_time_us: Optional[int] = None
    for key in ("out_time_us", "out_time_ms"):
        if key in values:
            try:
                output_time_us = int(values[key])
                break
            except ValueError:
                continue
    if output_time_us is None and "out_time" in values:
        match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d+)", values["out_time"])
        if match:
            hours = int(match.group(1) or 0)
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            fraction = float("0." + match.group(4))
            output_seconds = hours * 3600 + minutes * 60 + seconds + fraction
            output_time_us = int(output_seconds * 1_000_000)
    if output_time_us is None or expected_output_duration <= 0:
        return None
    percent = (output_time_us / 1_000_000.0) / expected_output_duration * 100.0
    return max(0.0, min(100.0, percent))


def estimate_eta(elapsed: float, percent: Optional[float]) -> Optional[float]:
    if percent is None or percent <= 0.0 or percent >= 100.0:
        return None
    return elapsed * (100.0 - percent) / percent


def invoke_ffmpeg_with_progress(
    ffmpeg_path: str,
    arguments: Sequence[str],
    source_duration: float,
    speed: float,
    file_index: int,
    file_count: int,
    overall_start: float,
    console: Console,
    logger: RunLogger,
    supervisor: ProcessSupervisor,
    cancellation: CancellationController,
    key_poller: KeyboardCancelPoller,
) -> ConversionResult:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [ffmpeg_path, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    supervisor.attach(process)

    progress_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    error_tail: Deque[str] = deque(maxlen=12)
    reader_finished = threading.Event()

    def read_progress() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                progress_queue.put(line.rstrip("\r\n"))
        finally:
            progress_queue.put(None)
            reader_finished.set()

    def read_errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            cleaned = line.rstrip("\r\n")
            if cleaned:
                error_tail.append(cleaned)
                logger.warning(f"FFmpeg: {cleaned}")

    stdout_thread = threading.Thread(target=read_progress, name="ffmpeg-progress-reader", daemon=True)
    stderr_thread = threading.Thread(target=read_errors, name="ffmpeg-stderr-reader", daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    values: Dict[str, str] = {}
    file_start = time.monotonic()
    expected_output_duration = source_duration / speed if source_duration > 0 else 0.0
    last_draw = 0.0
    was_cancelled = False
    latest_speed = "--"
    file_percent: Optional[float] = 0.0 if expected_output_duration > 0 else None

    try:
        while True:
            if cancellation.requested.is_set() or key_poller.cancel_pressed():
                was_cancelled = True
                logger.warning("Cancellation requested by the user.")
                console.end_progress()
                console.line("  Cancellation requested. Stopping FFmpeg...")
                supervisor.terminate(process)
                break

            # Drain all currently available FFmpeg progress fields without blocking.
            drained = 0
            while drained < 200:
                try:
                    line = progress_queue.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                if line is None:
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
                    if key == "speed" and value:
                        latest_speed = value

            file_percent = parse_progress_block(values, expected_output_duration)
            if file_percent is None and expected_output_duration > 0:
                file_percent = 0.0
            elapsed = time.monotonic() - file_start
            current_fraction = (file_percent / 100.0) if file_percent is not None else 0.0
            total_percent = ((file_index - 1 + current_fraction) / max(1, file_count)) * 100.0
            total_elapsed = time.monotonic() - overall_start
            total_eta = estimate_eta(total_elapsed, total_percent)

            now = time.monotonic()
            if now - last_draw >= 0.25:
                console.progress(
                    file_index=file_index,
                    file_count=file_count,
                    file_percent=file_percent,
                    total_percent=total_percent,
                    speed_text=latest_speed,
                    file_elapsed=elapsed,
                    file_eta=estimate_eta(elapsed, file_percent),
                    total_elapsed=total_elapsed,
                    total_eta=total_eta,
                )
                last_draw = now

            if process.poll() is not None and reader_finished.is_set() and progress_queue.empty():
                break
            time.sleep(0.05)

        if was_cancelled and process.poll() is None:
            supervisor.terminate(process)
        return_code = process.wait()
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        duration = time.monotonic() - file_start
        if not was_cancelled and return_code == 0:
            file_percent = 100.0
            final_total = (file_index / max(1, file_count)) * 100.0
            console.progress(
                file_index, file_count, 100.0, final_total, latest_speed, duration, 0.0,
                time.monotonic() - overall_start, estimate_eta(time.monotonic() - overall_start, final_total),
            )
        console.end_progress()
        return ConversionResult(was_cancelled, return_code, duration, latest_speed, list(error_tail))
    finally:
        if process.poll() is None:
            supervisor.terminate(process)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        if supervisor.active_process is process:
            supervisor.active_process = None
        try:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        except Exception:
            pass


def make_output_path(media_file: Path, root: Path, output_dir: Path, speed: float, settings: Settings) -> Path:
    relative_parent = media_file.parent.relative_to(root)
    target_dir = output_dir / relative_parent
    target_dir.mkdir(parents=True, exist_ok=True)
    speed_label = "1.5x" if speed == 1.5 else "2x"
    suffix = f"_{speed_label}" if settings.add_speed_suffix else ""
    return target_dir / f"{media_file.stem}{suffix}{media_file.suffix}"


def make_temporary_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.speedtmp_{uuid.uuid4().hex}{output_path.suffix}")


def open_output_folder(output_dir: Path, logger: RunLogger) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(output_dir))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(output_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", str(output_dir)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.warning(f"Could not open the output folder: {exc}")
        print(f"  Warning: could not open the output folder: {exc}")


def wait_before_exit() -> None:
    if getattr(sys.stdin, "isatty", lambda: False)():
        try:
            input("Press Enter to exit...")
        except (EOFError, KeyboardInterrupt):
            pass


def main() -> int:
    configure_console_io()
    root = Path(__file__).resolve().parent
    output_dir = root / "output"
    logs_dir = root / "Logs"
    console = Console()
    settings = Settings()

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.line(f"Could not create the output/Logs folders: {exc}")
        wait_before_exit()
        return 1

    log_name = datetime.now().strftime("Speed-Up-Media_%Y-%m-%d_%H-%M-%S-%f") + ".log"
    logger = RunLogger(logs_dir / log_name)
    supervisor: Optional[ProcessSupervisor] = None
    cancellation = CancellationController()
    key_poller = KeyboardCancelPoller()
    success_count = failed_count = skipped_count = cancelled_count = subtitle_error_count = 0
    queue_cancelled = False

    try:
        ffmpeg_path = locate_tool("ffmpeg", root)
        ffprobe_path = locate_tool("ffprobe", root)
        console.header(root, output_dir, logs_dir)
        if not ffmpeg_path or not ffprobe_path:
            console.line("ERROR: FFmpeg and/or FFprobe could not be found.")
            console.line("Add the FFmpeg bin folder to PATH or place ffmpeg and ffprobe next to this script.")
            logger.error("FFmpeg and/or FFprobe could not be found.")
            console.line(f"Log file: {logger.path}")
            wait_before_exit()
            return 1

        logger.info(f"Version: {VERSION}")
        logger.info(f"Input folder: {root}")
        logger.info(f"FFmpeg: {ffmpeg_path}")
        logger.info(f"FFprobe: {ffprobe_path}")
        console.line(f"  FFmpeg: {ffmpeg_path}")
        console.line(f"  FFprobe: {ffprobe_path}")
        console.line(f"  Log file: {logger.path}")

        media_files = get_media_files(root, settings, output_dir, logs_dir)
        console.line(f"  Files found: {len(media_files)}")
        console.line(f"  Quality: {settings.quality} | Recursive: {settings.recursive} | Speed suffix: {settings.add_speed_suffix}")
        console.line("")

        speed = read_speed_choice(settings, console)
        media_files = get_media_files(root, settings, output_dir, logs_dir)
        if not media_files:
            console.line("No audio or video files were found with the current search settings.")
            logger.warning("No media files found.")
            wait_before_exit()
            return 0

        files_to_process = read_file_selection(media_files, root, settings, console, output_dir, logs_dir)
        if not files_to_process:
            console.line("No files were selected.")
            wait_before_exit()
            return 0

        speed_label = "1.5x" if speed == 1.5 else "2x"
        console.line("")
        console.line(f"Starting: speed {speed_label} | files: {len(files_to_process)} | quality: {settings.quality}")
        console.line(
            "Settings: "
            f"recursive={settings.recursive}; speed suffix={settings.add_speed_suffix}; "
            f"skip existing={settings.skip_existing}; external subtitles={settings.external_subtitles}; "
            f"embedded subtitles={settings.embedded_subtitles}; chapters={settings.scale_chapters}; "
            f"artwork/attachments={settings.preserve_artwork}"
        )
        console.line("Progress uses a plain-text display for compatibility with standard Windows and Unix terminals.")
        console.line("Cancel with Q, Esc, or Ctrl+C. Closing the terminal also stops the FFmpeg process.")
        logger.info(f"Starting: speed={speed_label}; files={len(files_to_process)}; quality={settings.quality}")

        # Establish child-process lifetime management before starting any media tools.
        try:
            supervisor = ProcessSupervisor(logger)
            cancellation.install()
        except Exception as exc:
            console.line(f"ERROR: Safe child-process management could not be enabled: {exc}")
            console.line("No conversions were started.")
            logger.error(f"Could not initialize process cleanup: {exc}")
            wait_before_exit()
            return 1

        key_poller.start()
        overall_start = time.monotonic()
        processed_count = 0

        for file_index, media_file in enumerate(files_to_process, start=1):
            if cancellation.requested.is_set() or key_poller.cancel_pressed():
                queue_cancelled = True
                console.line("Cancellation requested. No additional files will be started.")
                break

            display_name = get_relative_display_path(media_file, root)
            try:
                output_path = make_output_path(media_file, root, output_dir, speed, settings)
            except Exception as exc:
                failed_count += 1
                logger.error(f"Could not create output path for '{media_file}': {exc}")
                console.line(f"[{file_index}/{len(files_to_process)}] Failed to prepare '{display_name}': {exc}")
                continue

            if settings.skip_existing and output_path.is_file():
                skipped_count += 1
                processed_count += 1
                total_percent = processed_count / len(files_to_process) * 100.0
                console.line(f"[{file_index}/{len(files_to_process)}] Skipping existing output: {output_path.name}")
                logger.info(f"Skipped existing output: {output_path}")
                console.progress(file_index, len(files_to_process), 100.0, total_percent, "skipped", 0.0, 0.0,
                                 time.monotonic() - overall_start, estimate_eta(time.monotonic() - overall_start, total_percent))
                console.end_progress()
                continue

            temporary_output = make_temporary_output_path(output_path)
            chapter_metadata: Optional[Path] = None
            console.line("")
            console.line(f"[{file_index}/{len(files_to_process)}] {display_name}")
            console.line(f"Output: {output_path}")
            logger.info(f"Processing: {media_file}")
            logger.info(f"Output: {output_path}")

            try:
                if cancellation.requested.is_set() or key_poller.cancel_pressed():
                    raise ConversionCancelled("The user cancelled before media processing started.")
                probe = get_probe_data(media_file, ffprobe_path, supervisor, logger, cancellation, key_poller)
                source_duration = get_source_duration(probe)
                if settings.scale_chapters and probe.get("chapters"):
                    try:
                        chapter_metadata = create_scaled_chapter_metadata(
                            probe["chapters"], speed, Path(tempfile.gettempdir())
                        )
                    except Exception as exc:
                        logger.warning(f"Could not prepare scaled chapters for '{media_file}': {exc}")
                        console.line(f"  Warning: could not prepare scaled chapters: {exc}")

                arguments = build_ffmpeg_arguments(
                    media_file, temporary_output, probe, speed, settings, chapter_metadata,
                )
                run_result = invoke_ffmpeg_with_progress(
                    ffmpeg_path, arguments, source_duration, speed, file_index, len(files_to_process),
                    overall_start, console, logger, supervisor, cancellation, key_poller,
                )

                if run_result.cancelled:
                    cancelled_count += 1
                    queue_cancelled = True
                    console.line("Current conversion cancelled. Remaining files will not be started.")
                    logger.warning("Current conversion cancelled; remaining queue was not started.")
                elif run_result.exit_code == 0 and temporary_output.is_file():
                    os.replace(temporary_output, output_path)
                    success_count += 1
                    processed_count += 1
                    completion = f"Completed in {format_duration(run_result.duration)} (FFmpeg speed: {run_result.speed})."
                    console.line(completion)
                    logger.info(completion)
                    sidecars = get_subtitle_sidecars(media_file)
                    if sidecars:
                        before_errors = len(sidecars)
                        processed_subtitles = write_subtitle_sidecars(
                            media_file, output_path, speed, settings, logger, console,
                        )
                        subtitle_error_count += max(0, before_errors - processed_subtitles)
                else:
                    failed_count += 1
                    processed_count += 1
                    tail = " | ".join(run_result.error_lines[-2:])
                    message = f"FFmpeg exited with error code {run_result.exit_code}."
                    if run_result.exit_code == 0:
                        message = "FFmpeg reported success, but no temporary output file was created."
                    if tail:
                        message += " " + tail
                    console.line(f"ERROR: {message}")
                    logger.error(message)
            except ConversionCancelled:
                cancelled_count += 1
                queue_cancelled = True
                console.end_progress()
                console.line("Current operation cancelled. Remaining files will not be started.")
                logger.warning(f"Operation cancelled while preparing '{media_file}'.")
            except Exception as exc:
                failed_count += 1
                processed_count += 1
                console.end_progress()
                console.line(f"ERROR: Failed to process '{display_name}': {exc}")
                logger.error(f"Failed to process '{media_file}': {exc}")
            finally:
                try:
                    temporary_output.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(f"Could not remove temporary output '{temporary_output}': {exc}")
                if chapter_metadata:
                    try:
                        chapter_metadata.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning(f"Could not remove temporary chapter metadata '{chapter_metadata}': {exc}")

            if queue_cancelled:
                break

        key_poller.restore()
        cancellation.restore()
        elapsed_total = time.monotonic() - overall_start
        console.line("")
        console.line("  ===================== SUMMARY =====================")
        console.line(f"  Succeeded: {success_count}")
        console.line(f"  Skipped:   {skipped_count}")
        console.line(f"  Failed:    {failed_count}")
        console.line(f"  Cancelled: {cancelled_count}")
        not_started = max(0, len(files_to_process) - success_count - skipped_count - failed_count - cancelled_count)
        if queue_cancelled and not_started:
            console.line(f"  Not started: {not_started}")
        if subtitle_error_count:
            console.line(f"  External subtitle errors: {subtitle_error_count}")
        console.line(f"  Total elapsed: {format_duration(elapsed_total)}")
        console.line(f"  Output folder: {output_dir}")
        console.line(f"  Log file: {logger.path}")
        logger.info(
            f"Summary: succeeded={success_count}; skipped={skipped_count}; failed={failed_count}; "
            f"cancelled={cancelled_count}; not_started={not_started}; subtitle_errors={subtitle_error_count}; "
            f"elapsed={format_duration(elapsed_total)}"
        )

        if settings.open_output_at_end:
            open_output_folder(output_dir, logger)

        wait_before_exit()
        return 130 if queue_cancelled else (1 if failed_count else 0)

    except KeyboardInterrupt:
        cancellation.requested.set()
        if supervisor:
            supervisor.cleanup()
        console.end_progress()
        console.line("\nCancellation requested. Child processes have been stopped.")
        logger.warning("KeyboardInterrupt received.")
        wait_before_exit()
        return 130
    finally:
        key_poller.restore()
        cancellation.restore()
        if supervisor:
            supervisor.cleanup()
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
