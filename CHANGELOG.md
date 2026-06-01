# Changelog

All notable changes to ROCORDER are recorded here. Versions follow the scheme
documented in [`CLAUDE.md`](./CLAUDE.md): patch for routine fixes and small
features, minor for visible new features or format additions, major reserved
for breaking changes that need re-recording or reinstalling.

The current version is the same string across `rocorder.lua`
(`ROCORDER_VERSION`), `xeno_loader.lua` (`ROCORDER_LOADER_VERSION`), and the
Blender add-on's `bl_info["version"]` / `ROCORDER_VERSION`.

## 1.17.0-alpha — 2026-06-02

**Texture alpha now blends OVER the body colour instead of cutting through
to nothing.** Matches Roblox's in-game rendering: a shirt PNG with a
transparent background shows the player's skin in the transparent regions,
a face decal shows the head's skin around the eyes/mouth, an accessory with
partial alpha shows the accessory's flat colour beneath. Before, the alpha
was wired straight to BSDF Alpha and blend mode HASHED — so you'd see
straight *through* the avatar wherever the texture wasn't fully opaque.

- `_image_material` now builds a Mix RGB node:
  - Color1 = part body colour (baked in)
  - Color2 = image-texture colour
  - Factor = image-texture alpha
  - Output → Principled BSDF Base Color
  - Material itself stays opaque.
- Roblox's actual see-through (`Part.Transparency`) is still applied
  separately when >0 — via BSDF Alpha + HASHED blend mode — so a literally
  semi-transparent part still renders see-through correctly. The two
  concepts (texture alpha = mix factor vs. part transparency = real
  see-through) are no longer conflated.
- Textures without an alpha channel are unchanged (texture colour goes
  straight to Base Color).
- Material cache key now includes the body colour and the transparency
  level, so two parts using the same texture but different skin colours
  get distinct materials (the colour is baked into the Mix node, so they
  can't share).

Importer-only change. Reinstall the add-on and re-import.

## 1.16.0-alpha — 2026-06-02

**Shirt/Pants now wrap a CharacterMesh body** the same way they wrap the
standard R6 box body — without the 1.15.0 splatter.

The right fix isn't to substitute Shirt/Pants on top of the mesh's own UVs
(those are sculpted-anatomical for the modeler's BaseTexture and don't
follow the R6 template), but to **overwrite** the mesh's UVs with a cube
projection into the R6 clothing template — exactly what `_build_clothed_box`
does for a real box, generalised for sculpted geometry.

- New helper `_r6_cube_project_clothing_uvs(bm, uv_layer, regions)`: for
  every face in the mesh, picks the dominant template face from the face
  normal (`Front`/`Back`/`Left`/`Right`/`Top`/`Bottom`), then projects all
  three corners into that template cell using the same `_FACE_AXES`
  u/v axes and the mesh's own bounding box as the normalization range.
  Vertices outside the box on any axis are clamped to the cell edge.
- `_add_mesh_geometry` accepts an optional `r6_clothing_regions` and applies
  the projection right after the mesh-authored UVs, before `place_mat` —
  projection has to run in part-local space, not rest pose.
- `_build_part_object` turns this on for a part with `charMesh=True`,
  classic clothing enabled, and a Torso/arm/leg name. Texture used is the
  Shirt (torso+arms) / Pants (legs) directly; the CharacterMesh's
  `BaseTextureId` is not blended in (single-material setup). Without
  clothing on the player, CharacterMesh body parts still render with their
  authored UVs and `BaseTextureId` (1.15.3 path unchanged).
- New build kind `mesh-clothed` so the per-player stats split out
  cube-projected meshes from plain ones.

Texture orientation per face inherits the table we tuned for the box
clothing in 1.14.2 — so if any single face still reads mirrored after the
shirt/pants come through, it's the same one-line `_FACE_AXES` flip as the
box case.

## 1.15.3-alpha — 2026-06-02

Two CharacterMesh rendering bugs the user spotted from the 1.15.2 import
(both confirmed by inspecting the geom files): bodies were squashed/blocky
and the shirt/pants textures splattered across the body wrong.

- **Render CharacterMesh meshes at authored size (no bbox auto-fit).** The
  importer was sending CharacterMesh through the MeshPart branch, which
  stretches the mesh's bbox to match the BasePart's size. But CharacterMesh
  meshes are sculpted at anatomical proportions (e.g. the captured torso
  mesh bbox is ~1.33×1.85×0.84 while the BasePart is the standard R6
  2×2×1) and Roblox renders them **as-is at the part's CFrame**, not
  bbox-stretched. The mismatched stretch was distorting every body part
  (torso wider, arms squished, legs stretched tall+thin) — exactly the
  blocky/squashed look in the user's screenshot. Now CharacterMesh and
  FileMesh both go through the `meshScale=[1,1,1]` path → meshes render at
  the size they were sculpted.

- **Don't substitute Shirt/Pants on CharacterMesh body parts.** 1.15.0
  preferred the player's Shirt/Pants texture over the CharacterMesh's
  `BaseTextureId` on body parts. That worked for the *old* "official"
  CharacterMeshes (whose UVs happen to follow the R6 clothing template),
  but game-authored CharacterMeshes (Violence District etc.) have
  **sculpted-anatomical UVs that don't match the clothing template** —
  painting the shirt across that layout splatters it nonsensically (the
  texture screenshot the user shared). CharacterMesh parts now use their
  own authored texture: `BaseTextureId` if present, otherwise plain
  colour. The classic clothing-box wrapping still applies for *plain Block*
  body parts (no CharacterMesh) — unchanged.

These are importer-only changes; the rig.json from 1.15.2 already has the
CharacterMesh data needed. Just reinstall the add-on and re-import.

## 1.15.2-alpha — 2026-06-02

**1.15.0 CharacterMesh capture didn't survive the throttled rescan.** The
log said "applied N CharacterMesh override(s)" at capture, but the saved
`rig.json` had every body part back as `shape=Block` with no `meshId` —
boxes on import. Diagnosed from a 1.15.0 recording where `characterMeshes:
3` was recorded but every body part was `shape=Block, meshId=None`.

Root cause: `Tracker:_rescanExistingAssets` runs ~1 Hz and rebuilds each
part record via `partInfo(inst, name)`. On a Block body part with a
CharacterMesh override, `partInfo()` returns no mesh (it only looks at the
BasePart + its SpecialMesh children — CharacterMesh is a sibling Instance
on the Character, not a child of the part). The rebuilt record replaced
the one captureRig had populated → override silently erased every second.

Fix:
- Extracted CharacterMesh discovery / apply into module-local helpers
  (`collectCharacterMeshOverrides`, `applyCharacterMeshOverride`).
- `_rescanExistingAssets` now re-collects overrides each tick and
  re-applies them to the fresh `partInfo` before deciding whether the
  fingerprint changed. So the override persists across rescans AND a
  mid-game add/swap of a CharacterMesh is picked up (same way mid-game
  clothing changes are caught).

Re-record to pick up the data (the rig.json from 1.15.0 still has the
override stripped).

## 1.15.1-alpha — 2026-06-02

- BACKLOG.md: added a P2 cluster covering Files-tab improvements (sort
  recordings, "show in folder" navigation, asset-cache clearing) and the
  cleaner-on-disk layout it enables (human-friendly filenames including
  date / time / game name; per-recording subfolder so the 4 sidecar files
  group together; shared `assets/` stays content-addressed; importer
  backward-compat for the existing `replay_<id>_<ts>` flat layout). No
  code change.

## 1.15.0-alpha — 2026-06-01

**Capture `CharacterMesh` overrides** — fixes the "arms/legs are wrong
shape" import bug in Violence District and any other game that replaces
blocky R6 body parts with sculpted meshes.

Background. Roblox's `CharacterMesh` instance is the canonical way to
replace an R6 body part's appearance: it's parented to the Character model,
targets a `BodyPart` enum (Head / Torso / LeftArm / RightArm / LeftLeg /
RightLeg), and supplies its own `MeshId` + `BaseTextureId` +
`OverlayTextureId`. We were only scanning `BaseParts` for mesh data, so the
override mesh was invisible to us and the original blocky boxes is what
imported.

Recorder:
- `captureRig` now scans the Character for `CharacterMesh` instances and
  records `{MeshId, BaseTextureId, OverlayTextureId}` per body part. The
  matching `rig.parts[i]` gets `meshId` overridden, `shape="CharacterMesh"`,
  `charMesh=true`, and `textureId`/`overlayTexture` filled in (the
  CharacterMesh's MeshId wins for that body part).
- `extractMeshFromPart` now takes an optional `partInfoRef` and falls back to
  extracting the mesh by content URL when the part itself has no mesh (so
  CharacterMesh meshes — which aren't attached to a single BasePart —
  extract via the normal queue).
- `enqueuePartAssets` also enqueues `overlayTexture` now.
- Logged at capture: `applied N CharacterMesh override(s) for <player>`.

Importer:
- A part with `shape="CharacterMesh"` flows through the MeshPart
  auto-fit branch (everything except `"FileMesh"`), which fits the mesh's
  bbox to the body part's size — exactly right since CharacterMesh meshes
  are sculpted at standard R6 body part dimensions (Torso 2×2×1, etc.).
- Texture priority for classic-R6 body parts (Torso / arms / legs): if
  the player has a Shirt/Pants AND classic clothing is enabled, that
  texture wins over `textureId` (= the CharacterMesh's `BaseTextureId`).
  CharacterMesh meshes have UVs matching the R6 clothing template, so
  shirt/pants applied as the texture wraps the sculpted body correctly,
  same as in-game.

Architecturally: this is also a more general fix for "body mesh data lost
during recording" — the asset-fetch path now reads mesh content from any
source (BasePart MeshPart / SpecialMesh / CharacterMesh override) instead
of being tied to a single part type, so future game-specific override
mechanisms (e.g. WrapDeformer) will be easier to add (same hook).

Requires re-record to pick up CharacterMesh data; the importer side is
backward compatible.

## 1.14.2-alpha — 2026-06-01

- **Classic-clothing region layout corrected (cell spacing).** 1.14.1 packed
  cells edge-to-edge, but the user's measurement of right-arm F (top-left
  217, bottom-right 280, 482) showed adjacent cells are separated by a **2-px
  gap** in the template — not zero. So spacing from one cell's top-left to
  the next is 66 px (= 64 cell + 2 gap), not 64. With L=19, this puts the
  cells at 19, 85, 151, 217 ✓ — matching the user's corrected reading. All
  18 rects rebuilt accordingly (anchors unchanged; cell sizes stay at the
  Roblox-standard 64×128 / 64×64 / 128×128 / 128×64).

## 1.14.1-alpha — 2026-06-01

- **Exact classic-clothing template coordinates.** 1.14.0's region rects were
  eyeballed from the guide image and landed a few px off (visible UV
  misalignment). Replaced with exact values computed from precise corner
  reads off the official template + the 64px grid: torso FRONT (231,74),
  right-limb L (19,355), left-limb F (308,355). All 18 face rects now sit on
  the true cell boundaries, so the shirt/pants line up with the design.
  (Per-face orientation unchanged — flip an entry in `_FACE_AXES` if a single
  face still reads mirrored.)

## 1.14.0-alpha — 2026-06-01

**Classic R6 2D clothing (Shirt / Pants) now wraps onto the box body** — the
last piece for fully-correct imports. The 3D mesh clothing already worked;
this fills in classic-body avatars.

- A classic Block body part (Torso / Left+Right Arm / Left+Right Leg) with no
  mesh, on a player wearing a Shirt/Pants, is now built as a box whose six
  faces are UV-mapped into the **585×559 classic template** layout, with the
  shirt texture on the torso+arms and the pants texture on the legs — so the
  clothing wraps like in-game instead of showing flat color.
- Template region coordinates were read directly off Roblox's official
  shirt/pants template guide (the `R·FRONT·L·BACK` torso cross with `UP`/`DOWN`,
  and the two limb crosses `L·B·R·F` / `F·L·B·R` with `U`/`D`). 64 px/stud,
  so torso front/back are 128×128, sides 64×128, caps 128×64; limb sides
  64×128, caps 64×64. The rects + per-face orientation live in clearly-named
  tables (`_TORSO_RECTS`, `_RIGHT_LIMB_RECTS`, `_LEFT_LIMB_RECTS`, `_FACE_AXES`)
  so any single mirrored/rotated face is a one-line tweak.
- New import option **"Apply classic clothing (Shirt/Pants)"** (default on).
- The recorder already captures `clothing` (shirt/pants/tshirt) and extracts
  the templates to `.rgba`, so no re-record is needed — just reinstall the
  add-on and re-import.

T-shirt (ShirtGraphic) overlay on the torso front is not yet applied (it's a
separate decal layer); noted for later. Head clothing N/A (head uses its face
decal).

## 1.13.1-alpha — 2026-06-01

The 1.12.2 mesh-cache migration **deleted nothing** — diagnosed from
KkielPv: their MeshPart bodies were still GEOM/1 (zero UVs → all UVs
collapsed to one corner in Blender) while their FileMesh clothing (a brand-
new asset) extracted fine as GEOM/2. Of 269 geom files, 181 were still the
broken GEOM/1.

Root cause: the migration listed files with `listfiles` and deleted via
those paths, but Xeno's `listfiles` returns paths in a format `delfile`
silently rejects (pcall swallowed the failure) — so every delete no-opped
while the marker still got written, permanently skipping the migration.

Fix — self-healing per-asset re-extraction:
- New `_geomStaleV1(id)` reads a cached mesh geom's header (via the
  canonical `_geomPath`, the format `delfile`/`readfile` actually accept;
  result remembered per id so each file is read at most once per session)
  and reports whether it's the broken GEOM/1.
- At enqueue, a mesh whose cached geom is stale GEOM/1 is deleted and
  re-extracted as GEOM/2 with correct UVs — instead of being skipped as
  "already cached". Logged as `mesh X was stale GEOM/1 … re-extracting`.
- Removed the dead listfiles-based migration block.
- Importer now logs a clear warning if it still loads a GEOM/1 file
  (`ZERO UVs — stale pre-1.12 file; re-record …`), so any remaining flat
  mesh is obvious instead of silent.

So: re-execute the loader and record with the avatars you care about — each
mesh present in that recording gets its UVs fixed on the spot. (Meshes not
in the recording stay GEOM/1 until a recording includes them — self-heals on
demand, no full-cache wipe.)

## 1.13.0-alpha — 2026-06-01

UVs are confirmed working (the API probe logs `GetFaceUVs=yes` and meshes now
extract with proper `[0..1]` UV ranges). This release captures the clothing
that was missing entirely.

- **Capture game-attached external 3D clothing/cosmetics.** Some games weld
  3D clothing (uniforms, costumes) onto a player but parent those MeshParts
  **outside** the player's `Character` — so the rig capture, which only
  scanned `Character` descendants, never saw them and they were absent from
  the import (not a failure in any log — just never recorded). `captureRig`
  now also walks `HumanoidRootPart:GetConnectedParts(true)` to find the whole
  rigidly-welded assembly and captures parts that are: not already the
  player's, not another player's body, and not anchored (so the welded map
  can't be pulled in), capped at 200. These import as extra root parts
  animated by their recorded world CFrame, same as the avatar's own
  MeshParts. Logged as `captured N externally-welded part(s)`. **Re-record
  to pick up this clothing.**
- New backward-compatible `externalParts` count on the rig (informational).

Note: external clothing attached *after* spawn (vs at spawn) is captured at
initial track + on respawn. If a game equips it well after spawn and it's
still missed, periodic external re-scan is the follow-up.

## 1.12.2-alpha — 2026-06-01

The 1.12.0 UV fix never actually ran, because the extractor caches assets by
file existence: every mesh was already on disk as a pre-fix GEOM/1 file (all
UVs zero), so `_isCached` skipped re-extracting it. Result: re-recording kept
reusing the broken files and textures still looked flat.

- **One-time mesh-cache migration.** On load, if the marker `assets/.geom_v2`
  is absent, every existing `.geom.json` is deleted (they're all pre-1.12
  zero-UV GEOM/1) and the marker is written. They then re-extract as GEOM/2
  with correct per-corner UVs on your next recording. Only filenames are
  listed (no file contents read), so it's fast. `.rgba` textures and bare
  files are untouched. **Just re-execute the loader and record again.**
- **EditableMesh API probe** logged once per session (console + debug log):
  `EditableMesh API probe: GetFaceUVs=yes GetUVs=… GetUV=yes …`. Tells us
  exactly which UV accessors this Roblox build exposes.
- **Per-mesh UV-range log**: `geom: N verts, M tris, UV range [0.000..0.973]`
  — or `… UV range [0.000..0.000]  *** UVs FLAT — extraction failed` if the
  UVs still don't come through. So the debug log now proves whether UV
  extraction worked, no guessing.

If after re-recording the debug log shows a non-flat UV range, the meshes will
texture correctly in Blender. (The classic R6 box body — Torso/arms/legs — is
genuinely primitive blocks in-engine, occluded in-game by mesh clothing;
texturing/handling those is the separate classic-R6 wrapping item in
BACKLOG.md.)

## 1.12.1-alpha — 2026-06-01

- Added `BACKLOG.md` to track planned/deferred features, and a pointer to it
  from `CLAUDE.md`. Seeded with: classic-R6 Shirt/Pants wrapping (P1), the
  exact classic-head mesh to replace the sphere approximation (P2), and a few
  noticed limitations (enclosed-body hiding, full rig-revision importer
  support, other classic primitives). No code/behavior change.

## 1.12.0-alpha — 2026-06-01

Fixes the two confirmed importer/extraction bugs from the in-game-vs-Blender
comparison: collapsed UVs (textures showed as flat color) and the classic
head rendering as a cube.

- **Mesh UVs were all (0,0) — FIXED (requires re-record).** The recorder's
  `extractMeshFromPart` called `em:GetUV(vertexId)`, but in the stable
  EditableMesh API `GetUV` takes a *UV id*, not a vertex id — UV ids are
  reached per-face via `GetFaceUVs(faceId)`. So every UV fell back to (0,0)
  and every face sampled one texel → solid flat color on every extracted
  mesh. Extraction now reads UVs correctly per face corner and writes a new
  **`ROCORDER-GEOM/2`** geom format (`verts` + `faces` + per-corner
  `faceUVs`). **You must re-record** — geom files already on disk are
  GEOM/1 with the zero UVs baked in.
- **Importer reads GEOM/2** (per-face-corner UVs, applied to bmesh loops) and
  still reads GEOM/1 / binary meshes (per-vertex UVs) for older recordings.
- **Classic head no longer a cube.** A classic Head is a Block part with a
  SpecialMesh `MeshType=Head` (no mesh id), so the importer fell back to a
  box. It now builds a size-fitted sphere and projects the face decal onto
  the front hemisphere — much closer to the in-game rounded head.

Known remaining (next): classic-R6 Shirt/Pants are extracted (`.rgba`) but
not yet wrapped onto the box Torso/arms/legs via the R6 UV template, so those
base-body boxes still show flat-colored under any layered-clothing meshes.
That's the last piece for full fidelity on classic-body avatars.

## 1.11.0-alpha — 2026-06-01

**The Blender importer now consumes the engine-extracted assets** the recorder
has been producing — `.geom.json` meshes and `.rgba` textures. This is what
turns the previously-unfetchable (CDN-401) UGC into real geometry and real
textures in Blender instead of grey boxes.

- **`<id>.geom.json` → mesh.** `AssetFetcher.get_mesh` now checks for the
  recorder's `.geom.json` (flat verts/uvs/normals/faces from EditableMesh)
  before anything else and parses it straight into the importer's mesh
  structure. No Roblox binary-mesh parsing, no CDN, no auth — and it's
  present for assets the CDN refuses. Falls back to the bare-file / CDN
  binary path when no `.geom.json` exists.
- **`<id>.rgba` → texture.** `AssetFetcher.get_image_path` now detects the
  recorder's raw-RGBA8 extraction (header `ROCORDER-RGBA8\n<w>\n<h>\n` +
  pixels), converts it to a PNG in the asset cache once (numpy-accelerated,
  vertical-flipped from Roblox top-origin to Blender bottom-origin), and
  feeds that PNG to the normal image-material path. Cached PNGs are reused
  on subsequent imports.
- **Import log** now breaks assets down as `N geom.json + N rgba
  (engine-extracted), N bare-local, N downloaded, …` so you can see how much
  came from the engine vs the network.
- Bare HTTP-fallback files (`<id>`) and the CDN path still work unchanged;
  the typed extraction files are simply preferred when present.

Still pending: classic-R6 Shirt/Pants templates are now extracted to
`.rgba`, but wrapping them onto the box body via the standard R6 UV layout
isn't applied yet — classic bodies render flat-colored. MeshPart bodies and
accessories (with real UVs) are textured correctly.

## 1.10.0-alpha — 2026-06-01

**New: per-player include / exclude filter with live Roblox avatar icons.**

A "Players" panel on the Record tab shows everyone currently in the server,
each as a row with their **Roblox avatar headshot** (the circular thumbnail,
loaded via `rbxthumb://`), display name, and a status chip you can tap.

- **Tap a player to cycle their state**: default → *include* → *exclude* →
  default.
- **Rule** (as requested): if ANY player is set to *include*, only included
  players are recorded — everyone else is paused. Otherwise everyone is
  recorded except those set to *exclude*. So "record only my friend" =
  tap the friend once (→ INCLUDED) and everyone else auto-pauses.
- **Instantly readable**: recorded players show a bright row + blue **REC**
  chip (or green **INCLUDED**); paused/excluded players are dimmed with a
  grey **paused** / red **EXCLUDED** chip, and their avatar greys out. A
  one-line summary at the top says exactly what's happening ("Recording all
  5 players" / "Recording only 2 included • 3 paused" / "Recording 4 of 5 •
  1 excluded").
- **Works live during recording AND Instant Replay.** The filter is checked
  every tick, so toggling someone takes effect on the very next frame —
  excluded players stop being recorded and their assets stop extracting
  immediately; re-including resumes both. No restart needed.
- **Tap yourself to record yourself.** The local player follows the
  existing `INCLUDE_LOCAL` setting by default, but explicitly tapping
  yourself to INCLUDED overrides it.
- Rows are managed diff-style so avatar thumbnails never flicker/reload on
  the 5 Hz status refresh. Filter state is per-session (resets when you
  re-execute the loader), as chosen.

## 1.9.23-alpha — 2026-06-01

Two correctness fixes: no double-extraction, and mid-game rig/skin/clothing
swaps are now caught.

- **No asset is ever extracted twice.** A race could create a duplicate
  queue entry: entry A is popped (removed from the dedup map) and is
  mid-extraction — `EXTRACTED` not yet set, file not yet written — when a
  second enqueue for the same id slips through (e.g. a t-shirt that's BOTH
  a torso decal AND a clothing entry). The duplicate then re-ran the full
  `CreateEditable*Async` + write ~10 s later (seen in the last log:
  `1028594` extracted at t=0.4 and again at t=10.1). `_processOne` now has
  a hard guard at the top: if the id is already extracted or cached, it's a
  no-op (counters still reconciled). Nothing extracts or downloads twice.

- **Mid-game skin / mesh swaps are detected.** `_rescanExistingAssets` only
  caught assets that *appeared* where there were none (late streaming). If
  a part's mesh/texture/colorMap *changed value* — which is exactly what
  happens when a round starts and the game reskins a player — it was
  silently missed. Now it compares full asset fingerprints and re-enqueues
  on any change.

- **Mid-game clothing swaps are detected.** Clothing was enqueued once at
  ENSURE and never re-checked. New `_rescanClothing` re-reads each player's
  Shirt / Pants / ShirtGraphic every throttled tick (cheap) and re-enqueues
  if the template changed.

- **The rescan window reopens when new parts appear.** When the game
  inserts new skin/tool meshes mid-recording, `_appendNewParts` detects the
  new parts and reopens the (previously-settled) rescan window so changed
  content on *existing* parts is re-checked alongside. Combined with the
  Character-swap path (`_rebuildRefs` already reopens), this covers the
  "lobby → round start, everyone gets a new rig/skin/uniform" case for both
  normal recording and Instant Replay.

## 1.9.22-alpha — 2026-06-01

**Clothing templates now extract via the engine (EditableImage), bypassing
CDN auth — the fix for "restricted clothing won't download".**

The whole point of the EditableImage approach is that it reads the bytes
the *client* already loaded for rendering, regardless of whether our
account can fetch the asset from the CDN. It's why otherwise-401 UGC
meshes/textures extracted fine. But clothing was never routed through it:

- Clothing (Shirt.ShirtTemplate / Pants.PantsTemplate) enqueues with
  `partInst = nil` because it isn't a single BasePart.
- In `_processOne`, the EditableImage path lived inside `if ref then`
  (a live part instance). With no part, clothing skipped it entirely and
  went straight to the HTTP fallback — which 401s on off-sale / private
  UGC. The `enqueueClothing` comment even *claimed* it used
  `Content.fromUri`, but the code never did.

Now, for any image entry with no live part, `_processOne` tries
`CreateEditableImageAsync` before HTTP, attempting several content-ref
forms for robustness:
1. the stored template URL,
2. `rbxassetid://<id>`,
3. the live `Shirt`/`Pants`/`ShirtGraphic` template read straight off an
   owning player's character (the exact Content the engine resolved).

Since the clothing is being rendered on a present player, its bytes are
in the client and EditableImage should return them. HTTP remains the
last-resort fallback. Off-sale clothing of players who have already
*left* still can't be recovered (no live render to read from) — but
clothing of present players should now extract regardless of CDN
permission.

Also: **the broken Actor scaffold is disabled.** Confirmed in the F9
console that Xeno does not execute engine-created Script instances even
with `RunContext=Client` under `PlayerScripts` — the worker showed a red
error and never signaled ready. The scaffold code is gated behind
`ENABLE_ACTOR_SCAFFOLD = false`, and any actor a prior version left in
`workspace` / `PlayerScripts` is now cleaned up on load (removes the red
console error). Parallel extraction via Actor is a dead end in Xeno; the
remaining ~160 ms extraction stalls stay, per the user's call to
prioritize getting all assets over smoothing those out.

## 1.9.21-alpha — 2026-06-01

Fix the Actor scaffold's "worker script never signaled ready" failure.
The 1.9.18-1.9.20 setup created a `LocalScript` under an `Actor`
parented to `workspace`. Two issues:

- **LocalScript under `workspace` doesn't auto-execute.** LocalScripts
  require a privileged ancestor (PlayerScripts, PlayerGui, Backpack,
  ReplicatedFirst, etc.). Source was being written and read back fine,
  but the script never started.
- Two fixes layered:
  1. Use `Script` with `RunContext = Enum.RunContext.Client` instead
     of `LocalScript`. This is the modern Roblox API and runs from
     any parent (including `workspace`).
  2. Prefer parenting the `Actor` under `Players.LocalPlayer.
     PlayerScripts` when available — there, even a plain `LocalScript`
     would run as a safety net.

If `Script.RunContext` doesn't exist on the user's Roblox build (pre-
2023), `Script` falls back to legacy behavior but is still likely to
run under a client executor's injection context.

## 1.9.20-alpha — 2026-06-01

Two important fixes from analyzing 1.9.18's F9 console output:

- **`BindToClose` wrapped in pcall.** The line
  `game:BindToClose(function() ... end)` near the bottom of the file
  now throws `BindToClose can only be called on the server.` on
  current Roblox client builds (apparently strict-mode enforcement
  changed). The bare call was halting script load partway through —
  everything after it (UI methods, `_destroy`, `PlayerRemoving`
  handler) never ran. Best-effort pcall: works in Studio / server
  context, silently no-ops on client executors.
- **Removed the misleading parallel-Luau probe.** 1.9.15-1.9.19 had a
  module-load probe that did `pcall(task.desynchronize); pcall(task.
  synchronize)` and considered the absence of a thrown error as proof
  that parallel Luau worked. It doesn't: Roblox just prints a warning
  (`task.synchronize() should only be called from a script that is a
  descendant of an Actor`) and treats the thread as still synchronized.
  The pcall returns success either way. So
  `[ROCORDER] parallel Luau available` was a lie. Removed the probe
  and its `_desyncSafe` / `_syncSafe` helpers (already unused since
  1.9.17). The Actor scaffold (`_G.ROCORDER_ACTOR_OK`) is the single
  source of truth for whether parallel extraction is actually live.

## 1.9.19-alpha — 2026-06-01

- **Extractor backend logged at recording START.** A new line in the
  debug log right after `START tickRate=...`:
  - `EXTRACTOR backend=actor-parallel` when the 1.9.18 Actor scaffold
    is live.
  - `EXTRACTOR backend=serial (actor probe failed: <reason>)` when the
    serial fallback is active.
  Means you no longer need to read the Roblox dev console (F9) to tell
  which path is running — the .debug.log captures it for every clip.

## 1.9.18-alpha — 2026-06-01

Real Actor + Script scaffold for parallel extraction. Where 1.9.15 had
the worker coroutine call `task.desynchronize` directly (Roblox's
editable APIs refused), this version creates an actual Roblox `Actor`
instance with a `LocalScript` inside it. Scripts under an Actor have
true parallel context, and the editable APIs accept them.

- **Scaffold setup at module load** (under `_G` so it survives reloads):
  - `Actor` named `_ROCORDER_ExtractorActor` parented to `workspace`
  - 3 `BindableEvent`s under the actor: `Job`, `Result`, `Ready`
  - `LocalScript` whose `Source` is set to a worker that listens on
    `Job`, runs `Create*Async` + read methods in desync, posts back
    on `Result`
- **Probe before trusting it**: scaffold writes the source, verifies the
  write stuck by reading it back, parents the script to start it,
  waits up to 2 s for a `Ready` signal, sends a `ping` job, waits up
  to 2 s for a `pong`. Sets `_G.ROCORDER_ACTOR_OK = true` only if the
  full round-trip works.
- **Fast path in `extractMeshFromPart` / `extractImageFromContent`**:
  when `ROCORDER_ACTOR_OK`, dispatch the job to the actor and use the
  result directly. On any actor failure (timeout, error in the worker,
  unexpected data), silently fall through to the existing serial
  extraction path — so a bad job can't break the recorder.
- **Console output** at script load tells you exactly which path is
  active:
  - `Actor scaffold installed and ping round-trip succeeded` → parallel
    extraction live. Main-thread stalls during extraction should
    largely disappear.
  - `Actor scaffold unavailable: Script.Source write …` → executor
    didn't let us set Source. Falls back to serial.
  - `Actor scaffold unavailable: ping timed out …` → Source was set
    but the worker isn't responding. Falls back to serial.

This is the architectural fix the 1.9.15-1.9.17 attempts were missing
— a real Actor parent for the worker script, not just a desynced
coroutine. Whether it actually works depends on Xeno allowing
`Script.Source` writes (most executors do).

## 1.9.17-alpha — 2026-06-01

Backed out the parallel-Luau extraction path. Roblox's client-side
editable APIs are more restrictive than docs implied: not just
`CreateEditable*Async` but also `EditableMesh:GetVertices` (and
presumably every other editable read method) refuse parallel context:

```
EXTRACT mesh ... FAILED — Function EditableMesh.GetVertices is
not safe to call in parallel
```

1.9.16's log showed every mesh failing extraction at GetVertices and
falling through to HTTP — which gave us raw mesh-format files, not
the structured `.geom.json` the importer wants. "0 stalls" was hollow
— we'd silently lost mesh extraction.

- **`_processOne` no longer desyncs.** Reverted to the 1.9.14 cascade:
  serial main-thread extraction, paced via `paceExtractor`. The 160 ms
  stalls during big-mesh extraction are back, but mesh extraction
  itself works again.
- **`extractMeshFromPart` / `extractImageFromContent` no longer
  internally sync.** The 1.9.16 `_syncSafe / _desyncSafe` wrappers
  around `Create*Async` are gone since the caller never desyncs.
- **Probe still runs at module load** — its result is informational,
  printed once. Useful diagnostic if Roblox loosens the parallel
  restrictions in a future version.

Net for the user: identical behavior to 1.9.14 (the last known-good
extractor). Future paths to eliminate the 160 ms stalls now require
genuine Actor scaffolding — out of scope without executor-specific
Source modification.

## 1.9.16-alpha — 2026-06-01

Recovers from the 1.9.15 regression. The parallel-Luau probe succeeded,
but Roblox explicitly refuses `CreateEditableMeshAsync` and
`CreateEditableImageAsync` in parallel context:

```
CreateEditableMeshAsync: Function AssetService.CreateEditableMeshAsync
is not safe to call in parallel
```

The 1.9.15 log showed most mesh + image extractions failing with that
error before fall-through to HTTP fallback (which thankfully rescued
many of them — but inefficiently and with stalls).

- **Sync briefly around the Create*Async calls only.** Inside
  `extractMeshFromPart` and `extractImageFromContent`, we now
  `_syncSafe()` just before the `CreateEditableMeshAsync` /
  `CreateEditableImageAsync` call, then `_desyncSafe()` right after.
  The Create*Async itself runs on the main thread (still blocks ~100 ms
  for big assets — can't help that), but everything else (per-vertex
  loops, GetFaces, ReadPixelsBuffer, JSON encode) continues to run in
  parallel.
- **Net win expected**: instead of one ~160 ms main-thread block per
  asset, we get one ~100 ms block (the unavoidable Create*Async load
  time) plus ~50-100 ms of work that happens off the main thread —
  invisible. Reduces stall band from ~160 ms to ~100 ms, eliminates
  the post-create-vert-loop chunk of main-thread time.

## 1.9.15-alpha — 2026-06-01

Experimental: parallel-Luau extraction to eliminate the remaining
160 ms stutter band.

- **Problem**: 1.9.14's log showed all remaining stalls were uniform
  ~160 ms, caused by `CreateEditableMeshAsync` /
  `CreateEditableImageAsync` blocking the main game thread during their
  initial asset load. These are C-bound API calls we can't pace inside.
- **Theory**: `task.desynchronize()` puts the calling thread on a
  worker VM thread; heavy API calls there don't stall the main thread.
  Officially requires the script to be parented to an `Actor`, but
  executors sometimes relax this requirement.
- **Probe at module load**: spawns a 1 s-bounded coroutine that tries
  `task.desynchronize(); task.synchronize()`. Prints which world we're
  in:
  - `parallel Luau available — extractor will desync around heavy API
    calls to keep main thread smooth` → success path active.
  - `parallel Luau unavailable: <error>` → probe failed, behavior
    identical to 1.9.14.
- **`_processOne` cascade** wraps the extraction block in
  `_desyncSafe` / `_syncSafe`. Synchronizes BEFORE any executor file
  I/O (`writefile`), HTTP request, or game-state introspection
  (`Players:GetPlayerByUserId`). Exception-safe — the resync runs even
  if extraction errors so the worker can't get stuck desynchronized.
- **Failure mode is silent and clean**. If `task.desynchronize` errors
  at runtime even after probe succeeded, the per-call pcall catches it
  and that single entry just runs in serial. Worker continues.

If this works in your executor, the ~160 ms stalls during extraction
should disappear entirely — the heavy API calls happen off the main
thread. Check the print at script load to see which mode you're in.

## 1.9.14-alpha — 2026-06-01

The 1.9.13 Decal-route experiment didn't work. Backed out so we don't
waste 0.3 s per clothing failure on a doomed `PreloadAsync`.

- **Reason it failed**: `Content.fromObject` exists on this Roblox
  build but only accepts `EditableImage` / `EditableMesh` instances —
  not Decals or any other Instance type. Debug log showed:
  `Content.fromObject failed: invalid argument #1 to 'fromObject'
  (Object expected, got table)` on every attempt. The trick is
  chicken-and-egg: we'd need to already have an EditableImage to
  wrap, which is what we're trying to create.
- **Decal-route step removed from `_processOne` cascade**. Image
  entries that the direct path doesn't handle now go straight to HTTP
  fallback as in 1.9.12 and earlier — no 0.3 s wasted per failure.
- **`_extractImageViaDecal` kept in source** as a comment-anchored
  record of what we tried, in case Roblox extends `fromObject` later.

Stutters: 1.9.13's log showed 24 stalls in 47 s, all uniform 160 ms
(was 17 in 49 s with one 320 ms outlier). The fail-fast HTTP and
adaptive-pace fixes worked — the 320 ms outlier is gone. What's left
is `CreateEditableMeshAsync` / `CreateEditableImageAsync` blocking
the main thread during their initial asset load. We can't pace inside
those C-bound API calls. The remaining options to eliminate that
final stall band are:

1. **Parallel-Luau extractor**: move the worker coroutine into an
   `Actor` so its API calls don't block the main thread. Substantial
   restructure of the queue worker. Right architectural fix.
2. **Accept current performance** and move to the next backlog item
   (per-player include/exclude filter, then rig revisions).

## 1.9.13-alpha — 2026-06-01

Experimental "Decal route" for clothing template extraction.

- **The bytes ARE loaded on the client** when a player is wearing a
  clothing template — they have to be, the engine renders them every
  frame. The problem was never byte availability; it was that
  `AssetService:CreateEditableImageAsync` rejects clothing asset IDs
  by content-type ("ShirtTemplate" / "PantsTemplate", not "Image").
- The new `_extractImageViaDecal` helper creates a hidden anchored
  `Part` (transparency 1, well underground), parents a `Decal` to it
  with `Texture` set to the failing URL, runs
  `ContentProvider:PreloadAsync` to ensure the engine has loaded the
  texture, then calls `Content.fromObject(decal)` and feeds the
  resulting `Content` to `CreateEditableImageAsync`. Theory: the
  content-type check fires on URL fetch, not on already-loaded engine
  bytes wrapped via `fromObject`. **May or may not work** depending
  on how Roblox's API revision implements the check.
- Wired as step 2 in `_processOne`'s cascade (after direct extract,
  before HTTP fallback). Image entries that the direct path didn't
  handle — including every clothing template — now get one more try
  before HTTP. On success the debug log shows `via Decal route`. On
  any failure (PreloadAsync timeout 5 s, `Content.fromObject` not in
  this Roblox build, CreateEditableImageAsync still rejecting) the
  fallback to HTTP is silent and unchanged.
- Hidden host folder `_ROCORDER_DecalHost` lives under `workspace`.
  Reused across script reloads so we don't leak orphan folders.

## 1.9.12-alpha — 2026-06-01

Three more performance/correctness fixes from the latest log analysis:

- **Adaptive `paceExtractor` recovery yield.** When a single chunk takes
  >30 ms (a tell-tale sign we just returned from a heavy Roblox API call
  we can't pace inside — `CreateEditableMeshAsync`,
  `CreateEditableImageAsync`, a big `ReadPixelsBuffer`), the pacer now
  yields **three** frames instead of one. Single-frame yields after a
  150 ms hitch leave the game only ~7 ms (at 144 fps) to render before
  the next chunk slams in — three frames give it ~20 ms, enough for a
  clean render. This is the dominant remaining cause of small stutters.
- **HTTP fallback fails fast on 401/403.** A clothing template that
  401s from `assetdelivery v1` will also 401 from `assetdelivery v2`
  (both check the same auth). The previous "try all endpoints × 2
  attempts × 0.8 s backoff" wasted 1-2 s per inaccessible asset and
  contributed visible stalls. Now: on the first 401/403 we break out
  of both endpoint and retry loops. 429 (rate-limit) and 5xx still
  retry as before.
- **"Player left" label now requires the player to have actually left.**
  Previously the tag fired whenever `_findLivePartRef` returned nil —
  even when the player was still in the game and just had their tool/
  accessory part destroyed. Now distinguished:
  - `(part instance destroyed — tool unequipped / accessory removed /
    script deleted)` when the owner is still in `Players:GetPlayers()`.
  - `(player left before extraction)` only when no owner remains.
  The `missed` per-player stat now reflects true disconnects only.

## 1.9.11-alpha — 2026-06-01

- **Mesh JSON encode is now manual & paced internally.**
  `HttpService:JSONEncode` of a 30 000-number array (the verts list for a
  10 k-vert mesh) is unbreakable C code that blocks for 20-40 ms. The
  outer-chunk pacing introduced in 1.9.9 couldn't help — pacing AROUND a
  20 ms op leaves the 20 ms hit intact. New `_encodeNumberArrayPaced`
  does the encode in a Lua loop with `paceExtractor()` every 1000
  elements, cutting each subarray encode into ~1 ms slices. `tostring`
  on a Roblox float produces JSON-valid number literals, so output bytes
  remain importer-compatible. Expected effect: small (~30-100 ms)
  stutters during mesh extraction also disappear.
- **Failure label no longer wrongly accuses player of leaving.** The
  fallback in `_processOne` printed `(player left before extraction)`
  whenever no live partRef existed — but clothing templates
  (Shirt/Pants) enqueue with `partInst = nil` by design, because
  `CreateEditableImageAsync` rejects clothing IDs. So clothing
  failures (off-sale UGC, private, restricted assets) were misattributed
  as the player leaving. Now distinguished:
  - `(asset not publicly accessible — likely off-sale / private
    clothing / restricted UGC)` for never-had-a-partInst entries.
  - `(player left before extraction)` only when an entry HAD a partRef
    and lost it.
  - No tag for "tried both paths, both failed for a generic reason".
  The `missed` stat (shown in the UI as "of which N missed: player
  left") now reflects actual player-leave events only.

## 1.9.10-alpha — 2026-06-01

Fixes the residual ~1 Hz stutter the user reported even after assets had
finished downloading.

- **Asset rescan settles after 3 quiet scans.** The 1 Hz
  `_rescanExistingAssets` sweep (added in 1.9.8 to catch avatar mesh
  content that streamed in after ENSURE) was running forever for every
  tracked player. Each call invokes `partInfo()` on every ref —
  ~15 pcall'd property reads per part — so 4 players × 20 parts ×
  15 pcalls ≈ 1200 pcalls per second of pure idle overhead, manifesting
  as a ~1 Hz hitch. The rescan now sets `entry.rescanSettled = true`
  after 3 consecutive scans found no new content, and skips entirely
  thereafter. Typical case: 3 s of low-cost scans, then zero overhead.
- **Settled flag resets on respawn.** When `_rebuildRefs` runs after a
  Character swap (death + respawn), `rescanSettled` is cleared so newly-
  attached parts whose mesh content arrives late get caught again.
- One-line settle log: `uid=X asset rescan settled (no new content for
  3 consecutive scans)` — visible per player in the debug log if you
  want to confirm the flag is firing.

## 1.9.9-alpha — 2026-05-31

Cuts the dominant remaining source of in-game stutter during extraction.

- **`paceExtractor()` is now called on every iteration** of the mesh
  vertex + face loops, not every 100. The helper short-circuits in ~1 µs
  when the budget isn't spent, so the extra calls are nearly free — and
  the old "every 100" gate let chunks accumulate ~200 ms of EditableMesh
  API work before the pace check could fire. That was the cause of the
  recurring ~200 ms heartbeat stalls observed during 500+ KB mesh
  extractions.
- **Mesh JSON encode is now chunked**. `HttpService:JSONEncode` is
  synchronous and blocks for 50-100 ms on a 500 KB mesh blob — a
  frame-killer on its own. New `_encodeGeomChunked` builds the JSON in
  four pieces (verts / uvs / normals / faces) with `paceExtractor()`
  between each, spreading the encode across 2-4 frames.
- **Legacy ASSET DOWNLOAD pass now respects `EXTRACTED[id]`** as a cache
  signal. Previously, the queue worker's HTTP fallback would save a
  clothing template to `<id>` (bare bin), then the legacy pass at Stop
  would re-fetch and re-save the same id — wasting one HTTP round-trip
  per clothing template that hit the fallback path. The inline cache
  check now hits the in-session flag first.

## 1.9.8-alpha — 2026-05-31

Late-joining players and mid-recording equips are now visible to the live
extractor + UI.

- **Mid-recording equips enqueue assets.** When a tool/accessory is equipped
  mid-recording, `Tracker:_appendNewParts` correctly appended the new part
  to the rig but never called `enqueuePartAssets` for it. Its mesh/texture
  had to be rescued by the end-of-recording legacy HTTP pass — which meant
  the live extractor UI showed no activity for the new accessory until
  recording stopped. Fixed by enqueuing inline when the part is appended.
- **Late-arriving mesh content is now caught.** Roblox streams an avatar
  in stages: skeleton + Motor6Ds first, mesh content second (SpecialMesh
  children attach, MeshPart fields populate). A player joining
  mid-recording would often capture with `shape=Block` parts and no
  `meshId` — then ~1 second later the engine fills in the FileMesh. The
  initial `partInfo` snapshot was frozen, so nothing noticed and no assets
  ever queued. New `Tracker:_rescanExistingAssets` reruns `partInfo` on
  each ref during the throttled (~1 Hz) sweep and enqueues any newly
  available asset ids. `_enqueueAsset` already dedupes by `EXTRACTED` +
  `Q.byId`, so the rescan is a no-op for assets we've already seen.
- **`entry.displayName` is now stored on Tracker entry** so `_appendNewParts`
  and `_rescanExistingAssets` can label per-player stats without re-looking-
  up the player.

## 1.9.7-alpha — 2026-05-31

Instant Replay no longer fills disk with assets from people who joined,
left, and never made it into a saved clip.

- **IR cache pruning**: a background scanner runs every 3 s while IR is on
  and no normal recording session is active. For each known asset id, it
  checks whether any owning player was seen within (IR_BUFFER_SEC + 5 s).
  If not — they've fallen out of the rolling buffer — every form of the
  file (`.geom.json` / `.rgba` / bare bin) is deleted and the id is
  forgotten. Logged as `IR cache: evicted N stale asset(s)` to the debug
  file when it happens.
- **Saved recordings are protected**: the scanner first builds a "kept"
  set of asset ids referenced by any `.rig.json` already on disk and
  never evicts those. The `ROCORDER/assets/` folder is shared across all
  recordings, so an id used by an older saved clip must not be deleted
  just because the original owner has left the live game.
- **Save-time race protection**: `SaveReplay` now sets a
  `_G.ROCORDER_SAVE_IN_PROGRESS` flag for the duration of the save call;
  the eviction scanner skips while it's held. Save body is wrapped in
  a pcall so the flag is always cleared even on a throw.
- **`ASSET_OWNERS` persistent map**: a new `{[id] = {[uid] = true}}` table
  lives under `_G` and survives the queue worker finishing a single
  asset. The previous `Q.byId[id].owners` was cleared as soon as the
  asset extracted, so the scanner had no way to know who originally
  needed it.

## 1.9.6-alpha — 2026-05-31

Asset extraction no longer stutters the game. Trade-off: extraction is
slower in wall-clock (often 2-3x), but the game stays smooth.

- **Frame-rate-aware pacing**: a Heartbeat-sampled frame-delta tracker
  drives a `paceExtractor()` helper that yields based on the game's
  *actual* current frame rate, not a fixed chunk size. Slices are capped
  at ~3 ms; if the game drops below 45 fps, the extractor yields two
  full frames before doing anything else.
- **Image extraction is now strip-based**: a 1024×1024 RGBA8 texture is
  4 MB. The previous `ReadPixelsBuffer(0, fullSize)` pulled that atomically
  — a single ~30-50 ms frame stall, visible as a hitch. We now read in
  ~64 KB row strips, pacing between strips.
- **Mesh extraction pacing tightened**: was yielding every 500 verts /
  500 faces (fixed), now paces every 100 with the budget-aware helper.
- **HTTP fallback retries once after 0.8 s** on failure. Clothing
  templates (Shirt.ShirtTemplate, Pants.PantsTemplate) MUST go through
  HTTP — `AssetService:CreateEditableImageAsync` rejects clothing-template
  asset IDs and the engine has no public read-back API for them. The
  Roblox CDN does serve them publicly though, and the retry handles the
  occasional 429 rate-limit during dense join bursts.
- **`_isCached` now also checks the in-session EXTRACTED flag**, not just
  the filesystem. This kills a tiny race where the legacy `downloadAssets`
  pass would re-fetch an asset the queue worker had just written but not
  yet flushed.

## 1.9.5-alpha — 2026-05-31

Fixes spurious "couldn't be fetched" failures for clothing templates (shirts
and pants).

- **The queue worker's HTTP fallback was throwing on every invocation**: it
  called `looksLikeAsset`, which was defined as a local nested inside the
  legacy `downloadAssets` pass much later in the file. From the worker's
  scope it was `nil`, so the call raised "attempt to call a nil value", got
  caught by the worker's outer pcall, and the entry was marked failed even
  though the same asset would download cleanly seconds later when the
  legacy pass ran. `looksLikeAsset` is now hoisted to module scope, so both
  paths see the same implementation.
- This was hitting shirt/pants templates specifically because
  `AssetService:CreateEditableImageAsync` doesn't accept clothing-template
  asset IDs — the extractor correctly fell through to HTTP, where the bug
  lived. Mesh-part textures still extracted fine and weren't affected.

## 1.9.4-alpha — 2026-05-31

- **Record tab is now scrollable.** When the Assets panel's per-player list
  grew, the bottom of the panel collided with the footer hotkey bar and you
  couldn't see the full status (or scroll to see more players). The Record
  view now uses a `ScrollingFrame` with `AutomaticCanvasSize`, same pattern
  the Settings tab already used. Content of any height is now reachable.

## 1.9.3-alpha — 2026-05-31

The "stuck at 31/39" report turned out to be a **UI bug**, not a worker bug.
Disk had 37 .geom.json + 31 .rgba files; the worker had drained the queue
just fine. 31 succeeded + 8 failed (player-left-before-extraction) = 39 seen.
The old headline "31 / 39 extracted" implied 8 were pending; really they were
already finished-but-failed.

- **Headline now distinguishes states explicitly**:
  - `extracting <kind> <id> (player)   (N done · N failed · N queued)` when
    actively processing
  - `N in queue (N done · N failed)` when waiting for the worker
  - `complete: N extracted · N couldn't be fetched (player left or asset
    permission-locked)` when totally finished with some failures
  - `complete: all N extracted` when totally finished with no failures
- **Progress bar now fills to `(done + failed) / total`** instead of just
  `done / total`. A failed item isn't pending — we won't try it again — so
  it should count toward the bar.
- **Stats line spells out the failure type**: `done N · failed N (of which M
  missed: player left) · queued N · worker tick K (Xs ago)`. The "missed"
  count is the subset of failures specifically caused by the player leaving
  before extraction, which is the most common cause and worth flagging.

Worker behavior unchanged. The watchdog and outer-pcall robustness from
1.9.2 stay in place.

## 1.9.2-alpha — 2026-05-31

User reported the worker still stalling at 144 fps, ruling out my frame-
health theory entirely. The fact that it stopped processing means something
is killing the coroutine. This release makes the worker basically
impossible to kill, and visible when it tries.

- **Outer pcall** around the entire iteration body. Anything inside (queue
  manipulation, perPlayer updates, anything) gets caught and the loop
  continues.
- **Watchdog coroutine** — wakes every 3 seconds, checks `Q.lastIterationAt`
  against `os.clock()`. If the queue has items but the worker hasn't ticked
  in > 5 seconds, respawn it. Self-exits when its `Q.watchdogVersion` is
  bumped so reloads don't accumulate watchdogs.
- **Worker prints to system console** on start and exit, with a version
  number — so "did the worker actually start?" is answerable by opening F9.
- **UI surfaces worker health.** Stats line now shows `worker tick N (X.Xs
  ago)`; if the worker has been silent for > 5 seconds with queue items,
  the headline gains ` — WORKER SILENT Xs` in red-ish wording so we can see
  the death without reading a debug log.

If your queue still hangs at "9 / 36", the new UI will tell us instantly
which of three things is true:
1. Worker tick count climbing → it's running, just slow
2. Worker tick frozen but watchdog warning fires → death + respawn loop
3. Worker tick 0 forever → worker never started in the first place

## 1.9.1-alpha — 2026-05-31

The 1.9.0 worker got stuck — debug log showed 9 extractions in the first 7s
then 26 seconds of silence even with 27 items still queued. Two real bugs.

- **Drop the frame-health gate.** The "only process when last heartbeat dt
  < 40ms" rule was too strict for any busy Roblox game. Many games sit at
  20–25fps (40–50ms/frame) under normal load, which the worker read as
  "always too busy" and never resumed. Politeness now comes entirely from
  the mid-mesh `task.wait()` (already added in 1.9.0) and a single
  `task.wait()` between entries. Throughput stays smooth and the queue
  actually drains.
- **Wrap `_processOne` in pcall.** If an extraction errored deep inside
  EditableMesh / `buffer.tostring` / `writefile`, the worker coroutine died
  silently and never recovered for the rest of the script session. Now the
  error is caught, the entry is marked failed, and the loop continues.

Expected effect: extraction completes for every newly-seen asset within
a few seconds of seeing the player, regardless of game framerate. UI
progress will tick steadily.

## 1.9.0-alpha — 2026-05-31

Reworked extractor for sustained Instant Replay sessions + new Assets status
panel in the Record tab. The 1.8.x extractor spawned a coroutine per player
that did all of their assets in a burst, producing 0.4–1.5s stutters every
time someone joined. Bad for hour-long IR. Now there's a single global
queue-driven worker that paces itself by frame health.

- **One global extractor coroutine.** Per-player `Tracker:ensure` no longer
  spawns its own coroutine — it just enqueues asset IDs. The single worker
  pops one entry at a time and only proceeds when the last heartbeat dt
  was healthy (< 40ms ≈ 25fps). On a busy frame it backs off 150ms and
  retries. Result: extraction work fills the idle time the game wasn't
  using, instead of competing with rendering.
- **Mid-mesh yielding.** `extractMeshFromPart` now `task.wait()`s every 500
  vertices and every 500 faces, so a 5000-vertex Rthro mesh becomes ten
  ~16ms chunks instead of one ~250ms stall.
- **Multi-partRef per asset for resilience.** If 3 players have the same
  hat, we still extract once but remember all 3 part instances. If player A
  leaves before we get to it, we use B's or C's. If everyone with that
  asset has left, the worker falls back to an authenticated HTTP fetch.
- **Player-left handling.** `Players.PlayerRemoving` flags the player's
  perPlayer stats with `leftAt` so the UI can show "left — N missed" for
  anyone whose assets we couldn't catch in time.
- **Assets status panel in the Record tab.** New section under the IR row,
  always visible. Shows:
  - Headline: `47 / 53 extracted — extracting mesh 12345 (lilia)`
  - Progress bar
  - Stats line: `queued N · done N · failed N · missed N`
  - Per-player list (sorted by most recent activity, top 8): name +
    `done/total` + status icon (✓ complete, … in progress, ← left,
    ⚠ left with misses)
  Updates every 200ms via the existing status loop.
- Extraction events still log to the active session's `.debug.log` (via
  `_G.ROCORDER_CURRENT_DBG` set in Start, cleared in Stop), so the per-
  asset `EXTRACT mesh X OK` lines you've seen still land in the right file.

The Blender importer still doesn't read `.geom.json` / `.rgba`. That's the
next big commit (1.10.0).

## 1.8.1-alpha — 2026-05-31

The 1.8.0 log showed the extractor catching essentially everything except
the **7 clothing assets** (Shirt/Pants templates) because those live on the
Shirt/Pants instances, not on parts.

- **Clothing extraction.** `extractClothingAssets(clothing, dbg)` walks
  `Shirt.ShirtTemplate`, `Pants.PantsTemplate`, `ShirtGraphic.Graphic` and
  feeds each through the existing `EditableImage` path. Called once per
  player at the end of the per-player extraction coroutine.
- Refactored the per-part image walk into a small helper `_extractImageRef`
  so the same dedup + write logic is used for clothing and decals.

Expected effect in the next debug log: those 7 "rejected via HttpGet" lines
should disappear and become `EXTRACT image <id> OK` entries instead.

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
