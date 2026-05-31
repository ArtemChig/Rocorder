# Changelog

All notable changes to ROCORDER are recorded here. Versions follow the scheme
documented in [`CLAUDE.md`](./CLAUDE.md): patch for routine fixes and small
features, minor for visible new features or format additions, major reserved
for breaking changes that need re-recording or reinstalling.

The current version is the same string across `rocorder.lua`
(`ROCORDER_VERSION`), `xeno_loader.lua` (`ROCORDER_LOADER_VERSION`), and the
Blender add-on's `bl_info["version"]` / `ROCORDER_VERSION`.

## 1.8.0-alpha — 2026-05-31

Pivot from "ask the CDN for the file" to "ask the engine for the geometry it
already loaded." This is the architectural fix for the 21/44-failed wall —
EditableMesh / EditableImage work for assets the CDN refuses because the
engine ALREADY has the bytes parsed in memory. Recorder side this commit;
Blender import support next.

- **In-engine asset extraction.** New `extractMeshFromPart()` uses
  `AssetService:CreateEditableMeshAsync(part.MeshContent)` to pull verts /
  uvs / normals / faces straight from the engine. Works for any MeshPart
  currently in the workspace, regardless of asset permission gating, because
  Roblox grants `EditableMesh` access whenever the part is actively rendering.
- **Image extraction.** `extractImageFromContent()` uses
  `AssetService:CreateEditableImageAsync()` + `ReadPixelsBuffer` to dump raw
  RGBA8 bytes. No PNG encode (we'd need a library); the Blender side reads
  the small text header (`ROCORDER-RGBA8\n<w>\n<h>\n` + bytes) and converts.
- **Storage**: `ROCORDER/assets/<id>.geom.json` (geometry, JSON) and
  `<id>.rgba` (raw pixels). Hash-keyed by asset ID so the same accessory
  shared across players is extracted once. Survives across recordings.
- **Background extraction.** Triggered from `Tracker:ensure` the first time
  a player is seen — runs in a `task.spawn` coroutine with `task.wait()`
  between assets so the game stays smooth and extractions survive Stop.
- **HTTP downloader stays as fallback.** Anything the extractor couldn't
  get (extremely rare — the API really does allow restricted-UGC access for
  parts you can see) falls through to the existing v1/v2/cdn HTTP chain.
  The downloader now skips assets the extractor already cached, so the
  failure tally only counts the assets that truly can't be reached.
- **Importer support for `.geom.json` / `.rgba` is NOT in this commit** —
  it's the next one. For now the assets/ folder will fill with files
  Blender can't yet read; full pipeline lands in 1.8.1 or 1.9.0.

## 1.7.2-alpha — 2026-05-31

Three real bugs caught from a single debug log.

- **Fix: dual asset download.** The log showed `ASSET DOWNLOAD start` twice
  with two different message formats (one 1.7.0 wording, one 1.7.1) and
  every asset downloaded twice. A re-loaded recorder's reload-guard
  `Stop()` was racing the user's F8 Stop, both kicking off the asset
  coroutine in parallel. `_downloadAssets` now uses a `_G` flag as a
  concurrency guard and refuses to start while one is already running.
- **Fix: `DIAG:` was invisible on failure.** The 1.7.1 diagnostic was
  nested inside the success branch, so when `getcustomasset` returned
  `nil` / wrong type / errored, the log showed nothing — defeating the
  whole point. The DIAG now ALWAYS prints on the first call (success,
  empty, wrong-type, or `pcall` error) so we can finally see what the
  executor does. A `SUCCESS via ... readfile('...')` line also prints
  when a candidate path works, so the winning shape is auditable.
- **Reload-guard visibility.** The reload guard now prints which
  previous-version instance it tore down, so "did the new version load?"
  is answerable from the system console without guessing.

Patch bump 1.7.1 → 1.7.2-alpha.

## 1.7.1-alpha — 2026-05-31

1.7.0 added `getcustomasset` support but the next log showed `via
getcustomasset` never appearing — Xeno returns a path my `readfile` shapes
didn't match, and I was guessing without proof. This release stops guessing.

- **Diagnose what `getcustomasset` actually returns.** The recorder now logs
  the FIRST `getcustomasset`/`getsynasset` return value verbatim (`DIAG: ...`)
  so we can see exactly what path Xeno hands back, and tries every common
  shape (`rbxasset://...` as-is, prefix-stripped, `asset_<id>.bin`, bare id)
  before giving up.
- **Probe more readers.** Added `readasset` and an `Xeno.getAssetBytes`
  hook; the startup line now reports availability of every reader name so
  we know what this executor exposes.

Patch bump 1.7.0 → 1.7.1-alpha (diagnostics + extra probes, same external
behavior).

## 1.7.0-alpha — 2026-05-31

The 1.6.3 log showed the validator + 4-endpoint fallback got 13/25 assets
saved — but `via contentProvider` never appeared in the log, meaning the
in-memory path wasn't actually being used. Also showed `getcustomasset=yes`
on Xeno. Two real changes:

- **In-memory asset fetch actually works now.** The recorder used to call
  `getcustomasset` and try `readfile()` on the returned path verbatim, which
  failed silently. It now (a) tries `readcustomasset` first (a direct
  bytes-getter some forks expose), then (b) tries `getcustomasset`/`getsynasset`
  and feeds the returned path to `readfile` with the `rbxasset://` prefix
  stripped (which is the executor-workspace path). Every success logs
  `via getcustomasset` / `via readcustomasset` so the path is auditable.
- **Manual-drop workflow for unfetchable assets.** When the executor *and*
  every HTTP endpoint still 401, those assets are genuinely beyond the
  recorder's reach (the asset's permissions exclude even the player). The
  recorder now writes `ROCORDER/assets/_missing.txt` listing every such ID.
  Drop a file named exactly `<id>` (or `<id>.mesh` / `<id>.png`) into
  `ROCORDER/assets/` — the importer's local-first lookup picks it up
  automatically. The importer's first log section now echoes the missing-IDs
  list at the top so it's obvious what's outstanding.

Minor bump per CLAUDE.md (new fetch path + new manifest file + new manual
workflow).

## 1.6.3-alpha — 2026-05-31

Logs from 1.6.2 confirmed the validator works (30 error-page bodies caught
and rejected instead of saved-as-meshes) but also showed the executor's
plain HTTP gets 401 on most modern UGC. So we now go through the path the
*client* uses — the asset bytes are already in memory, the CDN URL just
won't serve them on a raw GET.

- **ContentProvider / `getcustomasset` first.** Before any HTTP, the
  recorder tries the executor's `getcustomasset` (Xeno, Synapse, etc.),
  which returns the asset the client already has loaded. This works for
  any asset currently visible in-game — including UGC the CDN refuses.
- **Multi-endpoint HTTP fallback with proper headers.** If the in-memory
  copy isn't accessible, the HTTP fallback now sends a `User-Agent` +
  `Roblox-Place-Id` (modern Roblox CDN checks these) and walks v1 → v2 →
  `c0.rbxcdn.com` → `t0.rbxcdn.com` instead of giving up on one URL.
- **Source logging.** Each saved asset records which path succeeded
  (`contentProvider`, `assetdelivery.roblox.com`, `rbxcdn.com`, `HttpGet`)
  and each failure lists the status per endpoint, so the next log makes
  it unambiguous where the wall is. The startup line also reports
  `getcustomasset=yes/no` so we know whether the in-memory path is
  available on your executor at all.

## 1.6.2-alpha — 2026-05-31

Two bugs found from field logs — both made characters import wrong.

- **Fix: error pages saved as assets.** When `syn.request` returned 401 for a
  restricted asset, the recorder fell back to `game:HttpGet`, which returns the
  401 error *text* as a normal string — and the recorder saved that as the mesh
  file. The importer then "found" the local file, saw it wasn't a mesh, and
  *silently* fell back to a box (this is why girly mesh limbs / held items came
  in as boxes). The recorder now **validates** downloaded bytes (real meshes
  start with `version `, images have a known magic) and rejects error pages,
  and it **re-validates already-cached files** so the garbage saved by 1.6.0/
  1.6.1 gets replaced. The importer now **logs** a non-mesh local file instead
  of silently boxing it.
- **Fix: stale players in the rig.** The tracker persists across recordings
  (for Instant Replay), so a recording's `.rig.json` could include a player
  from an earlier session who has no frames in this `.rec` — importing as a
  frozen pile of boxes at the origin. The rig is now filtered to players
  actually seen during this recording (and clips to players in the buffered
  window).

After updating: re-record so the recorder re-downloads the real meshes (it
self-heals the stale `ROCORDER/assets` files), then re-import.

## 1.6.1-alpha — 2026-05-31

Executor asset download confirmed working (0 auth fails, all assets fetched
locally). Follow-up mesh-parser fix:

- **Fix: v3 mesh parsing** — the v3 header has a `sizeof_LOD` `u16` field that
  the parser skipped, so `numVerts`/`numFaces` were read from the wrong offsets
  and real v3 meshes blew past the buffer (`unpack_from requires a buffer of
  at least …`) and fell back to a box. Header now read correctly (16 bytes:
  sizeof, cbVertex, cbFace, sizeof_LOD, numLODs, numVerts, numFaces).
- LOD face-slicing in v3 and v4 is now range-checked before use, so a bad LOD
  table can't truncate or corrupt the face list.

## 1.6.0-alpha — 2026-05-31

Executor-side asset download — the real fix for "everything is a box". Import
logs proved that Roblox's CDN returns **401 even with a valid cookie** in
Blender (it gates raw downloads to genuine client sessions). So the executor,
which already has these assets loaded in an authenticated session, now
downloads them itself.

- **Recorder: Download Assets** (new Capture setting, default on). At Stop (and
  after saving an instant-replay clip) the recorder collects every mesh /
  texture / color-map / decal / clothing id the characters use and downloads
  them into `ROCORDER/assets/<id>` using the executor's HTTP (`syn.request` /
  `http.request` / `request` / … with a `game:HttpGet` fallback). Runs in a
  coroutine so it never hitches the game; progress is shown via notifications
  and logged to the `.debug.log`.
- **Importer: local assets first.** The importer now looks for the recorder's
  `ROCORDER/assets/` folder next to the `.rec` and uses those files directly —
  no network, no 401. It still falls back to anonymous + v2 + cookie download
  for anything not pre-downloaded. The asset summary now reports
  `local / downloaded / cache / fails`.
- **Better 401 diagnosis** — the importer logs the actual 401 response body
  once (so we can tell "auth required" from "no permission"), and the
  end-of-import guidance now points at the recorder's Download Assets option
  rather than the cookie.

Workflow: re-record with Download Assets on, then import the `.rec` from inside
`ROCORDER/` (so `assets/` sits beside it). Keep that folder with the `.rec` if
you move it.

## 1.5.1-alpha — 2026-05-31

Diagnosed from import logs: modern Roblox assets return **401 Unauthorized**
to anonymous downloads (only old/public assets serve without auth), and a
half-loaded character could be captured with no joints.

- **Asset auth** — the importer no longer retries 3× on a 401/403 (pointless,
  and it spammed the log). On an auth failure it now tries the authenticated
  **v2 CDN-location** flow, and if assets still fail it prints a clear
  one-line instruction (and a Blender popup) to paste your `.ROBLOSECURITY`
  cookie and re-import. Old/public assets still download without a cookie.
  The asset summary now reports an explicit `auth/401` count.
- **Fix: half-loaded rig** — the recorder now waits until a character actually
  has its Motor6D joints (with a 2s grace fallback) before capturing its rig.
  Previously a player seen mid-spawn could be captured with `joints=0`, which
  made every part pile at the origin. This also removes the early
  "got N parts" frames from such players.
- The "wrong part count" import-log line is reworded — parts that appear later
  (e.g. a tool you equip mid-recording) are normal, not corruption.

If a mesh/texture is a box after this, check the `.import.log`: `class=Part`
means it's a classic block (correct — needs clothing textures, still WIP);
`auth/401` means provide your cookie; a real parse error is logged per asset.

## 1.5.0-alpha — 2026-05-31

Separate per-part objects, classic faces/decals, and clothing capture —
addressing the "merged mesh / cube heads / missing faces" feedback. Formats
unchanged (additive rig fields only).

- **No more merged mesh** — each body part, accessory, hat, and tool piece is
  now its **own selectable object**, organized into `<player>_Body` and
  `<player>_Accessories` sub-collections. Every object is still skinned 100%
  to its bone via its own Armature modifier, so animation is identical — you
  just get full control to select / hide / edit / delete each piece.
- **Faces & decals** — the recorder now captures `Decal`/`Texture` instances
  on parts (the classic **face** lives here, plus logos and surface images).
  The importer applies a part's decal to the matching box face (mapped from the
  decal's Roblox `Face`), so classic heads finally show a face instead of a
  bare cube.
- **Clothing capture** — `Shirt` / `Pants` / `ShirtGraphic` templates and
  `SpecialMesh.MeshType` are now recorded (and logged). Applying classic
  shirt/pants *wrapping* needs the classic body-UV template and is the next
  step; for now they're captured + reported in the import log so the data's
  there.
- **Diagnostics** — the import log now prints, per part, `class=` (Part vs
  MeshPart), `shape=`, `meshType=`, and whether it has a mesh / texture /
  colorMap / decals, plus a per-player `meshes / boxes / box+decal` tally and
  any clothing found. This makes it obvious whether a "blocky" body is a
  classic `Part` (blocks are correct — needs clothing textures) or a
  `MeshPart` whose mesh failed to download (a real bug to chase).

Note for classic R6 avatars: the body/head ARE blocks in-game; their detail
comes from shirt/pants/face textures, not geometry. Heads now get their face;
full shirt/pants wrapping is the remaining piece.

## 1.4.0-alpha — 2026-05-31

Real meshes + textures + accessories/tools. This is the big one: the Blender
scene now reflects what you actually see in-game, not colored boxes. Formats
stay `ROCORDER/3` / `ROCORDER-RIG/2` (rig gains backward-compatible fields), so
older recordings still import and pre-1.4 importers still read 1.4 recordings.

- **Whole-character capture (recorder)** — the recorder now deep-scans the
  entire character, not just direct body parts. Accessories, hats, and
  **held tools / equipped items** are all captured and animated. Each part
  keeps a live Instance reference (so duplicate-named accessory parts resolve
  correctly), and a throttled re-scan picks up items equipped mid-recording.
  For every part it records `MeshId`, `TextureID`, `SurfaceAppearance.ColorMap`,
  and legacy `SpecialMesh` MeshId/TextureId/Scale.
- **Mesh + texture import (importer)** — new **"Import meshes & textures"**
  option (default on). For each part the importer downloads the real Roblox
  mesh and texture from the CDN, builds proper UV-mapped geometry scaled to the
  part's size, and binds it to the bone exactly like the box version. Works for
  any game — it's all driven by the asset IDs in the recording.
  - Supports Roblox mesh formats v1.x (text), v2.x, v3.x; v4+/skinned are
    best-effort. Anything that can't be fetched or parsed falls back to a box
    and is logged.
  - Assets are cached on disk (`rocorder_assets/` next to the .rec by default,
    or a folder you choose) so re-imports are instant and assets shared across
    players download once.
  - Optional **.ROBLOSECURITY** field for gated assets (blank by default;
    public assets cover almost everything). Stored as a password field with a
    security note.
- **Diagnostics** — the import log now reports per-player real-mesh / box /
  textured counts, every asset fetch, mesh versions encountered, and
  download / cache-hit / fail tallies, so mismatches are easy to trace.

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
