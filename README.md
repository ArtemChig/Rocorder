# ROCORDER

A two-part Roblox replay pipeline:

- **`rocorder.lua`** — a Roblox executor script (tested with Xeno) that records
  every player's per-bone CFrame at a fixed tick rate to a compact `.rec` file,
  plus a companion `.rig.json` capturing each player's rig (parts, sizes,
  colors, Motor6D hierarchy, joint pivots).
- **`blender_addon/rocorder_importer.py`** — a Blender add-on that imports a
  `.rec` into Blender, optionally building a full Armature per player whose
  bones mirror Roblox's Motor6D hierarchy and drive the recorded animation
  through pose bones. Each body part becomes a properly-sized colored mesh
  bound to its bone via a `Child Of` constraint.

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
| `PRECISION` | 2 | Decimal places used for stored floats |
| `HOTKEY` | F8 | Toggle key |
| `FOLDER` | `ROCORDER` | Output folder under the executor workspace |
| `INCLUDE_LOCAL` | true | Record the local player too |

## File formats

### `.rec`

```
{ JSON header on line 1: format, placeId, jobId, startedAt, tickRate, ... }
t=<sec>;<userId>:<bone>=<x>,<y>,<z>,<rx>,<ry>,<rz>;<userId>:<bone>=...
t=<sec>;...
```

Rotations are YXZ Euler angles in radians (`CFrame:ToEulerAnglesYXZ()`).
Positions are studs in Roblox's Y-up frame.

### `.rig.json`

```
{
  "format": "ROCORDER-RIG/1",
  "recFile": "replay_<...>.rec",
  "players": {
    "<userId>": {
      "userId": ..., "name": ..., "displayName": ..., "rigType": "R15",
      "parts": [
        { "name": "Head", "className": "MeshPart", "shape": "MeshPart",
          "size": [1, 1, 1], "color": [1, 0.8, 0.6], "transparency": 0,
          "restCFrame": [12 floats],
          "meshId": "rbxassetid://...", "textureId": "rbxassetid://..." }
      ],
      "joints": [
        { "name": "Neck", "part0": "UpperTorso", "part1": "Head",
          "pivot": [x, y, z] }
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
   - **Sphere Radius** — fallback radius for sphere placeholders.
   - **Match scene FPS to recording** — sets scene FPS to the recording's tick rate.
   - **Use companion rig file** — if off, every bone becomes a plain sphere.
   - **Build armature per player** — if on, build a real Armature with bones
     mirroring Motor6D hierarchy and animate via pose bones.

Open **Window → Toggle System Console** before importing to see the rig file
lookup log.

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
