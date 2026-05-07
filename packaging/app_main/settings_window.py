"""Native macOS Settings window (PyObjC).

Lives in the menu-bar process so it's available even when the uvicorn
server is offline (e.g. when the LLM API key hasn't been configured yet,
which would prevent the Web admin panel from loading).

Three toolbar tabs:
  • LLM        — provider, API keys, model, base URL, temperature, thinking
  • Web Server — port, session token, LAN-only
  • Paths      — work / sessions / output / custom skills / HF cache

Save writes to ``~/Library/Application Support/<AppName>/.env`` via
``dotenv_io.write_env`` (atomic, preserves comments and unknown lines).
"""

from __future__ import annotations

import logging
import secrets
from typing import Callable

from AppKit import (
    NSAlert,
    NSApp,
    NSBackingStoreBuffered,
    NSButton,
    NSFont,
    NSGridCellPlacementLeading,
    NSGridCellPlacementTrailing,
    NSGridView,
    NSImage,
    NSMakeRect,
    NSModalResponseOK,
    NSOpenPanel,
    NSPopUpButton,
    NSSecureTextField,
    NSSlider,
    NSStackView,
    NSStackViewDistributionFill,
    NSSwitch,
    NSTextField,
    NSTitledWindowMask,
    NSClosableWindowMask,
    NSMiniaturizableWindowMask,
    NSResizableWindowMask,
    NSToolbar,
    NSToolbarItem,
    NSUserInterfaceLayoutOrientationVertical,
    NSView,
    NSWindow,
    NSWindowController,
)
import objc
from Foundation import NSObject  # noqa: F401  (kept for symmetry / future use)

from . import dotenv_io
from .paths import AppPaths

log = logging.getLogger(__name__)

_PROVIDERS = ("kimi", "openai", "anthropic")

_TAB_LLM = "LLM"
_TAB_WEB = "Web Server"
_TAB_PATHS = "Paths"
_TABS = (_TAB_LLM, _TAB_WEB, _TAB_PATHS)


def _label(text: str, *, bold: bool = False) -> NSTextField:
    f = NSTextField.alloc().init()
    f.setStringValue_(text)
    f.setBezeled_(False)
    f.setDrawsBackground_(False)
    f.setEditable_(False)
    f.setSelectable_(False)
    if bold:
        f.setFont_(NSFont.boldSystemFontOfSize_(13))
    return f


def _text(value: str = "", *, secure: bool = False, placeholder: str = "") -> NSTextField:
    cls = NSSecureTextField if secure else NSTextField
    f = cls.alloc().init()
    f.setStringValue_(value)
    if placeholder:
        f.setPlaceholderString_(placeholder)
    f.setBezeled_(True)
    f.setEditable_(True)
    return f


def _switch(on: bool) -> NSSwitch:
    s = NSSwitch.alloc().init()
    s.setState_(1 if on else 0)
    return s


def _button(title: str, target, action: str) -> NSButton:
    b = NSButton.alloc().init()
    b.setTitle_(title)
    b.setBezelStyle_(1)  # rounded
    b.setTarget_(target)
    b.setAction_(action)
    return b


class _Row:
    """Holds a label + control pair so we can read it back on Save."""

    __slots__ = ("key", "label", "control", "extra")

    def __init__(self, key: str, label: NSTextField, control, extra=None):
        self.key = key
        self.label = label
        self.control = control
        self.extra = extra


class SettingsController(NSWindowController):
    """Backing window controller; built lazily via :func:`build_controller`."""

    paths_ref = None  # type: AppPaths | None
    on_save = None    # type: Callable[[bool], None] | None
    rows = None       # type: dict[str, list[_Row]] | None
    tab_views = None  # type: dict[str, NSView] | None
    container = None  # type: NSStackView | None
    current_tab = _TAB_LLM

    # ---- public API ----------------------------------------------------

    @objc.python_method
    def show(self):
        self.showWindow_(None)
        self.window().makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ---- toolbar -------------------------------------------------------

    def toolbarAllowedItemIdentifiers_(self, _toolbar):  # noqa: N802
        return list(_TABS)

    def toolbarDefaultItemIdentifiers_(self, _toolbar):  # noqa: N802
        return list(_TABS)

    def toolbarSelectableItemIdentifiers_(self, _toolbar):  # noqa: N802
        return list(_TABS)

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(  # noqa: N802
        self, _toolbar, identifier, _flag
    ):
        item = NSToolbarItem.alloc().initWithItemIdentifier_(identifier)
        item.setLabel_(identifier)
        item.setPaletteLabel_(identifier)
        item.setTarget_(self)
        item.setAction_("onTabSelected:")
        sym = {
            _TAB_LLM: "brain",
            _TAB_WEB: "network",
            _TAB_PATHS: "folder",
        }.get(identifier, "gear")
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(sym, identifier)
            if img is not None:
                item.setImage_(img)
        except Exception:
            pass
        return item

    def onTabSelected_(self, sender):  # noqa: N802
        ident = sender.itemIdentifier() if hasattr(sender, "itemIdentifier") else sender.label()
        self._show_tab(str(ident))

    @objc.python_method
    def _show_tab(self, name):
        if name not in self.tab_views:
            return
        self.current_tab = name
        for sub in list(self.container.arrangedSubviews()):
            self.container.removeArrangedSubview_(sub)
            sub.removeFromSuperview()
        self.container.addArrangedSubview_(self.tab_views[name])

    # ---- bottom buttons -----------------------------------------------

    def onCancel_(self, _sender):  # noqa: N802
        self.window().performClose_(None)

    def onSave_(self, _sender):  # noqa: N802
        self._save(False)

    def onSaveRestart_(self, _sender):  # noqa: N802
        self._save(True)

    @objc.python_method
    def _save(self, restart):
        updates = self._collect_updates()
        try:
            dotenv_io.write_env(self.paths_ref.env_file, updates)
        except Exception as e:
            log.exception("failed to write .env")
            self._error(f"Failed to save settings:\n{e}")
            return
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

    # ---- helpers -------------------------------------------------------

    def onGenerateToken_(self, _sender):  # noqa: N802
        for row in self.rows[_TAB_WEB]:
            if row.key == "KIMI_WEB_SESSION_TOKEN":
                row.control.setStringValue_(secrets.token_hex(32))
                return

    def onChooseDir_(self, sender):  # noqa: N802
        # Use the button's tag to identify which row triggered this.
        tag = int(sender.tag())
        for row in self.rows[_TAB_PATHS]:
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
        for row in self.rows[_TAB_PATHS]:
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

def _make_grid(rows: list[tuple[NSTextField, NSView]]) -> NSGridView:
    grid = NSGridView.gridViewWithViews_([[lbl, ctl] for lbl, ctl in rows])
    grid.columnAtIndex_(0).setXPlacement_(NSGridCellPlacementTrailing)
    grid.columnAtIndex_(1).setXPlacement_(NSGridCellPlacementLeading)
    grid.setRowSpacing_(8.0)
    grid.setColumnSpacing_(12.0)
    return grid


def _build_llm_tab(values: dict[str, str]) -> tuple[NSView, list[_Row]]:
    rows: list[_Row] = []
    grid_rows: list[tuple[NSTextField, NSView]] = []

    provider = NSPopUpButton.alloc().init()
    for p in _PROVIDERS:
        provider.addItemWithTitle_(p)
    cur = (values.get("LLM_PROVIDER") or "kimi").lower()
    if cur in _PROVIDERS:
        provider.selectItemWithTitle_(cur)
    grid_rows.append((_label("Provider"), provider))
    rows.append(_Row("LLM_PROVIDER", grid_rows[-1][0], provider))

    def add_text(key: str, label: str, *, secure: bool = False, placeholder: str = ""):
        ctl = _text(values.get(key, ""), secure=secure, placeholder=placeholder)
        ctl.setFrame_(NSMakeRect(0, 0, 320, 22))
        grid_rows.append((_label(label), ctl))
        rows.append(_Row(key, grid_rows[-1][0], ctl))

    add_text("KIMI_API_KEY", "Kimi API Key", secure=True, placeholder="sk-...")
    add_text("KIMI_BASE_URL", "Kimi Base URL", placeholder="https://api.moonshot.cn/v1")
    add_text("KIMI_MODEL_NAME", "Kimi Model")
    add_text("KIMI_MODEL_MAX_CONTEXT_SIZE", "Kimi Max Context")

    add_text("OPENAI_API_KEY", "OpenAI API Key", secure=True, placeholder="sk-...")
    add_text("OPENAI_BASE_URL", "OpenAI Base URL")

    add_text("ANTHROPIC_API_KEY", "Anthropic API Key", secure=True, placeholder="sk-ant-...")
    add_text("ANTHROPIC_BASE_URL", "Anthropic Base URL")

    thinking = _switch((values.get("LLM_THINKING") or "").lower() == "true")
    grid_rows.append((_label("Thinking"), thinking))
    rows.append(_Row("LLM_THINKING", grid_rows[-1][0], thinking))

    temp_val = 0.0
    try:
        temp_val = float(values.get("LLM_TEMPERATURE") or "0.0")
    except ValueError:
        pass
    temp = NSSlider.alloc().init()
    temp.setMinValue_(0.0)
    temp.setMaxValue_(2.0)
    temp.setDoubleValue_(temp_val)
    temp.setFrame_(NSMakeRect(0, 0, 200, 22))
    grid_rows.append((_label("Temperature"), temp))
    rows.append(_Row("LLM_TEMPERATURE", grid_rows[-1][0], temp))

    return _make_grid(grid_rows), rows


def _build_web_tab(values: dict[str, str], controller: SettingsController) -> tuple[NSView, list[_Row]]:
    rows: list[_Row] = []
    grid_rows: list[tuple[NSTextField, NSView]] = []

    port = _text(values.get("KIMI_WEB_PORT", "5494"))
    port.setFrame_(NSMakeRect(0, 0, 100, 22))
    grid_rows.append((_label("Port"), port))
    rows.append(_Row("KIMI_WEB_PORT", grid_rows[-1][0], port))

    token = _text(values.get("KIMI_WEB_SESSION_TOKEN", ""), secure=True, placeholder="leave empty to disable auth")
    token.setFrame_(NSMakeRect(0, 0, 320, 22))
    gen = _button("Generate", controller, "onGenerateToken:")
    token_row = NSStackView.stackViewWithViews_([token, gen])
    token_row.setSpacing_(8.0)
    grid_rows.append((_label("Session Token"), token_row))
    rows.append(_Row("KIMI_WEB_SESSION_TOKEN", grid_rows[-1][0], token))

    lan_only = _switch((values.get("KIMI_WEB_LAN_ONLY") or "").lower() == "true")
    grid_rows.append((_label("LAN Only"), lan_only))
    rows.append(_Row("KIMI_WEB_LAN_ONLY", grid_rows[-1][0], lan_only))

    return _make_grid(grid_rows), rows


def _build_paths_tab(values: dict[str, str], controller: SettingsController) -> tuple[NSView, list[_Row]]:
    rows: list[_Row] = []
    grid_rows: list[tuple[NSTextField, NSView]] = []

    spec = (
        ("KIMI_DEFAULT_WORK_DIR", "Default Work Directory"),
        ("KIMI_SESSION_DATA_DIR", "Session Data Directory"),
        ("KIMI_OUTPUT_DIR", "Output Directory"),
        ("CUSTOM_SKILLS_HOST_PATH", "Custom Skills Directory"),
        ("HF_CACHE_HOST_PATH", "HuggingFace Cache"),
    )
    for key, label in spec:
        f = _text(values.get(key, ""), placeholder="(use packaged default)")
        f.setFrame_(NSMakeRect(0, 0, 320, 22))
        f.setEditable_(False)  # browse only
        choose = _button("Choose…", controller, "onChooseDir:")
        reset = _button("Reset", controller, "onResetDir:")
        marker = object()
        choose.setTag_(id(marker))
        reset.setTag_(id(marker))
        line = NSStackView.stackViewWithViews_([f, choose, reset])
        line.setSpacing_(6.0)
        grid_rows.append((_label(label), line))
        rows.append(_Row(key, grid_rows[-1][0], f, marker))

    grid = _make_grid(grid_rows)

    reset_brand = _button("Reset Branding to Packaged Defaults", controller, "onResetBranding:")
    wrapper = NSStackView.stackViewWithViews_([grid, reset_brand])
    wrapper.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    wrapper.setSpacing_(20.0)
    wrapper.setAlignment_(2)  # NSLayoutAttributeLeft

    return wrapper, rows


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
    rect = NSMakeRect(0.0, 0.0, 620.0, 520.0)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False
    )
    window.setTitle_(f"{paths.app_name} Settings")
    window.center()
    window.setReleasedWhenClosed_(False)

    controller = SettingsController.alloc().initWithWindow_(window)
    controller.paths_ref = paths
    controller.on_save = on_save
    controller.rows = {}
    controller.tab_views = {}

    # Toolbar
    toolbar = NSToolbar.alloc().initWithIdentifier_("settings.toolbar")
    toolbar.setDelegate_(controller)
    toolbar.setDisplayMode_(2)  # icon + label
    toolbar.setAllowsUserCustomization_(False)
    toolbar.setSelectedItemIdentifier_(_TAB_LLM)
    window.setToolbar_(toolbar)

    # Tabs
    values = dotenv_io.read_editable(paths.env_file)
    llm_view, llm_rows = _build_llm_tab(values)
    web_view, web_rows = _build_web_tab(values, controller)
    paths_view, paths_rows = _build_paths_tab(values, controller)

    controller.tab_views = {
        _TAB_LLM: llm_view,
        _TAB_WEB: web_view,
        _TAB_PATHS: paths_view,
    }
    controller.rows = {
        _TAB_LLM: llm_rows,
        _TAB_WEB: web_rows,
        _TAB_PATHS: paths_rows,
    }

    # Layout: vertical stack with tab body + button row
    body_container = NSStackView.alloc().init()
    body_container.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    body_container.setDistribution_(NSStackViewDistributionFill)
    body_container.setSpacing_(12.0)
    body_container.setEdgeInsets_((20.0, 24.0, 20.0, 24.0))

    body_container.addArrangedSubview_(llm_view)
    controller.container = body_container

    cancel = _button("Cancel", controller, "onCancel:")
    save = _button("Save", controller, "onSave:")
    save_restart = _button("Save & Restart Server", controller, "onSaveRestart:")
    save_restart.setKeyEquivalent_("\r")  # Return = default

    spacer = NSView.alloc().init()
    button_row = NSStackView.stackViewWithViews_([cancel, spacer, save, save_restart])
    button_row.setSpacing_(8.0)

    outer = NSStackView.alloc().init()
    outer.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
    outer.setSpacing_(12.0)
    outer.setEdgeInsets_((16.0, 20.0, 16.0, 20.0))
    outer.addArrangedSubview_(body_container)
    outer.addArrangedSubview_(button_row)

    window.setContentView_(outer)
    return controller


__all__ = ["SettingsController", "build_controller"]
