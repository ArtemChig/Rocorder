# ROCORDER asset pipeline — redesign (evidence-grounded)

Status: **proposal**, derived from analysis of `rocorder.lua` /
`rocorder_importer.py` plus real recordings and a read-only in-engine probe
(`probe_assets.lua`, run in Violence District on a Potassium executor,
2026-06-25). Supersedes guesswork in `BACKLOG.md` P1 with measured facts.

## Why the current design fails

The recorder captures the *recipe* (asset ids, clothing ids, part flags) and
the importer re-derives the *appearance* by re-implementing Roblox's
renderer/compositor in Blender (585×559 template math, CharacterMesh, sphere
heads, decal projection). Every symptom — heads fail, UVs wrong, assets look
wrong, invisible hitboxes — traces to capturing one layer too early: we never
see what the engine actually drew.

## Chosen architecture: record-time self-compositing

Photograph-style render-bake was evaluated and **rejected on evidence**
(`probe_render.lua`, 2026-06-25): `CaptureService.CaptureScreenshot` times
out with no pixel readback, `SaveScreenshotCapture` only writes to camera
roll, and no executor framebuffer global exists — so rendered pixels cannot
be read back on this build. (Even if they could, screen-space→UV-space
reprojection is the hard part.)

What *is* available: the full **EditableImage write surface** —
`DrawImage`, `DrawImageTransformed`, `DrawRectangle`, `WritePixelsBuffer`.
So the core redesign is **record-time self-compositing**: the recorder
reconstructs each part's final texture in UV space (skin + clothing
templates drawn into the body's UV regions + decals), writes one
ready-to-apply texture per part, and the importer applies it 1:1. This is a
real paradigm shift (compositing leaves Blender, moves into capture) and —
because we control the UV mapping in the draw step — it handles R6 and R15
clothing with the same mechanism, dissolving BACKLOG P1.

## Measured facts (from the probe + debug logs)

1. **Working capture primitives:** `CreateEditableImageAsync(Content.fromUri)`
   and `CreateEditableMeshAsync(Content.fromUri)` both succeed and read the
   client's already-loaded bytes (bypassing CDN auth). This is the reliable
   core to build on.
2. **`Content.fromObject` takes an Editable *Object*, not an Instance**
   (`"Object expected, got Instance"`). It is **not** a way to read a part's
   composited texture. The GPU-composited body texture is **not reachable**
   from the client by any tested route (`TextureID` stays the base skin;
   no `TextureContent` composite; no `fromObject` read-back).
   - Consequence: the recorder's `_extractImageViaDecal` Decal-route
     (`Content.fromObject(decal)`) is **dead code on this build**.
3. **We capture raw templates, never composites:** every Shirt extracts at
   exactly 585×559 with real design variance — the source ShirtTemplate.
4. **CharacterMesh / sculpted R6 body UVs follow the R6 template.** Four
   different players' CharacterMesh torsos all sampled `U[0.031..0.45]
   V[0..0.98]` — the R6 template torso band. So the shirt/pants template can
   be applied **directly with the mesh's own authored UVs**. The importer
   currently throws those UVs away and force-fits a cube projection
   (`rocorder_importer.py` `_build_part_object`, the `r6_clothing_regions`
   path) — that override is the cause of "clothing splattered / UVs wrong"
   on these avatars.
5. **~12% of assets fail (HTTP 401/403 off-sale UGC), including a head.**
   The bytes were in the client while the avatar rendered, but serial
   post-hoc extraction missed the live instance and fell to the CDN, which
   refuses private UGC. (`ERRORS_LOST1` head mesh `86845461082446` + texture
   `122717312815290` are the worked example.)
6. **Classic Block heads** (`shape=Block(Head)`, no mesh id) still fall back
   to the sphere; only fix is a bundled mesh.

## Type coverage (evidence-based — `probe_types.lua`, 2026-06-25)

**Decisive constraint:** `CreateEditableMeshAsync`/`CreateEditableImageAsync`
accept only a **Content** (asset id / `MeshContent`), never a live instance
(`"Unable to cast Instance to Content"`). Anything without an asset id is
unextractable. This bounds every edge type below.

**Also confirmed:** EditableMesh exposes `GetVertexBoneWeights` + `GetFaceColors`
(skinning is *readable*); `SurfaceAppearance` with `AlphaMode` Transparency
AND Overlay is common in the wild (PBR is not optional).

| Type | Extractable? | Plan |
| --- | --- | --- |
| Block/Ball part | ✅ primitive | covered |
| MeshPart (MeshId/MeshContent) | ✅ | covered |
| SpecialMesh/FileMesh + MeshId | ✅ | covered |
| CharacterMesh body | ✅ | Stage 0/3 |
| Accessory/Handle mesh | ✅ | covered |
| Decal / face | ✅ | covered |
| Shirt/Pants/Graphic template | ✅ | Stage 3 composite |
| SurfaceAppearance maps (Color/Normal/Metal/Rough + AlphaMode) | ✅ | **Stage 7 (PBR)** |
| Cylinder/Wedge/CornerWedge part | ✅ shape known | **Stage 8 (primitives)** |
| SpecialMesh Sphere/Cylinder/Wedge/Torso/Prism | ✅ shape known | Stage 8 |
| `Texture` tiling (StudsPerTile) | ✅ image + params | Stage 8 (importer tiling) |
| Classic Head (SpecialMesh, no id) | ➡️ bundle mesh | Stage 5 |
| Skinned MeshPart | ✅ **fully capturable** — `GetBones` + `GetVertexBoneWeights` + per-frame `Bone.WorldCFrame` (confirmed `gaps/1`) | **Stage 9** (format extension) |
| Layered clothing (WrapLayer) | ⚠️ cage inputs extractable (`CageMeshId`/`CageOrigin`/`ImportOrigin` confirmed); worn-deformed shape live-only → must replicate the cage deform | **Stage 10** (best-effort) |
| Runtime/procedural mesh (MeshContent set) | ✅ extracts via `CreateEditableMeshAsync(MeshContent)` (confirmed) | covered by Stage 1 path |
| **Truly id-less / contentless mesh** | ❌ no id, no MeshContent (never observed) | placeholder flag |
| **UnionOperation / CSG** | ❌ confirmed dead — `MeshData` blob empty (len 0) across 6 games; only raycast voxelization | placeholder, or opt-in voxel |

The three ❌/⚠️ ceilings are platform limits, not missing work: live-instance
extraction is impossible, and the rigid-transform `.rec` cannot carry
per-vertex deformation. ROCORDER records *players*, and Unions/tiling/
primitives observed here were **map** geometry (not recorded), so their
practical avatar impact is low — the exception is a Union/runtime-mesh
*welded to a player* (externalParts path), which simply won't extract.

## Implementation plan (staged)

Each stage is independently shippable, version-bumped, and has an explicit
acceptance test. Code anchors reference current line locations; treat them
as "near here", not exact after edits.

### Stage 0 — Validate the UV foundation  *(importer-only, NO re-record)* — **DONE + VERIFIED (1.24.5-alpha)**

**Verified outcome:** authored-UV clothing is correct for *template-conformant*
CharacterMeshes (the majority) — a clear win over the old universal
cube-splatter. But *custom-UV* CharacterMeshes (e.g. ArtemChig228's torso
`376169183`, UVs in a sub-band U[0.03..0.78] V[0.13..0.97], no BaseTextureId)
render scrambled, because their real in-game look is Roblox's avatar
**composite** (unreachable), not the raw template. **Lesson for Stage 3:**
authored UVs are not universally trustworthy for body clothing — the reliable
signal is GEOMETRY (which body-region each surface point is). Stage 3 must use
a geometry-aware body projection (not authored UVs) so it handles both
conformant and custom-UV bodies. ArtemChig228-class meshes are a known
imperfection until Stage 3.
**Why first:** the whole self-compositing core assumes we can place clothing
into a body's real UV layout. Probe fact #4 says CharacterMesh authored UVs
already sit in the R6 template band. Prove it with data already on disk
before writing any recorder code.

**Change:** in `rocorder_importer.py` `_build_part_object`
(`blender_addon/rocorder_importer.py:1216`), for a body part that HAS a mesh
and whose `charMesh` flag is set, stop overriding the authored UVs with the
cube projection (`r6_clothing_regions` path, ~:1256). Instead apply the
Shirt/Pants template texture using the mesh's own authored UVs
(`_add_mesh_geometry` already loads `faceUVs`, :831). Keep the cube/box path
only for plain Block bodies (no mesh).

**Acceptance:** re-import `recordings/2026-06-25_…__cure-violence-district`
(already extracted). Jan_oof / ArtemChig228 torso+arms show the shirt mapped
correctly (no splatter). If it still misaligns, the authored UVs do NOT
follow the template after all → revisit before Stage 3.
**Version:** patch.

### Stage 1 — Eager, live, executor-only capture  *(recovers the ~12% loss)* — **DONE + VERIFIED (1.24.6-alpha)**
*Implemented:* `_downloadAssets` now tries `CreateEditable*Async` by asset id
(CDN-bypass) before HTTP; `_processOne` gained a no-live-part mesh-by-id path;
`collectAssetIds` records id→kind. Dead-code cleanup (Actor scaffold, Decal
route) deferred to a low-risk pass — harmless as-is.
*Verified:* Rivals recording, 449 assets → **156 saved via by-id CDN-bypass,
0 failed** (those 156 off-sale UGC assets would previously have 401'd on HTTP).
**Change (`rocorder.lua`):** extract each asset the moment its live instance
is first seen (in `enqueuePartAssets` / the rescan path, :2137), via the
Editable* route (`extractMeshFromPart` :1008, `extractImageFromContent`
:1195) — while the instance is guaranteed alive. Decouple "seen" from
"extracted" so a player leaving can't strand assets. Demote HTTP
(`_processOne` :1816) to only genuinely non-live, publicly-served assets
(clothing templates). 
**Cleanup:** delete the dead Actor scaffold (:609–820, `ENABLE_ACTOR_SCAFFOLD
= false`) and the dead `Content.fromObject` Decal-route
(`_extractImageViaDecal` :1301; proven non-functional — `fromObject` rejects
Instances on this build).
**Acceptance:** re-record the same scene; the 9 prior `401/403` failures
(incl. `ERRORS_LOST1` head `86845461082446`) now land on disk via Editable*.
Debug log shows 0 HTTP failures for live UGC.
**Version:** minor.

### Stage 2 — Hitbox / invisibility cull  *(general, replaces name hacks)* — **DONE, UNVERIFIED (1.25.0-alpha — not yet tested)**
*Implemented:* recorder `partInfo` emits `rendered`/`cullReason` using effective
transparency `1-(1-Transparency)(1-LocalTransparencyModifier)` + has-drawable;
importer culls `rendered=false` (bone kept), legacy fallback for old rigs.
Hidden `_Hitboxes` collection deferred (skip is the fix); enclosure test for
opaque-but-invisible boxes is a future refinement.
**Change (`rocorder.lua` `partInfo` :2201):** compute `rendered` (bool) +
`cullReason` per part from: effective transparency (`Transparency +
LocalTransparencyModifier`), presence of any drawable (mesh / decal /
texture / SurfaceAppearance), and an enclosure test (untextured box that
encloses a textured assembly ⇒ hitbox). Emit into the part record.
**Importer (`build_player` :1753):** parts with `rendered=false` go to a
hidden `<label>_Hitboxes` collection instead of the current single
`transparency>=0.999` skip (:1761) — non-destructive, user can unhide.
**Acceptance:** Violence District `HRP_Clone` / `Hurtbox` land in
`_Hitboxes`, body parts don't; no hand-coded names involved.
**Version:** minor (format add → `ROCORDER-RIG/3`).

### Stage 3 — Clothing — **SOLVED (1.30.0-alpha) via compositor UV remap**
Final solution: body mesh UVs are coordinates in Roblox's composite atlas;
remap each body vertex's UV through Roblox's shipped `Composit*Base` meshes into
Shirt/Pants template space (the exact in-engine mapping). CharacterMesh bodies
use the remap; blocky bodies use the box→template cube projection (different UV
layout). Verified perfect by the user. Self-compositing bake (3a) and cylindrical
reprojection (3b) were both abandoned in favour of this. R15 is the same
mechanism with `R15Composit*Base` meshes (next). Earlier notes below kept for
history.
*3a (single-canvas composite) REVERTED in 1.27.1:* the R6 template reuses the
same limb cells for arms (shirt) and legs (pants), so merging shirt+pants onto
one canvas made the pants overwrite the arm cells — arms rendered with the leg
texture. The per-part Stage 0 path (shirt→arms, pants→legs) is correct.
`bakedTextureId` is now ignored/ungenerated. A PER-PART composite (separate
texture per body part) could work but is marginal vs Stage 0 — not pursued.
*Case 5 (R15 classic clothing) closed NEGATIVE (`probe_case5.lua`, 19 avatars):*
R15 body parts expose no readable texture (`TextureContent` = `SourceType=None`);
the composite is unreachable, so R15 classic clothing needs reprojection (3b),
same as custom-UV R6.
*3b v1 implemented (1.27.0, importer-only, opt-in):* new "Reproject clothing"
import checkbox → `_cylindrical_project_clothing_uvs` wraps the template around
CharacterMesh bodies cylindrically instead of using authored UVs. Approximate
(seam/cell-orientation may need tuning vs a render); default off so conformant
avatars are untouched. **Scope:** custom-UV R6 CharacterMesh (ArtemChig228).
**Still pending:** R15 bodies (not `charMesh`, need separate part-name handling
+ R15 composite) and visual tuning of the cylindrical params.

> **Revised after Stage 0:** a single template-space composite sampled by
> authored UVs only works for template-conformant meshes (custom-UV meshes
> like `376169183` scramble). The robust version bakes the clothing into
> **each mesh's OWN UV space** using GEOMETRY as the bridge: for every mesh
> triangle we know both its body-region (from vertex position/normal → R6
> template coord) and its authored UV (texture coord), so `DrawImageTransformed`
> the template content from template-space into the triangle's UV-space slot.
> The result applies 1:1 with authored UVs on ANY mesh, conformant or not.
> The template-space variant below is kept as the simple path for plain Block
> bodies (which have no authored UVs of their own).

**New module (`rocorder.lua`):** `compositeAvatarBodyTexture(char, rig)`.
For Block bodies, builds ONE composite per avatar in the R6 585×559 template
space. For mesh bodies, bakes per-part into the mesh's own UV space via the
geometry bridge above.

Algorithm (uses the confirmed EditableImage write surface — `DrawImage`,
`DrawImageTransformed`, `DrawRectangle`, `WritePixelsBuffer`):
1. Create a 585×559 EditableImage canvas.
2. **Skin base:** read `Character` `BodyColors` (Head/Torso/LeftArm/RightArm/
   LeftLeg/RightLeg Color3); `DrawRectangle` each body-part region (port the
   `_TPL` rects from the importer, :1092+) filled with that skin color.
   (v1 simplification allowed: single skin fill if BodyColors uniform.)
3. **Shirt:** `CreateEditableImageAsync(ShirtTemplate)` → `DrawImage` onto the
   canvas (template-aligned, alpha-composited) — covers torso + arms.
4. **Pants:** same, drawn on top — covers legs + lower torso overlap.
5. (defer: body decals.)
6. `ReadPixelsBuffer`/`WritePixelsBuffer` → write `comp_<uid>.rgba`.
Each CharacterMesh/Block body part gets `bakedTextureId = "comp_<uid>"` in
its rig record. Accessories / FileMesh handles keep their own mesh+texture
(no compositing — that path already works).
**Acceptance:** import shows correctly-clothed bodies with NO importer-side
template math; a player with shirt-but-no-pants shows skin legs.
**Version:** minor (format add: per-part `bakedTextureId`).

### Stage 4 — Importer simplification
With Stage 3 emitting baked textures, the importer's body path collapses to
"apply `bakedTextureId` 1:1 with authored UVs (CharacterMesh) or box UVs
(Block)". Delete `_r6_cube_project_clothing_uvs` (:774), `_build_clothed_box`
(:1179), and the `_clothing_for_part` template-rect machinery (:1150, 1092+)
once nothing references them. The importer stops being a second compositor.
**Acceptance:** byte-for-byte same visual result as Stage 3 with ~200 fewer
lines of Blender-side reconstruction.
**Version:** minor.

### Stage 5 — Classic head mesh  *(BACKLOG P2)*
Bundle the real Roblox classic-head mesh as static add-on data; use it for
`meshType=="Head"` parts with no mesh id instead of the sphere
(`_add_primitive_local` :1038). Keep face-decal front projection.
**Version:** minor.

### Stage 6 — R15 + classic clothing  *(deferred; needs an R15 probe run)*
R15 MeshPart bodies have per-limb UV islands, NOT the R6 template layout, so
Stage 3's template-space composite doesn't sample correctly. Extend
`compositeAvatarBodyTexture` to `DrawImageTransformed` each R6 template
region into the corresponding R15 UV island, using a one-time R6→R15 region
map. **Blocked until** an R15 probe run confirms the R15 body UV layout (run
`probe_assets.lua` on a modern avatar wearing classic clothing).
**Version:** minor.

### Stage 7 — PBR (SurfaceAppearance)  *(common in the wild, promoted)*
The census found `SurfaceAppearance` on multiple avatars with both
`AlphaMode.Transparency` and `AlphaMode.Overlay`. Extract Color/Normal/
Metalness/Roughness maps (partly captured, `partInfo` :2241; expand to all
four) and wire into `_image_material` (:909) as a principled BSDF. Honor
`AlphaMode`: **Overlay** = colormap composited *over* the base albedo (so for
body parts it layers onto the Stage-3 composite, not replaces it);
**Transparency** = alpha-blended. Per-part `alphaMode` added to the rig.
**Version:** minor.

### Stage 8 — Primitive shapes + tiling textures  *(low avatar impact)*
Map `Cylinder`/`Wedge`/`CornerWedge` parts and SpecialMesh
`Sphere`/`Cylinder`/`Wedge`/`Torso`/`Prism` to real Blender primitives
instead of boxes (`_add_primitive_local` :1029). Reproduce `Texture` tiling
(`StudsPerTileU/V`, offsets) as UV scaling in the importer. Mostly matters
for stylized/blocky avatar cosmetics; map geometry isn't recorded.
**Version:** minor.

### Stage 9 — Skinned meshes  *(confirmed feasible; format extension)*
The census + `gaps/1` confirmed `GetBones`, `GetVertexBoneWeights`, and live
`Bone.WorldCFrame`/`Transform` reads all work — even normal R15 hands are
skinned. So skinned meshes are fully capturable:
- **Rig (`.rig.json`):** for a skinned MeshPart, capture its `GetBones()` list
  (names + bind pose) and per-vertex bone weights (`GetVertexBoneWeights`,
  4-slot; confirm the bone-index mapping for the slots during impl).
- **Stream (`.rec`):** add a per-bone transform stream (each `Bone.WorldCFrame`
  per tick) alongside the existing per-part stream. This is the format
  extension — a new bone-channel grammar (major-ish; gate behind a version
  guard, additive so old files still import).
- **Importer:** build a real skinned mesh (vertex groups per bone, weights as
  captured) instead of one rigid bone per part.
**Risk:** med (format work). **Version:** minor (additive `.rec`/`.rig`).

### Stage 10 — Layered clothing (WrapLayer)  *(best-effort deform)*
Inputs confirmed extractable (`WrapTarget`/`WrapLayer` `CageMeshId` extracts;
`CageOrigin`/`ImportOrigin` readable). The worn-deformed mesh is live-only, so
replicate Roblox's cage deform: capture the layer cage + the body `WrapTarget`
cage + origins, and deform the authored garment mesh onto the body cage
(record-time in Lua or importer-side). Exact cage weighting is undocumented →
**fidelity is best-effort**; fall back to the undeformed garment if too rough.
**Risk:** med-high (uncertain fidelity). **Version:** minor.

### Inherent gaps (documented, not fixable)
- **Union/CSG welded to a player:** confirmed dead — `MeshData` blob empty on
  the client across all probed games; no asset id; live-instance extraction
  rejected. Emit an `unextractable` flag + reason so the importer places a
  labelled placeholder box. Optional opt-in: raycast voxelization (crude,
  slow) for users who want an approximate shape.
- **Truly id-less / contentless runtime mesh:** if a part has neither MeshId
  nor MeshContent, nothing to extract (never observed in probing). Same
  placeholder flag.

## Sequencing
0 → 1 → 2 → 3 → 4 first (each verifiable, mostly independent; 0 needs no
re-record). 5 (classic head) and 7 (PBR) are self-contained adds; do 7 soon
since PBR is common. 8 is low priority. 9 (skinned) is a bigger format-
extension piece — schedule after the core (3/4) lands. 10 (layered) and 6
(R15 clothing) are the uncertain-fidelity items; do them last. Stage 0 is the
cheapest validation of the foundation and must run before Stage 3 commits.
The two inherent gaps (Union, contentless mesh) get a flag, not a fix.
