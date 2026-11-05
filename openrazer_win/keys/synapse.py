"""Macros exported from Razer Synapse, as bindings this port can play.

Synapse 2, 3 and 4 export a macro as XML::

    <Macro>
      <Name>My macro</Name>
      <MacroEvents>
        <MacroEvent>                    <!-- Type 1: a key -->
          <Type>1</Type>
          <Delay>50</Delay>             <!-- ms before this event -->
          <KeyEvent><Makecode>30</Makecode><State>0</State></KeyEvent>   <!-- 0 down, 1 up -->
        </MacroEvent>
        <MacroEvent>                    <!-- Type 2: a mouse button -->
          <Type>2</Type>
          <MouseEvent><MouseButton>1</MouseButton><State>0</State></MouseEvent>
        </MacroEvent>
        <MacroEvent>                    <!-- Type 3: the cursor's path, in desktop pixels -->
          <Type>3</Type>
          <MouseMovement>
            <MouseMovementEvent><X>976</X><Y>717</Y><Delay>0</Delay></MouseMovementEvent>
          </MouseMovement>
        </MacroEvent>
      </MacroEvents>
    </Macro>

What ``<Makecode>`` holds depends on the version: a set 1 scan code in Synapse
2 and 3 (29 is Ctrl, 72 the up arrow), a Windows virtual key in Synapse 4 (17
is Ctrl, 38 the up arrow), which also writes ``<Id>``, ``<isPairing>`` and
``<selected>`` and often a ``<ScanCode>`` -- which is how the two are told apart.
Both are what Windows itself uses, so keys come across exactly; and the cursor
positions are the desktop's own pixels, so a macro recorded on this PC goes
where it went in Synapse.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Dict, List, NamedTuple, Optional, Tuple, Union

from .codes import KEYS, Key
from .inject import _EXTENDED_VKS
from .macros import MAX_DELAY_MS, MAX_EVENTS, MacroError, validate

CODES_AUTO = 'auto'
CODES_SCANCODE = 'scancode'
CODES_VK = 'vk'
CODES = (CODES_AUTO, CODES_SCANCODE, CODES_VK)

#: Synapse files are small; this is only there to refuse something that is not one.
MAX_FILE_SIZE = 16 << 20

#: Scan codes that only exist with the E0 prefix, so need no flag to be read right.
_ALWAYS_EXTENDED = frozenset((0x5B, 0x5C, 0x5D))

#: Scan codes that are the numeric keypad without E0 and the navigation keys
#: with it.  Synapse 3 does not keep the flag and shows them as the navigation
#: keys (arrows, Home, ...), so they are read that way unless asked otherwise.
_NAV_OR_NUMPAD = frozenset((0x47, 0x48, 0x49, 0x4B, 0x4D, 0x4F, 0x50, 0x51, 0x52, 0x53))

#: Synapse ``<MouseButton>`` -> button.  1 = left is confirmed from real
#: exports; the others follow Synapse's order of the buttons (left, right,
#: middle, back, forward).  A button map overrides them.
DEFAULT_BUTTON_MAP: Dict[int, Union[str, Tuple[int, bool]]] = {
    1: 'left', 2: 'right', 3: 'middle', 4: 'x1', 5: 'x2',
}
_BUTTON_WORDS = {
    'left': 'left', 'leftbutton': 'left', 'lbutton': 'left',
    'right': 'right', 'rightbutton': 'right', 'rbutton': 'right',
    'middle': 'middle', 'middlebutton': 'middle', 'mbutton': 'middle',
    'scrollclick': 'middle', 'wheelclick': 'middle',
    'xbutton1': 'x1', 'button4': 'x1', 'back': 'x1',
    'xbutton2': 'x2', 'button5': 'x2', 'forward': 'x2',
}
#: Wheel actions, as (amount, horizontal); 120 is one notch.
_WHEEL_WORDS = {
    'scrollup': (120, False), 'wheelup': (120, False),
    'scrolldown': (-120, False), 'wheeldown': (-120, False),
    'scrollleft': (-120, True), 'wheelleft': (-120, True),
    'scrollright': (120, True), 'wheelright': (120, True),
}

_VK_BY_SCAN = {(key.scan, key.extended): key.vk for key in KEYS.values()}
_SCAN_BY_VK = {}
for _key in KEYS.values():
    _SCAN_BY_VK.setdefault(_key.vk, _key)
# The keypad, which the shared key table leaves out: (scan code, virtual key).
_KEYPAD = ((0x52, 0x60), (0x4F, 0x61), (0x50, 0x62), (0x51, 0x63), (0x4B, 0x64), (0x4C, 0x65),
           (0x4D, 0x66), (0x47, 0x67), (0x48, 0x68), (0x49, 0x69), (0x4E, 0x6B), (0x4A, 0x6D),
           (0x53, 0x6E))
for _scan, _vk in _KEYPAD:
    _VK_BY_SCAN.setdefault((_scan, False), _vk)
    _SCAN_BY_VK.setdefault(_vk, Key(_scan, False, _vk))
# Shift, Ctrl and Alt without a side are the left ones, as Synapse records them.
for _generic, _left in ((0x10, 'KEY_LEFTSHIFT'), (0x11, 'KEY_LEFTCTRL'), (0x12, 'KEY_LEFTALT')):
    _SCAN_BY_VK[_generic] = KEYS[_left]


class SynapseError(ValueError):
    """Not a Synapse macro export, or nothing in it can be played."""


class SynapseMacro(NamedTuple):
    name: str
    #: A binding, ``{"type": "keys", "events": [...]}``, ready for macro.set.
    action: dict
    #: What could not be brought across exactly, one line each.
    warnings: List[str]
    #: ``synapse3`` (scan codes, Synapse 2 and 3) or ``synapse4`` (virtual keys).
    version: str

    def as_dict(self) -> dict:
        return {'name': self.name, 'action': self.action, 'warnings': list(self.warnings),
                'version': self.version}


def _local(tag) -> str:
    return tag.rsplit('}', 1)[-1].lower() if isinstance(tag, str) else ''


def _child(element, name: str):
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element, name: str) -> Optional[str]:
    child = _child(element, name)
    if child is None or child.text is None or not child.text.strip():
        return None
    return child.text.strip()


def _int(element, name: str, default: Optional[int] = None) -> Optional[int]:
    value = _text(element, name)
    if value is None:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _truthy(value: Optional[str]) -> bool:
    return str(value).strip().lower() in ('1', 'true', 'yes')


def _is_synapse4(macro) -> bool:
    return any(_local(e.tag) in ('ispairing', 'selected', 'scancode') for e in macro.iter())


def _key_from_scancode(code: int, extended: bool, numpad: bool) -> Optional[dict]:
    if code > 0xFF:
        # Some tools write extended keys as 0xE0xx or 0x1xx.
        if code >> 8 in (0xE0, 0x01):
            extended = True
        code &= 0xFF
    if not 0 < code < 0x80:
        return None
    if code in _ALWAYS_EXTENDED or (code in _NAV_OR_NUMPAD and not numpad):
        extended = True
    return {'vk': _VK_BY_SCAN.get((code, extended), 0), 'scan': code, 'ext': extended}


def _key_from_vk(vk: int, scan: Optional[int]) -> Optional[dict]:
    if not 0 < vk <= 0xFF:
        return None
    extended = vk in _EXTENDED_VKS
    if not scan:
        known = _SCAN_BY_VK.get(vk)
        # Typed by scan code when the key is a known one: it is the same key
        # whatever the keyboard layout, as when Synapse recorded it.
        if known is not None:
            return {'vk': vk, 'scan': known.scan, 'ext': known.extended}
        return {'vk': vk, 'scan': 0, 'ext': extended}
    return {'vk': vk, 'scan': scan & 0xFF, 'ext': extended or bool(scan >> 8)}


def _parse_key(element, item, use_vk: bool,
               numpad: bool) -> Tuple[Optional[dict], Optional[str]]:
    makecode = _int(element, 'makecode')
    # Synapse 4 writes the scan code beside <KeyEvent>, not in it.
    scancode = _int(element, 'scancode', _int(item, 'scancode'))
    extended = any('extend' in _local(c.tag) and _truthy(c.text)
                   for c in list(element) + list(item))
    if makecode is None and scancode is None:
        return None, 'a key event without a Makecode'
    if use_vk and makecode is not None:
        key = _key_from_vk(makecode, scancode)
    else:
        code = makecode if makecode is not None else scancode
        key = _key_from_scancode(code, extended, numpad)
    if key is None:
        return None, 'unknown key code {0} ({1})'.format(
            makecode if makecode is not None else scancode,
            'virtual key' if use_vk else 'scan code')
    return key, None


def _parse_button(element, button_map) -> Tuple[Optional[str], object, Optional[str]]:
    raw = _text(element, 'mousebutton') or _text(element, 'button')
    if raw is None:
        return None, None, 'a mouse event without a MouseButton'
    try:
        number = int(raw)
    except ValueError:
        word = raw.replace(' ', '').replace('_', '').lower()
        if word in _WHEEL_WORDS:
            return 'wheel', _WHEEL_WORDS[word], None
        if word in _BUTTON_WORDS:
            return 'button', _BUTTON_WORDS[word], None
        return None, None, 'unknown mouse button {0!r}'.format(raw)
    value = button_map.get(number)
    if value is None:
        return None, None, 'unknown mouse button {0} (a button map can name it)'.format(number)
    if isinstance(value, tuple):
        return 'wheel', value, None
    return 'button', value, None


def _parse_macro(macro, codes: str, numpad: bool, keep_delays: bool, delay_ms: int,
                 relative: bool, button_map) -> SynapseMacro:
    name = _text(macro, 'name') or 'Synapse macro'
    use_vk = _is_synapse4(macro) if codes == CODES_AUTO else codes == CODES_VK
    warnings: List[str] = []
    events: List[dict] = []
    pending = 0
    capped = False

    def add(event: dict) -> None:
        nonlocal pending, capped
        delay = pending if keep_delays else delay_ms
        pending = 0
        if delay > MAX_DELAY_MS:
            capped = True
            delay = MAX_DELAY_MS
        event['delay'] = max(0, delay) if events else 0
        events.append(event)

    container = _child(macro, 'macroevents')
    for index, item in enumerate(c for c in container if _local(c.tag) == 'macroevent'):
        pending += _int(item, 'delay', 0) or 0
        key = _child(item, 'keyevent')
        mouse = _child(item, 'mouseevent')
        movement = _child(item, 'mousemovement')
        if key is not None:
            event, problem = _parse_key(key, item, use_vk, numpad)
            if problem:
                warnings.append('event {0}: skipped {1}'.format(index + 1, problem))
                continue
            event['down'] = _int(key, 'state', 0) != 1
            add(event)
        elif mouse is not None:
            kind, value, problem = _parse_button(mouse, button_map)
            if problem:
                warnings.append('event {0}: skipped {1}'.format(index + 1, problem))
                continue
            if kind == 'wheel':
                if _int(mouse, 'state', 0) == 1:
                    continue        # letting go of a wheel turn means nothing
                amount, horizontal = value
                add({'mouse': 'wheel', 'amount': amount, 'horizontal': horizontal})
            else:
                add({'mouse': value, 'down': _int(mouse, 'state', 0) != 1})
        elif movement is not None:
            last = None
            for point in movement:
                if _child(point, 'x') is None and _child(point, 'y') is None:
                    continue
                pending += _int(point, 'delay', 0) or 0
                x = _int(point, 'x', last[0] if last else 0)
                y = _int(point, 'y', last[1] if last else 0)
                last = (x, y)
                add({'mouse': 'move', 'x': x, 'y': y})
        else:
            warnings.append('event {0}: skipped an event of a kind this port does not know '
                            '(type {1})'.format(index + 1, _text(item, 'type') or '?'))
    if capped:
        warnings.append('pauses longer than {0} s were shortened to {0} s'.format(
            MAX_DELAY_MS // 1000))
    if len(events) > MAX_EVENTS:
        warnings.append('only the first {0} of {1} events were kept'.format(
            MAX_EVENTS, len(events)))
        events = events[:MAX_EVENTS]
    action: dict = {'type': 'keys', 'events': events}
    if relative:
        action['moves'] = 'relative'
    return SynapseMacro(name, action, warnings, 'synapse4' if use_vk else 'synapse3')


def parse(source: Union[str, bytes], codes: str = CODES_AUTO, numpad: bool = False,
          keep_delays: bool = True, delay_ms: int = 0, relative: bool = False,
          button_map: Optional[dict] = None) -> List[SynapseMacro]:
    """Every macro in a Synapse export.

    :param source: the path of the ``.xml`` file, or its contents
    :param codes: what ``<Makecode>`` holds -- ``auto`` tells from the file,
        ``scancode`` for Synapse 2 and 3, ``vk`` for Synapse 4
    :param numpad: read the scan codes Synapse 3 shares between the keypad and
        the arrows / Home / End / Page Up / Page Down / Insert / Delete as the keypad
    :param keep_delays: keep the recorded pauses; without, `delay_ms` between events
    :param relative: replay the cursor's path from wherever it is, not at the
        recorded positions
    :param button_map: Synapse button number -> ``left``, ``right``, ``middle``,
        ``x1``, ``x2``, or a wheel turn as (amount, horizontal)
    """
    if codes not in CODES:
        raise ValueError('codes is one of {0}'.format(', '.join(CODES)))
    if isinstance(source, str) and not source.lstrip().startswith('<'):
        path = os.path.expandvars(os.path.expanduser(source))
        try:
            if os.path.getsize(path) > MAX_FILE_SIZE:
                raise SynapseError('{0} is too large to be a Synapse macro'.format(path))
            with open(path, 'rb') as handle:
                data: Union[str, bytes] = handle.read()
        except OSError as error:
            raise SynapseError('cannot read {0}: {1}'.format(path, error.strerror)) from error
    else:
        data = source
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise SynapseError('not an XML file: {0}'.format(error)) from None
    mapping = dict(DEFAULT_BUTTON_MAP)
    mapping.update(button_map or {})
    macros = [element for element in root.iter()
              if _local(element.tag) == 'macro' and _child(element, 'macroevents') is not None]
    if not macros:
        raise SynapseError('no Synapse macro in this file')
    result = []
    for element in macros:
        macro = _parse_macro(element, codes, numpad, keep_delays, delay_ms, relative, mapping)
        if macro.action['events']:
            try:
                macro = macro._replace(action=validate(macro.action))
            except MacroError as error:
                raise SynapseError('{0!r}: {1}'.format(macro.name, error)) from error
        result.append(macro)
    return result
