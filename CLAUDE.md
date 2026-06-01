# ROCORDER — project policy for Claude

Read this on entry to the project. These rules apply to every change.

Planned/deferred features live in [`BACKLOG.md`](./BACKLOG.md) — consult it
when picking up "what's next", and move an item into `CHANGELOG.md` when it
ships.

## Versioning

**Every change bumps the project version.** No exception. If the diff is
worth committing, it's worth bumping.

The version is a single string used in all three places — keep them in sync:

| File | Field |
| --- | --- |
| `blender_addon/rocorder_importer.py` | `bl_info["version"]` (tuple) AND `ROCORDER_VERSION` (string) |
| `rocorder.lua` | `local ROCORDER_VERSION` near the top |
| `xeno_loader.lua` | `local ROCORDER_LOADER_VERSION` near the top |

Bump policy (semver-ish, with the bias the user asked for — small steps by
default):

- **PATCH** (`1.0.0 → 1.0.1`) — the default. Bug fixes, log/debug tweaks,
  doc edits, internal refactors, small additive features that don't change
  the import flow, performance work, comment-only changes.

- **MINOR** (`1.0.x → 1.1.0`) — when at least one of:
  - a new user-visible option / checkbox / hotkey,
  - a new field added to the `.rec` or `.rig.json` header (backward-compatible),
  - a non-trivial new module / subsystem,
  - a behavioral default changes,
  - a chain of related patches that together feel like a "feature" landing.

- **MAJOR** (`1.x.0 → 2.0.0`) — only for breaking changes that require the
  user to re-record (incompatible `.rec` format), reinstall the add-on
  (removed/renamed operator property the user has bound), or otherwise
  invalidate existing files. Pause and confirm with the user before doing this.

Pre-release suffix `-alpha` stays on until the user explicitly says "drop
alpha" or bumps to a release. While alpha, all of the above bumps still apply
— e.g. patch goes `1.0.0-alpha → 1.0.1-alpha`.

## Changelog

Every bump adds an entry to `CHANGELOG.md` at the top, under a new heading:

```
## <version> — <YYYY-MM-DD>

- bullet for each user-facing thing in the diff
```

Keep entries terse and oriented toward "what changed for the user", not
internal mechanics — the commit body is the place for mechanics.

## Commit / push policy

- Don't commit unless the user asked. When you do commit, include the
  version bump and the CHANGELOG entry in the same commit as the change.
- Tag releases as `v<version>` (e.g. `v1.0.1-alpha`) only when the user
  asks for a release; routine commits don't get tags.
- Never `git push --force` against `main`.

## File format notes (for sanity-checking diffs)

- `.rec` format identifier: `ROCORDER/3`. If you change the line grammar or
  remove a field, that's a **major** bump.
- Rig JSON format identifier: `ROCORDER-RIG/3` (per-player `revisions[]`).
  Importer also reads the legacy `ROCORDER-RIG/2` (flat parts/joints) as a
  single-life record. Same rule for changes.
- Adding a new field to either is **minor**, backward-compatible.

## Style / behavior reminders

- Imports prefer the standard rigid-skinning model (vertex-group weight 1.0
  per part, single Armature modifier). Do not reintroduce Child Of
  constraints — they were the source of the bone-popping bug.
- Recorder must guarantee uniform complete frames per player per tick.
  Never emit a partial entry; hold last value.
- `_flush` / `_flushDebug` retain their buffer on `appendfile` failure.
  Don't revert that.
