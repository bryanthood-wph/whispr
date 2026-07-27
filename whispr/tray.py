"""System tray controller: heartbeat icon, state display, and mid-call kill switch.

Draws two small in-memory icons (idle = grey dot, recording = red dot) with
Pillow, and manages a pystray icon whose menu offers "Stop & keep" /
"Stop & discard" while recording, both disabled while idle.
"""

from __future__ import annotations

import threading
from typing import Callable

from PIL import Image, ImageDraw
import pystray

from whispr.log import get_logger

log = get_logger("tray")

_ICON_SIZE = 64
_IDLE_COLOR = (140, 140, 140, 255)
_RECORDING_COLOR = (220, 30, 30, 255)


def _make_dot_icon(color: tuple[int, int, int, int]) -> Image.Image:
    """Draw a filled circle of the given color on a transparent 64x64 canvas."""
    image = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 6
    draw.ellipse(
        (margin, margin, _ICON_SIZE - margin, _ICON_SIZE - margin),
        fill=color,
    )
    return image


class TrayController:
    """Owns the system-tray icon and drives it from application state changes.

    The main app calls `set_idle()` / `set_recording(label)` as call state
    changes, and supplies callbacks for the two stop actions and quit. All
    pystray interaction happens through this controller so callers never touch
    pystray directly.
    """

    def __init__(
        self,
        on_stop_keep: Callable[[], None],
        on_stop_discard: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        """Store callbacks and build (but do not run) the tray icon."""
        self._on_stop_keep = on_stop_keep
        self._on_stop_discard = on_stop_discard
        self._on_quit = on_quit

        self._lock = threading.Lock()
        self._recording = False
        self._label = ""
        self._running = False

        self._idle_image = _make_dot_icon(_IDLE_COLOR)
        self._recording_image = _make_dot_icon(_RECORDING_COLOR)

        self._icon = pystray.Icon(
            "whispr",
            icon=self._idle_image,
            title="whispr — idle",
            menu=pystray.Menu(
                pystray.MenuItem(
                    "Stop & keep",
                    self._handle_stop_keep,
                    enabled=self._is_recording,
                ),
                pystray.MenuItem(
                    "Stop & discard",
                    self._handle_stop_discard,
                    enabled=self._is_recording,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit whispr", self._handle_quit),
            ),
        )

    def _is_recording(self, _item: pystray.MenuItem) -> bool:
        """Return whether the stop menu items should currently be enabled."""
        with self._lock:
            return self._recording

    def _handle_stop_keep(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        """Fire the stop-and-keep callback, then return the tray to idle."""
        log.info("Menu action: Stop & keep")
        try:
            self._on_stop_keep()
        except Exception:  # never let an exception escape into the OS callback
            log.exception("stop-keep callback raised")
        finally:
            self.set_idle()

    def _handle_stop_discard(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        """Fire the stop-and-discard callback, then return the tray to idle."""
        log.info("Menu action: Stop & discard")
        try:
            self._on_stop_discard()
        except Exception:
            log.exception("stop-discard callback raised")
        finally:
            self.set_idle()

    def _handle_quit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        """Fire the quit callback and tear down the tray icon."""
        log.info("Menu action: Quit whispr")
        try:
            self._on_quit()
        except Exception:
            log.exception("quit callback raised")
        finally:
            self.stop()

    def run(self) -> None:
        """Run the pystray event loop, blocking the calling thread.

        Must be called on the main thread on Windows.
        """
        log.info("Starting tray icon (blocking run)")
        with self._lock:
            self._running = True
        self._icon.run()

    def set_idle(self) -> None:
        """Transition to idle state: grey icon, idle tooltip, stop items disabled."""
        with self._lock:
            self._recording = False
            self._label = ""
            running = self._running
        log.info("State -> idle")
        self._icon.icon = self._idle_image
        self._icon.title = "whispr — idle"
        if running:
            self._icon.update_menu()

    def set_recording(self, label: str) -> None:
        """Transition to recording state: red icon, tooltip shows the call label."""
        with self._lock:
            self._recording = True
            self._label = label
            running = self._running
        log.info("State -> recording: %s", label)
        self._icon.icon = self._recording_image
        self._icon.title = f"whispr — recording: {label}"
        if running:
            self._icon.update_menu()

    def stop(self) -> None:
        """Tear down the tray icon."""
        log.info("Stopping tray icon")
        with self._lock:
            self._running = False
        self._icon.stop()
