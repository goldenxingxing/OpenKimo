"""Native macOS Settings window (PyObjC) — single-page scrolling layout.

Layout: a vertical NSScrollView holds three sections (LLM / Web Server /
Paths) separated by horizontal dividers. A sticky button bar at the bottom
of the window provides Cancel / Save / Save & Restart Server.

All layout uses plain ``NSView`` + explicit Auto Layout edge anchors. We
intentionally avoid ``NSStackView``, ``NSGridView`` and ``NSBox`` because
their stretching semantics fight the form's edge-anchor pins (see commit
history for details).

Save writes to ``~/Library/Application Support/<AppName>/.env`` via
``dotenv_io.write_env`` (atomic, preserves comments and unknown lines).
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Callable

from AppKit import (
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSFontWeightSemibold,
    NSImage,
    NSLayoutConstraint,
    NSMakeRect,
    NSModalResponseOK,
    NSOpenPanel,
    NSPopUpButton,
    NSScrollView,
    NSSecureTextField,
    NSSlider,
    NSSwitch,
    NSTextField,
    NSTitledWindowMask,
    NSClosableWindowMask,
    NSMiniaturizableWindowMask,
    NSResizableWindowMask,
    NSView,
    NSWindow,
    NSWindowController,
)
import objc
from Foundation import NSObject  # noqa: F401  (kept for symmetry / future use)

from . import dotenv_io
from .paths import AppPaths

log = logging.getLogger(__name__)

# Provider types accepted by the runtime. Keep in sync with
# ``kimi_cli.config.LLMProvider``.
_PROVIDER_TYPES: tuple[str, ...] = ("kimi", "openai_legacy", "anthropic")

# Default max context used when the user leaves the field blank (matches the
# placeholder shown in the Settings UI and kimi-cli's documented default).
_DEFAULT_MAX_CONTEXT: int = 1000000

# Capability flags exposed under each row's "Advanced" disclosure.
_CAPABILITIES: tuple[str, ...] = (
    "image_in",
    "video_in",
    "thinking",
    "always_thinking",
)

# Legacy single-provider env keys we clear once a fresh LLM_PROVIDERS blob is
# written, so the runtime parser unambiguously picks up the new format.
_LEGACY_LLM_KEYS: tuple[str, ...] = (
    "LLM_PROVIDER",
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_MAX_CONTEXT_SIZE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)

# ---- module-level layout constants (per spec §3) -----------------------

OUTER_HMARGIN: float = 24.0
OUTER_VMARGIN: float = 20.0
SECTION_VGAP: float = 24.0
ROW_VGAP: float = 8.0
LABEL_INPUT_GAP: float = 12.0
LABEL_COLUMN_WIDTH: float = 160.0
BUTTONBAR_HEIGHT: float = 56.0
BUTTONBAR_HMARGIN: float = 20.0


# ---- low-level helpers --------------------------------------------------

def _v() -> NSView:
    """Construct an NSView with auto-resizing translation disabled."""
    view = NSView.alloc().init()
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return view


def _label(text: str, *, semibold: bool = False, size: float = 13.0,
           secondary: bool = False) -> NSTextField:
    f = NSTextField.alloc().init()
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    if semibold:
        try:
            f.setFont_(NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold))
        except Exception:
            f.setFont_(NSFont.boldSystemFontOfSize_(size))
    else:
        f.setFont_(NSFont.systemFontOfSize_(size))
    if secondary:
        try:
            f.setTextColor_(NSColor.secondaryLabelColor())
        except Exception:
            pass
    f.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return f


def _text(value: str = "", *, secure: bool = False, placeholder: str = "") -> NSTextField:
    cls = NSSecureTextField if secure else NSTextField
    f = cls.alloc().init()
    f.setStringValue_(value)
    if placeholder:
        f.setPlaceholderString_(placeholder)
    f.setBezeled_(True)
    f.setEditable_(True)
    f.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return f


def _switch(on: bool) -> NSSwitch:
    s = NSSwitch.alloc().init()
    s.setState_(1 if on else 0)
    s.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return s


def _button(title: str, target, action: str) -> NSButton:
    b = NSButton.alloc().init()
    b.setTitle_(title)
    b.setBezelStyle_(1)  # rounded
    b.setTarget_(target)
    b.setAction_(action)
    b.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return b


def _flat_button(title: str, target, action: str) -> NSButton:
    """A leading-aligned, frameless-ish button used for + Add Provider."""
    b = NSButton.alloc().init()
    b.setTitle_(title)
    b.setBezelStyle_(11)  # NSBezelStyleRecessed (subtle)
    b.setTarget_(target)
    b.setAction_(action)
    b.setTranslatesAutoresizingMaskIntoConstraints_(False)
    return b


def _divider() -> NSView:
    v = _v()
    v.setWantsLayer_(True)
    try:
        v.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    except Exception:
        v.layer().setBackgroundColor_(NSColor.grayColor().CGColor())
    return v


def _form_row(
    label: NSTextField,
    control: NSView,
    *,
    fixed_width: float | None = None,
) -> NSView:
    """Build one label-on-left, control-on-right form row.

    If ``fixed_width`` is not None, the control is pinned to that width and a
    trailing spacer absorbs the remainder; otherwise the control stretches to
    the row's trailing edge.
    """
    row = _v()
    row.addSubview_(label)
    row.addSubview_(control)

    label.setContentHuggingPriority_forOrientation_(750, 0)
    control.setContentHuggingPriority_forOrientation_(250, 0)

    cs: list[Any] = [
        label.leadingAnchor().constraintEqualToAnchor_(row.leadingAnchor()),
        label.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
        label.widthAnchor().constraintEqualToConstant_(LABEL_COLUMN_WIDTH),
        control.leadingAnchor().constraintEqualToAnchor_constant_(
            label.trailingAnchor(), LABEL_INPUT_GAP),
        control.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
        control.topAnchor().constraintGreaterThanOrEqualToAnchor_(row.topAnchor()),
        control.bottomAnchor().constraintLessThanOrEqualToAnchor_(row.bottomAnchor()),
    ]

    if fixed_width is not None:
        spacer = _v()
        row.addSubview_(spacer)
        cs.extend([
            control.widthAnchor().constraintEqualToConstant_(fixed_width),
            spacer.leadingAnchor().constraintEqualToAnchor_constant_(
                control.trailingAnchor(), 8.0),
            spacer.trailingAnchor().constraintEqualToAnchor_(row.trailingAnchor()),
            spacer.centerYAnchor().constraintEqualToAnchor_(row.centerYAnchor()),
            spacer.heightAnchor().constraintEqualToConstant_(1.0),
        ])
    else:
        cs.append(
            control.trailingAnchor().constraintEqualToAnchor_(row.trailingAnchor())
        )

    # The row's intrinsic vertical span is dictated by the control's height
    # (most controls have a sensible intrinsicContentSize). Pin the row's
    # height >= the control's height so it doesn't collapse, and make the row
    # tall enough to vertically center the label too.
    cs.extend([
        row.heightAnchor().constraintGreaterThanOrEqualToAnchor_(control.heightAnchor()),
        row.heightAnchor().constraintGreaterThanOrEqualToAnchor_(label.heightAnchor()),
    ])

    NSLayoutConstraint.activateConstraints_(cs)
    return row


def _compound_token_field(
    token_field: NSTextField,
    generate_btn: NSButton,
) -> NSView:
    """[ token field …………… ] [ Generate ] compound."""
    compound = _v()
    compound.addSubview_(token_field)
    compound.addSubview_(generate_btn)

    generate_btn.setContentHuggingPriority_forOrientation_(752, 0)
    token_field.setContentHuggingPriority_forOrientation_(250, 0)

    NSLayoutConstraint.activateConstraints_([
        token_field.leadingAnchor().constraintEqualToAnchor_(compound.leadingAnchor()),
        token_field.centerYAnchor().constraintEqualToAnchor_(compound.centerYAnchor()),
        token_field.trailingAnchor().constraintEqualToAnchor_constant_(
            generate_btn.leadingAnchor(), -8.0),
        generate_btn.trailingAnchor().constraintEqualToAnchor_(compound.trailingAnchor()),
        generate_btn.centerYAnchor().constraintEqualToAnchor_(compound.centerYAnchor()),
        compound.heightAnchor().constraintGreaterThanOrEqualToAnchor_(
            token_field.heightAnchor()),
        compound.heightAnchor().constraintGreaterThanOrEqualToAnchor_(
            generate_btn.heightAnchor()),
    ])
    return compound


def _compound_path_field(
    path_field: NSTextField,
    choose_btn: NSButton,
    reset_btn: NSButton,
) -> NSView:
    """[ path field ………… ] [ Choose… ] [ Reset ] compound."""
    compound = _v()
    compound.addSubview_(path_field)
    compound.addSubview_(choose_btn)
    compound.addSubview_(reset_btn)

    choose_btn.setContentHuggingPriority_forOrientation_(752, 0)
    reset_btn.setContentHuggingPriority_forOrientation_(752, 0)
    path_field.setContentHuggingPriority_forOrientation_(250, 0)

    NSLayoutConstraint.activateConstraints_([
        path_field.leadingAnchor().constraintEqualToAnchor_(compound.leadingAnchor()),
        path_field.centerYAnchor().constraintEqualToAnchor_(compound.centerYAnchor()),
        path_field.trailingAnchor().constraintEqualToAnchor_constant_(
            choose_btn.leadingAnchor(), -6.0),
        choose_btn.centerYAnchor().constraintEqualToAnchor_(compound.centerYAnchor()),
        choose_btn.trailingAnchor().constraintEqualToAnchor_constant_(
            reset_btn.leadingAnchor(), -6.0),
        reset_btn.centerYAnchor().constraintEqualToAnchor_(compound.centerYAnchor()),
        reset_btn.trailingAnchor().constraintEqualToAnchor_(compound.trailingAnchor()),
        compound.heightAnchor().constraintGreaterThanOrEqualToAnchor_(
            path_field.heightAnchor()),
        compound.heightAnchor().constraintGreaterThanOrEqualToAnchor_(
            choose_btn.heightAnchor()),
    ])
    return compound


def _section_view(header_text: str) -> tuple[NSView, NSTextField]:
    """Construct a section container with a semibold 17pt header label.

    Returns ``(section_view, header_label)`` so callers can chain rows under
    the header.
    """
    section = _v()
    header = _label(header_text, semibold=True, size=17.0)
    section.addSubview_(header)
    NSLayoutConstraint.activateConstraints_([
        header.topAnchor().constraintEqualToAnchor_(section.topAnchor()),
        header.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        header.trailingAnchor().constraintLessThanOrEqualToAnchor_(
            section.trailingAnchor()),
    ])
    return section, header


# ---- data classes -------------------------------------------------------

class _Row:
    """Holds a label + control pair so we can read it back on Save."""

    __slots__ = ("key", "label", "control", "extra")

    def __init__(self, key: str, label: NSTextField, control, extra=None):
        self.key = key
        self.label = label
        self.control = control
        self.extra = extra


class _ProviderRow:
    """One dynamically-managed entry in the LLM provider list."""

    __slots__ = (
        "view",
        "title_label",
        "name_field",
        "type_popup",
        "api_key",
        "base_url",
        "model",
        "max_context",
        "default_radio",
        "advanced_btn",
        "advanced_view",
        "advanced_visible",
        "advanced_height_zero",
        "caps",
    )

    def __init__(
        self,
        *,
        view,
        title_label,
        name_field,
        type_popup,
        api_key,
        base_url,
        model,
        max_context,
        default_radio,
        advanced_btn,
        advanced_view,
        advanced_height_zero,
        caps,
    ):
        self.view = view
        self.title_label = title_label
        self.name_field = name_field
        self.type_popup = type_popup
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.max_context = max_context
        self.default_radio = default_radio
        self.advanced_btn = advanced_btn
        self.advanced_view = advanced_view
        self.advanced_visible = False
        self.advanced_height_zero = advanced_height_zero
        self.caps = caps  # dict[str, NSButton]

    def get_name(self) -> str:
        return str(self.name_field.stringValue()).strip()

    def has_api_key(self) -> bool:
        return bool(str(self.api_key.stringValue()).strip())

    def is_default(self) -> bool:
        return bool(self.default_radio.state())

    def selected_type(self) -> str:
        return str(self.type_popup.titleOfSelectedItem() or _PROVIDER_TYPES[0])

    def to_dict(self) -> dict[str, Any]:
        raw_ctx = str(self.max_context.stringValue()).strip()
        try:
            ctx = int(raw_ctx) if raw_ctx else _DEFAULT_MAX_CONTEXT
        except ValueError:
            ctx = _DEFAULT_MAX_CONTEXT
        if ctx <= 0:
            ctx = _DEFAULT_MAX_CONTEXT
        out: dict[str, Any] = {
            "name": self.get_name(),
            "type": self.selected_type(),
            "api_key": str(self.api_key.stringValue()).strip(),
            "base_url": str(self.base_url.stringValue()).strip(),
            "model": str(self.model.stringValue()).strip(),
            "max_context_size": ctx,
        }
        caps = [k for k in _CAPABILITIES if self.caps[k].state()]
        if caps:
            out["capabilities"] = caps
        return out


# ---- parsers (carried over verbatim) -----------------------------------

def _parse_existing_providers(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM_PROVIDERS env value into a list of entry dicts."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in parsed:
        if isinstance(entry, dict) and entry.get("name"):
            out.append(entry)
    return out


def _legacy_entries(values: dict[str, str]) -> list[dict[str, Any]]:
    """Synthesize provider entries from legacy single-provider env keys."""
    out: list[dict[str, Any]] = []
    if values.get("KIMI_API_KEY"):
        out.append({
            "name": "kimi",
            "type": "kimi",
            "api_key": values.get("KIMI_API_KEY", ""),
            "base_url": values.get("KIMI_BASE_URL", "") or "https://api.moonshot.cn/v1",
            "model": values.get("KIMI_MODEL_NAME", "") or "kimi-k2",
            "max_context_size": int(values.get("KIMI_MODEL_MAX_CONTEXT_SIZE") or 1000000),
        })
    if values.get("OPENAI_API_KEY"):
        out.append({
            "name": "openai",
            "type": "openai_legacy",
            "api_key": values.get("OPENAI_API_KEY", ""),
            "base_url": values.get("OPENAI_BASE_URL", "") or "https://api.openai.com/v1",
            "model": "gpt-4o",
            "max_context_size": 128000,
        })
    if values.get("ANTHROPIC_API_KEY"):
        out.append({
            "name": "anthropic",
            "type": "anthropic",
            "api_key": values.get("ANTHROPIC_API_KEY", ""),
            "base_url": values.get("ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com",
            "model": "claude-3-5-sonnet-20241022",
            "max_context_size": 200000,
        })
    return out


# ---- controller ---------------------------------------------------------

class SettingsController(NSWindowController):
    """Backing window controller; built lazily via :func:`build_controller`."""

    paths_ref = None  # type: AppPaths | None
    on_save = None    # type: Callable[[bool], None] | None
    rows = None       # type: dict[str, list[_Row]] | None

    # LLM section state
    provider_rows = None              # type: list[_ProviderRow] | None
    providers_container = None        # type: NSView | None
    providers_chain_constraints = None  # type: list | None
    providers_bottom_pin = None       # type: Any | None
    add_provider_button = None        # type: NSButton | None

    # ---- public API ----------------------------------------------------

    @objc.python_method
    def show(self):
        self.showWindow_(None)
        self.window().makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ---- bottom buttons -----------------------------------------------

    def onCancel_(self, _sender):  # noqa: N802
        self.window().performClose_(None)

    def onSave_(self, _sender):  # noqa: N802
        self._save(False)

    def onSaveRestart_(self, _sender):  # noqa: N802
        self._save(True)

    @objc.python_method
    def _save(self, restart):
        try:
            updates = self._collect_updates()
        except ValueError as e:
            self._error(str(e))
            return
        # LLM settings only reach a session worker when it (re)starts, so a
        # plain Save would leave every live session on the old provider
        # config indefinitely. Promote to restart when an LLM key changed.
        if not restart:
            try:
                current = dotenv_io.read_env(self.paths_ref.env_file)
                llm_keys = ("LLM_PROVIDERS", "LLM_DEFAULT_PROVIDER", *_LEGACY_LLM_KEYS)
                restart = any(
                    (updates.get(k) or "") != (current.get(k) or "")
                    for k in llm_keys
                    if k in updates
                )
                if restart:
                    log.info("LLM config changed; promoting Save to Save & Restart")
            except Exception:
                log.exception("failed to diff LLM keys; leaving restart flag as-is")
        try:
            dotenv_io.write_env(self.paths_ref.env_file, updates)
        except Exception as e:
            log.exception("failed to write .env")
            self._error(f"Failed to save settings:\n{e}")
            return
        # Sync config.toml with what's now in .env: prune stale entries the
        # user removed, then upsert the surviving ones so edits to
        # base_url / api_key / type / max_context / capabilities / default
        # actually take effect (otherwise _build_global_config keeps the
        # stale toml values).
        providers_json = updates.get("LLM_PROVIDERS", "")
        if providers_json:
            try:
                from . import configtoml
                parsed = [
                    p for p in json.loads(providers_json)
                    if isinstance(p, dict) and p.get("name")
                ]
                keep_names = {str(p["name"]) for p in parsed}
                toml_path = self.paths_ref.sessions_dir / "config.toml"
                if keep_names:
                    configtoml.prune(toml_path, keep_names)
                    configtoml.update_providers(toml_path, parsed)
                    configtoml.update_models(toml_path, parsed)
                    configtoml.set_default_model(
                        toml_path, updates.get("LLM_DEFAULT_PROVIDER", "")
                    )
            except Exception:
                log.exception("config.toml sync failed; continuing")
        if self.on_save:
            try:
                self.on_save(restart)
            except Exception:
                log.exception("on_save callback raised")
        self.window().performClose_(None)

    @objc.python_method
    def _collect_updates(self):
        out: dict[str, str] = {}
        for rows in self.rows.values():
            for row in rows:
                if row.key is None:
                    continue
                out[row.key] = self._read_control(row)

        seen_names: set[str] = set()
        provider_list: list[dict[str, Any]] = []
        default_name = ""
        for prow in (self.provider_rows or []):
            name = prow.get_name()
            if not name or not prow.has_api_key():
                continue
            if name.lower() in seen_names:
                raise ValueError(
                    f"Duplicate provider name: {name!r}. Names must be unique."
                )
            seen_names.add(name.lower())
            provider_list.append(prow.to_dict())
            if prow.is_default():
                default_name = name

        if provider_list:
            out["LLM_PROVIDERS"] = json.dumps(
                provider_list, ensure_ascii=False, separators=(",", ":")
            )
            out["LLM_DEFAULT_PROVIDER"] = default_name or provider_list[0]["name"]
            for key in _LEGACY_LLM_KEYS:
                out[key] = ""
        else:
            out["LLM_PROVIDERS"] = ""
            out["LLM_DEFAULT_PROVIDER"] = ""
        return out

    @staticmethod
    @objc.python_method
    def _read_control(row):
        c = row.control
        if isinstance(c, NSPopUpButton):
            return str(c.titleOfSelectedItem() or "")
        if isinstance(c, NSSwitch):
            return "true" if c.state() else "false"
        if isinstance(c, NSSlider):
            return f"{c.doubleValue():.2f}"
        if isinstance(c, (NSTextField, NSSecureTextField)):
            return str(c.stringValue())
        return ""

    # ---- provider row actions -----------------------------------------

    @objc.python_method
    def _row_by_tag(self, tag):
        for r in (self.provider_rows or []):
            if id(r) == tag:
                return r
        return None

    def onSetDefault_(self, sender):  # noqa: N802
        tag = int(sender.tag())
        for r in (self.provider_rows or []):
            r.default_radio.setState_(1 if id(r) == tag else 0)

    def onToggleAdvanced_(self, sender):  # noqa: N802
        tag = int(sender.tag())
        row = self._row_by_tag(tag)
        if row is None:
            return
        row.advanced_visible = not row.advanced_visible
        row.advanced_view.setHidden_(not row.advanced_visible)
        # Toggle the pin: when hidden, force height==0; when visible,
        # release that constraint so the panel claims its intrinsic height.
        if row.advanced_height_zero is not None:
            row.advanced_height_zero.setActive_(not row.advanced_visible)
        row.advanced_btn.setTitle_(
            ("▼ " if row.advanced_visible else "▶ ") + "Advanced (capabilities)"
        )

    def onAddProvider_(self, _sender):  # noqa: N802
        row = _build_provider_row(self, entry=None)
        self.provider_rows.append(row)
        self.providers_container.addSubview_(row.view)
        self._rebuild_provider_chain()
        if not any(r.is_default() for r in self.provider_rows[:-1]):
            row.default_radio.setState_(1)
        self._renumber_providers()

    def onRemoveProvider_(self, sender):  # noqa: N802
        tag = int(sender.tag())
        row = self._row_by_tag(tag)
        if row is None:
            return
        a = NSAlert.alloc().init()
        a.setMessageText_("Remove provider")
        display = row.get_name() or "this provider"
        a.setInformativeText_(f"Remove “{display}” from the list?")
        a.addButtonWithTitle_("Remove")
        a.addButtonWithTitle_("Cancel")
        if a.runModal() != NSAlertFirstButtonReturn:
            return
        was_default = row.is_default()
        self.provider_rows.remove(row)
        row.view.removeFromSuperview()
        self._rebuild_provider_chain()
        if was_default and self.provider_rows:
            self.provider_rows[0].default_radio.setState_(1)
        self._renumber_providers()

    @objc.python_method
    def _renumber_providers(self):
        for i, r in enumerate(self.provider_rows or [], start=1):
            r.title_label.setStringValue_(f"Provider {i}")

    @objc.python_method
    def _rebuild_provider_chain(self):
        """Recompute the vertical chain for the providers_container.

        Deactivate the previously-installed chain constraints, then install
        leading/trailing/top constraints for each row in order. The last
        row's bottom is pinned to ``providers_container.bottom`` so the
        container's height grows with the rows.
        """
        if self.providers_chain_constraints:
            NSLayoutConstraint.deactivateConstraints_(
                self.providers_chain_constraints)
        self.providers_chain_constraints = []

        container = self.providers_container
        rows = self.provider_rows or []
        cs: list = []
        prev = None
        for r in rows:
            cs.append(
                r.view.leadingAnchor().constraintEqualToAnchor_(
                    container.leadingAnchor()))
            cs.append(
                r.view.trailingAnchor().constraintEqualToAnchor_(
                    container.trailingAnchor()))
            if prev is None:
                cs.append(
                    r.view.topAnchor().constraintEqualToAnchor_(
                        container.topAnchor()))
            else:
                cs.append(
                    r.view.topAnchor().constraintEqualToAnchor_constant_(
                        prev.view.bottomAnchor(), 12.0))
            prev = r

        if prev is not None:
            bottom = prev.view.bottomAnchor().constraintEqualToAnchor_(
                container.bottomAnchor())
            cs.append(bottom)
            self.providers_bottom_pin = bottom
        else:
            # Empty list: container has zero intrinsic height.
            zero = container.heightAnchor().constraintEqualToConstant_(0.0)
            cs.append(zero)
            self.providers_bottom_pin = zero

        NSLayoutConstraint.activateConstraints_(cs)
        self.providers_chain_constraints = cs

    # ---- helpers (other actions) --------------------------------------

    def onGenerateToken_(self, _sender):  # noqa: N802
        for row in self.rows["WEB"]:
            if row.key == "KIMI_WEB_SESSION_TOKEN":
                row.control.setStringValue_(secrets.token_hex(32))
                return

    def onChooseDir_(self, sender):  # noqa: N802
        tag = int(sender.tag())
        for row in self.rows["PATHS"]:
            if id(row.extra) == tag:
                panel = NSOpenPanel.openPanel()
                panel.setCanChooseFiles_(False)
                panel.setCanChooseDirectories_(True)
                panel.setAllowsMultipleSelection_(False)
                panel.setCanCreateDirectories_(True)
                if panel.runModal() == NSModalResponseOK:
                    url = panel.URLs()[0]
                    row.control.setStringValue_(str(url.path()))
                return

    def onResetDir_(self, sender):  # noqa: N802
        tag = int(sender.tag())
        for row in self.rows["PATHS"]:
            if id(row.extra) == tag:
                row.control.setStringValue_("")
                return

    def onResetBranding_(self, _sender):  # noqa: N802
        from . import seed_branding
        try:
            seed_branding.reset_to_packaged(self.paths_ref)
        except Exception as e:
            log.exception("branding reset failed")
            self._error(f"Reset failed:\n{e}")
            return
        a = NSAlert.alloc().init()
        a.setMessageText_("Branding reset")
        a.setInformativeText_("Refresh your browser to see the packaged defaults.")
        a.runModal()

    @objc.python_method
    def _error(self, msg):
        a = NSAlert.alloc().init()
        a.setMessageText_("Settings")
        a.setInformativeText_(msg)
        a.runModal()


# ---- builders ----------------------------------------------------------

def _build_provider_row(controller, entry: dict[str, Any] | None = None) -> _ProviderRow:
    """Build one provider row — a single NSView with explicit anchors.

    See spec §3 "Provider row internal" for the constraint plan.
    """
    entry = entry or {}

    row = _v()

    # Leading 3pt accent bar (control accent color).
    accent = _v()
    accent.setWantsLayer_(True)
    try:
        accent.layer().setBackgroundColor_(NSColor.controlAccentColor().CGColor())
    except Exception:
        accent.layer().setBackgroundColor_(NSColor.systemBlueColor().CGColor())
    row.addSubview_(accent)

    # Section title (re-numbered by controller after add/remove).
    title_label = _label("Provider", size=11.0, secondary=True)
    row.addSubview_(title_label)

    # Form rows — each label/control pair in its own NSView.
    name_field = _text(str(entry.get("name", "")), placeholder="provider name")

    type_popup = NSPopUpButton.alloc().init()
    type_popup.setTranslatesAutoresizingMaskIntoConstraints_(False)
    for t in _PROVIDER_TYPES:
        type_popup.addItemWithTitle_(t)
    type_value = str(entry.get("type", _PROVIDER_TYPES[0]))
    if type_value in _PROVIDER_TYPES:
        type_popup.selectItemWithTitle_(type_value)

    api_key = _text(str(entry.get("api_key", "")), secure=True, placeholder="API key")
    base_url = _text(str(entry.get("base_url", "")), placeholder="https://...")
    model = _text(str(entry.get("model", "")), placeholder="model id")

    max_ctx_val = entry.get("max_context_size", "")
    max_context = _text(
        str(max_ctx_val) if max_ctx_val else "",
        placeholder="1000000",
    )

    fr_name = _form_row(_label("Name"), name_field)
    fr_type = _form_row(_label("Type"), type_popup, fixed_width=180.0)
    fr_apikey = _form_row(_label("API Key"), api_key)
    fr_baseurl = _form_row(_label("Base URL"), base_url)
    fr_model = _form_row(_label("Model"), model)
    fr_maxctx = _form_row(_label("Max Context"), max_context, fixed_width=120.0)

    for fr in (fr_name, fr_type, fr_apikey, fr_baseurl, fr_model, fr_maxctx):
        row.addSubview_(fr)

    default_radio = NSButton.radioButtonWithTitle_target_action_(
        "Set as default", controller, "onSetDefault:"
    )
    default_radio.setTranslatesAutoresizingMaskIntoConstraints_(False)
    default_radio.setState_(0)
    row.addSubview_(default_radio)

    advanced_btn = NSButton.alloc().init()
    advanced_btn.setBezelStyle_(11)  # recessed/flat
    advanced_btn.setTitle_("▶ Advanced (capabilities)")
    advanced_btn.setTarget_(controller)
    advanced_btn.setAction_("onToggleAdvanced:")
    advanced_btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
    row.addSubview_(advanced_btn)

    raw_caps = entry.get("capabilities")
    if isinstance(raw_caps, list) and raw_caps:
        cap_initial = {str(c) for c in raw_caps}
    else:
        cap_initial = set(_CAPABILITIES)

    advanced_view = _v()
    advanced_view.setHidden_(True)
    row.addSubview_(advanced_view)

    caps: dict[str, NSButton] = {}
    cap_buttons: list[NSButton] = []
    for c in _CAPABILITIES:
        btn = NSButton.checkboxWithTitle_target_action_(c, None, None)
        btn.setTranslatesAutoresizingMaskIntoConstraints_(False)
        btn.setState_(1 if c in cap_initial else 0)
        caps[c] = btn
        cap_buttons.append(btn)
        advanced_view.addSubview_(btn)

    # Vertical chain inside advanced_view.
    cap_cs: list = []
    prev_btn = None
    for btn in cap_buttons:
        cap_cs.append(btn.leadingAnchor().constraintEqualToAnchor_(
            advanced_view.leadingAnchor()))
        if prev_btn is None:
            cap_cs.append(btn.topAnchor().constraintEqualToAnchor_(
                advanced_view.topAnchor()))
        else:
            cap_cs.append(btn.topAnchor().constraintEqualToAnchor_constant_(
                prev_btn.bottomAnchor(), 4.0))
        prev_btn = btn
    if prev_btn is not None:
        cap_cs.append(prev_btn.bottomAnchor().constraintEqualToAnchor_(
            advanced_view.bottomAnchor()))
    NSLayoutConstraint.activateConstraints_(cap_cs)

    # Mutable height==0 constraint on advanced_view (active when collapsed).
    advanced_height_zero = advanced_view.heightAnchor().constraintEqualToConstant_(0.0)
    advanced_height_zero.setPriority_(999)
    advanced_height_zero.setActive_(True)

    remove_btn = _button("Remove", controller, "onRemoveProvider:")
    row.addSubview_(remove_btn)

    # Constraint plan for the provider row (per spec §3).
    PAD_LEADING_CONTENT = 9.0  # space between accent bar and content
    PAD_INNER = 12.0           # row internal trailing padding
    accent_to_content_inset = PAD_LEADING_CONTENT  # accent_bar.trailing + 9

    cs: list = []

    # Accent bar
    cs.extend([
        accent.leadingAnchor().constraintEqualToAnchor_(row.leadingAnchor()),
        accent.topAnchor().constraintEqualToAnchor_(row.topAnchor()),
        accent.bottomAnchor().constraintEqualToAnchor_(row.bottomAnchor()),
        accent.widthAnchor().constraintEqualToConstant_(3.0),
    ])

    # Title label
    cs.extend([
        title_label.leadingAnchor().constraintEqualToAnchor_constant_(
            accent.trailingAnchor(), accent_to_content_inset),
        title_label.topAnchor().constraintEqualToAnchor_constant_(
            row.topAnchor(), 8.0),
        title_label.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
            row.trailingAnchor(), -PAD_INNER),
    ])

    # Form rows chain (top under the title)
    form_rows = (fr_name, fr_type, fr_apikey, fr_baseurl, fr_model, fr_maxctx)
    prev_fr = None
    for fr in form_rows:
        cs.append(fr.leadingAnchor().constraintEqualToAnchor_constant_(
            accent.trailingAnchor(), accent_to_content_inset))
        cs.append(fr.trailingAnchor().constraintEqualToAnchor_constant_(
            row.trailingAnchor(), -PAD_INNER))
        if prev_fr is None:
            cs.append(fr.topAnchor().constraintEqualToAnchor_constant_(
                title_label.bottomAnchor(), 8.0))
        else:
            cs.append(fr.topAnchor().constraintEqualToAnchor_constant_(
                prev_fr.bottomAnchor(), ROW_VGAP))
        prev_fr = fr

    # default_radio aligned under the input column
    radio_leading_offset = (
        accent_to_content_inset + LABEL_COLUMN_WIDTH + LABEL_INPUT_GAP
    )
    cs.extend([
        default_radio.topAnchor().constraintEqualToAnchor_constant_(
            fr_maxctx.bottomAnchor(), 10.0),
        default_radio.leadingAnchor().constraintEqualToAnchor_constant_(
            accent.trailingAnchor(), radio_leading_offset),
    ])

    # advanced_btn under the radio
    cs.extend([
        advanced_btn.topAnchor().constraintEqualToAnchor_constant_(
            default_radio.bottomAnchor(), 6.0),
        advanced_btn.leadingAnchor().constraintEqualToAnchor_(
            default_radio.leadingAnchor()),
    ])

    # advanced_view under the disclosure
    cs.extend([
        advanced_view.topAnchor().constraintEqualToAnchor_constant_(
            advanced_btn.bottomAnchor(), 4.0),
        advanced_view.leadingAnchor().constraintEqualToAnchor_(
            advanced_btn.leadingAnchor()),
        advanced_view.trailingAnchor().constraintEqualToAnchor_constant_(
            row.trailingAnchor(), -PAD_INNER),
    ])

    # remove_btn at the bottom right
    cs.extend([
        remove_btn.topAnchor().constraintEqualToAnchor_constant_(
            advanced_view.bottomAnchor(), 8.0),
        remove_btn.trailingAnchor().constraintEqualToAnchor_constant_(
            row.trailingAnchor(), -PAD_INNER),
        remove_btn.bottomAnchor().constraintEqualToAnchor_constant_(
            row.bottomAnchor(), -12.0),
    ])

    NSLayoutConstraint.activateConstraints_(cs)

    pr = _ProviderRow(
        view=row,
        title_label=title_label,
        name_field=name_field,
        type_popup=type_popup,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_context=max_context,
        default_radio=default_radio,
        advanced_btn=advanced_btn,
        advanced_view=advanced_view,
        advanced_height_zero=advanced_height_zero,
        caps=caps,
    )
    tag = id(pr)
    default_radio.setTag_(tag)
    advanced_btn.setTag_(tag)
    remove_btn.setTag_(tag)
    return pr


def _build_llm_section(
    values: dict[str, str],
    controller: SettingsController,
) -> tuple[NSView, list[_Row], list[_ProviderRow], NSView, NSButton]:
    """Build the LLM section.

    Returns ``(section_view, global_rows, provider_rows, providers_container,
    add_button)``.
    """
    section, header = _section_view("LLM")

    rows: list[_Row] = []

    # Global controls
    thinking = _switch((values.get("LLM_THINKING") or "").lower() == "true")
    try:
        temp_val = float(values.get("LLM_TEMPERATURE") or "0.0")
    except ValueError:
        temp_val = 0.0
    temp = NSSlider.alloc().init()
    temp.setMinValue_(0.0)
    temp.setMaxValue_(2.0)
    temp.setDoubleValue_(temp_val)
    temp.setTranslatesAutoresizingMaskIntoConstraints_(False)
    # Slider should stretch but at least 200pt wide.
    temp.widthAnchor().constraintGreaterThanOrEqualToConstant_(200.0).setActive_(True)

    fr_thinking = _form_row(_label("Thinking"), thinking, fixed_width=60.0)
    fr_temp = _form_row(_label("Temperature"), temp)
    rows.append(_Row("LLM_THINKING", _label("Thinking"), thinking))
    rows.append(_Row("LLM_TEMPERATURE", _label("Temperature"), temp))

    section.addSubview_(fr_thinking)
    section.addSubview_(fr_temp)

    # Providers container
    providers_container = _v()
    section.addSubview_(providers_container)

    # Pre-build any existing provider rows.
    entries = _parse_existing_providers(values.get("LLM_PROVIDERS", ""))
    if not entries:
        entries = _legacy_entries(values)

    default_value = (
        values.get("LLM_DEFAULT_PROVIDER")
        or values.get("LLM_PROVIDER")
        or (entries[0]["name"] if entries else "")
    ).strip().lower()

    provider_rows: list[_ProviderRow] = []
    default_set = False
    for entry in entries:
        prow = _build_provider_row(controller, entry)
        provider_rows.append(prow)
        providers_container.addSubview_(prow.view)
        if not default_set and str(entry.get("name", "")).strip().lower() == default_value:
            prow.default_radio.setState_(1)
            default_set = True
    if not default_set and provider_rows:
        provider_rows[0].default_radio.setState_(1)

    # + Add Provider button (left-aligned, flat)
    add_btn = _flat_button("+ Add Provider", controller, "onAddProvider:")

    section.addSubview_(add_btn)

    # Section internal vertical chain.
    cs: list = [
        # header pinned by _section_view
        fr_thinking.topAnchor().constraintEqualToAnchor_constant_(
            header.bottomAnchor(), 12.0),
        fr_thinking.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        fr_thinking.trailingAnchor().constraintEqualToAnchor_(section.trailingAnchor()),

        fr_temp.topAnchor().constraintEqualToAnchor_constant_(
            fr_thinking.bottomAnchor(), ROW_VGAP),
        fr_temp.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        fr_temp.trailingAnchor().constraintEqualToAnchor_(section.trailingAnchor()),

        providers_container.topAnchor().constraintEqualToAnchor_constant_(
            fr_temp.bottomAnchor(), 14.0),
        providers_container.leadingAnchor().constraintEqualToAnchor_(
            section.leadingAnchor()),
        providers_container.trailingAnchor().constraintEqualToAnchor_(
            section.trailingAnchor()),

        add_btn.topAnchor().constraintEqualToAnchor_constant_(
            providers_container.bottomAnchor(), 10.0),
        add_btn.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        add_btn.bottomAnchor().constraintEqualToAnchor_(section.bottomAnchor()),
    ]
    NSLayoutConstraint.activateConstraints_(cs)

    return section, rows, provider_rows, providers_container, add_btn


def _build_web_section(
    values: dict[str, str],
    controller: SettingsController,
) -> tuple[NSView, list[_Row]]:
    section, header = _section_view("Web Server")
    rows: list[_Row] = []

    port = _text(values.get("KIMI_WEB_PORT", "5494") or "5494")
    fr_port = _form_row(_label("Port"), port, fixed_width=100.0)
    rows.append(_Row("KIMI_WEB_PORT", _label("Port"), port))

    token = _text(
        values.get("KIMI_WEB_SESSION_TOKEN", ""),
        secure=True,
        placeholder="leave empty to disable auth",
    )
    gen = _button("Generate", controller, "onGenerateToken:")
    token_compound = _compound_token_field(token, gen)
    fr_token = _form_row(_label("Session Token"), token_compound)
    rows.append(_Row("KIMI_WEB_SESSION_TOKEN", _label("Session Token"), token))

    lan_only = _switch((values.get("KIMI_WEB_LAN_ONLY") or "").lower() == "true")
    fr_lan = _form_row(_label("LAN Only"), lan_only, fixed_width=60.0)
    rows.append(_Row("KIMI_WEB_LAN_ONLY", _label("LAN Only"), lan_only))

    for fr in (fr_port, fr_token, fr_lan):
        section.addSubview_(fr)

    cs: list = [
        fr_port.topAnchor().constraintEqualToAnchor_constant_(
            header.bottomAnchor(), 12.0),
        fr_port.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        fr_port.trailingAnchor().constraintEqualToAnchor_(section.trailingAnchor()),

        fr_token.topAnchor().constraintEqualToAnchor_constant_(
            fr_port.bottomAnchor(), ROW_VGAP),
        fr_token.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        fr_token.trailingAnchor().constraintEqualToAnchor_(section.trailingAnchor()),

        fr_lan.topAnchor().constraintEqualToAnchor_constant_(
            fr_token.bottomAnchor(), ROW_VGAP),
        fr_lan.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        fr_lan.trailingAnchor().constraintEqualToAnchor_(section.trailingAnchor()),
        fr_lan.bottomAnchor().constraintEqualToAnchor_(section.bottomAnchor()),
    ]
    NSLayoutConstraint.activateConstraints_(cs)

    return section, rows


def _build_branding_section(
    controller: SettingsController,
) -> tuple[NSView, list[_Row]]:
    section, header = _section_view("Branding")

    reset_brand = _button(
        "Reset Branding to Packaged Defaults", controller, "onResetBranding:")
    section.addSubview_(reset_brand)

    NSLayoutConstraint.activateConstraints_([
        reset_brand.topAnchor().constraintEqualToAnchor_constant_(
            header.bottomAnchor(), 12.0),
        reset_brand.leadingAnchor().constraintEqualToAnchor_(section.leadingAnchor()),
        reset_brand.bottomAnchor().constraintEqualToAnchor_(section.bottomAnchor()),
    ])

    return section, []


def build_controller(
    paths: AppPaths,
    on_save: Callable[[bool], None],
) -> SettingsController:
    """Construct an off-screen Settings window. Call ``.show()`` to display."""
    style = (
        NSTitledWindowMask
        | NSClosableWindowMask
        | NSMiniaturizableWindowMask
        | NSResizableWindowMask
    )
    rect = NSMakeRect(0.0, 0.0, 720.0, 640.0)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False
    )
    window.setTitle_(f"{paths.app_name} Settings")
    from Foundation import NSMakeSize
    window.setContentMinSize_(NSMakeSize(560.0, 480.0))
    window.center()
    window.setReleasedWhenClosed_(False)

    controller = SettingsController.alloc().initWithWindow_(window)
    controller.paths_ref = paths
    controller.on_save = on_save
    controller.rows = {}
    controller.provider_rows = []
    controller.providers_chain_constraints = []

    values = dotenv_io.read_editable(paths.env_file)

    # Build the three sections.
    llm_section, llm_rows, provider_rows, providers_container, add_button = (
        _build_llm_section(values, controller)
    )
    web_section, web_rows = _build_web_section(values, controller)
    branding_section, branding_rows = _build_branding_section(controller)

    controller.rows = {
        "LLM": llm_rows,
        "WEB": web_rows,
        "BRANDING": branding_rows,
    }
    controller.provider_rows = provider_rows
    controller.providers_container = providers_container
    controller.add_provider_button = add_button

    # Wire the providers chain (must happen AFTER controller fields are set).
    controller._rebuild_provider_chain()

    # Build the scroll content view.
    scroll_content = _v()
    scroll_content.addSubview_(llm_section)
    div1 = _divider()
    scroll_content.addSubview_(div1)
    scroll_content.addSubview_(web_section)
    div2 = _divider()
    scroll_content.addSubview_(div2)
    scroll_content.addSubview_(branding_section)

    cs_content: list = [
        # LLM section
        llm_section.topAnchor().constraintEqualToAnchor_constant_(
            scroll_content.topAnchor(), OUTER_VMARGIN),
        llm_section.leadingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.leadingAnchor(), OUTER_HMARGIN),
        llm_section.trailingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.trailingAnchor(), -OUTER_HMARGIN),

        # Divider 1
        div1.topAnchor().constraintEqualToAnchor_constant_(
            llm_section.bottomAnchor(), SECTION_VGAP),
        div1.leadingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.leadingAnchor(), OUTER_HMARGIN),
        div1.trailingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.trailingAnchor(), -OUTER_HMARGIN),
        div1.heightAnchor().constraintEqualToConstant_(1.0),

        # Web section
        web_section.topAnchor().constraintEqualToAnchor_constant_(
            div1.bottomAnchor(), SECTION_VGAP),
        web_section.leadingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.leadingAnchor(), OUTER_HMARGIN),
        web_section.trailingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.trailingAnchor(), -OUTER_HMARGIN),

        # Divider 2
        div2.topAnchor().constraintEqualToAnchor_constant_(
            web_section.bottomAnchor(), SECTION_VGAP),
        div2.leadingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.leadingAnchor(), OUTER_HMARGIN),
        div2.trailingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.trailingAnchor(), -OUTER_HMARGIN),
        div2.heightAnchor().constraintEqualToConstant_(1.0),

        # Branding section
        branding_section.topAnchor().constraintEqualToAnchor_constant_(
            div2.bottomAnchor(), SECTION_VGAP),
        branding_section.leadingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.leadingAnchor(), OUTER_HMARGIN),
        branding_section.trailingAnchor().constraintEqualToAnchor_constant_(
            scroll_content.trailingAnchor(), -OUTER_HMARGIN),
        branding_section.bottomAnchor().constraintEqualToAnchor_constant_(
            scroll_content.bottomAnchor(), -OUTER_VMARGIN),
    ]
    NSLayoutConstraint.activateConstraints_(cs_content)

    # Scroll view wrapping the content.
    scroll = NSScrollView.alloc().init()
    scroll.setTranslatesAutoresizingMaskIntoConstraints_(False)
    scroll.setHasVerticalScroller_(True)
    scroll.setHasHorizontalScroller_(False)
    scroll.setBorderType_(0)  # NSNoBorder
    scroll.setDrawsBackground_(False)
    scroll.setDocumentView_(scroll_content)

    # CRITICAL: pin scroll_content's width to the scroll's clip view to make
    # vertical-only scrolling work.
    NSLayoutConstraint.activateConstraints_([
        scroll_content.widthAnchor().constraintEqualToAnchor_(
            scroll.contentView().widthAnchor()),
    ])

    # Sticky button bar.
    button_bar = _v()
    cancel = _button("Cancel", controller, "onCancel:")
    save = _button("Save", controller, "onSave:")
    save_restart = _button("Save & Restart Server", controller, "onSaveRestart:")
    save_restart.setKeyEquivalent_("\r")  # default action

    sep_top = _divider()
    button_bar.addSubview_(sep_top)
    button_bar.addSubview_(cancel)
    button_bar.addSubview_(save)
    button_bar.addSubview_(save_restart)

    for btn in (cancel, save, save_restart):
        btn.setContentHuggingPriority_forOrientation_(752, 0)

    NSLayoutConstraint.activateConstraints_([
        sep_top.topAnchor().constraintEqualToAnchor_(button_bar.topAnchor()),
        sep_top.leadingAnchor().constraintEqualToAnchor_(button_bar.leadingAnchor()),
        sep_top.trailingAnchor().constraintEqualToAnchor_(button_bar.trailingAnchor()),
        sep_top.heightAnchor().constraintEqualToConstant_(1.0),

        cancel.leadingAnchor().constraintEqualToAnchor_constant_(
            button_bar.leadingAnchor(), BUTTONBAR_HMARGIN),
        cancel.centerYAnchor().constraintEqualToAnchor_(button_bar.centerYAnchor()),

        save_restart.trailingAnchor().constraintEqualToAnchor_constant_(
            button_bar.trailingAnchor(), -BUTTONBAR_HMARGIN),
        save_restart.centerYAnchor().constraintEqualToAnchor_(button_bar.centerYAnchor()),

        save.trailingAnchor().constraintEqualToAnchor_constant_(
            save_restart.leadingAnchor(), -8.0),
        save.centerYAnchor().constraintEqualToAnchor_(button_bar.centerYAnchor()),
    ])

    # Wire scroll + button_bar onto the window's contentView.
    content = window.contentView()
    content.addSubview_(scroll)
    content.addSubview_(button_bar)

    NSLayoutConstraint.activateConstraints_([
        scroll.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
        scroll.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
        scroll.topAnchor().constraintEqualToAnchor_(content.topAnchor()),
        scroll.bottomAnchor().constraintEqualToAnchor_(button_bar.topAnchor()),

        button_bar.leadingAnchor().constraintEqualToAnchor_(content.leadingAnchor()),
        button_bar.trailingAnchor().constraintEqualToAnchor_(content.trailingAnchor()),
        button_bar.bottomAnchor().constraintEqualToAnchor_(content.bottomAnchor()),
        button_bar.heightAnchor().constraintEqualToConstant_(BUTTONBAR_HEIGHT),
    ])

    return controller


__all__ = ["SettingsController", "build_controller"]
