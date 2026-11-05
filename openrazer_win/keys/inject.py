"""Sending keys and mouse input to Windows, as if typed and moved by hand.

Everything sent from here is tagged in ``dwExtraInfo`` so the daemon's own
hook can tell its injections from keys someone actually pressed -- without
that, a translated key would be translated again, and a macro would record
itself.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Union

from .codes import Key

logger = logging.getLogger(__name__)

#: "Sent by openrazer-win" -- what the hook looks for in dwExtraInfo.
MARKER = 0x52415A52      # 'RAZR'

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

#: Button name -> (press flag, release flag, mouseData).
MOUSE_BUTTON_FLAGS = {
    'left': (0x0002, 0x0004, 0),
    'right': (0x0008, 0x0010, 0),
    'middle': (0x0020, 0x0040, 0),
    'x1': (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON1),
    'x2': (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP, XBUTTON2),
}

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
#: Physical pixels on every monitor -- what the mouse hook records in.
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

#: Virtual keys whose events must carry the extended flag even when sent by
#: virtual key: Windows otherwise treats them as their keypad twins.
_EXTENDED_VKS = frozenset((
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
    0x6F, 0x90, 0xA3, 0xA5, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB7,
))


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_ushort), ('wScan', ctypes.c_ushort),
                ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long),
                ('mouseData', ctypes.c_ulong), ('dwFlags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('mi', _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ulong), ('union', _INPUTUNION)]


def absolute_position(x: int, y: int, desktop: tuple) -> tuple:
    """Desktop pixels -> the 0-65535 range ``MOUSEEVENTF_VIRTUALDESK`` takes.

    `desktop` is the virtual screen as (left, top, width, height); a point off
    it is held at its edge, as the real cursor would be.
    """
    left, top, width, height = desktop
    x = min(max(x, left), left + width - 1)
    y = min(max(y, top), top + height - 1)
    return (int(round((x - left) * 65535 / max(1, width - 1))),
            int(round((y - top) * 65535 / max(1, height - 1))))


class Injector:
    """Sends key and mouse events through ``SendInput``."""

    def __init__(self):
        self._send = None
        self._metrics = None
        self._dpi_context = None
        if sys.platform == 'win32':
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int)
            user32.SendInput.restype = ctypes.c_uint
            self._send = user32.SendInput
            user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
            user32.GetSystemMetrics.restype = ctypes.c_int
            self._metrics = user32.GetSystemMetrics
            # Windows 10 1607 and later; without it positions are scaled the
            # way the system thinks this process expects.
            context = getattr(user32, 'SetThreadDpiAwarenessContext', None)
            if context is not None:
                context.argtypes = (ctypes.c_ssize_t,)
                context.restype = ctypes.c_ssize_t
                self._dpi_context = context

    def _send_inputs(self, array, count: int) -> None:
        sent = self._send(count, array, ctypes.sizeof(_INPUT))
        if sent != count:
            logger.debug('SendInput sent %d of %d events: WinError %d', sent, count,
                         ctypes.get_last_error())

    def _emit(self, events: list) -> None:
        if self._send is None or not events:
            return
        array = (_INPUT * len(events))()
        for slot, (vk, scan, flags) in zip(array, events):
            slot.type = INPUT_KEYBOARD
            slot.union.ki = _KEYBDINPUT(vk, scan, flags, 0, MARKER)
        self._send_inputs(array, len(events))

    def _mouse(self, dx: int, dy: int, data: int, flags: int) -> None:
        if self._send is None:
            return
        array = (_INPUT * 1)()
        array[0].type = INPUT_MOUSE
        # mouseData is a DWORD; a wheel turned down is a negative delta.
        array[0].union.mi = _MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, MARKER)
        self._send_inputs(array, 1)

    def desktop(self) -> tuple:
        """The virtual screen, all monitors together, in physical pixels."""
        if self._metrics is None:
            return (0, 0, 1920, 1080)
        if self._dpi_context is not None:
            self._dpi_context(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return (self._metrics(SM_XVIRTUALSCREEN), self._metrics(SM_YVIRTUALSCREEN),
                self._metrics(SM_CXVIRTUALSCREEN), self._metrics(SM_CYVIRTUALSCREEN))

    def mouse_move_to(self, x: int, y: int) -> None:
        """Put the cursor at desktop pixel x, y."""
        nx, ny = absolute_position(x, y, self.desktop())
        self._mouse(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)

    def mouse_move_by(self, dx: int, dy: int) -> None:
        """Move the mouse as if pushed: what games that hide the cursor read."""
        if dx or dy:
            self._mouse(int(dx), int(dy), 0, MOUSEEVENTF_MOVE)

    def mouse_button(self, button: str, down: bool) -> None:
        press, release, data = MOUSE_BUTTON_FLAGS[button]
        self._mouse(0, 0, data, press if down else release)

    def mouse_wheel(self, amount: int, horizontal: bool = False) -> None:
        """Turn the wheel by `amount`; 120 is one notch, positive is up (or right)."""
        self._mouse(0, 0, int(amount), MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL)

    def key(self, target: Union[Key, int], down: bool) -> None:
        """Press or release a key: a :class:`Key` by scan code, an int by virtual key."""
        flags = 0 if down else KEYEVENTF_KEYUP
        if isinstance(target, Key):
            if target.vk in (0x13,) or target.scan >= 0x64:
                # Pause and F13-F24 go by virtual key: their scan codes are not
                # mapped the same way on every keyboard layout.
                vk_flags = flags | (KEYEVENTF_EXTENDEDKEY if target.extended else 0)
                self._emit([(target.vk, target.scan, vk_flags)])
                return
            flags |= KEYEVENTF_SCANCODE
            if target.extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            self._emit([(0, target.scan, flags)])
            return
        if target in _EXTENDED_VKS:
            flags |= KEYEVENTF_EXTENDEDKEY
        self._emit([(int(target), 0, flags)])

    def tap(self, target: Union[Key, int]) -> None:
        self.key(target, True)
        self.key(target, False)

    def raw(self, vk: int, scan: int, extended: bool, down: bool) -> None:
        """Replay a key exactly as it was recorded."""
        flags = 0 if down else KEYEVENTF_KEYUP
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if scan:
            flags |= KEYEVENTF_SCANCODE
            self._emit([(0, scan, flags)])
        else:
            self._emit([(vk, 0, flags)])

    def text(self, text: str) -> None:
        """Type `text` character by character, independent of keyboard layout."""
        events = []
        for unit in _utf16_units(text):
            events.append((0, unit, KEYEVENTF_UNICODE))
            events.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        self._emit(events)


def _utf16_units(text: str) -> list:
    data = text.encode('utf-16-le')
    return [int.from_bytes(data[i:i + 2], 'little') for i in range(0, len(data), 2)]


class RecordingInjector:
    """An :class:`Injector` stand-in that writes down what it would send."""

    def __init__(self):
        self.sent: list = []

    def key(self, target, down: bool) -> None:
        self.sent.append(('key', target, down))

    def tap(self, target) -> None:
        self.key(target, True)
        self.key(target, False)

    def raw(self, vk: int, scan: int, extended: bool, down: bool) -> None:
        self.sent.append(('raw', vk, scan, extended, down))

    def text(self, text: str) -> None:
        self.sent.append(('text', text))

    def mouse_move_to(self, x: int, y: int) -> None:
        self.sent.append(('move_to', x, y))

    def mouse_move_by(self, dx: int, dy: int) -> None:
        self.sent.append(('move_by', dx, dy))

    def mouse_button(self, button: str, down: bool) -> None:
        self.sent.append(('button', button, down))

    def mouse_wheel(self, amount: int, horizontal: bool = False) -> None:
        self.sent.append(('wheel', amount, horizontal))
