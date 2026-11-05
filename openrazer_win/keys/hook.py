"""Low-level hooks: a keyboard one that may hold a key back, and a mouse one that watches.

The effects engine's hook only watches.  This one is the user-space half of
what the kernel driver does with a Razer keyboard in driver mode: a key
pressed with Fn held has to be *replaced* -- F1 swallowed, Mute sent in its
place -- so the hook must be able to keep the original from reaching anything
else.  It runs on its own thread with its own message loop, and the decision
is made synchronously; anything slow is handed off by the callback.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable, NamedTuple, Optional

from .inject import MARKER

logger = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10


class KeyEvent(NamedTuple):
    vk: int
    scan: int
    extended: bool
    down: bool
    #: Sent by software rather than typed.
    injected: bool
    #: Sent by this daemon.
    ours: bool
    #: Milliseconds, from the system's tick count.
    time: int


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', ctypes.c_ulong), ('scanCode', ctypes.c_ulong),
                ('flags', ctypes.c_ulong), ('time', ctypes.c_ulong),
                ('dwExtraInfo', ctypes.c_size_t)]


def is_available() -> bool:
    return sys.platform == 'win32'


class MouseEvent(NamedTuple):
    """One thing the mouse did.  `kind` is ``move``, ``wheel`` or a button name."""

    kind: str
    x: int
    y: int
    #: For a button, pressed; for the wheel, unused.
    down: bool
    #: For the wheel: 120 is one notch, positive is up (or right).
    amount: int
    horizontal: bool
    injected: bool
    ours: bool
    time: int


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long),
                ('mouseData', ctypes.c_ulong), ('flags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('dwExtraInfo', ctypes.c_size_t)]


WH_MOUSE_LL = 14
WM_MOUSEMOVE = 0x0200
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
LLMHF_INJECTED = 0x01

#: Button messages -> (button, pressed).  X buttons are told apart by mouseData.
_MOUSE_BUTTONS = {
    0x0201: ('left', True), 0x0202: ('left', False),
    0x0204: ('right', True), 0x0205: ('right', False),
    0x0207: ('middle', True), 0x0208: ('middle', False),
}


def _signed16(value: int) -> int:
    value &= 0xFFFF
    return value - 0x10000 if value & 0x8000 else value


def decode_mouse(message: int, x: int, y: int, data: int, flags: int, extra: int,
                 when: int) -> Optional[MouseEvent]:
    """A low-level mouse message as a :class:`MouseEvent`, or ``None`` if not one."""
    injected = bool(flags & LLMHF_INJECTED)
    ours = extra == MARKER
    if message == WM_MOUSEMOVE:
        return MouseEvent('move', x, y, False, 0, False, injected, ours, when)
    if message in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
        return MouseEvent('wheel', x, y, False, _signed16(data >> 16),
                          message == WM_MOUSEHWHEEL, injected, ours, when)
    if message in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        button = 'x1' if (data >> 16) & 0xFFFF == 1 else 'x2'
        return MouseEvent(button, x, y, message == WM_XBUTTONDOWN, 0, False, injected, ours, when)
    known = _MOUSE_BUTTONS.get(message)
    if known is None:
        return None
    return MouseEvent(known[0], x, y, known[1], 0, False, injected, ours, when)


class _LowLevelHook:
    """A ``WH_*_LL`` hook on its own thread with its own message loop."""

    _hook_id = 0
    _struct: type = ctypes.Structure
    _name = 'hook'

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._procedure = None
        self._ready = threading.Event()
        self.running = False

    def start(self) -> bool:
        if not is_available():
            return False
        if self._thread is not None:
            return self.running
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)
        return self.running

    def stop(self) -> None:
        if self._thread is None:
            return
        if self._thread_id:
            from ctypes import wintypes

            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.PostThreadMessageW.argtypes = (
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.PostThreadMessageW.restype = wintypes.BOOL
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None
        self.running = False

    def _handle(self, message: int, raw) -> bool:
        """Deal with one message; True swallows it."""
        raise NotImplementedError

    def _run(self) -> None:
        from ctypes import wintypes

        procedure_type = ctypes.WINFUNCTYPE(
            wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(self._struct))
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        # Every handle-returning call gets its restype declared: the c_int
        # default truncates 64-bit handles, which is what once made
        # SetWindowsHookEx fail with ERROR_MOD_NOT_FOUND.
        kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.SetWindowsHookExW.argtypes = (
            ctypes.c_int, procedure_type, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = (
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(self._struct))
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = wintypes.BOOL

        def callback(code, message, data):
            if code >= 0:
                try:
                    if self._handle(message, data.contents):
                        return 1
                except Exception:  # noqa: BLE001 - a hook must never raise
                    logger.debug('%s failed', self._name, exc_info=True)
            return user32.CallNextHookEx(None, code, message, data)

        # Held for as long as the hook is installed, or Windows calls into a
        # trampoline the garbage collector has freed.
        self._procedure = procedure_type(callback)
        hook = user32.SetWindowsHookExW(
            self._hook_id, self._procedure, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            logger.warning('cannot install the %s: WinError %d', self._name,
                           ctypes.get_last_error())
            self._ready.set()
            return
        self._thread_id = kernel32.GetCurrentThreadId()
        self.running = True
        self._ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
        self._procedure = None
        self.running = False


class FilterHook(_LowLevelHook):
    """Calls ``on_event(KeyEvent) -> bool`` for every key; True swallows it."""

    _hook_id = WH_KEYBOARD_LL
    _struct = _KBDLLHOOKSTRUCT
    _name = 'key-filter'

    def __init__(self, on_event: Callable[[KeyEvent], bool]):
        super().__init__()
        self._on_event = on_event

    def _handle(self, message: int, raw) -> bool:
        if message not in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
            return False
        return bool(self._on_event(KeyEvent(
            vk=raw.vkCode, scan=raw.scanCode,
            extended=bool(raw.flags & LLKHF_EXTENDED),
            down=message in (WM_KEYDOWN, WM_SYSKEYDOWN),
            injected=bool(raw.flags & LLKHF_INJECTED),
            ours=raw.dwExtraInfo == MARKER, time=raw.time)))


class MouseHook(_LowLevelHook):
    """Calls ``on_event(MouseEvent)`` for everything the mouse does; never holds it back.

    Only installed while a macro is being recorded: every mouse message waits
    on a low-level hook, so it is not left in place when nothing needs it.
    """

    _hook_id = WH_MOUSE_LL
    _struct = _MSLLHOOKSTRUCT
    _name = 'mouse-recorder'

    def __init__(self, on_event: Callable[[MouseEvent], None]):
        super().__init__()
        self._on_event = on_event

    def _handle(self, message: int, raw) -> bool:
        event = decode_mouse(message, raw.x, raw.y, raw.mouseData, raw.flags,
                             raw.dwExtraInfo, raw.time)
        if event is not None:
            self._on_event(event)
        return False
