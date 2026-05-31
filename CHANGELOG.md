# Changelog

All notable changes to ROCORDER are recorded here. Versions follow the scheme
documented in [`CLAUDE.md`](./CLAUDE.md): patch for routine fixes and small
features, minor for visible new features or format additions, major reserved
for breaking changes that need re-recording or reinstalling.

The current version is the same string across `rocorder.lua`
(`ROCORDER_VERSION`), `xeno_loader.lua` (`ROCORDER_LOADER_VERSION`), and the
Blender add-on's `bl_info["version"]` / `ROCORDER_VERSION`.

## 1.3.2-alpha — 2026-05-30

- **Fix**: importing a recording that includes camera data crashed with
  `TypeError: bpy_struct.keyframe_insert() property "angle" not animatable`.
  Blender's `Camera.angle` is a derived property (computed from `lens` +
  sensor size) and isn't directly animatable. We now convert the recorded
  vertical FOV to focal length —
  `f = sensor_height / (2 · tan(fov / 2))` — and keyframe `lens` instead.
  With `sensor_fit = "VERTICAL"` the mapping is exact, so the imported
  camera renders identical to the in-game one.

## 1.3.1-alpha — 2026-05-30

- Indicator overlay redesigned to look like a classic record button:
  a colored ring (UIStroke on a transparent circle) with a smaller solid
  dot in the middle, instead of a single filled blob. Both elements share
  the state color (red recording / white buffering) and the 25% opacity.
  Bumped to 22px so the ring + inner dot have room to read as an icon.

## 1.3.0-alpha — 2026-05-30

UI polish + new on-screen indicator overlay + advanced-settings reveal.

- **Save Instant Replay button** — renamed from "Save Last N Seconds" (which
  was misleading when a recording just started). It now also reads as
  visibly disabled (muted text, muted border, no hover) when Instant Replay
  is off, instead of just being unresponsive.
- **Indicator overlay** — a small dot in a screen corner appears while
  capturing. **Red** while a full recording is in progress, **white** while
  Instant Replay is buffering. Sits at 25% opacity with a thin dark outline
  so it stays visible on bright backgrounds without being obnoxious.
  - New setting **Indicator > Show indicator** (default on).
  - New setting **Indicator > Corner** — TopLeft / TopRight / BottomLeft /
    BottomRight (default TopRight).
- **Advanced settings collapse** — Position Decimals, Rotation Decimals,
  Flush Interval, and Max Catchup are now flagged advanced and hidden by
  default behind a "Show advanced settings ▼" toggle at the bottom of the
  Settings tab. The vital basics (Tick Rate, Max Distance, Include Local,
  Debug, Instant Replay, Indicator, Hotkeys) stay visible up front.
- New setting type **`choice`** — cycling button that walks through a
  fixed list of string values. Used by the new Indicator Corner setting;
  available for any future setting that needs a small enum.

## 1.2.1-alpha — 2026-05-30

Four small fixes from the 1.2.0 screenshots.

- **Fix**: deleting a recording no longer flickers every other row out + back
  in. The Delete handler now destroys just that row optimistically; the list
  only fully re-populates if the last row was deleted (to show the empty
  state).
- **Fix**: the `CLIP` pill is bigger (46×18) and explicitly center-aligned in
  both axes, so the text actually sits in the middle of the pill.
- **Fix**: the "Save Last N Seconds" button now shows the real number from
  `IR_BUFFER_SEC` (e.g. "Save Last 30 Seconds") and re-renders whenever the
  setting changes.
- **Fix**: F7 (default Save-Replay hotkey) now fires. Roblox marks F-keys as
  `processed = true` because the engine consumes them for built-in features,
  and our handler was bailing on `processed`. We now ignore `processed` for
  our hotkeys and instead skip them only when a settings TextBox is focused
  (via `UserInputService:GetFocusedTextBox`), which is the actual case
  worth guarding against.

## 1.2.0-alpha — 2026-05-30

Files-tab metadata, camera source, functional Sources tab. The `.rec` format
identifier stays at `ROCORDER/3` and the change is backward-compatible: pre-1.2
importers silently skip the new `cam:` chunks, and the new importer reads pre-
1.2 recordings unchanged.

- **Files tab** now shows for each recording: filename, **duration**, **date
  recorded**, **game name**, file size, plus a small **CLIP** pill on
  instant-replay clips. Game name comes from `MarketplaceService:GetProductInfo`
  (cached). For pre-1.2 recordings without a `.meta.json` sidecar the date /
  game still appear (read from the header), but duration shows as `?`.
- **Meta sidecar** — every recording (full session OR replay clip) now also
  writes a `<base>.meta.json` next to the `.rec` with the duration / frame
  count / place / size. Tiny; lets the Files tab refresh instantly without
  having to scan the (potentially huge) `.rec` itself.
- **Camera capture source** — new `SRC_CAMERA` toggle records the local
  camera's CFrame + FOV per tick into a `cam:` chunk on each frame line.
  Off by default. The Blender importer detects this and creates a real
  Camera object (`base_name + "_camera"`) animated frame-by-frame with the
  right world transform and `lens_unit=FOV` (vertical FOV in radians, exact
  mapping from Roblox's `FieldOfView`).
- **Sources tab** is now functional: each enabled source gets a real
  ON/OFF toggle bound to settings. Cross-tab sync keeps them consistent
  with anything else that flips them. "Audio events" stays as a `PLANNED`
  pill until that source is built.
- `_G.ROCORDER.cfg.SRC_PLAYER_PARTS` / `SRC_CAMERA` and the corresponding
  `_G.ROCORDER:SetSetting` calls work the same as any other setting.

## 1.1.2-alpha — 2026-05-30

- **Fix**: tab buttons (Record / Settings / Files / Sources) were invisible in
  1.1.1 because the tab-bar's bottom divider was a full-width child of the
  tab bar's `UIListLayout` — it claimed 100% of the horizontal layout row,
  pushing every tab button off the right edge where `ClipsDescendants` hid
  them. The divider now lives on the window directly, positioned just under
  the tab bar, so the tab bar's layout only sees actual tab buttons.

## 1.1.1-alpha — 2026-05-30

UI polish + fixes from the first round of screenshots.

- **Fix**: status panel no longer truncates "Players tracked" at the bottom —
  the panel auto-sizes to fit its content with a proper list layout and a
  thin divider between the status row and the detail rows.
- **Fix**: "Save Last N Seconds" is now a visible outlined button. The old
  ghost style matched the content background and rendered as plain text.
- **Fix**: Start/Stop button no longer snaps back to blue on mouse-leave
  after the status loop has swapped it to red — hover/leave colors live on
  attributes so external code can change a button's role atomically.
- Bumped window to 640×560 with a 28px footer that shows the current
  record / save-replay / toggle-window hotkeys live.
- Tabs get an accent underline indicator under the active one and a smooth
  tweened hover (instead of the old hard color swap).
- Title bar gets a small accent stripe + monospace version label, plus a
  minimize button (collapses the window to just the title bar).
- All button hovers tween rather than snap (TweenService, 120ms).

## 1.1.0-alpha — 2026-05-30

In-game UI and Instant Replay land on the recorder side. The file formats
(`ROCORDER/3`, `ROCORDER-RIG/2`) are unchanged — existing importers keep
working, recordings from 1.0 still import.

- **In-game UI** — a draggable window with four tabs (Record / Settings /
  Files / Sources). Open with `Right Shift` (rebindable). Shows live status:
  recording state, elapsed time, tick count, approximate file size, replay
  buffer fill, tracked-player count.
- **Instant Replay** — when enabled, the recorder continuously buffers the
  last N seconds (default 30s) in memory without writing to disk. Press
  `F7` or the Save button to dump the rolling buffer as a normal `.rec` /
  `.rig.json` clip. Works alongside a normal recording.
- **Settings persistence** — all settings live in `ROCORDER/settings.json`
  and survive reloads. Edit them in the UI or via `_G.ROCORDER:SetSetting`.
  Hotkeys are part of the settings and can be rebound from the UI.
- **Files tab** — lists every `.rec` in the workspace with size, refresh
  button, and a delete button per row (also removes the matching
  `.rig.json` and `.debug.log`).
- **Sources tab** — surface the planned capture-source modules. "Player
  parts" is the only enabled one for now; "Player cameras" (CFrame + FOV)
  and "Audio events" are listed as planned.
- **Internals** — Tracker / Session / Replay are now distinct subsystems so
  one snapshot per tick feeds whichever consumers are active. Adding a new
  capture source in the future means writing a new Source module that
  drops into the same loop without disturbing anything else.

API additions on `_G.ROCORDER`: `OpenUI()`, `CloseUI()`, `ToggleUI()` (via
the hotkey), `SaveReplay([seconds])`, `GetRecordings()`,
`DeleteRecording(name)`, `SetSetting(key, value)`, and a `cfg` table you
can read.

## 1.0.0-alpha — 2026-05-30

First tagged alpha. Project consolidated under a single project version.

Highlights of the work that landed in pre-1.0 development and is shipped here:

- **Recorder (`rocorder.lua`, format `ROCORDER/3`)**
  - Per-part world position + quaternion at 3/5-decimal precision.
  - Uniform complete frames: every tracked part is written every tick, with
    last-value-held during respawn / stream-out so no bone has holes.
  - Rig (`ROCORDER-RIG/2`) captured per player on first sighting and written
    at Stop so late joiners are included.
  - Resilient flush: `appendfile` is `pcall`ed and the buffer is retained on
    failure (fixes the silent-drop gaps that looked like recorder bugs).
  - Debug log (`<rec>.debug.log`) with ensure events, char/part lost-regained
    transitions, heartbeat stalls, and explicit `*** GAP CREATED` events when
    the catchup cap is hit.

- **Importer (Blender add-on, v1.0.0)**
  - One armature + one skinned mesh per player, each part weighted 1.0 to its
    bone via an Armature modifier. No Child Of constraints.
  - Canonical rest pose derived from Motor6D `C0/C1` (standard R6 defaults
    optionally substituted to neutralize runtime mutations like aim-rotation).
  - Self-loop Motor6Ds skipped so the bone hierarchy can't collapse to identity.
  - Leaf bones extend from joint through part center; bone roll aligned with
    each part's canonical local Z so widgets sit flat on the meshes.
  - Debug log (`<rec>.import.log`) with rig structure, bones requested vs
    created, mesh skip reasons, per-bone keyframe counts, gap detection.
