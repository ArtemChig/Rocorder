# ROCORDER — future feature backlog

Things we've decided we want but haven't built yet. Newest decisions go
wherever they fit by priority. When one lands, move it out of here and into
`CHANGELOG.md` under the version that shipped it.

Priority key: **P1** = committed next / actively wanted · **P2** = wanted,
not urgent · **P3** = nice-to-have / noticed-in-passing.

(Shipped: classic-R6 Shirt/Pants wrapping landed in 1.14.0-alpha — face
orientation per the official 585×559 template; tune `_FACE_AXES` /
`*_RECTS` in the importer if any face is mirrored.)

---

## P1 — Paint classic Shirt/Pants onto R15 MeshPart bodies (avatar composite)

Rivals (and most modern games) use **R15 MeshPart bodies** (`UpperTorso`,
`LeftUpperArm`, `RightHand`, …) wearing classic 2D **Shirt/Pants**. We
record the clothing IDs and now extract their textures fine (1.19.4
Decal-preload path), but the importer never *applies* them to R15 bodies —
the clothing path in `_build_part_object` only matches **R6** part names
(`"Torso"`, `"Left Arm"`, …) with a `charMesh` flag. So R15 bodies render
with only their base skin texture (`14523777036`), and the user sees
"body colors, no clothing".

Why it's not a trivial wrap: classic clothing is authored for the R6
585×559 template. Roblox's avatar **composite** server re-projects that
onto the **R15** body's own UV layout (a different template). Applying the
R6 shirt directly via the R15 mesh's authored UVs would be misaligned.

Two possible approaches:
1. **Recorder-side capture of the composite.** If the composited body
   texture is reachable at record time (e.g. via the rendered MeshPart
   appearance / an EditableImage of what the client actually shows),
   capture that and treat it as the body texture — no reprojection needed.
   Needs investigation: the MeshPart's `TextureID` stays the base skin, so
   the composite is applied at a layer we haven't found an API for yet.
2. **Importer-side reprojection.** Replicate Roblox's R6→R15 clothing
   template mapping and bake shirt/pants onto the R15 body UVs. Correct but
   substantial (need the per-part R6-region → R15-UV correspondence).

Approach 1 is preferred if the composite is reachable. Start there.

---

## P2 — Bundle the exact classic-head mesh (replace the sphere approximation)

Right now a classic Head (a Block part with SpecialMesh `MeshType=Head`,
no MeshId) is imported as a size-fitted **sphere** with the face decal
projected on the front. That's much closer than the old cube, but the real
Roblox classic head is a slightly rounded/egg shape, not a perfect sphere —
and there's no public content id for it, so the recorder can't extract it.

Plan: bundle the actual classic-head mesh as a small static vertex/UV table
(e.g. an `.obj` or an embedded Python list) in the add-on, and use it for
`meshType == "Head"` parts instead of the sphere. Pixel-accurate, one-time
data, no network.

- Gating to preserve (already correct): only applies when `meshType=="Head"`
  AND there's no real mesh. Custom/MeshPart/FileMesh heads render their own
  geometry; avatars with no head get nothing. (See the head decision tree in
  the 2026-06-01 discussion.)
- Keep the face-decal front projection.
- Honor the part's size (ellipsoid for a non-uniform head).

---

## P2 — Files-tab upgrades + cleaner recordings folder layout

A grouped set of improvements to the recordings UI and how files are laid
out on disk. Bundling them because they touch the same code paths and
"better naming" is the linchpin — clean filenames make every other piece
nicer (sort columns, breadcrumbs, file-explorer jumps, the cache list).

- **Sort recordings in the Files tab.** Sortable columns: date, length,
  size, game/place name, player count. Current order is whatever
  `listfiles` returns — usually mtime-ish but not promised, and there's no
  way to flip newest-vs-oldest or find the longest clip in a session.

- **Navigate to a recording in the file explorer.** A button per row
  ("Show in folder") that opens the OS file explorer with the recording's
  `.rec` selected (Explorer / Finder / Nautilus). Lets the user grab a
  recording's files without hunting the path.

- **Clear cached assets / per-recording cache.** A button in the Files tab
  to clear cached asset files (the `assets/` folder) — globally
  ("clear all assets") and per-recording ("clear only assets this recording
  references"). Useful when the auth cookie changes, a re-record-with-the-
  UV-fix is wanted, or you want to free disk after a long IR session.
  Confirmation prompt before destructive action.

- **Better recording filenames.** Current scheme:
  `replay_<placeId>_<unixSeconds>.rec` — opaque, hard to sort by anything
  human-readable, the placeId says nothing about the game. Move to a
  human-friendly pattern, e.g.:
  `<YYYY-MM-DD>_<HH-MM-SS>__<game-name-slug>__<clip-or-session>.rec`
  Examples:
  - `2026-06-02_14-30-12__violence-district__clip.rec`
  - `2026-06-02_14-30-12__violence-district__replay.rec` (saved IR)
  Game name comes from `MarketplaceService:GetProductInfo(placeId).Name`,
  cached locally so it isn't refetched per recording. Keep `placeId` and
  `jobId` in the meta sidecar (they already are) — filenames don't need
  them. Sort-by-date and group-by-game become trivial because the
  filename itself encodes it.

- **Cleaner recordings folder layout.** Instead of every recording's
  `.rec` + `.rig.json` + `.meta.json` + `.debug.log` (4 files) littering one
  folder, group them per recording in a subfolder named after the
  recording: `ROCORDER/recordings/<recording-name>/{rec,rig.json,
  meta.json,debug.log}`. The shared `assets/` folder stays at the
  workspace root (it's content-addressed and cross-recording). One
  per-recording folder = one drag-to-archive unit, easier to delete an old
  session, and the Files tab lists subfolders instead of grouping by
  filename prefix.

- **Migration**: importer should still read old flat layout + old
  `replay_<id>_<ts>` names (backward compat, no breakage on existing
  files). New recordings use the new layout.

## P3 — Candidates / known limitations (noticed, not yet committed)

- **Hide enclosed classic base-body boxes on layered-clothing avatars.** When
  a player has layered-clothing MeshParts (e.g. `Torso_2`, `rightarm`,
  `LegLeft`) that fully enclose the classic R6 box body, both render and
  overlap in Blender — the base boxes are invisible in-game but show in the
  import. Consider auto-hiding (or moving to a separate, hidden collection)
  the classic body parts when enclosing mesh parts exist. Risk: detecting
  "enclosed" reliably.

- **Importer support for full rig revisions.** The recorder now re-detects
  changed mesh / texture / clothing mid-recording (1.9.23) and re-enqueues
  the new assets. But if a player's whole *rig structure* changes mid-session
  (e.g. R6 → R15, or a complete model swap), the importer still builds a
  single armature from the final part set. A faithful version would emit
  per-revision armatures with visibility keyframed at the swap boundary
  (the same idea we used for IR save). Needs a `.rig.json` revision list
  (would be a minor, backward-compatible format add → `ROCORDER-RIG/3`).

- **Other classic Part shapes / MeshTypes as real primitives.** Parts with
  `Shape == "Cylinder"` or SpecialMesh `MeshType` of `Cylinder` / `Wedge` /
  `CornerWedge` / `Torso` / `Prism` etc. currently import as boxes (unless
  they have a MeshId). Map each to a proper Blender primitive for better
  fidelity on stylized/blocky builds. Low impact for mesh-heavy avatars.
