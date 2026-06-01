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
