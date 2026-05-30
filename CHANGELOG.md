# Changelog

All notable changes to ROCORDER are recorded here. Versions follow the scheme
documented in [`CLAUDE.md`](./CLAUDE.md): patch for routine fixes and small
features, minor for visible new features or format additions, major reserved
for breaking changes that need re-recording or reinstalling.

The current version is the same string across `rocorder.lua`
(`ROCORDER_VERSION`), `xeno_loader.lua` (`ROCORDER_LOADER_VERSION`), and the
Blender add-on's `bl_info["version"]` / `ROCORDER_VERSION`.

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
