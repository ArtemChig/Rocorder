bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (6, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replays as skinned, animated armatures",
    "category": "Import-Export",
}

# ============================================================================
# How this works (the math that makes it robust)
# ----------------------------------------------------------------------------
# The recorder stores, per part per frame, the part's WORLD transform T (as
# position + quaternion in Roblox coordinates). The companion .rig.json gives,
# per part, the Motor6D C0/C1 offsets we use to derive the rig's canonical rest
# pose D (a clean T-pose), plus sizes/colors/shapes for geometry.
#
# We build a real armature whose bones are drawn joint-to-joint (so it looks
# like a Roblox skeleton) and bind ONE combined mesh to it with the standard
# rigid-skinning method: every vertex of a part is weighted 1.0 to that part's
# bone, deformed by a single Armature modifier. No Child Of constraints.
#
# A vertex bound 100% to bone B is deformed to:
#       world_vert = pose.matrix[B] @ rest.matrix[B]^-1 @ vert_rest
# We place the part's verts at the canonical rest D (vert_rest = D @ v_local),
# rest.matrix[B] = R (the bone's rest matrix, R = bone.matrix_local). So:
#       world_vert = pose[B] @ R^-1 @ D @ v_local
# To force this to equal the recorded world pose (T @ v_local) we just need:
#       pose.matrix[B] = T @ D^-1 @ R
# This holds for ANY bone rest geometry R, which is the whole point: the bones
# can look however we like (joint-to-joint) without affecting accuracy. Each
# part lands exactly where it was recorded, every frame.
#
# We keyframe pose-bone basis matrices (location + quaternion) computed from
# that target pose, parents accounted for explicitly so we never depend on
# Blender's live pose-evaluation order.
# ============================================================================

import json
import os
import bpy
import bmesh
from bpy.props import StringProperty, FloatProperty, BoolProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Quaternion


# Roblox (Y-up, right-handed) -> Blender (Z-up, right-handed): (x, y, z) -> (x, -z, y)
ROBLOX_TO_BLENDER = Matrix((
    (1, 0,  0, 0),
    (0, 0, -1, 0),
    (0, 1,  0, 0),
    (0, 0,  0, 1),
))
ROBLOX_TO_BLENDER_INV = ROBLOX_TO_BLENDER.inverted()


# ----------------------------------------------------------------------------
# Coordinate conversion
# ----------------------------------------------------------------------------
def _conjugate(rob_mat, scale):
    """Conjugate a Roblox-space 4x4 into Blender space and scale translation.

    Because conjugation by (Scale_s @ R) is a group homomorphism, converting
    each factor of a product and multiplying gives the same result as
    converting the product — so C0/C1 chains compose correctly."""
    mat = ROBLOX_TO_BLENDER @ rob_mat @ ROBLOX_TO_BLENDER_INV
    mat.translation = mat.translation * scale
    return mat


def roblox_components_to_blender_matrix(comp, scale):
    """CFrame:GetComponents() (x,y,z, R00..R22 row-major) -> Blender 4x4."""
    px, py, pz = comp[0], comp[1], comp[2]
    rob = Matrix((
        (comp[3], comp[4],  comp[5],  px),
        (comp[6], comp[7],  comp[8],  py),
        (comp[9], comp[10], comp[11], pz),
        (0.0,     0.0,      0.0,      1.0),
    ))
    return _conjugate(rob, scale)


def roblox_posquat_to_blender(px, py, pz, qx, qy, qz, qw, scale):
    """Recorded world position + quaternion -> Blender 4x4 world matrix."""
    rot = Quaternion((qw, qx, qy, qz))
    n = rot.magnitude
    if n > 1e-12:
        rot = rot * (1.0 / n)
    else:
        rot = Quaternion((1.0, 0.0, 0.0, 0.0))
    rob = rot.to_matrix().to_4x4()
    rob.translation = Vector((px, py, pz))
    return _conjugate(rob, scale)


# ----------------------------------------------------------------------------
# Frame parsing  (ROCORDER/3 only)
# ----------------------------------------------------------------------------
def parse_frame_line_v3(line):
    """'t=1.23;uid:p0|p1|...;uid:...'  where each part = px,py,pz,qx,qy,qz,qw
    -> (t, { uid: [ (px,py,pz,qx,qy,qz,qw), ... ] })  parts positional."""
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

    out = {}
    for chunk in parts[1:]:
        if ":" not in chunk:
            continue
        uid_str, blob = chunk.split(":", 1)
        try:
            uid = int(uid_str)
        except ValueError:
            continue
        part_vals = []
        for part_str in blob.split("|"):
            vals = part_str.split(",")
            if len(vals) != 7:
                continue
            try:
                part_vals.append(tuple(float(v) for v in vals))
            except ValueError:
                continue
        if part_vals:
            out[uid] = part_vals
    return t, out


# ----------------------------------------------------------------------------
# Materials / geometry
# ----------------------------------------------------------------------------
def _color_material(color, transparency):
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
            alpha_input = bsdf.inputs.get("Alpha")
            if alpha_input is not None:
                alpha_input.default_value = a
    return mat


def _add_part_geometry(bm, part, place_mat, scale):
    """Add one part's geometry to the shared bmesh, transformed into place at
    `place_mat` (the part's canonical rest, Blender space). Returns the list of
    newly created BMVerts (so the caller can build a vertex group).

    Roblox local axes map to Blender local axes by the same (x,y,z)->(x,-z,y)
    swap, so a Roblox size (sx,sy,sz) becomes a Blender-local box (sx,sz,sy)."""
    shape = part.get("shape") or "Block"
    size = part.get("size", [1.0, 1.0, 1.0])
    sx, sy, sz = (float(s) for s in size)

    if shape == "Ball":
        diam = min(sx, sy, sz)
        ret = bmesh.ops.create_uvsphere(
            bm, u_segments=16, v_segments=8, radius=max(diam * 0.5 * scale, 1e-4))
        new_verts = ret["verts"]
    else:
        ret = bmesh.ops.create_cube(bm, size=1.0)
        new_verts = ret["verts"]
        bmesh.ops.scale(
            bm,
            vec=(max(sx * scale, 1e-4), max(sz * scale, 1e-4), max(sy * scale, 1e-4)),
            verts=new_verts,
        )

    # move the fresh geometry into world place
    bmesh.ops.transform(bm, matrix=place_mat, verts=new_verts)
    return new_verts


# ----------------------------------------------------------------------------
# Rig file
# ----------------------------------------------------------------------------
def load_rig_file(rec_filepath, header, report):
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
           "No companion .rig.json next to the .rec — falling back to spheres.")
    return None


# ----------------------------------------------------------------------------
# Canonical rest pose (the T-pose), from Motor6D C0/C1
# ----------------------------------------------------------------------------
def compute_canonical_rest_poses(player_rig, scale):
    """Per-part canonical world CFrame D in armature space, walking the C0/C1
    chain: roots at Identity, child = parent @ C0 @ C1^-1. This is the rig's
    structural rest pose, independent of what the player was doing when the
    recording started. Falls back to the recorded restCFrame for unreachable
    parts."""
    parts = [p for p in player_rig.get("parts", []) if p.get("name")]
    joints = player_rig.get("joints", [])

    parent_of = {}
    children_of = {}
    c0c1 = {}
    for j in joints:
        p0, p1 = j.get("part0"), j.get("part1")
        if not p0 or not p1:
            continue
        parent_of[p1] = p0
        children_of.setdefault(p0, []).append(p1)
        c0, c1 = j.get("c0"), j.get("c1")
        if c0 and len(c0) >= 12 and c1 and len(c1) >= 12:
            c0c1[(p0, p1)] = (c0, c1)

    names = [p["name"] for p in parts]
    rest_cf = {p["name"]: p.get("restCFrame") for p in parts}

    D = {}
    roots = [n for n in names if n not in parent_of]
    queue = list(roots)
    for r in roots:
        D[r] = Matrix.Identity(4)

    while queue:
        parent = queue.pop(0)
        for child in children_of.get(parent, []):
            if child in D:
                continue
            pair = c0c1.get((parent, child))
            if pair and parent in D:
                c0, c1 = pair
                c0m = roblox_components_to_blender_matrix(c0, scale)
                c1m = roblox_components_to_blender_matrix(c1, scale)
                D[child] = D[parent] @ c0m @ c1m.inverted()
            else:
                rcf = rest_cf.get(child)
                D[child] = (roblox_components_to_blender_matrix(rcf, scale)
                            if rcf and len(rcf) >= 12
                            else D.get(parent, Matrix.Identity(4)).copy())
            queue.append(child)

    for name in names:
        if name in D:
            continue
        rcf = rest_cf.get(name)
        D[name] = (roblox_components_to_blender_matrix(rcf, scale)
                   if rcf and len(rcf) >= 12 else Matrix.Identity(4))

    return D, parent_of, children_of, c0c1


def compute_joint_pivots(player_rig, D, c0c1, scale):
    """Joint pivots at the canonical T-pose, in armature space. The pivot of
    Motor6D(Part0=A, Part1=B) is D[A] @ C0. Returns:
        head_pivot[child]   = Vector (where `child` attaches to its parent)
        tail_pivots[parent] = [Vector, ...] (where children attach to it)"""
    head_pivot = {}
    tail_pivots = {}
    for (p0, p1), (c0, _c1) in c0c1.items():
        if p0 not in D:
            continue
        c0m = roblox_components_to_blender_matrix(c0, scale)
        pivot = (D[p0] @ c0m).translation.copy()
        head_pivot[p1] = pivot
        tail_pivots.setdefault(p0, []).append(pivot)
    return head_pivot, tail_pivots


# ----------------------------------------------------------------------------
# Armature + skinned mesh
# ----------------------------------------------------------------------------
def build_player(player_rig, label, scale, collection):
    """Build the armature + a single skinned mesh for one player.

    Returns a dict with:
        arm        : armature object
        parent_of  : { bone: parent_bone }
        D          : { bone: canonical rest matrix (Blender) }
        R          : { bone: bone.matrix_local }
        order      : [ bone_name, ... ]  (positional, matches .rec part order)
    or None if the rig had no usable parts.
    """
    parts = [p for p in player_rig.get("parts", []) if p.get("name")]
    if not parts:
        return None

    D, parent_of, _children_of, c0c1 = compute_canonical_rest_poses(player_rig, scale)
    head_pivot, tail_pivots = compute_joint_pivots(player_rig, D, c0c1, scale)
    order = [p["name"] for p in parts]

    # ---- armature with joint-to-joint bones (purely visual) ----
    arm_data = bpy.data.armatures.new(label + "_arm")
    arm_obj = bpy.data.objects.new(label + "_rig", arm_data)
    collection.objects.link(arm_obj)

    prev_active = bpy.context.view_layer.objects.active
    prev_mode = bpy.context.mode if bpy.context.mode else "OBJECT"
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    if prev_mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        ebs = arm_data.edit_bones
        for p in parts:
            name = p["name"]
            Qd = D.get(name, Matrix.Identity(4))
            size = p.get("size", [1.0, 1.0, 1.0])

            head = (head_pivot[name].copy() if name in head_pivot
                    else Qd.translation.copy())

            if tail_pivots.get(name):
                tail = max(tail_pivots[name],
                           key=lambda v: (v - head).length_squared).copy()
            else:
                # leaf: extend along the part's canonical local Y
                local_y = Vector((Qd[0][1], Qd[1][1], Qd[2][1]))
                if local_y.length < 1e-6:
                    local_y = Vector((0.0, 0.0, 1.0))
                local_y.normalize()
                tail = head + local_y * max(abs(float(size[1])) * scale, 0.1)

            eb = ebs.new(name)
            eb.head = head
            eb.tail = tail
            if (eb.tail - eb.head).length < 1e-4:
                eb.tail = eb.head + Vector((0.0, 0.0, 0.1))

        for child, parent in parent_of.items():
            if child in ebs and parent in ebs:
                ebs[child].parent = ebs[parent]
                ebs[child].use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = prev_active

    R = {b.name: b.matrix_local.copy() for b in arm_data.bones}
    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "QUATERNION"

    # ---- one combined skinned mesh ----
    mesh = bpy.data.meshes.new(label + "_mesh")
    bm = bmesh.new()
    groups = []        # (bone_name, [BMVert])
    mat_index = {}     # color/alpha key -> material slot index
    materials = []     # ordered list of materials for the mesh

    for p in parts:
        name = p["name"]
        if name not in R:
            continue
        # skip geometry for invisible parts (e.g. HumanoidRootPart) but KEEP the
        # bone — it still carries its children.
        if float(p.get("transparency", 0.0)) >= 0.999:
            continue
        place = D.get(name, Matrix.Identity(4))
        verts = _add_part_geometry(bm, p, place, scale)
        if not verts:
            continue
        groups.append((name, verts))

        key = "{}|{}".format(p.get("color", [0.7, 0.7, 0.7]),
                             p.get("transparency", 0.0))
        if key not in mat_index:
            mat_index[key] = len(materials)
            materials.append(_color_material(p.get("color", [0.7, 0.7, 0.7]),
                                             float(p.get("transparency", 0.0))))
        idx = mat_index[key]
        faces = set()
        for v in verts:
            faces.update(v.link_faces)
        for fc in faces:
            fc.material_index = idx

    bm.verts.index_update()
    group_indices = [(name, [v.index for v in verts]) for name, verts in groups]
    bm.to_mesh(mesh)
    bm.free()

    for m in materials:
        mesh.materials.append(m)

    mesh_obj = bpy.data.objects.new(label + "_skin", mesh)
    collection.objects.link(mesh_obj)
    for name, idxs in group_indices:
        vg = mesh_obj.vertex_groups.new(name=name)
        vg.add(idxs, 1.0, "REPLACE")

    mesh_obj.parent = arm_obj
    mesh_obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True

    return {
        "arm": arm_obj,
        "mesh": mesh_obj,
        "parent_of": parent_of,
        "D": D,
        "R": R,
        "order": order,
    }


# ----------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------
def _player_label(roster, uid):
    info = roster.get(uid, {})
    name = info.get("displayName") or info.get("name") or "Player"
    return "{}_{}".format(name, uid)


def import_replay(context, filepath, scale, set_fps, build_armature, report):
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
        if fmt_name != "ROCORDER/3":
            report({"ERROR"},
                   "This importer needs ROCORDER/3 recordings (got '{}'). "
                   "Please re-record with the current rocorder.lua.".format(fmt_name))
            return {"CANCELLED"}

        tick_rate = float(header.get("tickRate", 30))
        roster = {p["userId"]: p for p in header.get("roster", []) if "userId" in p}

        frames = []
        for raw in fh:
            t, data = parse_frame_line_v3(raw)
            if t is None:
                continue
            frames.append((t, data))

    if not frames:
        report({"ERROR"}, "No frames found in recording.")
        return {"CANCELLED"}

    rig_data = load_rig_file(filepath, header, report)
    rig_players = (rig_data or {}).get("players", {})

    scene = context.scene
    if set_fps:
        scene.render.fps = max(1, int(round(tick_rate)))
        scene.render.fps_base = 1.0
    fps = scene.render.fps / scene.render.fps_base

    base_name = bpy.path.display_name_from_filepath(filepath) or "ROCORDER"
    root_coll = bpy.data.collections.new("ROCORDER_" + base_name)
    scene.collection.children.link(root_coll)

    players = {}        # uid -> build_player() dict (armature path)
    fallback_objs = {}  # (uid, part_index) -> object (no-rig sphere path)
    fallback_quat = {}  # (uid, part_index) -> last Quaternion
    player_colls = {}

    def player_coll(uid):
        sub = player_colls.get(uid)
        if sub is None:
            sub = bpy.data.collections.new(_player_label(roster, uid))
            root_coll.children.link(sub)
            player_colls[uid] = sub
        return sub

    def ensure_player(uid):
        if uid in players:
            return players[uid]
        rig = rig_players.get(str(uid))
        if not rig:
            return None
        built = build_player(rig, _player_label(roster, uid), scale, player_coll(uid))
        if built is None:
            return None
        built["last_quat"] = {}
        built["arm"]["rocorder_user_id"] = str(uid)
        players[uid] = built
        return built

    use_armatures = build_armature and bool(rig_players)
    if use_armatures:
        for uid_key in rig_players:
            try:
                ensure_player(int(uid_key))
            except ValueError:
                pass

    last_frame = 1
    keyframes = 0
    bone_keys = 0

    for t, data in frames:
        frame_num = int(round(t * fps)) + 1
        last_frame = max(last_frame, frame_num)

        for uid, part_list in data.items():
            built = ensure_player(uid) if use_armatures else None

            if built is not None:
                order = built["order"]
                R = built["R"]
                D = built["D"]
                parent_of = built["parent_of"]
                last_q = built["last_quat"]
                arm = built["arm"]

                # 1) target pose.matrix per bone: pose = T @ D^-1 @ R
                pose = {}
                for i, vals in enumerate(part_list):
                    if i >= len(order):
                        break
                    name = order[i]
                    if name not in R:
                        continue
                    T = roblox_posquat_to_blender(*vals, scale)
                    pose[name] = T @ D[name].inverted() @ R[name]

                # 2) basis (parents handled explicitly) -> keyframe loc + quat
                for name, pmat in pose.items():
                    parent = parent_of.get(name)
                    if parent in pose:
                        basis = (R[name].inverted() @ R[parent]
                                 @ pose[parent].inverted() @ pmat)
                    else:
                        basis = R[name].inverted() @ pmat

                    loc, rot, _ = basis.decompose()
                    prev = last_q.get(name)
                    if prev is not None and prev.dot(rot) < 0.0:
                        rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
                    last_q[name] = rot

                    pb = arm.pose.bones.get(name)
                    if pb is None:
                        continue
                    pb.location = loc
                    pb.rotation_quaternion = rot
                    pb.keyframe_insert(data_path="location", frame=frame_num)
                    pb.keyframe_insert(data_path="rotation_quaternion", frame=frame_num)
                    keyframes += 2
                    bone_keys += 1
                continue

            # ---- fallback: no rig (or armature disabled) -> a sphere per part ----
            sub = player_coll(uid)
            for i, vals in enumerate(part_list):
                key = (uid, i)
                obj = fallback_objs.get(key)
                if obj is None:
                    m = bpy.data.meshes.new("{}_p{}_mesh".format(_player_label(roster, uid), i))
                    bm = bmesh.new()
                    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=6,
                                              radius=0.5 * scale)
                    bm.to_mesh(m)
                    bm.free()
                    obj = bpy.data.objects.new("{}_p{}".format(_player_label(roster, uid), i), m)
                    obj.rotation_mode = "QUATERNION"
                    sub.objects.link(obj)
                    fallback_objs[key] = obj

                T = roblox_posquat_to_blender(*vals, scale)
                loc, rot, _ = T.decompose()
                prev = fallback_quat.get(key)
                if prev is not None and prev.dot(rot) < 0.0:
                    rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
                fallback_quat[key] = rot
                obj.location = loc
                obj.rotation_quaternion = rot
                obj.keyframe_insert(data_path="location", frame=frame_num)
                obj.keyframe_insert(data_path="rotation_quaternion", frame=frame_num)
                keyframes += 2

    # linear interpolation everywhere
    def set_linear(obj):
        ad = obj.animation_data
        if not ad or not ad.action:
            return
        for fc in ad.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

    for built in players.values():
        set_linear(built["arm"])
    for obj in fallback_objs.values():
        set_linear(obj)

    scene.frame_start = 1
    scene.frame_end = last_frame
    scene.frame_current = 1

    report({"INFO"},
           "ROCORDER v3: {} armatures, {} fallback objs, {} frames, "
           "{} keyframes ({} bone-keys).".format(
               len(players), len(fallback_objs), len(frames), keyframes, bone_keys))
    return {"FINISHED"}


# ----------------------------------------------------------------------------
# Operator / UI
# ----------------------------------------------------------------------------
class IMPORT_OT_rocorder(Operator, ImportHelper):
    bl_idname = "import_scene.rocorder"
    bl_label = "Import Roblox Replay"
    bl_description = "Import a ROCORDER .rec file as a skinned, animated armature"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".rec"
    filter_glob: StringProperty(default="*.rec", options={"HIDDEN"})

    scale: FloatProperty(
        name="Scale",
        description="World scale. 1.0 = 1 Roblox stud per Blender unit",
        default=1.0, min=0.0001, soft_max=10.0,
    )
    set_scene_fps: BoolProperty(
        name="Match scene FPS to recording",
        description="Set scene FPS to the recording's tick rate so 1 frame == 1 sample",
        default=True,
    )
    build_armature: BoolProperty(
        name="Build armature per player",
        description="Build a real armature (bones mirror Roblox's Motor6D rig) "
                    "with a skinned mesh bound via an Armature modifier. Turn "
                    "off to import plain animated spheres instead",
        default=True,
    )

    def execute(self, context):
        return import_replay(
            context, self.filepath, self.scale,
            self.set_scene_fps, self.build_armature, self.report,
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
