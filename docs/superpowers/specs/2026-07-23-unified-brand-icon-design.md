# Unified Brand Icon Design

## Goal

Use the black-background, blue-circle, white-letter OpenKimo artwork as the
single built-in brand icon across the Web UI and packaged desktop
applications. Keep the existing administrator branding controls so a deployed
instance can still override the built-in logo and favicon.

## Canonical asset

`kimi-cli/web/public/logo.png` is the only source image checked into the code
for the built-in product icon. It contains the current 1024×1024 blue-circle
artwork.

The old black-and-white artwork at that path is replaced. The duplicate source
file `packaging/icon.png` is deleted.

Generated files such as `kimi-cli/web/dist/logo.png`, `AppIcon.icns`,
`MenuBarIcon.png`, and Windows `.ico` files are build outputs derived from the
canonical asset; they are not independent source artwork.

## Consumers

- The browser tab favicon continues to request `/logo.png`.
- The expanded and collapsed Web sidebar continues to use `/logo.png` as its
  built-in default.
- The branding settings preview continues to use `/logo.png` as its default.
- macOS packaging reads `../kimi-cli/web/public/logo.png` to generate the
  application and menu-bar icons.
- Windows packaging reads the same source to generate application, tray, and
  shortcut icons.
- The macOS branding seed uses the same source as the default Web logo and
  favicon.

## Custom branding

No branding API, database, or administration UI behavior changes. A configured
custom logo or favicon still takes precedence over `/logo.png`. Resetting
branding restores the unified built-in icon.

## Verification

1. Assert that the canonical image is the blue-circle 1024×1024 PNG.
2. Assert that `packaging/icon.png` no longer exists.
3. Build the Web frontend and verify its emitted `logo.png` matches the
   canonical asset byte-for-byte.
4. Run relevant branding and packaging tests.
5. Build the macOS ARM64 application and verify its internal version,
   architecture, code signature, and DMG integrity.
6. Visually inspect the canonical image and representative generated icons.

