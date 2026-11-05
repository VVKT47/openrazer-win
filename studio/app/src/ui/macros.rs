//! Macros: a library to build them in, and the keys to put them on.
//!
//! The daemon stores what each macro key does, per device. The library is
//! Studio's: macros are made and named once, then put on any key of any
//! device -- the binding is a copy, so changing the library later does not
//! change a key until the macro is put on it again.

use super::app::{scroll, short_name, App};
use crate::settings::LibraryMacro;
use crate::worker::{Command, Device, Snapshot};
use egui::{vec2, Align, CornerRadius, Frame, Layout, Margin, RichText, Sense, Stroke};
use openrazer_studio_core::theme::{self, icon, ACCENT, TEXT, TEXT_DIM, TEXT_FAINT};
use openrazer_studio_core::widgets::{self, Button, IconButton};
use openrazer_studio_core::{t, tf};
use serde_json::{json, Value};
use std::time::Instant;

/// Key names and Windows virtual-key codes, as offered in the editor.
#[rustfmt::skip]
pub const KEYS: &[(&str, u8)] = &[
    ("A", 0x41), ("B", 0x42), ("C", 0x43), ("D", 0x44), ("E", 0x45), ("F", 0x46), ("G", 0x47), ("H", 0x48), ("I", 0x49),
    ("J", 0x4A), ("K", 0x4B), ("L", 0x4C), ("M", 0x4D), ("N", 0x4E), ("O", 0x4F), ("P", 0x50), ("Q", 0x51), ("R", 0x52),
    ("S", 0x53), ("T", 0x54), ("U", 0x55), ("V", 0x56), ("W", 0x57), ("X", 0x58), ("Y", 0x59), ("Z", 0x5A),
    ("0", 0x30), ("1", 0x31), ("2", 0x32), ("3", 0x33), ("4", 0x34), ("5", 0x35), ("6", 0x36), ("7", 0x37), ("8", 0x38), ("9", 0x39),
    ("F1", 0x70), ("F2", 0x71), ("F3", 0x72), ("F4", 0x73), ("F5", 0x74), ("F6", 0x75), ("F7", 0x76), ("F8", 0x77),
    ("F9", 0x78), ("F10", 0x79), ("F11", 0x7A), ("F12", 0x7B), ("F13", 0x7C), ("F14", 0x7D), ("F15", 0x7E), ("F16", 0x7F),
    ("F17", 0x80), ("F18", 0x81), ("F19", 0x82), ("F20", 0x83), ("F21", 0x84), ("F22", 0x85), ("F23", 0x86), ("F24", 0x87),
    ("Ctrl", 0xA2), ("Shift", 0xA0), ("Alt", 0xA4), ("Win", 0x5B), ("Right Ctrl", 0xA3), ("Right Shift", 0xA1), ("Right Alt", 0xA5),
    ("Enter", 0x0D), ("Esc", 0x1B), ("Tab", 0x09), ("Space", 0x20), ("Backspace", 0x08), ("Caps Lock", 0x14),
    ("Insert", 0x2D), ("Delete", 0x2E), ("Home", 0x24), ("End", 0x23), ("Page Up", 0x21), ("Page Down", 0x22),
    ("←", 0x25), ("↑", 0x26), ("→", 0x27), ("↓", 0x28), ("Print Screen", 0x2C), ("Pause", 0x13),
    ("-", 0xBD), ("=", 0xBB), ("[", 0xDB), ("]", 0xDD), ("\\", 0xDC), (";", 0xBA), ("'", 0xDE), (",", 0xBC), (".", 0xBE), ("/", 0xBF), ("`", 0xC0),
    ("Mute", 0xAD), ("Volume down", 0xAE), ("Volume up", 0xAF), ("Next track", 0xB0), ("Previous track", 0xB1), ("Play/Pause", 0xB3), ("Stop", 0xB2),
];

/// Keys Windows wants flagged as extended when they are typed for you.
fn extended(vk: u8) -> bool {
    matches!(vk, 0x21..=0x28 | 0x2D | 0x2E | 0x5B | 0xA3 | 0xA5 | 0xAD..=0xB3)
}

pub fn key_name(vk: u8) -> String {
    match KEYS.iter().find(|(_, code)| *code == vk) {
        Some((name, _)) => t_key(name).to_string(),
        None => format!("key {vk:#04x}"),
    }
}

/// Key names that are words are translated; letters and symbols are not.
fn t_key(name: &'static str) -> &'static str {
    match name {
        // Keys keep the names printed on them; only the ones that are words get translated.
        "Mute" | "Volume down" | "Volume up" | "Next track" | "Previous track" | "Play/Pause" | "Right Ctrl" | "Right Shift" | "Right Alt" => t(name),
        other => other,
    }
}

/// One step of a key macro, as the editor shows it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Step {
    /// Press and let go.
    Tap(u8),
    Down(u8),
    Up(u8),
    /// Wait this many milliseconds.
    Wait(u32),
}

pub fn to_events(steps: &[Step]) -> Vec<Value> {
    let mut events = Vec::new();
    let mut delay = 0u32;
    let mut event = |vk: u8, down: bool, delay: &mut u32| {
        events.push(json!({ "vk": vk, "scan": 0, "ext": extended(vk), "down": down, "delay": (*delay).min(30_000) }));
        *delay = 0;
    };
    for step in steps {
        match *step {
            Step::Tap(vk) => {
                event(vk, true, &mut delay);
                event(vk, false, &mut delay);
            }
            Step::Down(vk) => event(vk, true, &mut delay),
            Step::Up(vk) => event(vk, false, &mut delay),
            Step::Wait(ms) => delay += ms,
        }
    }
    events
}

/// Whether an event is the mouse's rather than a key's.
fn is_mouse(event: &Value) -> bool {
    event.get("mouse").is_some()
}

/// Whether a binding does anything with the mouse: those are kept whole, as
/// recorded or imported, since the step editor only knows keys.
pub fn has_mouse(action: &Value) -> bool {
    action["events"].as_array().is_some_and(|events| events.iter().any(is_mouse))
}

/// What the mouse does in a macro, in a few words, if anything.
fn mouse_summary(events: &[Value]) -> Option<String> {
    let clicks = events.iter().filter(|e| is_mouse(e) && e["down"] == true).count();
    let moves = events.iter().any(|e| e["mouse"] == "move");
    let scrolls = events.iter().any(|e| e["mouse"] == "wheel");
    let mut parts: Vec<String> = Vec::new();
    match clicks {
        0 => {}
        1 => parts.push(t("1 click").to_string()),
        n => parts.push(tf("{} clicks", &[&n])),
    }
    if moves {
        parts.push(t("movement").to_string());
    }
    if scrolls {
        parts.push(t("scrolling").to_string());
    }
    (!parts.is_empty()).then(|| tf("Mouse: {}", &[&parts.join(", ")]))
}

/// How long a macro takes to play, in seconds.
fn duration(events: &[Value]) -> f64 {
    events.iter().map(|e| e["delay"].as_u64().unwrap_or(0)).sum::<u64>() as f64 / 1000.0
}

/// The key steps of a macro; mouse events are left out.
pub fn from_events(events: &[Value]) -> Vec<Step> {
    let events: Vec<&Value> = events.iter().filter(|e| !is_mouse(e)).collect();
    let mut steps = Vec::new();
    let mut i = 0;
    while i < events.len() {
        let e = events[i];
        let vk = e["vk"].as_u64().unwrap_or(0) as u8;
        let delay = e["delay"].as_u64().unwrap_or(0) as u32;
        if delay > 0 {
            steps.push(Step::Wait(delay));
        }
        let down = e["down"] != false;
        let next = events.get(i + 1);
        // A press let go at once is one tap.
        if down && next.is_some_and(|n| n["vk"].as_u64() == Some(vk as u64) && n["down"] == false && n["delay"].as_u64().unwrap_or(0) == 0) {
            steps.push(Step::Tap(vk));
            i += 2;
            continue;
        }
        steps.push(if down { Step::Down(vk) } else { Step::Up(vk) });
        i += 1;
    }
    steps
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Keys,
    Text,
    Launch,
    Disabled,
}

struct Draft {
    /// Index in the library; None for one not saved yet.
    index: Option<usize>,
    name: String,
    kind: Kind,
    steps: Vec<Step>,
    text: String,
    target: String,
    /// When recording started, and when the last key came.
    recording: Option<(Instant, Instant, egui::Modifiers)>,
    pick: u8,
    /// A macro that uses the mouse, kept exactly as recorded or imported.
    recorded: Option<Value>,
    /// ... replayed from wherever the cursor is, rather than at the same places.
    relative: bool,
}

impl Draft {
    fn new() -> Draft {
        Draft { index: None, name: String::new(), kind: Kind::Keys, steps: Vec::new(), text: String::new(), target: String::new(), recording: None, pick: 0x41, recorded: None, relative: false }
    }

    fn from(index: usize, item: &LibraryMacro) -> Draft {
        let mut draft = Draft::new();
        draft.index = Some(index);
        draft.name = item.name.clone();
        match item.action["type"].as_str() {
            Some("text") => {
                draft.kind = Kind::Text;
                draft.text = item.action["text"].as_str().unwrap_or_default().into();
            }
            Some("launch") => {
                draft.kind = Kind::Launch;
                draft.target = item.action["target"].as_str().unwrap_or_default().into();
            }
            Some("disabled") => draft.kind = Kind::Disabled,
            _ if has_mouse(&item.action) => {
                draft.recorded = Some(item.action.clone());
                draft.relative = item.action["moves"] == "relative";
            }
            _ => draft.steps = from_events(item.action["events"].as_array().map(Vec::as_slice).unwrap_or(&[])),
        }
        draft
    }

    fn action(&self) -> Result<Value, &'static str> {
        if let (Kind::Keys, Some(recorded)) = (self.kind, &self.recorded) {
            let mut action = recorded.clone();
            if let Some(map) = action.as_object_mut() {
                if self.relative {
                    map.insert("moves".into(), json!("relative"));
                } else {
                    map.remove("moves");
                }
            }
            return Ok(action);
        }
        Ok(match self.kind {
            Kind::Keys if self.steps.iter().all(|s| matches!(s, Step::Wait(_))) => return Err(t("Add at least one key.")),
            Kind::Keys => json!({ "type": "keys", "events": to_events(&self.steps) }),
            Kind::Text if self.text.is_empty() => return Err(t("Type the text it should type.")),
            Kind::Text => json!({ "type": "text", "text": self.text }),
            Kind::Launch if self.target.trim().is_empty() => return Err(t("Choose what it should open.")),
            Kind::Launch => json!({ "type": "launch", "target": self.target.trim() }),
            Kind::Disabled => json!({ "type": "disabled" }),
        })
    }
}

#[derive(Default)]
pub struct MacrosUi {
    draft: Option<Draft>,
    selected: Option<usize>,
    /// The last import from Synapse taken into the library.
    imported: u64,
}

pub fn page(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    take_imported(app, snap);
    widgets::page_header(ui, t("Macros"), t("Make a macro once, then put it on any macro key."), |ui| {
        if ui.add(Button::primary(t("New macro")).icon(icon::ADD)).clicked() {
            new_draft(app);
        }
        if ui.add(Button::secondary(t("Import from Synapse")).icon(icon::DOWNLOAD)).on_hover_text(t("Bring over a macro exported from Razer Synapse as .xml: keys, clicks, scrolling, mouse movement and pauses")).clicked() {
            if let Some(path) = rfd::FileDialog::new().set_title(t("Choose a macro exported from Razer Synapse")).add_filter(t("Synapse macro"), &["xml"]).pick_file() {
                app.send(Command::ImportSynapse { path: path.display().to_string() });
            }
        }
    });
    scroll(ui, "macros", |ui| {
        widgets::columns(ui, 0.32, 820.0, |ui, column| {
            if column == 0 {
                library(app, ui);
                return;
            }
            if app.macros.draft.is_some() {
                editor(app, ui);
            } else {
                widgets::panel(ui, t("Macro"), |ui| {
                    widgets::empty_state(ui, t("No macro open"), t("Choose one from the library, or make a new one."), |ui| {
                        if ui.add(Button::secondary(t("New macro")).icon(icon::ADD)).clicked() {
                            new_draft(app);
                        }
                    });
                });
            }
            assign(app, ui, snap);
        });
    });
}

/// Put the macros of a finished Synapse import into the library, once.
fn take_imported(app: &mut App, snap: &Snapshot) {
    let Some((number, found)) = &snap.imported else { return };
    if *number <= app.macros.imported {
        return;
    }
    app.macros.imported = *number;
    let mut added = 0usize;
    let mut skipped = 0usize;
    let mut warnings = 0usize;
    for item in found["macros"].as_array().map(Vec::as_slice).unwrap_or(&[]) {
        if item["action"]["events"].as_array().is_none_or(Vec::is_empty) {
            skipped += 1;
            continue;
        }
        let name = item["name"].as_str().filter(|n| !n.trim().is_empty()).unwrap_or("Synapse");
        warnings += item["warnings"].as_array().map_or(0, Vec::len);
        app.settings.macros.push(LibraryMacro { name: name.trim().to_string(), action: item["action"].clone() });
        added += 1;
    }
    if added == 0 {
        app.toast(t("Nothing in that file can be played here"), true);
        return;
    }
    app.save_settings();
    let first = app.settings.macros.len() - added;
    open(app, first);
    let text = match added {
        1 => tf("“{}” brought over from Synapse", &[&app.settings.macros[first].name]),
        n => tf("{} macros brought over from Synapse", &[&n]),
    };
    if warnings + skipped > 0 {
        app.toast(tf("{}; {} parts could not be brought over exactly", &[&text, &(warnings + skipped)]), false);
    } else {
        app.toast(text, false);
    }
}

/// Open the library's macro `index` in the editor.
pub fn open(app: &mut App, index: usize) {
    if let Some(item) = app.settings.macros.get(index) {
        app.macros.draft = Some(Draft::from(index, item));
        app.macros.selected = Some(index);
    }
}

fn new_draft(app: &mut App) {
    let mut draft = Draft::new();
    draft.name = tf("Macro {}", &[&(app.settings.macros.len() + 1)]);
    app.macros.draft = Some(draft);
    app.macros.selected = None;
}

fn library(app: &mut App, ui: &mut egui::Ui) {
    let count = app.settings.macros.len().to_string();
    widgets::panel_with(
        ui,
        t("Library"),
        |ui| {
            widgets::chip(ui, &count);
        },
        |ui| {
            ui.spacing_mut().item_spacing.y = 4.0;
            if app.settings.macros.is_empty() {
                widgets::help(ui, t("Nothing here yet. Make a macro once, then put it on any macro key."));
            }
            let mut open: Option<usize> = None;
            for (index, item) in app.settings.macros.iter().enumerate() {
                let selected = app.macros.selected == Some(index);
                let (response, _) = widgets::list_row(ui, selected, 48.0, |ui| {
                    let glyph = match item.action["type"].as_str() {
                        Some("text") => icon::TEXT,
                        Some("launch") => icon::OPEN,
                        Some("disabled") => icon::CLOSE,
                        _ if has_mouse(&item.action) => icon::MOUSE,
                        _ => icon::KEYBOARD,
                    };
                    ui.add_space(2.0);
                    widgets::icon(ui, glyph, 14.0, if selected { ACCENT } else { TEXT_DIM });
                    let room = ui.available_width() - 4.0;
                    let (rect, _) = ui.allocate_exact_size(vec2(room, 40.0), Sense::hover());
                    let name = widgets::galley_fit(ui, &item.name, theme::bold(13.5), TEXT, room);
                    let (text, _) = summary(Some(&item.action), "");
                    let line = widgets::galley_fit(ui, &text, theme::body(12.0), TEXT_FAINT, room);
                    ui.painter().galley(egui::pos2(rect.left(), rect.center().y - name.size().y + 1.0), name, TEXT);
                    ui.painter().galley(egui::pos2(rect.left(), rect.center().y + 1.0), line, TEXT_FAINT);
                });
                if response.clicked() {
                    open = Some(index);
                }
            }
            if let Some(index) = open {
                app.macros.selected = Some(index);
                app.macros.draft = Some(Draft::from(index, &app.settings.macros[index]));
            }
        },
    );
    widgets::panel(ui, t("Tips"), |ui| {
        widgets::help(ui, t("An unbound macro key sends F13–F17, so any game or app that takes hotkeys can use it as is."));
        widgets::help(ui, t("On the keyboard: Fn + F9 records a macro straight onto a key."));
        widgets::help(ui, t("Macros from Razer Synapse come over with their clicks, scrolling and mouse movement: export them from Synapse as .xml, then Import from Synapse."));
    });
}

/// Turn what was typed in the window into steps, while recording.
fn record(ui: &egui::Ui, draft: &mut Draft) {
    let Some((_, last, previous)) = draft.recording else { return };
    let mut last = last;
    let mut previous = previous;
    let events: Vec<egui::Event> = ui.input(|i| i.events.clone());
    let modifiers = ui.input(|i| i.modifiers);
    let push = |draft: &mut Draft, step: Step, last: &mut Instant| {
        let gap = last.elapsed().as_millis() as u32;
        if gap > 30 && !draft.steps.is_empty() {
            draft.steps.push(Step::Wait(gap.min(30_000)));
        }
        draft.steps.push(step);
        *last = Instant::now();
    };
    for (on, was, vk) in [(modifiers.ctrl, previous.ctrl, 0xA2u8), (modifiers.shift, previous.shift, 0xA0), (modifiers.alt, previous.alt, 0xA4)] {
        if on != was {
            push(draft, if on { Step::Down(vk) } else { Step::Up(vk) }, &mut last);
        }
    }
    previous = modifiers;
    for event in events {
        if let egui::Event::Key { key, pressed, repeat: false, .. } = event {
            if let Some(vk) = vk_of(key) {
                push(draft, if pressed { Step::Down(vk) } else { Step::Up(vk) }, &mut last);
            }
        }
    }
    // Tidy a press immediately let go into one tap.
    let mut tidy: Vec<Step> = Vec::new();
    for step in draft.steps.drain(..) {
        if let (Step::Up(up), Some(Step::Down(down))) = (step, tidy.last().copied()) {
            if up == down {
                tidy.pop();
                tidy.push(Step::Tap(up));
                continue;
            }
        }
        tidy.push(step);
    }
    draft.steps = tidy;
    if let Some(recording) = draft.recording.as_mut() {
        recording.1 = last;
        recording.2 = previous;
    }
}

fn vk_of(key: egui::Key) -> Option<u8> {
    use egui::Key::*;
    Some(match key {
        A => 0x41, B => 0x42, C => 0x43, D => 0x44, E => 0x45, F => 0x46, G => 0x47, H => 0x48, I => 0x49, J => 0x4A, K => 0x4B, L => 0x4C, M => 0x4D,
        N => 0x4E, O => 0x4F, P => 0x50, Q => 0x51, R => 0x52, S => 0x53, T => 0x54, U => 0x55, V => 0x56, W => 0x57, X => 0x58, Y => 0x59, Z => 0x5A,
        Num0 => 0x30, Num1 => 0x31, Num2 => 0x32, Num3 => 0x33, Num4 => 0x34, Num5 => 0x35, Num6 => 0x36, Num7 => 0x37, Num8 => 0x38, Num9 => 0x39,
        F1 => 0x70, F2 => 0x71, F3 => 0x72, F4 => 0x73, F5 => 0x74, F6 => 0x75, F7 => 0x76, F8 => 0x77, F9 => 0x78, F10 => 0x79, F11 => 0x7A, F12 => 0x7B,
        Enter => 0x0D, Escape => 0x1B, Tab => 0x09, Space => 0x20, Backspace => 0x08, Insert => 0x2D, Delete => 0x2E, Home => 0x24, End => 0x23,
        PageUp => 0x21, PageDown => 0x22, ArrowLeft => 0x25, ArrowUp => 0x26, ArrowRight => 0x27, ArrowDown => 0x28,
        Minus => 0xBD, Equals => 0xBB, OpenBracket => 0xDB, CloseBracket => 0xDD, Backslash => 0xDC, Semicolon => 0xBA, Quote => 0xDE,
        Comma => 0xBC, Period => 0xBE, Slash => 0xBF, Backtick => 0xC0,
        _ => return None,
    })
}

fn editor(app: &mut App, ui: &mut egui::Ui) {
    let mut close = false;
    let mut save: Option<(Option<usize>, LibraryMacro)> = None;
    let mut delete: Option<usize> = None;
    let mut problem: Option<&str> = None;
    let Some(draft) = app.macros.draft.as_mut() else { return };
    record(ui, draft);
    if draft.recording.is_some() {
        ui.ctx().request_repaint();
    }
    let saved = draft.index;
    widgets::panel_with(
        ui,
        if saved.is_some() { t("Macro") } else { t("New macro") },
        |ui| {
            if let Some(index) = saved {
                if ui.add(Button::danger(t("Delete")).icon(icon::DELETE).small()).clicked() {
                    delete = Some(index);
                }
            }
        },
        |ui| {
            widgets::row(ui, t("Name"), "", |ui| {
                widgets::text_field(ui, &mut draft.name, t("Macro"), ui.available_width().min(360.0));
            });
            widgets::divider(ui);
            widgets::stacked(ui, t("What it does"), |ui| {
                let options = vec![(Kind::Keys, t("Key presses").to_string()), (Kind::Text, t("Type text").to_string()), (Kind::Launch, t("Open something").to_string()), (Kind::Disabled, t("Do nothing").to_string())];
                if widgets::segmented(ui, &options, &mut draft.kind) {
                    draft.recording = None;
                }
            });
            match draft.kind {
                Kind::Keys => keys_editor(ui, draft),
                Kind::Text => {
                    widgets::stacked(ui, t("Text to type"), |ui| {
                        widgets::text_area(ui, &mut draft.text, t("Text to type"), 4);
                        widgets::help(ui, t("Typed exactly as written, whatever the keyboard layout."));
                    });
                }
                Kind::Launch => {
                    widgets::stacked(ui, t("What to open"), |ui| {
                        ui.horizontal(|ui| {
                            widgets::text_field(ui, &mut draft.target, t("A program, document, folder or https:// link"), ui.available_width() - 120.0);
                            if ui.add(Button::secondary(t("Browse")).icon(icon::FOLDER).width(112.0)).clicked() {
                                if let Some(path) = rfd::FileDialog::new().set_title(t("What should this macro open?")).pick_file() {
                                    draft.target = path.display().to_string();
                                }
                            }
                        });
                    });
                }
                Kind::Disabled => {
                    widgets::help(ui, t("The key will do nothing at all: handy for a key that gets in the way."));
                }
            }
            widgets::divider(ui);
            ui.horizontal(|ui| {
                ui.spacing_mut().item_spacing.x = 8.0;
                if ui.add(Button::primary(t("Save to library")).icon(icon::SAVE)).clicked() {
                    match draft.action() {
                        Ok(action) => {
                            let name = if draft.name.trim().is_empty() { t("Macro").to_string() } else { draft.name.trim().to_string() };
                            save = Some((draft.index, LibraryMacro { name, action }));
                        }
                        Err(why) => problem = Some(why),
                    }
                }
                if ui.add(Button::secondary(t("Close"))).clicked() {
                    close = true;
                }
            });
        },
    );
    if let Some(why) = problem {
        app.toast(why, true);
    }
    if let Some((index, item)) = save {
        let name = item.name.clone();
        match index {
            Some(i) if i < app.settings.macros.len() => app.settings.macros[i] = item,
            _ => {
                app.settings.macros.push(item);
                let index = app.settings.macros.len() - 1;
                app.macros.selected = Some(index);
                if let Some(draft) = app.macros.draft.as_mut() {
                    draft.index = Some(index);
                }
            }
        }
        app.save_settings();
        app.toast(tf("“{}” saved to the library", &[&name]), false);
    }
    if let Some(index) = delete {
        app.settings.macros.remove(index);
        app.save_settings();
        app.macros.draft = None;
        app.macros.selected = None;
    }
    if close {
        app.macros.draft = None;
        app.macros.selected = None;
    }
}

/// A macro that uses the mouse: what it does, and how its movement is replayed.
/// Its steps are kept as they came, since the step editor only knows keys.
fn recorded_editor(ui: &mut egui::Ui, draft: &mut Draft) {
    let Some(recorded) = draft.recorded.as_ref() else { return };
    let events = recorded["events"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    let (text, _) = summary(Some(recorded), "");
    let length = tf("{} events, {} s", &[&events.len(), &format!("{:.1}", duration(events))]);
    let moves = events.iter().any(|e| e["mouse"] == "move");
    let mut start_over = false;
    widgets::stacked(ui, t("Steps"), |ui| {
        Frame::new()
            .fill(theme::WELL)
            .stroke(Stroke::new(1.0_f32, theme::BORDER_HI))
            .corner_radius(CornerRadius::same(theme::RADIUS_SM))
            .inner_margin(Margin::same(10))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.horizontal(|ui| {
                    widgets::icon(ui, icon::MOUSE, 14.0, ACCENT);
                    ui.label(RichText::new(&text).font(theme::bold(13.0)).color(TEXT));
                });
                ui.label(widgets::faint(&length));
            });
        widgets::help(ui, t("Recorded with the mouse, or brought over from Razer Synapse: it plays as it was made. To change it, record it again, or start over with key steps."));
    });
    if moves {
        widgets::stacked(ui, t("Mouse movement"), |ui| {
            let options = vec![(false, t("At the same places").to_string()), (true, t("From where the cursor is").to_string())];
            widgets::segmented(ui, &options, &mut draft.relative);
            widgets::help(ui, if draft.relative {
                t("The cursor follows the same path, starting wherever it is. Games that hide the cursor see this kind of movement.")
            } else {
                t("The cursor goes to the same spots on the screen as when the macro was made.")
            });
        });
    }
    if ui.add(Button::ghost(t("Start over with key steps")).icon(icon::CLEAR).small()).clicked() {
        start_over = true;
    }
    if start_over {
        draft.recorded = None;
        draft.relative = false;
        draft.steps.clear();
    }
}

/// Steps of a key macro: recorded, or built one by one.
fn keys_editor(ui: &mut egui::Ui, draft: &mut Draft) {
    if draft.recorded.is_some() {
        recorded_editor(ui, draft);
        return;
    }
    let recording = draft.recording.is_some();
    widgets::row(ui, t("Record"), t("Type the macro in this window; pauses are kept."), |ui| {
        if recording {
            if ui.add(Button::danger(t("Stop recording")).icon(icon::STOP).min_width(160.0)).clicked() {
                draft.recording = None;
            }
            ui.label(RichText::new(t("Recording…")).color(theme::DANGER));
        } else if ui.add(Button::secondary(t("Record")).icon(icon::RECORD).min_width(160.0)).clicked() {
            draft.steps.clear();
            draft.recording = Some((Instant::now(), Instant::now(), ui.input(|i| i.modifiers)));
        }
    });
    // The steps, in order.
    let mut remove: Option<usize> = None;
    let mut swap: Option<(usize, usize)> = None;
    widgets::stacked(ui, t("Steps"), |ui| {
        Frame::new()
            .fill(theme::WELL)
            .stroke(Stroke::new(1.0_f32, if recording { theme::DANGER.gamma_multiply(0.7) } else { theme::BORDER_HI }))
            .corner_radius(CornerRadius::same(theme::RADIUS_SM))
            .inner_margin(Margin::same(10))
            .show(ui, |ui| {
                ui.set_width(ui.available_width());
                ui.set_min_height(36.0);
                if draft.steps.is_empty() {
                    ui.label(widgets::faint(t("No steps yet: record them, or add them below.")));
                }
                ui.horizontal_wrapped(|ui| {
                    ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
                    let count = draft.steps.len();
                    for (i, step) in draft.steps.iter_mut().enumerate() {
                        let (text, colour) = match *step {
                            Step::Tap(vk) => (key_name(vk), TEXT),
                            Step::Down(vk) => (format!("{} ↓", key_name(vk)), ACCENT),
                            Step::Up(vk) => (format!("{} ↑", key_name(vk)), TEXT_DIM),
                            Step::Wait(ms) => (format!("{ms} ms"), TEXT_FAINT),
                        };
                        let response = Frame::new()
                            .fill(theme::SURFACE_HI)
                            .stroke(Stroke::new(1.0_f32, theme::BORDER_HI))
                            .corner_radius(CornerRadius::same(4))
                            .inner_margin(Margin { left: 8, right: 2, top: 2, bottom: 2 })
                            .show(ui, |ui| {
                                ui.horizontal(|ui| {
                                    ui.spacing_mut().item_spacing.x = 2.0;
                                    if let Step::Wait(ms) = step {
                                        widgets::icon(ui, icon::TIMER, 11.0, TEXT_FAINT);
                                        let mut value = *ms as f64;
                                        if widgets::number_field(ui, ("wait", i), &mut value, 10.0..=30_000.0, 0, "ms", 84.0) {
                                            *ms = value.round() as u32;
                                        }
                                    } else {
                                        ui.label(RichText::new(&text).font(theme::bold(12.5)).color(colour));
                                    }
                                    ui.add_space(4.0);
                                    if ui.add(IconButton::new(icon::BACK, t("Earlier")).size(20.0).enabled(i > 0)).clicked() {
                                        swap = Some((i, i - 1));
                                    }
                                    if ui.add(IconButton::new(icon::FORWARD, t("Later")).size(20.0).enabled(i + 1 < count)).clicked() {
                                        swap = Some((i, i + 1));
                                    }
                                    if ui.add(IconButton::new(icon::CLOSE, t("Remove")).danger().size(20.0)).clicked() {
                                        remove = Some(i);
                                    }
                                });
                            })
                            .response;
                        response.on_hover_text(match step {
                            Step::Tap(_) => t("Press and let go"),
                            Step::Down(_) => t("Hold down"),
                            Step::Up(_) => t("Let go"),
                            Step::Wait(_) => t("Wait"),
                        });
                    }
                });
            });
    });
    if let Some(i) = remove {
        draft.steps.remove(i);
    }
    if let Some((a, b)) = swap {
        draft.steps.swap(a, b);
    }
    widgets::stacked(ui, t("Add a step"), |ui| {
        ui.horizontal_wrapped(|ui| {
            ui.spacing_mut().item_spacing = vec2(6.0, 6.0);
            let mut pick = draft.pick;
            widgets::select(ui, "pick-key", key_name(pick), 150.0, |ui| {
                for (name, vk) in KEYS {
                    if widgets::option(ui, pick == *vk, t_key(name)).clicked() {
                        pick = *vk;
                    }
                }
            });
            draft.pick = pick;
            if ui.add(Button::secondary(t("Press")).small()).on_hover_text(t("Press and let go")).clicked() {
                draft.steps.push(Step::Tap(draft.pick));
            }
            if ui.add(Button::secondary(t("Hold")).small()).on_hover_text(t("Hold down, for combinations such as Ctrl + C")).clicked() {
                draft.steps.push(Step::Down(draft.pick));
            }
            if ui.add(Button::secondary(t("Let go")).small()).clicked() {
                draft.steps.push(Step::Up(draft.pick));
            }
            if ui.add(Button::secondary(t("Pause")).icon(icon::TIMER).small()).clicked() {
                draft.steps.push(Step::Wait(100));
            }
            if ui.add(Button::ghost(t("Clear")).icon(icon::CLEAR).small().enabled(!draft.steps.is_empty())).clicked() {
                draft.steps.clear();
            }
        });
    });
    let held: Vec<u8> = {
        let mut down: Vec<u8> = Vec::new();
        for step in &draft.steps {
            match step {
                Step::Down(vk) => down.push(*vk),
                Step::Up(vk) => down.retain(|d| d != vk),
                _ => {}
            }
        }
        down
    };
    if !held.is_empty() {
        ui.horizontal(|ui| {
            widgets::icon(ui, icon::WARNING, 13.0, theme::WARN);
            ui.label(RichText::new(tf("Still held down at the end: {}", &[&held.iter().map(|vk| key_name(*vk)).collect::<Vec<_>>().join(", ")])).size(12.5).color(theme::WARN));
        });
    }
}

/// Every device with macro keys, and what each key does.
fn assign(app: &mut App, ui: &mut egui::Ui, snap: &Snapshot) {
    let with_keys: Vec<Device> = snap
        .devices
        .iter()
        .filter(|d| snap.details.get(&d.serial).and_then(|x| x.macros.as_ref()).is_some_and(|m| m["keys"].as_array().is_some_and(|k| !k.is_empty())))
        .cloned()
        .collect();
    if with_keys.is_empty() {
        widgets::panel(ui, t("Macro keys"), |ui| {
            widgets::help(ui, t("None of the devices here has macro keys the port can handle. Macros made in the library can be put on a keyboard's macro keys once one is plugged in."));
            widgets::help(ui, t("Mouse buttons cannot be reassigned yet."));
        });
        return;
    }
    for device in with_keys {
        device_keys(app, ui, &device, snap);
    }
}

/// The macro keys of one device: what each does, and changing it. Every row
/// has the same buttons in the same places, greyed where they do not apply.
pub fn device_keys(app: &mut App, ui: &mut egui::Ui, device: &Device, snap: &Snapshot) {
    let Some(info) = snap.details.get(&device.serial).and_then(|d| d.macros.clone()) else { return };
    let serial = device.serial.clone();
    let handled = info["handled"] == true;
    let keys: Vec<String> = info["keys"].as_array().map(|k| k.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let bindings = info["bindings"].clone();
    let recording = &snap.keys["recording"];
    let recording_here = recording["serial"] == serial.as_str();
    let record_mouse = info["options"]["record_mouse"] == true;
    // Sent once the panel is drawn: its header and body cannot both hold `app`.
    let mut set_mouse: Option<bool> = None;

    widgets::panel_with(
        ui,
        &tf("{} · macro keys", &[&super::app::title(device, snap)]),
        |ui| {
            if handled {
                widgets::badge(ui, t("Live"), ACCENT);
            } else if snap.keys["running"] == true {
                widgets::badge(ui, t("Idle"), theme::WARN).on_hover_text(t("The daemon could not read this keyboard's key report, so its macro keys do nothing. Bindings can still be set up."));
            } else {
                widgets::badge(ui, t("Off"), theme::WARN).on_hover_text(t("The daemon runs without key handling (--no-keys). Restart it normally to use macro keys."));
            }
            // An older daemon has no such option, and could not record the mouse.
            if info.get("options").is_some() {
                ui.add_space(8.0);
                let mut on = record_mouse;
                ui.add_enabled_ui(!recording_here, |ui| {
                    if widgets::toggle(ui, &mut on).on_hover_text(t("Record clicks, scrolling and mouse movement along with the keys — with the record button here, and with Fn + F9 on the keyboard")).changed() {
                        set_mouse = Some(on);
                    }
                });
                widgets::icon(ui, icon::MOUSE, 13.0, if record_mouse { ACCENT } else { TEXT_FAINT });
                ui.label(widgets::faint(t("Record the mouse too")));
            }
        },
        |ui| {
            ui.spacing_mut().item_spacing.y = 6.0;
            for key in &keys {
                let action = bindings.get(key);
                let (text, bound) = summary(action, key);
                let live = recording_here && recording["key"] == key.as_str();
                Frame::new().fill(theme::SURFACE_HI).corner_radius(CornerRadius::same(theme::RADIUS_SM)).inner_margin(Margin::symmetric(10, 6)).show(ui, |ui| {
                    ui.set_width(ui.available_width());
                    ui.horizontal(|ui| {
                        ui.spacing_mut().item_spacing.x = 10.0;
                        widgets::keycap(ui, key, 56.0, bound);
                        let tools = 200.0 + 4.0 * 34.0;
                        let room = (ui.available_width() - tools).max(60.0);
                        let (rect, response) = ui.allocate_exact_size(vec2(room, 32.0), Sense::hover());
                        let (line, colour) = if live {
                            (tf("Recording… {} events so far", &[&recording["events"].as_u64().unwrap_or(0)]), theme::DANGER)
                        } else {
                            (text.clone(), if bound { TEXT } else { TEXT_DIM })
                        };
                        let galley = widgets::galley_fit(ui, &line, theme::body(13.5), colour, room);
                        ui.painter().galley(egui::pos2(rect.left(), rect.center().y - galley.size().y / 2.0), galley, colour);
                        response.on_hover_text(&line);
                        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                            ui.spacing_mut().item_spacing.x = 6.0;
                            if ui.add(IconButton::new(icon::DELETE, t("Back to the default")).danger().size(30.0).enabled(bound && !live)).clicked() {
                                app.send(Command::MacroDelete { serial: serial.clone(), key: key.clone() });
                            }
                            if ui.add(IconButton::new(icon::SAVE, t("Copy into the library")).size(30.0).enabled(bound && !live)).clicked() {
                                if let Some(action) = action {
                                    app.settings.macros.push(LibraryMacro { name: tf("{} of {}", &[key, &short_name(&device.name)]), action: action.clone() });
                                    app.save_settings();
                                    app.toast(t("Copied into the library"), false);
                                }
                            }
                            if ui.add(IconButton::new(icon::PLAY, t("Run it now")).size(30.0).enabled(bound && !live)).clicked() {
                                app.send(Command::MacroPlay { serial: serial.clone(), key: key.clone() });
                            }
                            if live {
                                if ui.add(IconButton::new(icon::STOP, t("Stop recording")).danger().on(true).size(30.0)).clicked() {
                                    app.send(Command::MacroRecordStop { serial: serial.clone() });
                                }
                            } else {
                                let tip = if record_mouse { t("Record: type and use the mouse anywhere, then stop") } else { t("Record on the keyboard: type the macro anywhere, then stop") };
                                if ui.add(IconButton::new(icon::RECORD, tip).size(30.0).enabled(handled && !recording_here)).clicked() {
                                    app.send(Command::MacroRecordStart { serial: serial.clone(), key: key.clone(), mouse: record_mouse });
                                }
                            }
                            let mut chosen: Option<usize> = None;
                            ui.add_enabled_ui(!live, |ui| {
                                widgets::select(ui, ("assign", &serial, key), RichText::new(t("Put a macro on it")).color(TEXT_DIM), 190.0, |ui| {
                                    if app.settings.macros.is_empty() {
                                        ui.label(widgets::faint(t("The library is empty")));
                                    }
                                    for (i, item) in app.settings.macros.iter().enumerate() {
                                        if widgets::option(ui, false, &item.name).clicked() {
                                            chosen = Some(i);
                                        }
                                    }
                                });
                            });
                            if let Some(i) = chosen {
                                let action = app.settings.macros[i].action.clone();
                                app.send(Command::MacroSet { serial: serial.clone(), key: key.clone(), action });
                            }
                        });
                    });
                });
            }
        },
    );
    if let Some(on) = set_mouse {
        app.send(Command::MacroRecordMouse { serial, on });
    }
}

/// One line saying what a binding does.
pub fn summary(action: Option<&Value>, key: &str) -> (String, bool) {
    let Some(action) = action else {
        let default = match key {
            "M1" => "F13",
            "M2" => "F14",
            "M3" => "F15",
            "M4" => "F16",
            "M5" => "F17",
            "M6" | "SIDE1" => "F18",
            "SIDE2" => "F19",
            "SIDE3" => "F20",
            "DIAL" => "F24",
            _ => "—",
        };
        return (tf("Sends {}", &[&default]), false);
    };
    let text = match action["type"].as_str().unwrap_or_default() {
        "keys" => {
            let events = action["events"].as_array().map(Vec::as_slice).unwrap_or(&[]);
            let steps = from_events(events);
            let mut chords: Vec<String> = Vec::new();
            let mut held: Vec<String> = Vec::new();
            for step in steps {
                match step {
                    Step::Tap(vk) => {
                        let mut chord = held.clone();
                        chord.push(key_name(vk));
                        chords.push(chord.join("+"));
                    }
                    Step::Down(vk) => held.push(key_name(vk)),
                    Step::Up(vk) => {
                        let name = key_name(vk);
                        if held.last() == Some(&name) && chords.last().is_none_or(|c| !c.starts_with(&held.join("+"))) {
                            chords.push(held.join("+"));
                        }
                        held.retain(|h| *h != name);
                    }
                    Step::Wait(_) => {}
                }
            }
            let mut shown = chords.iter().take(6).cloned().collect::<Vec<_>>().join(", ");
            if chords.len() > 6 {
                shown.push_str(" …");
            }
            match (chords.is_empty(), mouse_summary(events)) {
                (true, Some(mouse)) => mouse,
                (false, Some(mouse)) => format!("{} · {}", tf("Presses {}", &[&shown]), mouse),
                _ => tf("Presses {}", &[&shown]),
            }
        }
        "text" => tf("Types “{}”", &[&truncate(action["text"].as_str().unwrap_or_default(), 48)]),
        "launch" => tf("Opens {}", &[&truncate(action["target"].as_str().unwrap_or_default(), 56)]),
        "disabled" => t("Does nothing").to_string(),
        _ => t("Other").to_string(),
    };
    (text, true)
}

fn truncate(text: &str, limit: usize) -> String {
    let flat = text.replace('\n', " ⏎ ");
    if flat.chars().count() <= limit {
        flat
    } else {
        format!("{}…", flat.chars().take(limit).collect::<String>())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn steps_become_events_and_back() {
        let steps = vec![Step::Down(0xA2), Step::Tap(0x43), Step::Up(0xA2), Step::Wait(250), Step::Tap(0x25)];
        let events = to_events(&steps);
        assert_eq!(events.len(), 6);
        assert_eq!(events[4]["delay"], 250);
        assert_eq!(events[4]["ext"], true, "arrows are extended keys");
        assert_eq!(from_events(&events), steps);
    }

    #[test]
    fn a_chord_reads_as_one() {
        set_english();
        let action = json!({"type": "keys", "events": to_events(&[Step::Down(0xA2), Step::Tap(0x43), Step::Up(0xA2), Step::Tap(0x70)])});
        assert_eq!(summary(Some(&action), "M1").0, "Presses Ctrl+C, F1");
    }

    #[test]
    fn mouse_events_are_left_out_of_the_steps() {
        let events = vec![
            json!({"mouse": "left", "down": true, "delay": 0}),
            json!({"vk": 0x41, "scan": 0, "ext": false, "down": true, "delay": 5}),
            json!({"mouse": "move", "x": 1, "y": 2, "delay": 0}),
            json!({"vk": 0x41, "scan": 0, "ext": false, "down": false, "delay": 0}),
            json!({"mouse": "left", "down": false, "delay": 0}),
        ];
        assert_eq!(from_events(&events), vec![Step::Wait(5), Step::Tap(0x41)], "a press let go at once, with the mouse between, is still a tap");
        assert!(has_mouse(&json!({"type": "keys", "events": events})));
        assert!(!has_mouse(&json!({"type": "keys", "events": to_events(&[Step::Tap(0x41)])})));
    }

    #[test]
    fn a_mouse_macro_says_what_the_mouse_does() {
        set_english();
        let clicks = json!({"type": "keys", "events": [
            {"mouse": "left", "down": true, "delay": 0}, {"mouse": "left", "down": false, "delay": 0},
            {"mouse": "right", "down": true, "delay": 0}, {"mouse": "right", "down": false, "delay": 0},
            {"mouse": "move", "x": 1, "y": 2, "delay": 0}, {"mouse": "wheel", "amount": -120, "delay": 0},
        ]});
        assert_eq!(summary(Some(&clicks), "M1").0, "Mouse: 2 clicks, movement, scrolling");
        let mut both = to_events(&[Step::Down(0xA2), Step::Tap(0x43), Step::Up(0xA2)]);
        both.push(json!({"mouse": "left", "down": true, "delay": 0}));
        let action = json!({"type": "keys", "events": both});
        assert_eq!(summary(Some(&action), "M1").0, "Presses Ctrl+C · Mouse: 1 click");
    }

    #[test]
    fn a_mouse_macro_is_kept_whole_when_saved_again() {
        let action = json!({"type": "keys", "events": [
            {"mouse": "move", "x": 5, "y": 6, "delay": 0}, {"vk": 0x41, "scan": 30, "ext": false, "down": true, "delay": 9},
        ]});
        let mut draft = Draft::from(0, &LibraryMacro { name: "m".into(), action: action.clone() });
        assert!(draft.recorded.is_some() && draft.steps.is_empty());
        assert_eq!(draft.action().unwrap(), action);
        draft.relative = true;
        assert_eq!(draft.action().unwrap()["moves"], "relative");
        assert_eq!(draft.action().unwrap()["events"], action["events"]);
        let relative = Draft::from(0, &LibraryMacro { name: "m".into(), action: draft.action().unwrap() });
        assert!(relative.relative);
    }

    #[test]
    fn an_unbound_key_says_what_it_sends() {
        set_english();
        assert_eq!(summary(None, "M1"), ("Sends F13".to_string(), false));
    }

    fn set_english() {
        openrazer_studio_core::i18n::set(openrazer_studio_core::i18n::Lang::En);
    }
}
