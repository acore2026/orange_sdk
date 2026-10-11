"""Opt-in lifecycle observations; does not change capture or encoding ownership."""
from __future__ import annotations

from functools import wraps
from collections import deque
import logging
import os
import threading
import time
import traceback
import weakref

_ENABLED = os.environ.get("AGENT_VIDEO_DIAGNOSTICS") == "1"
_LOGGER = logging.getLogger(__name__)
_LOCK = threading.Lock()
_ACTIVE: dict[int, dict] = {}
_RECENT = deque(maxlen=16)


def observe_delivery(source, frame):
    if _ENABLED:
        with _LOCK:
            _RECENT.append(("delivered", getattr(source, "id", id(source)), id(frame), time.monotonic()))


def trace_encode(function):
    @wraps(function)
    def wrapped(self, frame, *args, **kwargs):
        if not _ENABLED:
            return function(self, frame, *args, **kwargs)
        thread = threading.get_native_id()
        with _LOCK:
            _RECENT.append(("encode_begin", thread, id(frame), time.monotonic()))
            _ACTIVE[thread] = {"frame": id(frame), "pts": frame.pts,
                               "started_monotonic": time.monotonic()}
        try:
            return function(self, frame, *args, **kwargs)
        finally:
            with _LOCK:
                _ACTIVE.pop(thread, None)
                _RECENT.append(("encode_end", thread, id(frame), time.monotonic()))
    return wrapped


def observe_source(source):
    if not _ENABLED or getattr(source, "_video_diagnostics", False):
        return
    source._video_diagnostics = True
    original_stop = weakref.WeakMethod(source.stop)
    source_ref = weakref.ref(source)
    source_id = getattr(source, "id", id(source))
    _LOGGER.info("video_lifecycle source_observed source=%s monotonic=%.6f", source_id, time.monotonic())

    def stop():
        target = source_ref()
        method = original_stop()
        if target is None or method is None:
            return
        with _LOCK:
            active = dict(_ACTIVE)
            recent = list(_RECENT)
        _LOGGER.warning(
            "video_lifecycle source_stop_begin source=%s state=%s monotonic=%.6f active_encodes=%r recent=%r caller=%s",
            source_id, getattr(target, "readyState", "unknown"), time.monotonic(), active, recent,
            "".join(traceback.format_stack(limit=16)),
        )
        try:
            return method()
        finally:
            _LOGGER.warning("video_lifecycle source_stop_end source=%s monotonic=%.6f", source_id, time.monotonic())

    source.stop = stop
