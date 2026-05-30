bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (4, 2, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replay files as animated spheres",
    "category": "Import-Export",
}

import json
import os
import bpy
import bmesh
from bpy.props import StringProperty, FloatProperty, BoolProperty, IntProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Euler, Quaternion


# Roblox (Y-up, right-handed) -> Blender (Z-up, right-handed)
# (x, y, z) -> (x, -z, y)
ROBLOX_TO_BLENDER = Matrix((
    (1, 0,  0, 0),
    (0, 0, -1, 0),
    (0, 1,  0, 0),
    (0, 0,  0, 1),
))
ROBLOX_TO_BLENDER_INV = ROBLOX_TO_BLENDER.inverted()


def parse_frame_line_v1(line):
    """'t=1.23;uid:x,y,z,rx,ry,rz;...' -> (t, [(uid, x,y,z,rx,ry,rz), ...])"""
    line = line.strip()
    if not line:
        return None, None
    parts = line.split(";")
    if not parts[0].startswith("t="):
        return None, None
    try:
        t = float(parts[0][2:])
    except ValueError:
        return None, None
    out = []
    for chunk in parts[1:]:
        if ":" not in chunk:
            continue
        uid_str, vals_str = chunk.split(":", 1)
        vals = vals_str.split(",")
        if len(vals) != 6:
            continue
        try:
            uid = int(uid_str)
            x, y, z, rx, ry, rz = (float(v) for v in vals)
        except ValueError:
            continue
        out.append((uid, x, y, z, rx, ry, rz))
    return t, out


def parse_frame_line_v2(line):
    """'t=1.23;uid:bone=x,y,z,rx,ry,rz;...' -> (t, [(uid, bone, x,y,z,rx,ry,rz), ...])"""
    line = line.strip()
    if not line:
        return None, None
    parts = line.split(";")
    if not parts[0].startswith("t="):
        return None, None
    try:
        t = float(parts[0][2:])
    except ValueError:
        return None, None
    out = []
    for chunk in parts[1:]:
        if ":" not in chunk or "=" not in chunk:
            continue
        uid_str, rest = chunk.split(":", 1)
        bone, vals_str = rest.split("=", 1)
        vals = vals_str.split(",")
        if len(vals) != 6:
            continue
        try:
            uid = int(uid_str)
            x, y, z, rx, ry, rz = (float(v) for v in vals)
        except ValueError:
            continue
        out.append((uid, bone, x, y, z, rx, ry, rz))
    return t, out


def roblox_pose_to_blender(x, y, z, rx, ry, rz, scale):
    rob_rot = Euler((rx, ry, rz), "YXZ").to_matrix().to_4x4()
    rob_loc = Matrix.Translation((x, y, z))
    mat = ROBLOX_TO_BLENDER @ (rob_loc @ rob_rot) @ ROBLOX_TO_BLENDER_INV
    mat.translation *= scale
    return mat


def roblox_components_to_blender_matrix(comp, scale):
    """Roblox CFrame:GetComponents() -> (x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22)
    Build the 4x4 in Roblox space, then conjugate by the axis-swap so the
    result lives in Blender space and scale the translation."""
    px, py, pz = comp[0], comp[1], comp[2]
    r00, r01, r02 = comp[3], comp[4], comp[5]
    r10, r11, r12 = comp[6], comp[7], comp[8]
    r20, r21, r22 = comp[9], comp[10], comp[11]
    rob = Matrix((
        (r00, r01, r02, px),
        (r10, r11, r12, py),
        (r20, r21, r22, pz),
        (0.0, 0.0, 0.0, 1.0),
    ))
    mat = ROBLOX_TO_BLENDER @ rob @ ROBLOX_TO_BLENDER_INV
    mat.translation *= scale
    return mat


def roblox_position_to_blender(pos, scale):
    """(x, y, z)_roblox -> Vector(x, -z, y) * scale"""
    return Vector((pos[0] * scale, -pos[2] * scale, pos[1] * scale))


def make_sphere(name, radius, collection):
    mesh = bpy.data.meshes.new(name + "_mesh")
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)

    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=radius)
    bm.to_mesh(mesh)
    bm.free()

    obj.rotation_mode = "QUATERNION"
    return obj


def _color_material(color, transparency):
    """Return a Principled-BSDF material keyed by color so we share materials
    across identical parts."""
    r, g, b = (float(c) for c in color)
    a = max(0.0, 1.0 - float(transparency))
    key = "ROCORDER_M_{:.3f}_{:.3f}_{:.3f}_{:.3f}".format(r, g, b, a)
    mat = bpy.data.materials.get(key)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(key)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (r, g, b, 1.0)
        if a < 1.0:
            mat.blend_method = "BLEND"
            # 'Alpha' input was renamed in newer Blender versions; try both
            alpha_input = bsdf.inputs.get("Alpha")
            if alpha_input is not None:
                alpha_input.default_value = a
    return mat


def make_part_object(name, part_info, collection):
    """Build a Blender mesh sized to match a Roblox BasePart. Roblox's local
    axes map to Blender's local axes via the same (x, y, z) -> (x, -z, y) swap
    we apply to world-space matrices, so the *mesh* dimensions get Y and Z
    swapped here: a Roblox part of size (sx, sy, sz) becomes a Blender mesh
    of size (sx, sz, sy)."""
    sx, sy, sz = part_info.get("size", [1.0, 1.0, 1.0])
    shape = (part_info.get("shape") or "Block")

    mesh = bpy.data.meshes.new(name + "_mesh")
    bm = bmesh.new()

    if shape == "Ball":
        # Roblox Ball is uniform; use smallest axis as diameter to stay inside
        # the bounding box even for asymmetric sizes.
        diam = min(sx, sy, sz)
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=10,
                                  radius=diam / 2.0)
    else:
        # Block, MeshPart, Wedge, Cylinder, etc. — all approximated by an
        # axis-aligned bounding box of the correct size. Mesh content for
        # MeshParts could be fetched later via the saved meshId.
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=(sx, sz, sy), verts=bm.verts)

    bm.to_mesh(mesh)
    bm.free()

    color = part_info.get("color", [0.7, 0.7, 0.7])
    transparency = float(part_info.get("transparency", 0.0))
    mesh.materials.append(_color_material(color, transparency))

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.rotation_mode = "QUATERNION"

    if transparency >= 0.99:
        obj.hide_viewport = True
        obj.hide_render = True

    # surface mesh metadata for future "fetch real meshes" passes
    if part_info.get("meshId"):
        obj["rocorder_mesh_id"] = part_info["meshId"]
    if part_info.get("textureId"):
        obj["rocorder_texture_id"] = part_info["textureId"]
    obj["rocorder_shape"] = shape

    return obj


def load_rig_file(rec_filepath, header, report):
    """Find and parse the companion .rig.json. Returns the parsed dict or None."""
    candidates = []
    rig_field = header.get("rigFile")
    rec_dir = os.path.dirname(rec_filepath)
    if rig_field:
        candidates.append(os.path.join(rec_dir, rig_field))
    base, _ = os.path.splitext(rec_filepath)
    fallback = base + ".rig.json"
    if fallback not in candidates:
        candidates.append(fallback)

    for path in candidates:
        if os.path.isfile(path):
            print("[ROCORDER] Loading rig file:", path)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                report({"WARNING"},
                       "Found rig file {} but couldn't parse it: {}".format(path, e))
                return None

    print("[ROCORDER] No rig file found. Searched:")
    for p in candidates:
        print("    ", p)
    report({"WARNING"},
           "No companion .rig.json next to the .rec — falling back to spheres. "
           "If this recording was made before rig support was added, re-record it.")
    return None


# ----------------------------------------------------------------------------
# Armature building / animation
# ----------------------------------------------------------------------------

def _topological_order(bone_names, parent_of):
    """Return bone_names sorted so every parent appears before its children."""
    seen = set()
    out = []
    bones = set(bone_names)
    def visit(name):
        if name in seen or name not in bones:
            return
        seen.add(name)
        p = parent_of.get(name)
        if p is not None:
            visit(p)
        out.append(name)
    for n in bone_names:
        visit(n)
    return out


def build_player_armature(arm_name, player_rig, scale, collection):
    """Build an Armature object whose bones mirror the Roblox Motor6D hierarchy.

    Bones are placed JOINT-TO-JOINT (head at parent joint pivot, tail at the
    farthest child joint pivot) so the rig visually looks like a Roblox
    skeleton. Because that makes bone.matrix_local (B) differ from the part's
    rest CFrame (Q), the caller must compensate when computing pose bone
    matrices: target the bone at `P @ Q⁻¹ @ B` instead of `P` directly. With
    that compensation and `Child Of(inverse=B⁻¹, mesh.matrix_world=Q)` the
    mesh evaluates to exactly P.

    Returns (arm_obj, parent_of, rest_world, head_world_rob, part_rest_world):
        parent_of[bone]        = parent bone name (or absent if root)
        rest_world[bone]       = B = bone.matrix_local in armature space
        head_world_rob[bone]   = (x, y, z) bone head position in Roblox coords
        part_rest_world[bone]  = Q = part's rest CFrame in Blender (or absent
                                 when the recording has no restCFrame for it)
    """
    parts = [p for p in player_rig.get("parts", []) if p.get("name")]
    joints = player_rig.get("joints", [])
    if not parts:
        return None, {}, {}, {}

    parent_of = {}
    for j in joints:
        p0, p1 = j.get("part0"), j.get("part1")
        if p0 and p1:
            parent_of[p1] = p0

    # head pivots come from joints whose part1 == this bone (the joint that
    # connects it to its parent). tails come from any joint whose part0 == this
    # bone (i.e. a child's connection). Leaves get a synthetic tail along the
    # part's local Y axis so the bone has real length.
    head_pivot = {}
    tail_pivots = {}
    for j in joints:
        p0, p1 = j.get("part0"), j.get("part1")
        pivot = j.get("pivot")
        if not pivot:
            continue
        if p1:
            head_pivot[p1] = pivot
        if p0:
            tail_pivots.setdefault(p0, []).append(pivot)

    rest_cf_rob = {}
    parts_by_name = {}
    for p in parts:
        parts_by_name[p["name"]] = p
        if p.get("restCFrame"):
            rest_cf_rob[p["name"]] = p["restCFrame"]

    arm_data = bpy.data.armatures.new(arm_name + "_data")
    arm_obj = bpy.data.objects.new(arm_name, arm_data)
    collection.objects.link(arm_obj)

    # entering edit mode requires the armature to be the active object
    prev_active = bpy.context.view_layer.objects.active
    prev_mode = bpy.context.mode if bpy.context.mode else "OBJECT"
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.mode_set(mode="EDIT")

    edit_bones = arm_data.edit_bones
    head_world_rob = {}
    part_rest_world = {}
    try:
        for p in parts:
            bone_name = p["name"]
            rest_cf = p.get("restCFrame")
            size    = p.get("size", [1.0, 1.0, 1.0])

            # Q = part rest CFrame in Blender — needed by the animation
            # compensation. Stored here so we don't recompute later.
            Q = None
            if rest_cf and len(rest_cf) >= 12:
                Q = roblox_components_to_blender_matrix(rest_cf, scale)
                part_rest_world[bone_name] = Q

            # ---- joint-to-joint bone placement (visually skeletal) ----
            # head: joint with parent (or part center / origin as fallbacks)
            if bone_name in head_pivot:
                head = roblox_position_to_blender(head_pivot[bone_name], scale)
                head_rob = head_pivot[bone_name]
            elif Q is not None:
                head = Q.translation.copy()
                head_rob = (rest_cf[0], rest_cf[1], rest_cf[2])
            else:
                head = Vector((0.0, 0.0, 0.0))
                head_rob = (0.0, 0.0, 0.0)
            head_world_rob[bone_name] = head_rob

            # tail: pick the child-joint pivot farthest from head, so multi-
            # child parts (UpperTorso has Neck + both Shoulders) still get a
            # sensible bone direction. Leaves extend along part local Y.
            if bone_name in tail_pivots and tail_pivots[bone_name]:
                candidates = [roblox_position_to_blender(pv, scale)
                              for pv in tail_pivots[bone_name]]
                tail = max(candidates, key=lambda v: (v - head).length_squared)
            elif Q is not None:
                local_y = Vector((Q[0][1], Q[1][1], Q[2][1]))
                if local_y.length < 1e-6:
                    local_y = Vector((0.0, 0.0, 1.0))
                local_y.normalize()
                length = max(abs(size[1]) * scale, 0.1)
                tail = head + local_y * length
            else:
                tail = head + Vector((0.0, 0.0, 1.0))

            eb = edit_bones.new(bone_name)
            eb.head = head
            eb.tail = tail
            if (eb.tail - eb.head).length < 1e-4:
                eb.tail = eb.head + Vector((0.0, 0.0, 0.1))

        # parenting must happen AFTER all bones exist
        for child_name, parent_name in parent_of.items():
            if child_name in edit_bones and parent_name in edit_bones:
                edit_bones[child_name].parent = edit_bones[parent_name]
                edit_bones[child_name].use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = prev_active

    rest_world = {b.name: b.matrix_local.copy() for b in arm_data.bones}

    # rotation_mode for all pose bones to QUATERNION for clean keyframes
    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "QUATERNION"

    return arm_obj, parent_of, rest_world, head_world_rob, part_rest_world


def bind_mesh_to_bone(mesh_obj, arm_obj, bone_name, rest_world_mesh):
    """Place the mesh at rest_world_mesh and attach a Child Of constraint so
    it follows the bone's pose deviations from rest. The constraint's
    inverse_matrix equals the bone's world rest matrix inverted, which makes
    `bone_world_pose @ inverse_matrix @ mesh_rest_world` cancel cleanly to
    `mesh_rest_world` when the bone is at rest, and apply the bone's relative
    motion otherwise."""
    mesh_obj.matrix_world = rest_world_mesh
    con = mesh_obj.constraints.new("CHILD_OF")
    con.target = arm_obj
    con.subtarget = bone_name
    bone_world_rest = arm_obj.matrix_world @ arm_obj.data.bones[bone_name].matrix_local
    con.inverse_matrix = bone_world_rest.inverted()


def _player_label(roster, uid):
    info = roster.get(uid, {})
    return info.get("displayName") or info.get("name") or "Player"


def import_replay(context, filepath, scale, sphere_radius, set_fps, use_rig, build_armature, report):
    try:
        fh = open(filepath, "r", encoding="utf-8")
    except OSError as e:
        report({"ERROR"}, "Could not open file: {}".format(e))
        return {"CANCELLED"}

    with fh:
        header_line = fh.readline()
        try:
            header = json.loads(header_line)
        except json.JSONDecodeError:
            report({"ERROR"}, "First line is not a valid ROCORDER header.")
            return {"CANCELLED"}

        fmt_name = header.get("format")
        if fmt_name == "ROCORDER/1":
            version = 1
        elif fmt_name == "ROCORDER/2":
            version = 2
        else:
            report({"WARNING"},
                   "Unknown format '{}', assuming ROCORDER/2.".format(fmt_name))
            version = 2

        tick_rate = float(header.get("tickRate", 30))
        roster = {p["userId"]: p for p in header.get("roster", []) if "userId" in p}

        parser = parse_frame_line_v2 if version == 2 else parse_frame_line_v1
        frames = []
        for raw in fh:
            t, entries = parser(raw)
            if t is None:
                continue
            frames.append((t, entries))

    if not frames:
        report({"ERROR"}, "No frames found in recording.")
        return {"CANCELLED"}

    # rig is only meaningful for v2 (per-bone) recordings
    rig_data = None
    rig_lookup = {}  # str(uid) -> { bone_name: part_info }
    if version == 2 and use_rig:
        rig_data = load_rig_file(filepath, header, report)
        if rig_data is not None:
            for uid_key, player_rig in rig_data.get("players", {}).items():
                rig_lookup[str(uid_key)] = {
                    p["name"]: p for p in player_rig.get("parts", [])
                    if "name" in p
                }

    scene = context.scene
    if set_fps:
        scene.render.fps = max(1, int(round(tick_rate)))
        scene.render.fps_base = 1.0
    fps = scene.render.fps / scene.render.fps_base

    base_name = bpy.path.display_name_from_filepath(filepath) or "ROCORDER"
    root_coll = bpy.data.collections.new("ROCORDER_" + base_name)
    scene.collection.children.link(root_coll)

    objects = {}       # v1 key = uid; v2-object-mode key = (uid, bone)
    last_quat = {}     # same keying as objects
    player_colls = {}  # uid -> sub-collection (v2)

    # Per-player armature state (only populated when build_armature is on AND
    # the rig file has that player). Mid-game joiners with no rig data fall
    # back to per-bone objects in the same sub-collection.
    armatures        = {}  # uid -> arm_obj
    arm_parent_of    = {}  # uid -> { bone_name: parent_bone_name }
    arm_rest_world   = {}  # uid -> { bone_name: B = bone.matrix_local }
    arm_part_rest    = {}  # uid -> { bone_name: Q = part rest CFrame (Blender) }
    arm_last_quat    = {}  # uid -> { bone_name: Quaternion } for sign continuity

    last_frame = 1
    keyframes_written = 0
    bones_animated = 0

    def get_or_make_player_coll(uid):
        sub = player_colls.get(uid)
        if sub is None:
            sub = bpy.data.collections.new(
                "{}_{}".format(_player_label(roster, uid), uid))
            root_coll.children.link(sub)
            player_colls[uid] = sub
        return sub

    def ensure_player_armature(uid):
        """Lazily create the armature + per-bone meshes for one player. Only
        called when build_armature is enabled and rig data is available."""
        if uid in armatures or str(uid) not in rig_lookup:
            return armatures.get(uid)
        player_rig = rig_data["players"][str(uid)]
        sub = get_or_make_player_coll(uid)
        arm_name = "{}_{}_rig".format(_player_label(roster, uid), uid)
        arm_obj, parent_of, rest_world, _heads_rob, part_rest_world = build_player_armature(
            arm_name, player_rig, scale, sub)
        if arm_obj is None:
            return None
        arm_obj["rocorder_user_id"] = str(uid)

        # build meshes for every part and bind each to its bone
        for p in player_rig.get("parts", []):
            bone_name = p.get("name")
            if not bone_name or bone_name not in rest_world:
                continue
            mesh_name = "{}_{}_{}".format(
                _player_label(roster, uid), uid, bone_name)
            mesh = make_part_object(mesh_name, p, sub)
            mesh["rocorder_user_id"] = str(uid)
            mesh["rocorder_bone"] = bone_name
            # rest world pose for the mesh comes straight from restCFrame
            rest_cf = p.get("restCFrame")
            if rest_cf and len(rest_cf) >= 12:
                rest_world_mesh = roblox_components_to_blender_matrix(rest_cf, scale)
            else:
                rest_world_mesh = rest_world[bone_name]
            bind_mesh_to_bone(mesh, arm_obj, bone_name, rest_world_mesh)

        armatures[uid] = arm_obj
        arm_parent_of[uid] = parent_of
        arm_rest_world[uid] = rest_world
        arm_part_rest[uid] = part_rest_world
        arm_last_quat[uid] = {}
        return arm_obj

    # Decide which players go through the armature path. v2 only. Requires
    # build_armature flag, a successfully loaded rig, AND non-empty rig data
    # for that player.
    armature_uids = set()
    if version == 2 and build_armature and rig_data is not None:
        for uid_key in rig_lookup.keys():
            try:
                armature_uids.add(int(uid_key))
            except ValueError:
                pass

    # Pre-create everything that's known up-front: v1 spheres, and v2 armatures
    # for players present in the rig.
    if version == 1:
        all_uids = set(roster.keys())
        for _t, entries in frames:
            for uid, *_ in entries:
                all_uids.add(uid)
        for uid in sorted(all_uids):
            obj = make_sphere(
                "{}_{}".format(_player_label(roster, uid), uid),
                sphere_radius, root_coll)
            obj["rocorder_user_id"] = str(uid)
            objects[uid] = obj
    elif version == 2:
        for uid in sorted(armature_uids):
            ensure_player_armature(uid)

    for t, entries in frames:
        frame_num = int(round(t * fps)) + 1
        if frame_num > last_frame:
            last_frame = frame_num

        if version == 1:
            for entry in entries:
                uid, x, y, z, rx, ry, rz = entry
                obj = objects.get(uid)
                if obj is None:
                    continue
                mat = roblox_pose_to_blender(x, y, z, rx, ry, rz, scale)
                loc, rot, _ = mat.decompose()
                prev = last_quat.get(uid)
                if prev is not None and prev.dot(rot) < 0.0:
                    rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
                last_quat[uid] = rot
                obj.location = loc
                obj.rotation_quaternion = rot
                obj.keyframe_insert(data_path="location", frame=frame_num)
                obj.keyframe_insert(data_path="rotation_quaternion", frame=frame_num)
                keyframes_written += 2
            continue

        # ---- v2: split entries by player so armature math can run on a
        # complete per-player snapshot for this frame (parents-before-children).
        per_player = {}  # uid -> { bone_name: world_matrix_blender }
        for entry in entries:
            uid, bone, x, y, z, rx, ry, rz = entry
            mat = roblox_pose_to_blender(x, y, z, rx, ry, rz, scale)
            per_player.setdefault(uid, {})[bone] = mat

        for uid, bone_to_mat in per_player.items():
            if uid in armature_uids:
                arm_obj = armatures.get(uid)
                if arm_obj is None:
                    arm_obj = ensure_player_armature(uid)
                    if arm_obj is None:
                        continue

                rest_world = arm_rest_world[uid]
                parent_of  = arm_parent_of[uid]
                part_rest  = arm_part_rest[uid]
                last_q_map = arm_last_quat[uid]

                # process bones parents-first so each child can read its
                # parent's already-computed pose for this frame
                order = _topological_order(
                    [b for b in bone_to_mat.keys() if b in rest_world],
                    parent_of)
                pose_world_this_frame = {}

                for bone_name in order:
                    recorded_P = bone_to_mat[bone_name]
                    B = rest_world[bone_name]
                    Q = part_rest.get(bone_name)

                    # ---- compensation: target the bone at `effective` so the
                    # Child Of constraint (mesh.matrix_world=Q, inverse=B^-1)
                    # produces mesh_world == recorded_P. When Q is missing
                    # (old recording without restCFrame), fall back to no
                    # compensation; mesh will be offset by a constant per
                    # bone but the bone pose still tracks the recorded data.
                    if Q is not None:
                        effective = recorded_P @ Q.inverted() @ B
                    else:
                        effective = recorded_P
                    pose_world_this_frame[bone_name] = effective

                    parent_name = parent_of.get(bone_name)
                    if (parent_name and parent_name in pose_world_this_frame
                            and parent_name in rest_world):
                        parent_pose = pose_world_this_frame[parent_name]
                        parent_rest = rest_world[parent_name]
                        rest_local  = parent_rest.inverted() @ B
                        pose_local  = parent_pose.inverted() @ effective
                        matrix_basis = rest_local.inverted() @ pose_local
                    else:
                        matrix_basis = B.inverted() @ effective

                    loc, rot, _ = matrix_basis.decompose()
                    prev = last_q_map.get(bone_name)
                    if prev is not None and prev.dot(rot) < 0.0:
                        rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
                    last_q_map[bone_name] = rot

                    pb = arm_obj.pose.bones.get(bone_name)
                    if pb is None:
                        continue
                    pb.location = loc
                    pb.rotation_quaternion = rot
                    pb.keyframe_insert(data_path="location", frame=frame_num)
                    pb.keyframe_insert(data_path="rotation_quaternion",
                                       frame=frame_num)
                    keyframes_written += 2
                    bones_animated += 1
                continue

            # ---- v2 object mode for this player (no rig, or build_armature off,
            # or mid-game joiner). One object per (uid, bone), lazily created.
            for bone, desired in bone_to_mat.items():
                key = (uid, bone)
                obj = objects.get(key)
                if obj is None:
                    sub = get_or_make_player_coll(uid)
                    obj_name = "{}_{}_{}".format(
                        _player_label(roster, uid), uid, bone)
                    part_info = rig_lookup.get(str(uid), {}).get(bone)
                    if part_info is not None:
                        obj = make_part_object(obj_name, part_info, sub)
                    else:
                        obj = make_sphere(obj_name, sphere_radius, sub)
                    obj["rocorder_user_id"] = str(uid)
                    obj["rocorder_bone"] = bone
                    objects[key] = obj

                loc, rot, _ = desired.decompose()
                prev = last_quat.get(key)
                if prev is not None and prev.dot(rot) < 0.0:
                    rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
                last_quat[key] = rot
                obj.location = loc
                obj.rotation_quaternion = rot
                obj.keyframe_insert(data_path="location", frame=frame_num)
                obj.keyframe_insert(data_path="rotation_quaternion",
                                    frame=frame_num)
                keyframes_written += 2

    # linear interpolation across the board, both for mesh objects and
    # pose-bone actions.
    def set_linear(obj):
        ad = obj.animation_data
        if not ad or not ad.action:
            return
        for fcurve in ad.action.fcurves:
            for kp in fcurve.keyframe_points:
                kp.interpolation = "LINEAR"
    for obj in objects.values():
        set_linear(obj)
    for arm_obj in armatures.values():
        set_linear(arm_obj)

    scene.frame_start = 1
    scene.frame_end = last_frame
    scene.frame_current = 1

    if version == 2:
        mode_desc = []
        if armatures: mode_desc.append("{} armatures".format(len(armatures)))
        if objects:   mode_desc.append("{} loose meshes".format(len(objects)))
        if not mode_desc: mode_desc.append("nothing imported")
        report({"INFO"},
               "ROCORDER v2: {}, {} frames, {} keyframes ({} bone-keys).".format(
                   ", ".join(mode_desc), len(frames),
                   keyframes_written, bones_animated))
    else:
        report({"INFO"},
               "ROCORDER v1: {} players, {} frames, {} keyframes.".format(
                   len(objects), len(frames), keyframes_written))
    return {"FINISHED"}


class IMPORT_OT_rocorder(Operator, ImportHelper):
    bl_idname = "import_scene.rocorder"
    bl_label = "Import Roblox Replay"
    bl_description = "Import a ROCORDER .rec file as animated spheres"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".rec"
    filter_glob: StringProperty(default="*.rec", options={"HIDDEN"})

    scale: FloatProperty(
        name="Scale",
        description="World scale. 1.0 = 1 Roblox stud per Blender unit",
        default=1.0,
        min=0.0001,
        soft_max=10.0,
    )
    sphere_radius: FloatProperty(
        name="Sphere Radius",
        description="Radius of each player sphere, in Blender units (after scale)",
        default=2.0,
        min=0.001,
        soft_max=20.0,
    )
    set_scene_fps: BoolProperty(
        name="Match scene FPS to recording",
        description="Set scene FPS to the recording's tick rate so 1 frame == 1 sample",
        default=True,
    )
    use_rig: BoolProperty(
        name="Use companion rig file",
        description="If a .rig.json sits next to the .rec, build properly-sized "
                    "colored boxes per body part instead of plain spheres",
        default=True,
    )
    build_armature: BoolProperty(
        name="Build armature per player",
        description="When the rig file is available, build a Blender armature "
                    "whose bones mirror Roblox's Motor6D hierarchy and drive "
                    "the animation through pose bones. Each part mesh is bound "
                    "to its bone via a Child Of constraint",
        default=True,
    )

    def execute(self, context):
        return import_replay(
            context,
            self.filepath,
            self.scale,
            self.sphere_radius,
            self.set_scene_fps,
            self.use_rig,
            self.build_armature,
            self.report,
        )


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_rocorder.bl_idname, text="Roblox Replay (.rec)")


_classes = (IMPORT_OT_rocorder,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
