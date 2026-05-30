# ROCORDER

A two-part Roblox replay pipeline:

- **`rocorder.lua`** — a Roblox executor script (tested with Xeno) that records
  every player part's world position + rotation (as a quaternion) at a fixed
  tick rate to a compact `.rec` file, plus a companion `.rig.json` capturing
  each player's rig (parts, sizes, colors, Motor6D `C0`/`C1` hierarchy).
- **`blender_addon/rocorder_importer.py`** — a Blender add-on that imports a
  `.rec`, building a full Armature per player whose bones mirror Roblox's
  Motor6D hierarchy. Each body part becomes a properly-sized colored mesh,
  combined into one mesh that is **skinned** to the armature (each part weighted
  1.0 to its bone) and driven by a single Armature modifier — no constraints.

## Use cases

Post-game cinematography, replay analysis, machinima — anywhere you want to
take what happened in a Roblox round and edit it in a real 3D editor.
This is **not** a gameplay cheat: the script only reads positions/rotations
that the client already sees, and produces output for offline post-processing.

## Recorder usage (Roblox)

1. Paste `rocorder.lua` into your executor.
2. **F8** to start a recording, **F8** again to stop.
3. Files land under `<executor>/workspace/ROCORDER/`:
   - `replay_<placeId>_<unixTime>.rec`     — per-tick pose stream
   - `replay_<placeId>_<unixTime>.rig.json` — companion rig snapshot

You can also drive it programmatically:

```lua
_G.ROCORDER:Start()
_G.ROCORDER:Stop()
_G.ROCORDER:Toggle()
_G.ROCORDER:IsRecording()
```

### Config (top of the file)

| Key | Default | Meaning |
| --- | --- | --- |
| `TICK_RATE` | 30 | Samples per second |
| `FLUSH_INTERVAL` | 0.5 | Seconds between disk flushes |
| `MAX_CATCHUP_SEC` | 5.0 | Cap on backfilled frames after a stall |
| `MAX_DISTANCE` | 0 | Studs from the camera; players beyond it are skipped (0 = unlimited) |
| `POS_PRECISION` | 3 | Decimal places for positions (studs) |
| `ROT_PRECISION` | 5 | Decimal places for quaternion components |
| `HOTKEY` | F8 | Toggle key |
| `FOLDER` | `ROCORDER` | Output folder under the executor workspace |
| `INCLUDE_LOCAL` | true | Record the local player too |

## File formats

### `.rec` (`ROCORDER/3`)

```
{ JSON header on line 1: format, placeId, jobId, startedAt, tickRate, ... }
t=<sec>;<userId>:<part0>|<part1>|...;<userId>:<part0>|...
t=<sec>;...
```

Each `<partK>` is `px,py,pz,qx,qy,qz,qw` — the part's **world** position (studs,
Roblox Y-up frame) plus its world rotation as a **quaternion**. Parts are
**positional**: the K-th part of a player maps to the K-th entry of that
player's `parts` array in the `.rig.json`. Every frame lists **all** of a
player's parts in that fixed order (briefly-missing parts hold their last value),
so no bone ever has a hole in its keyframe stream.

### `.rig.json` (`ROCORDER-RIG/2`)

```
{
  "format": "ROCORDER-RIG/2",
  "recFile": "replay_<...>.rec",
  "players": {
    "<userId>": {
      "userId": ..., "name": ..., "displayName": ..., "rigType": "R15",
      "parts": [                       // order == positional index in the .rec
        { "name": "Head", "className": "MeshPart", "shape": "MeshPart",
          "size": [1, 1, 1], "color": [1, 0.8, 0.6], "transparency": 0,
          "restCFrame": [12 floats],
          "meshId": "rbxassetid://...", "textureId": "rbxassetid://..." }
      ],
      "joints": [                      // Motor6D structure -> canonical rest pose
        { "name": "Neck", "part0": "UpperTorso", "part1": "Head",
          "c0": [12 floats], "c1": [12 floats] }
      ]
    }
  }
}
```

## Importer usage (Blender 3.0+)

1. **Edit → Preferences → Add-ons → Install…**, pick
   `blender_addon/rocorder_importer.py`, enable it.
2. **File → Import → Roblox Replay (.rec)**, pick a `.rec` (the matching
   `.rig.json` is found automatically next to it).
3. Options in the import dialog:
   - **Scale** — 1.0 = 1 stud per Blender unit.
   - **Match scene FPS to recording** — sets scene FPS to the recording's tick rate.
   - **Build armature per player** — on: build a real Armature whose bones mirror
     the Motor6D rig, with one skinned mesh per player bound via an Armature
     modifier (each part weighted 1.0 to its bone). Off: import plain animated
     spheres instead.

The importer only reads `ROCORDER/3` recordings — re-record with the current
`rocorder.lua` if you have older files. Open **Window → Toggle System Console**
before importing to see the rig-file lookup log.

### How the rig stays accurate

The recorder stores each part's **world** transform `T` per frame. The importer
derives the rig's canonical rest pose `D` from the Motor6D `C0`/`C1` offsets and
gives each bone a rest matrix `R` (drawn joint-to-joint, purely cosmetic). Each
part's mesh is skinned 100% to its bone, so a vertex deforms to
`pose[B] · R⁻¹ · D · v`. Setting `pose[B] = T · D⁻¹ · R` makes every vertex land
exactly at its recorded world pose **regardless of `R`** — which is why the bones
can look like a Roblox skeleton without ever affecting animation accuracy.

## Coordinate convention

Roblox is Y-up, right-handed; Blender is Z-up, right-handed. The importer maps
`(x, y, z) → (x, -z, y)` and conjugates rotations by the same swap.

## Repo layout

```
rocorder.lua                       # the executor recorder
blender_addon/
  rocorder_importer.py             # the Blender add-on
.gitignore                         # ignores *.rec, *.rig.json, blender backups, etc.
README.md
```

## License

TBD.
