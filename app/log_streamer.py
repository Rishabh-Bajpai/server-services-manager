"""Real-time log streaming via journalctl -f.

Each (unit, priority) pair has at most one active subprocess shared
across all subscribers. Subscribers are tracked in a refcount; when
the last subscriber leaves, the subprocess is reaped.

Lines are delivered via Server-Sent Events (SSE) on a long-poll
endpoint. Each subscriber gets its own queue that the journalctl
reader thread fans lines out to.
"""
import logging
import os
import queue
import subprocess
import threading
from typing import Dict, List, Optional

logger = logging.getLogger("LogStreamer")

_VALID_PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


class _Subscriber:
    """One SSE consumer. Each subscriber has its own queue."""
    def __init__(self, sid: str):
        self.sid = sid
        self.queue: queue.Queue = queue.Queue(maxsize=2000)
        self.alive = True


class StreamHandle:
    """One journalctl subprocess + its subscribers + replay buffer."""

    def __init__(self, unit: str, priority: str, process: subprocess.Popen):
        self.unit = unit
        self.priority = priority
        self.process = process
        self.subscribers: Dict[str, _Subscriber] = {}
        self.buffer: List[str] = []
        self.max_buffer = 500
        self._lock = threading.Lock()
        self._stopped = False

    def add_subscriber(self, sid: str) -> List[str]:
        with self._lock:
            if sid not in self.subscribers:
                self.subscribers[sid] = _Subscriber(sid)
            return list(self.buffer)

    def remove_subscriber(self, sid: str) -> bool:
        with self._lock:
            self.subscribers.pop(sid, None)
            return not self.subscribers

    def fanout_line(self, line: str) -> None:
        with self._lock:
            self.buffer.append(line)
            if len(self.buffer) > self.max_buffer:
                self.buffer = self.buffer[-self.max_buffer:]
            subs = list(self.subscribers.values())
        for sub in subs:
            try:
                sub.queue.put_nowait(line)
            except queue.Full:
                pass

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            if self.process.poll() is None:
                self.process.terminate()
            # Don't block on wait() — it can conflict with eventlet's
            # mainloop. The OS will reap the zombie once the reader
            # thread's pipe is fully drained.
        except Exception as e:
            logger.warning(f"error stopping journalctl for {self.unit}: {e}")


class LogStreamer:
    """Manages all active journalctl streams."""

    def __init__(self):
        self._streams: Dict[str, StreamHandle] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stream_key(unit: str, priority: str) -> str:
        return f"{unit}::{priority}"

    def subscribe(
        self,
        unit: str,
        sid: str,
        priority: str = "info",
        lines: int = 0,
    ) -> StreamHandle:
        """Subscribe ``sid`` to live log lines for ``unit``."""
        if priority not in _VALID_PRIORITIES:
            raise ValueError(f"invalid priority: {priority}")
        if not unit or "\n" in unit or " " in unit:
            raise ValueError("invalid unit name")

        key = self._stream_key(unit, priority)
        with self._lock:
            handle = self._streams.get(key)
            if handle is not None:
                handle.add_subscriber(sid)
                return handle

        new_handle = self._create_handle(unit, priority, lines)
        if new_handle is None:
            raise RuntimeError("journalctl not available")
        with self._lock:
            existing = self._streams.get(key)
            if existing is not None:
                new_handle.stop()
                existing.add_subscriber(sid)
                return existing
            self._streams[key] = new_handle
            new_handle.add_subscriber(sid)

        self._start_reader(new_handle)
        return new_handle

    def get_subscriber_queue(self, handle: StreamHandle, sid: str) -> Optional[queue.Queue]:
        with handle._lock:
            sub = handle.subscribers.get(sid)
            return sub.queue if sub else None

    def get_replay(self, handle: StreamHandle) -> List[str]:
        with handle._lock:
            return list(handle.buffer)

    def _create_handle(self, unit: str, priority: str, lines: int) -> Optional[StreamHandle]:
        if not os.path.exists("/usr/bin/journalctl"):
            logger.warning("journalctl not found; cannot stream")
            return None
        cmd = ["journalctl", "-u", unit, "-f", "-o", "short", "--no-pager", "-p", priority]
        if lines and lines > 0:
            cmd += ["-n", str(min(lines, 5000))]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"failed to spawn journalctl for {unit}: {e}")
            return None
        return StreamHandle(unit, priority, proc)

    def _start_reader(self, handle: StreamHandle) -> None:
        def reader():
            try:
                for line in iter(handle.process.stdout.readline, ""):
                    if not line:
                        break
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    handle.fanout_line(line)
            except Exception as e:
                logger.warning(f"reader thread for {handle.unit} crashed: {e}")
            finally:
                with handle._lock:
                    subs = list(handle.subscribers.values())
                for sub in subs:
                    try:
                        sub.queue.put_nowait(None)
                    except queue.Full:
                        pass
                with self._lock:
                    self._streams.pop(self._stream_key(handle.unit, handle.priority), None)

        t = threading.Thread(target=reader, name=f"log-streamer-{handle.unit}", daemon=True)
        t.start()

    def unsubscribe(self, unit: str, sid: str, priority: str = "info") -> None:
        key = self._stream_key(unit, priority)
        with self._lock:
            handle = self._streams.get(key)
            if handle is None:
                return
            empty = handle.remove_subscriber(sid)
            if empty:
                self._streams.pop(key, None)
                handle_to_stop = handle
            else:
                handle_to_stop = None
        if handle_to_stop is not None:
            handle_to_stop.stop()
            logger.info(f"reaped journalctl for {unit} (no subscribers)")

    def disconnect_sid(self, sid: str) -> None:
        with self._lock:
            handles = list(self._streams.values())
        for handle in handles:
            empty = handle.remove_subscriber(sid)
            if empty:
                with self._lock:
                    self._streams.pop(self._stream_key(handle.unit, handle.priority), None)
                handle.stop()

    def active_streams(self) -> List[dict]:
        with self._lock:
            return [
                {
                    "unit": h.unit,
                    "priority": h.priority,
                    "subscribers": len(h.subscribers),
                    "buffer_size": len(h.buffer),
                }
                for h in self._streams.values()
            ]

    def shutdown(self) -> None:
        with self._lock:
            handles = list(self._streams.values())
            self._streams.clear()
        for h in handles:
            h.stop()


_streamer: Optional[LogStreamer] = None


def get_streamer() -> LogStreamer:
    global _streamer
    if _streamer is None:
        _streamer = LogStreamer()
    return _streamer
