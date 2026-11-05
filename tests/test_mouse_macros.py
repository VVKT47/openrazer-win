"""Mouse actions in macros, and macros brought over from Razer Synapse.

Synapse's macros carry clicks, scrolling and the cursor's path along with the
keys; these tests drive recording with emulated hook events, playback with a
recording injector, and the importer with exports shaped like Synapse's.
"""
from __future__ import annotations

import json

import pytest

from openrazer_win.core.persistence import Persistence
from openrazer_win.daemon.protocol import INVALID_PARAMS, RpcError
from openrazer_win.daemon.server import DaemonService
from openrazer_win.hid.fake import FakeHidBackend, FakeRazerDevice
from openrazer_win.keys import KeyService, MacroError, MacroStore
from openrazer_win.keys.codes import KEYS, describe_action
from openrazer_win.keys.hook import KeyEvent, MouseEvent, decode_mouse
from openrazer_win.keys.inject import MARKER, RecordingInjector, absolute_position
from openrazer_win.keys.macros import MAX_EVENTS, has_mouse, validate
from openrazer_win.keys.synapse import SynapseError, parse

BLACKWIDOW_CHROMA = 0x0203
A = KEYS['KEY_A']


class FakeHook:
    running = False

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False


def key(k, down, t=0):
    return KeyEvent(vk=k.vk, scan=k.scan, extended=k.extended, down=down,
                    injected=False, ours=False, time=t)


class FakeMouseHook(FakeHook):
    starts = 0

    def start(self):
        self.starts += 1
        return super().start()


def move(x, y, t, injected=False):
    return MouseEvent('move', x, y, False, 0, False, injected, False, t)


def click(button, down, t, x=0, y=0):
    return MouseEvent(button, x, y, down, 0, False, False, False, t)


def wheel(amount, t, horizontal=False):
    return MouseEvent('wheel', 0, 0, False, amount, horizontal, False, False, t)


@pytest.fixture
def rig(tmp_path):
    fake = FakeRazerDevice(BLACKWIDOW_CHROMA, 'Razer BlackWidow Chroma', interface=2, keys=True)
    backend = FakeHidBackend([fake])
    injector = RecordingInjector()
    mouse_hook = FakeMouseHook()
    keys = KeyService(backend, macros=MacroStore(str(tmp_path / 'macros.json')),
                      injector=injector, hook=FakeHook(), mouse_hook=mouse_hook)
    service = DaemonService(backend=backend, persistence=Persistence(str(tmp_path / 'p.json')),
                            enable_effects=False, keys=keys, macros=keys.macros)
    service.start()
    device = service.manager.devices[0]
    yield service, keys, device, injector, mouse_hook
    service.stop()


# -- the stored format ------------------------------------------------------

def test_mouse_events_are_kept_and_cleaned():
    action = validate({'type': 'keys', 'moves': 'relative', 'events': [
        {'mouse': 'left', 'down': True},
        {'mouse': 'move', 'x': '10', 'y': -5, 'delay': 8, 'extra': 'dropped'},
        {'mouse': 'wheel', 'amount': -240, 'horizontal': 1},
        {'mouse': 'left', 'down': False},
        {'vk': 0x41, 'down': True},
    ]})
    assert action['moves'] == 'relative'
    assert action['events'][1] == {'mouse': 'move', 'x': 10, 'y': -5, 'delay': 8}
    assert action['events'][2] == {'mouse': 'wheel', 'amount': -240, 'horizontal': True, 'delay': 0}
    assert has_mouse(action)
    # Absolute is the default and is not written out.
    assert 'moves' not in validate({'type': 'keys', 'events': [{'mouse': 'right'}]})


@pytest.mark.parametrize('event, message', [
    ({'mouse': 'button6'}, 'unknown mouse event'),
    ({'mouse': 'move', 'x': 1}, 'malformed'),
    ({'mouse': 'move', 'x': 10 ** 7, 'y': 0}, 'cannot go'),
    ({'mouse': 'wheel', 'amount': 0}, 'wheel turn'),
    ({'mouse': 'left', 'delay': 60000}, 'delays'),
])
def test_bad_mouse_events_are_refused(event, message):
    with pytest.raises(MacroError, match=message):
        validate({'type': 'keys', 'events': [event]})


def test_a_long_mouse_path_fits():
    events = [{'mouse': 'move', 'x': i, 'y': i, 'delay': 10} for i in range(MAX_EVENTS)]
    assert len(validate({'type': 'keys', 'events': events})['events']) == MAX_EVENTS
    with pytest.raises(MacroError):
        validate({'type': 'keys', 'events': events + events[:1]})


def test_options_are_saved_beside_the_bindings_without_becoming_a_device(tmp_path):
    path = str(tmp_path / 'macros.json')
    store = MacroStore(path)
    assert store.options() == {'record_mouse': False}
    store.set('SERIAL', 'M1', {'type': 'text', 'text': 'hi'})
    store.set_options(record_mouse=True)
    again = MacroStore(path)
    assert again.options() == {'record_mouse': True}
    assert again.bindings('SERIAL') == {'M1': {'type': 'text', 'text': 'hi'}}
    assert again.bindings('__options__') == {}
    with pytest.raises(MacroError):
        store.set_options(colour='red')


def test_a_file_without_options_still_loads(tmp_path):
    path = tmp_path / 'macros.json'
    path.write_text(json.dumps({'SERIAL': {'M1': {'type': 'disabled'}}}))
    store = MacroStore(str(path))
    assert store.options() == {'record_mouse': False}
    assert store.get('SERIAL', 'M1') == {'type': 'disabled'}


# -- the hook and the injector ----------------------------------------------

def test_mouse_messages_are_decoded():
    assert decode_mouse(0x0200, 5, 6, 0, 0, 0, 10) == move(5, 6, 10)
    assert decode_mouse(0x0201, 1, 2, 0, 0, 0, 1).kind == 'left'
    assert decode_mouse(0x0205, 1, 2, 0, 0, 0, 1)[:4] == ('right', 1, 2, False)
    down = decode_mouse(0x020A, 0, 0, (0x10000 - 120) << 16, 0, 0, 1)
    assert (down.kind, down.amount, down.horizontal) == ('wheel', -120, False)
    assert decode_mouse(0x020E, 0, 0, 120 << 16, 0, 0, 1).horizontal
    assert decode_mouse(0x020B, 0, 0, 2 << 16, 0, 0, 1)[:4] == ('x2', 0, 0, True)
    assert decode_mouse(0x020C, 0, 0, 1 << 16, 0, 0, 1).kind == 'x1'
    injected = decode_mouse(0x0200, 0, 0, 0, 0x01, MARKER, 1)
    assert injected.injected and injected.ours
    assert decode_mouse(0x0100, 0, 0, 0, 0, 0, 1) is None


def test_desktop_pixels_become_the_virtual_desk_range():
    # Two 1920x1080 monitors, the second to the left of the primary one.
    desktop = (-1920, 0, 3840, 1080)
    assert absolute_position(-1920, 0, desktop) == (0, 0)
    assert absolute_position(1919, 1079, desktop) == (65535, 65535)
    assert absolute_position(0, 540, desktop) == (round(1920 * 65535 / 3839), round(540 * 65535 / 1079))
    # Off the desktop is held at its edge.
    assert absolute_position(5000, -10, desktop) == (65535, 0)


# -- recording --------------------------------------------------------------

def test_recording_takes_the_mouse_when_asked(rig):
    service, keys, device, _, mouse_hook = rig
    assert service.rpc_macro_record_start(device.serial, 'M1', mouse=True) == {
        'recording': 'M1', 'mouse': True}
    assert mouse_hook.running
    keys.handle_mouse(click('left', False, 0))            # letting go of the Record click
    keys.handle_key(key(A, True, t=100))
    keys.handle_mouse(move(100, 100, 110))
    for step in range(1, 20):                             # 1000 Hz: one kept per 10 ms
        keys.handle_mouse(move(100 + step, 100, 110 + step))
    keys.handle_mouse(move(500, 500, 140, injected=True))  # a macro playing: not recorded
    keys.handle_mouse(click('right', True, 200))
    keys.handle_mouse(click('right', False, 260))
    keys.handle_mouse(wheel(-120, 300))
    keys.handle_key(key(A, False, t=350))
    keys.handle_mouse(move(900, 50, 400))                 # off to the Stop button
    keys.handle_mouse(click('left', True, 450))
    keys.handle_mouse(click('left', False, 470))
    saved = service.rpc_macro_record_stop()
    assert not mouse_hook.running
    events = saved['events']
    assert events[0] == {'vk': A.vk, 'scan': A.scan, 'ext': False, 'down': True, 'delay': 0}
    moves = [e for e in events if e.get('mouse') == 'move']
    assert [(e['x'], e['delay']) for e in moves] == [(100, 10), (110, 10), (119, 9)]
    assert [e.get('mouse') for e in events[len(moves) + 1:]] == ['right', 'right', 'wheel', None]
    assert events[-1]['delay'] == 50
    assert keys.macros.get(device.serial, 'M1') == saved


def test_without_the_mouse_only_keys_are_recorded(rig):
    service, keys, device, _, mouse_hook = rig
    assert service.rpc_macro_record_start(device.serial, 'M2')['mouse'] is False
    assert not mouse_hook.running
    keys.handle_key(key(A, True, t=0))
    keys.handle_mouse(click('left', True, 5))
    keys.handle_key(key(A, False, t=10))
    saved = service.rpc_macro_record_stop()
    assert all('mouse' not in e for e in saved['events'])


def test_the_option_decides_when_not_told(rig):
    service, keys, device, _, mouse_hook = rig
    service.rpc_macro_options(record_mouse=True)
    assert service.rpc_macro_list(device.serial)['options'] == {'record_mouse': True}
    assert service.rpc_macro_record_start(device.serial, 'M3')['mouse'] is True
    assert service.rpc_keys_status()['recording']['mouse'] is True
    service.rpc_macro_record_stop()
    with pytest.raises(RpcError):
        service.rpc_macro_options(nonsense=1)


def test_a_last_click_is_kept_when_stopped_from_the_keyboard(rig):
    service, keys, device, _, _ = rig
    service.rpc_macro_record_start(device.serial, 'M4', mouse=True)
    keys.handle_mouse(move(10, 10, 0))
    keys.handle_mouse(click('left', True, 50))
    keys.handle_mouse(click('left', False, 90))
    saved = service.rpc_macro_record_stop(trim_click=False)
    assert [e['mouse'] for e in saved['events']] == ['move', 'left', 'left']


def test_only_the_stop_click_is_nothing(rig):
    service, keys, device, _, _ = rig
    service.rpc_macro_record_start(device.serial, 'M5', mouse=True)
    keys.handle_mouse(move(10, 10, 0))
    keys.handle_mouse(click('left', True, 50))
    keys.handle_mouse(click('left', False, 90))
    assert service.rpc_macro_record_stop() is None
    assert keys.macros.get(device.serial, 'M5') is None


# -- playback ---------------------------------------------------------------

def test_playing_puts_the_cursor_where_it_was(rig):
    _, keys, _, injector, _ = rig
    keys.play({'type': 'keys', 'events': [
        {'mouse': 'move', 'x': 10, 'y': 20, 'delay': 0},
        {'mouse': 'left', 'down': True, 'delay': 0},
        {'mouse': 'move', 'x': 15, 'y': 18, 'delay': 1},
        {'mouse': 'left', 'down': False, 'delay': 0},
        {'mouse': 'wheel', 'amount': 240, 'horizontal': False, 'delay': 0},
        {'vk': A.vk, 'scan': A.scan, 'ext': False, 'down': True, 'delay': 0},
        {'vk': A.vk, 'scan': A.scan, 'ext': False, 'down': False, 'delay': 0},
    ]})
    assert injector.sent == [
        ('move_to', 10, 20), ('button', 'left', True), ('move_to', 15, 18),
        ('button', 'left', False), ('wheel', 240, False),
        ('raw', A.vk, A.scan, False, True), ('raw', A.vk, A.scan, False, False)]


def test_relative_playback_follows_the_path_from_the_cursor(rig):
    _, keys, _, injector, _ = rig
    keys.play({'type': 'keys', 'moves': 'relative', 'events': [
        {'mouse': 'move', 'x': 100, 'y': 100, 'delay': 0},
        {'mouse': 'move', 'x': 110, 'y': 95, 'delay': 0},
        {'mouse': 'move', 'x': 90, 'y': 95, 'delay': 0},
    ]})
    assert injector.sent == [('move_by', 10, -5), ('move_by', -20, 0)]


def test_a_macro_cut_short_lets_go_of_the_mouse(rig):
    _, keys, _, injector, _ = rig

    class Broken(RecordingInjector):
        def mouse_wheel(self, amount, horizontal=False):
            raise OSError('gone')

    keys.injector = broken = Broken()
    with pytest.raises(OSError):
        keys.play({'type': 'keys', 'events': [
            {'mouse': 'x1', 'down': True, 'delay': 0},
            {'mouse': 'wheel', 'amount': 120, 'delay': 0}]})
    assert broken.sent[-1] == ('button', 'x1', False)


def test_listings_describe_mouse_macros():
    events = [{'mouse': 'move', 'x': 1, 'y': 1}, {'mouse': 'move', 'x': 2, 'y': 2},
              {'mouse': 'left', 'down': True}, {'mouse': 'left', 'down': False},
              {'mouse': 'wheel', 'amount': -120},
              {'vk': 0x41, 'scan': 0x1E, 'ext': False, 'down': True}]
    assert describe_action(events) == '[moves x2] mouse-left wheel-1 A'


# -- Razer Synapse ----------------------------------------------------------

SYNAPSE3_KEYS = b"""<?xml version="1.0" encoding="utf-8"?>
<Macro xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <Name>Ctrl Up</Name>
  <Guid>bb3cd146-955f-4701-b0ed-43639b031c0b</Guid>
  <MacroEvents>
    <MacroEvent><Type>1</Type><KeyEvent><Makecode>29</Makecode></KeyEvent></MacroEvent>
    <MacroEvent><Type>1</Type><Delay>50</Delay><KeyEvent><Makecode>72</Makecode><State>0</State></KeyEvent></MacroEvent>
    <MacroEvent><Type>1</Type><Delay>20</Delay><KeyEvent><Makecode>72</Makecode><State>1</State></KeyEvent></MacroEvent>
    <MacroEvent><Type>1</Type><Delay>5</Delay><KeyEvent><Makecode>29</Makecode><State>1</State></KeyEvent></MacroEvent>
    <MacroEvent><Type>9</Type><Delay>5</Delay></MacroEvent>
  </MacroEvents>
</Macro>
"""

SYNAPSE3_MOUSE = b"""<?xml version="1.0" encoding="utf-8"?>
<Macro>
  <Name>LR6</Name>
  <MacroEvents>
    <MacroEvent><Type>2</Type><MouseEvent><MouseButton>1</MouseButton><State>0</State></MouseEvent></MacroEvent>
    <MacroEvent>
      <Type>3</Type>
      <MouseMovement>
        <MouseMovementEvent><Type>3</Type><X>976</X><Y>717</Y><Delay>0</Delay></MouseMovementEvent>
        <MouseMovementEvent><Type>3</Type><X>968</X><Y>729</Y><Delay>29</Delay></MouseMovementEvent>
        <MouseMovementEvent><Type>3</Type><X>960</X><Y>741</Y><Delay>26</Delay></MouseMovementEvent>
      </MouseMovement>
    </MacroEvent>
    <MacroEvent><Type>2</Type><Delay>2</Delay><MouseEvent><MouseButton>1</MouseButton><State>1</State></MouseEvent></MacroEvent>
    <MacroEvent><Type>2</Type><MouseEvent><MouseButton>ScrollDown</MouseButton><State>0</State></MouseEvent></MacroEvent>
    <MacroEvent><Type>2</Type><MouseEvent><MouseButton>ScrollDown</MouseButton><State>1</State></MouseEvent></MacroEvent>
  </MacroEvents>
</Macro>
"""

SYNAPSE4_KEYS = b"""<?xml version="1.0" encoding="utf-8"?>
<Macro>
  <Name>shift up</Name>
  <MacroEvents>
    <MacroEvent><Type>1</Type><Id>1792666983228</Id>
      <KeyEvent><Makecode>16</Makecode><State>0</State></KeyEvent>
      <flag>0</flag><selected>false</selected><isPairing>false</isPairing><ScanCode>42</ScanCode>
    </MacroEvent>
    <MacroEvent><Type>1</Type><Id>1</Id><Delay>10</Delay>
      <KeyEvent><Makecode>38</Makecode><State>0</State></KeyEvent><selected>false</selected><isPairing>false</isPairing>
    </MacroEvent>
    <MacroEvent><Type>1</Type><Id>2</Id><Delay>10</Delay>
      <KeyEvent><Makecode>104</Makecode><State>0</State></KeyEvent><selected>false</selected><isPairing>false</isPairing>
    </MacroEvent>
    <MacroEvent><Type>1</Type><Id>3</Id>
      <KeyEvent><Makecode>16</Makecode><State>1</State></KeyEvent><selected>false</selected><isPairing>false</isPairing>
    </MacroEvent>
  </MacroEvents>
</Macro>
"""


def test_synapse3_scan_codes():
    [macro] = parse(SYNAPSE3_KEYS)
    assert (macro.name, macro.version) == ('Ctrl Up', 'synapse3')
    events = macro.action['events']
    ctrl, up = KEYS['KEY_LEFTCTRL'], KEYS['KEY_UP']
    assert [(e['vk'], e['scan'], e['ext'], e['down'], e['delay']) for e in events] == [
        (ctrl.vk, 0x1D, False, True, 0), (up.vk, 0x48, True, True, 50),
        (up.vk, 0x48, True, False, 20), (ctrl.vk, 0x1D, False, False, 5)]
    assert len(macro.warnings) == 1 and 'type 9' in macro.warnings[0]


def test_synapse3_numpad_and_no_delays():
    [macro] = parse(SYNAPSE3_KEYS, numpad=True, keep_delays=False, delay_ms=3)
    up = macro.action['events'][1]
    assert (up['vk'], up['scan'], up['ext']) == (0x68, 0x48, False)
    assert [e['delay'] for e in macro.action['events']] == [0, 3, 3, 3]


def test_synapse4_virtual_keys_are_typed_by_scan_code():
    [macro] = parse(SYNAPSE4_KEYS)
    assert macro.version == 'synapse4'
    events = macro.action['events']
    assert [(e['vk'], e['scan'], e['ext']) for e in events] == [
        (0x10, 42, False), (0x26, 0x48, True), (0x68, 0x48, False), (0x10, 0x2A, False)]
    assert macro.warnings == []


def test_synapse_mouse_clicks_path_and_wheel():
    [macro] = parse(SYNAPSE3_MOUSE)
    events = macro.action['events']
    assert events == [
        {'mouse': 'left', 'down': True, 'delay': 0},
        {'mouse': 'move', 'x': 976, 'y': 717, 'delay': 0},
        {'mouse': 'move', 'x': 968, 'y': 729, 'delay': 29},
        {'mouse': 'move', 'x': 960, 'y': 741, 'delay': 26},
        {'mouse': 'left', 'down': False, 'delay': 2},
        {'mouse': 'wheel', 'amount': -120, 'horizontal': False, 'delay': 0},
    ]
    assert 'moves' not in macro.action
    assert parse(SYNAPSE3_MOUSE, relative=True)[0].action['moves'] == 'relative'


def test_synapse_button_numbers_can_be_mapped():
    xml = SYNAPSE3_MOUSE.replace(b'<MouseButton>1</MouseButton>', b'<MouseButton>7</MouseButton>')
    [macro] = parse(xml)
    assert sum('button 7' in w for w in macro.warnings) == 2
    [macro] = parse(xml, button_map={7: 'x2'})
    assert macro.action['events'][0]['mouse'] == 'x2'


def test_a_synapse_file_with_several_macros(tmp_path):
    both = (b'<Macros>' + SYNAPSE3_KEYS.split(b'?>', 1)[1] + SYNAPSE4_KEYS.split(b'?>', 1)[1]
            + b'</Macros>')
    path = tmp_path / 'macros.xml'
    path.write_bytes(both)
    assert [(m.name, m.version) for m in parse(str(path))] == [
        ('Ctrl Up', 'synapse3'), ('shift up', 'synapse4')]


def test_a_synapse_file_saved_as_utf16(tmp_path):
    path = tmp_path / 'utf16.xml'
    path.write_bytes(SYNAPSE3_KEYS.decode().replace('utf-8', 'utf-16').encode('utf-16'))
    assert parse(str(path))[0].name == 'Ctrl Up'


@pytest.mark.parametrize('source, message', [
    (b'not xml', 'not an XML'),
    (b'<Profile><Name>x</Name></Profile>', 'no Synapse macro'),
    ('C:/does/not/exist.xml', 'cannot read'),
])
def test_what_is_not_a_synapse_macro(source, message):
    with pytest.raises(SynapseError, match=message):
        parse(source)


def test_importing_through_the_daemon(rig, tmp_path):
    service, keys, device, _, _ = rig
    path = tmp_path / 'lr6.xml'
    path.write_bytes(SYNAPSE3_MOUSE)
    looked = service.rpc_macro_import_synapse(path=str(path))
    assert [m['name'] for m in looked['macros']] == ['LR6'] and 'bound' not in looked
    bound = service.rpc_macro_import_synapse(xml=SYNAPSE3_KEYS.decode(), serial=device.serial,
                                             key='M2', relative=True)
    assert keys.macros.get(device.serial, 'M2') == bound['bound']
    assert bound['bound']['moves'] == 'relative'
    with pytest.raises(RpcError) as error:
        service.rpc_macro_import_synapse(xml='<x/>')
    assert error.value.code == INVALID_PARAMS
    with pytest.raises(RpcError):
        service.rpc_macro_import_synapse()
