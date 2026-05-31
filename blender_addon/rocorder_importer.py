bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (1, 3, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replays as skinned, animated armatures",
    "warning": "Alpha — file formats and options may still change",
    "category": "Import-Export",
}
ROCORDER_VERSION = "1.3.0-alpha"

# ============================================================================
# Skinning math (why bone visuals can be anything without breaking animation)
# ----------------------------------------------------------------------------
# A vertex of part P, bound 100% to bone B, deforms to:
#       world_vert = pose.matrix[B] @ rest.matrix[B]^-1 @ vert_rest
# We place verts at canonical rest D[P] (= D @ v_local), set rest = R = bone
# matrix_local. To force the vertex to recorded world T @ v_local:
#       pose.matrix[B] = T @ D[P]^-1 @ R
# This works for ANY R, so bones can be drawn however we want (joint-to-joint,
# with whatever roll) and accuracy is unaffected.
# ============================================================================

import json
import math
import os
import time
import bpy
import bmesh
from bpy.props import StringProperty, FloatProperty, BoolProperty
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Quaternion


ROBLOX_TO_BLENDER = Matrix((
    (1, 0,  0, 0),
    (0, 0, -1, 0),
    (0, 1,  0, 0),
    (0, 0,  0, 1),
))
ROBLOX_TO_BLENDER_INV = ROBLOX_TO_BLENDER.inverted()


# Standard R6 Motor6D C0/C1 in CFrame:GetComponents() row-major form
# (x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22). These are the
# values Roblox sets on a freshly-spawned R6 character. Many games mutate
# C0/C1 at runtime (e.g. shooters that rotate the upper body to aim, or
# scripts that "look at" the cursor), which means the captured C0/C1 no
# longer represent the structural rest — they encode whatever the game was
# doing at capture time. Overriding with these constants gives every R6
# character a clean canonical T-pose regardless of script interference.
R6_STANDARD_JOINTS = {
    # (part0, part1): (c0, c1)
    ("HumanoidRootPart", "Torso"): (
        [0, 0, 0,  -1, 0, 0,  0, 0, 1,  0, 1, 0],
        [0, 0, 0,  -1, 0, 0,  0, 0, 1,  0, 1, 0],
    ),
    ("Torso", "Head"): (
        [0,  1.0, 0,  -1, 0, 0,  0, 0, 1,  0, 1, 0],
        [0, -0.5, 0,  -1, 0, 0,  0, 0, 1,  0, 1, 0],
    ),
    ("Torso", "Right Arm"): (
        [ 1.0, 0.5, 0,  0, 0, 1,  0, 1, 0,  -1, 0, 0],
        [-0.5, 0.5, 0,  0, 0, 1,  0, 1, 0,  -1, 0, 0],
    ),
    ("Torso", "Left Arm"): (
        [-1.0, 0.5, 0,  0, 0, -1,  0, 1, 0,  1, 0, 0],
        [ 0.5, 0.5, 0,  0, 0, -1,  0, 1, 0,  1, 0, 0],
    ),
    ("Torso", "Right Leg"): (
        [1.0, -1.0, 0,  0, 0, 1,  0, 1, 0,  -1, 0, 0],
        [0.5,  1.0, 0,  0, 0, 1,  0, 1, 0,  -1, 0, 0],
    ),
    ("Torso", "Left Leg"): (
        [-1.0, -1.0, 0,  0, 0, -1,  0, 1, 0,  1, 0, 0],
        [-0.5,  1.0, 0,  0, 0, -1,  0, 1, 0,  1, 0, 0],
    ),
}


# ----------------------------------------------------------------------------
# Coordinate conversion
# ----------------------------------------------------------------------------
def _conjugate(rob_mat, scale):
    mat = ROBLOX_TO_BLENDER @ rob_mat @ ROBLOX_TO_BLENDER_INV
    mat.translation = mat.translation * scale
    return mat


def roblox_components_to_blender_matrix(comp, scale):
    rob = Matrix((
        (comp[3], comp[4],  comp[5],  comp[0]),
        (comp[6], comp[7],  comp[8],  comp[1]),
        (comp[9], comp[10], comp[11], comp[2]),
        (0.0,     0.0,      0.0,      1.0),
    ))
    return _conjugate(rob, scale)


def roblox_posquat_to_blender(px, py, pz, qx, qy, qz, qw, scale):
    rot = Quaternion((qw, qx, qy, qz))
    n = rot.magnitude
    rot = rot * (1.0 / n) if n > 1e-12 else Quaternion((1.0, 0.0, 0.0, 0.0))
    rob = rot.to_matrix().to_4x4()
    rob.translation = Vector((px, py, pz))
    return _conjugate(rob, scale)


def roblox_cam_posquat_to_blender(px, py, pz, qx, qy, qz, qw, scale):
    """Camera conversion: only LEFT-multiply by the axis swap, no right
    multiplication. Body parts conjugate (R_swap @ M @ R_swap^-1) because we
    want their LOCAL frame's axes to line up between Roblox and Blender for
    vertex coordinates. Cameras instead need Blender's camera convention (view
    along local -Z, up = +Y) to map onto Roblox's (view along local -Z, up =
    +Y) after the world-space axis swap — and that requires skipping the right
    multiplication. The result is a 4x4 such that the Blender camera's
    lookVector in world space equals R_swap applied to Roblox's lookVector."""
    rot = Quaternion((qw, qx, qy, qz))
    n = rot.magnitude
    rot = rot * (1.0 / n) if n > 1e-12 else Quaternion((1.0, 0.0, 0.0, 0.0))
    rob = rot.to_matrix().to_4x4()
    rob.translation = Vector((px, py, pz))
    mat = ROBLOX_TO_BLENDER @ rob
    mat.translation = mat.translation * scale
    return mat


# ----------------------------------------------------------------------------
# Frame parsing
# ----------------------------------------------------------------------------
def parse_frame_line_v3(line):
    """'t=1.23;uid:p0|p1|...;cam:px,py,pz,qx,qy,qz,qw,fov;...'
       -> (t, { uid: [ (7 floats), ... ] }, camera_tuple_or_None)

    Camera chunks (prefix 'cam:') are parsed into an 8-float tuple. Unknown
    prefixes are skipped so future sources don't break the importer.
    """
    line = line.strip()
    if not line:
        return None, None, None
    parts = line.split(";")
    if not parts[0].startswith("t="):
        return None, None, None
    try:
        t = float(parts[0][2:])
    except ValueError:
        return None, None, None

    players = {}
    camera = None
    for chunk in parts[1:]:
        if ":" not in chunk:
            continue
        prefix, blob = chunk.split(":", 1)
        if prefix == "cam":
            vals = blob.split(",")
            if len(vals) == 8:
                try:
                    camera = tuple(float(v) for v in vals)
                except ValueError:
                    pass
            continue
        # otherwise treat as a player uid
        try:
            uid = int(prefix)
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
            players[uid] = part_vals
    return t, players, camera


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

    bmesh.ops.transform(bm, matrix=place_mat, verts=new_verts)
    return new_verts


# ----------------------------------------------------------------------------
# Rig file
# ----------------------------------------------------------------------------
def load_rig_file(rec_filepath, header, log):
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
            log("Loading rig file: {}".format(path))
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError) as e:
                log("ERROR: rig file {} unparseable: {}".format(path, e))
                return None
    log("WARNING: no .rig.json found; tried: {}".format(", ".join(candidates)))
    return None


# ----------------------------------------------------------------------------
# Canonical rest pose
# ----------------------------------------------------------------------------
def compute_canonical_rest_poses(player_rig, scale, force_standard_r6=True):
    """Build the canonical rest pose by walking Motor6D C0/C1 from each root.

    Diagnostic reports (returned for the log):
        skipped_self_loops : joints with part0 == part1 (engines occasionally
                             produce these and they create cycles in the bone
                             hierarchy that silently kill skinning)
        overridden_joints  : standard R6 joints whose captured C0/C1 we
                             replaced with canonical defaults
    """
    parts = [p for p in player_rig.get("parts", []) if p.get("name")]
    joints = player_rig.get("joints", [])
    rig_type = player_rig.get("rigType")
    use_std = force_standard_r6 and rig_type == "R6"

    parent_of = {}
    children_of = {}
    c0c1 = {}
    skipped_self_loops = []
    overridden_joints = []
    for j in joints:
        p0, p1 = j.get("part0"), j.get("part1")
        if not p0 or not p1:
            continue
        # Self-loops (e.g. an extra Motor6D with Part0 == Part1) create a
        # cycle in parent_of when seen after the real joint, which collapses
        # the pose-basis formula to identity for that bone. Skip them.
        if p0 == p1:
            skipped_self_loops.append((p0, p1, j.get("name")))
            continue

        c0, c1 = j.get("c0"), j.get("c1")
        if use_std:
            std = R6_STANDARD_JOINTS.get((p0, p1))
            if std is not None:
                c0_std, c1_std = std
                overridden_joints.append((p0, p1, j.get("name")))
                c0, c1 = c0_std, c1_std

        parent_of[p1] = p0
        children_of.setdefault(p0, []).append(p1)
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

    return D, parent_of, children_of, c0c1, roots, skipped_self_loops, overridden_joints


def compute_joint_pivots(player_rig, D, c0c1, scale):
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
def build_player(player_rig, label, scale, collection, log, force_standard_r6=True):
    parts = [p for p in player_rig.get("parts", []) if p.get("name")]
    if not parts:
        log("  no parts in rig — skipping player")
        return None

    (D, parent_of, _children_of, c0c1, roots,
     skipped_self_loops, overridden_joints) = compute_canonical_rest_poses(
        player_rig, scale, force_standard_r6=force_standard_r6)
    head_pivot, tail_pivots = compute_joint_pivots(player_rig, D, c0c1, scale)
    order = [p["name"] for p in parts]

    if skipped_self_loops:
        log("  *** skipped {} self-loop joints (these would have broken "
            "skinning):".format(len(skipped_self_loops)))
        for p0, p1, jname in skipped_self_loops:
            log("    {!r}: {} -> {}".format(jname, p0, p1))
    if overridden_joints:
        log("  applied standard R6 C0/C1 to {} joints (rigType=R6, "
            "force_standard_r6=True):".format(len(overridden_joints)))
        for p0, p1, jname in overridden_joints:
            log("    {!r}: {} -> {}".format(jname, p0, p1))

    log("  roots: {}".format(roots))
    log("  parent_of:")
    for child, parent in parent_of.items():
        log("    {} <- {}".format(child, parent))
    log("  canonical D (translation, Blender coords):")
    for name in order:
        t = D.get(name, Matrix.Identity(4)).translation
        log("    {:24s} ({:+.3f}, {:+.3f}, {:+.3f})".format(name, t.x, t.y, t.z))

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

    requested_names = []   # what we tried to create
    created_names = []     # what edit_bones actually held after creation
    try:
        ebs = arm_data.edit_bones
        for p in parts:
            name = p["name"]
            Qd = D.get(name, Matrix.Identity(4))
            size = p.get("size", [1.0, 1.0, 1.0])
            center = Qd.translation.copy()

            # ---- head: at this part's incoming joint (or part center for roots)
            head = head_pivot[name].copy() if name in head_pivot else center.copy()

            # ---- tail: at farthest child joint pivot, OR for leaves, extend
            # from head through the part center to the far end. This is much
            # more robust than "extend along the part's local Y" because it
            # works no matter how the part's local axes happen to be oriented.
            tail = None
            if tail_pivots.get(name):
                tail = max(tail_pivots[name],
                           key=lambda v: (v - head).length_squared).copy()
            else:
                # Leaf bone: aim the bone from the parent joint through the
                # part center, with length = max(part extent, 2*head→center).
                direction = center - head
                d_len = direction.length
                if d_len > 1e-5:
                    direction = direction / d_len
                    # rough extent: largest blender-space dimension of the part
                    extent = max(
                        abs(float(size[0])), abs(float(size[1])), abs(float(size[2])),
                    ) * scale
                    length = max(extent, 2.0 * d_len, 0.1)
                else:
                    # head sits at the part center exactly — fall back to local Y
                    local_y = Vector((Qd[0][1], Qd[1][1], Qd[2][1]))
                    if local_y.length < 1e-6:
                        local_y = Vector((0.0, 0.0, 1.0))
                    direction = local_y.normalized()
                    length = max(abs(float(size[1])) * scale, 0.1)
                tail = head + direction * length

            requested_names.append(name)
            eb = ebs.new(name)
            eb.head = head
            eb.tail = tail
            if (eb.tail - eb.head).length < 1e-4:
                eb.tail = eb.head + Vector((0.0, 0.0, 0.1))

            # Roll: align the bone's local Z to the part's canonical local Z,
            # so the bone visual aligns with the part's orientation. (Purely
            # cosmetic — skinning math is unaffected by R.)
            local_z = Vector((Qd[0][2], Qd[1][2], Qd[2][2]))
            if local_z.length > 1e-6:
                try:
                    eb.align_roll(local_z)
                except Exception as e:
                    log("    WARN align_roll failed for {}: {}".format(name, e))

            created_names.append(eb.name)  # may differ if Blender renamed

        for child, parent in parent_of.items():
            if child == parent:
                log("    skip self-parent edit-bone link: {}".format(child))
                continue
            if child in ebs and parent in ebs:
                ebs[child].parent = ebs[parent]
                ebs[child].use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.context.view_layer.objects.active = prev_active

    R = {b.name: b.matrix_local.copy() for b in arm_data.bones}
    for pb in arm_obj.pose.bones:
        pb.rotation_mode = "QUATERNION"

    # ---- detect Blender renames or silent drops ----
    rename_map = {}      # original -> renamed (only when different)
    missing = []
    for req, got in zip(requested_names, created_names):
        if req != got:
            rename_map[req] = got
        if got not in R:
            missing.append(req)
    if rename_map:
        log("  WARN bones renamed by Blender (skinning will break for these):")
        for req, got in rename_map.items():
            log("    {!r} -> {!r}".format(req, got))
    if missing:
        log("  WARN bones missing from R after edit-mode exit: {}".format(missing))
    log("  bones requested={} created={} in R={}".format(
        len(requested_names), len(created_names), len(R)))

    # ---- combined skinned mesh ----
    mesh = bpy.data.meshes.new(label + "_mesh")
    bm = bmesh.new()
    groups = []
    mat_index = {}
    materials = []
    skipped_parts = []

    for p in parts:
        name = p["name"]
        if name not in R:
            skipped_parts.append((name, "bone missing from R"))
            continue
        if float(p.get("transparency", 0.0)) >= 0.999:
            skipped_parts.append((name, "transparency>=0.999 (bone kept, geom skipped)"))
            continue
        place = D.get(name, Matrix.Identity(4))
        verts = _add_part_geometry(bm, p, place, scale)
        if not verts:
            skipped_parts.append((name, "no verts produced"))
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

    if skipped_parts:
        log("  mesh build — skipped {} parts:".format(len(skipped_parts)))
        for name, reason in skipped_parts:
            log("    {}: {}".format(name, reason))
    log("  mesh build — verts={} mats={} vertex_groups={}".format(
        len(mesh.vertices), len(materials), len(mesh_obj.vertex_groups)))

    return {
        "arm": arm_obj,
        "mesh": mesh_obj,
        "parent_of": parent_of,
        "D": D,
        "R": R,
        "order": order,
        "rename_map": rename_map,
    }


# ----------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------
def _player_label(roster, uid):
    info = roster.get(uid, {})
    name = info.get("displayName") or info.get("name") or "Player"
    return "{}_{}".format(name, uid)


def _short_matrix(m):
    t = m.translation
    return "({:+.3f},{:+.3f},{:+.3f})".format(t.x, t.y, t.z)


def import_replay(context, filepath, scale, set_fps, build_armature,
                  force_standard_r6, debug, report):
    log_lines = []
    log_path = None
    if debug:
        base, _ = os.path.splitext(filepath)
        log_path = base + ".import.log"

    def log(msg):
        line = str(msg)
        print("[ROCORDER]", line)
        log_lines.append(line)

    def flush_log():
        if log_path:
            try:
                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(log_lines) + "\n")
            except OSError as e:
                print("[ROCORDER] could not write import log:", e)

    log("=" * 76)
    log("ROCORDER import @ {} — importer v{}".format(
        time.strftime("%Y-%m-%d %H:%M:%S"), ROCORDER_VERSION))
    log("file:  {}".format(filepath))
    log("scale: {}  set_fps: {}  build_armature: {}  force_standard_r6: {}".format(
        scale, set_fps, build_armature, force_standard_r6))

    try:
        fh = open(filepath, "r", encoding="utf-8")
    except OSError as e:
        report({"ERROR"}, "Could not open file: {}".format(e))
        log("FATAL open() failed: {}".format(e))
        flush_log()
        return {"CANCELLED"}

    with fh:
        header_line = fh.readline()
        try:
            header = json.loads(header_line)
        except json.JSONDecodeError:
            report({"ERROR"}, "First line is not a valid ROCORDER header.")
            log("FATAL header JSON invalid")
            flush_log()
            return {"CANCELLED"}

        fmt_name = header.get("format")
        log("header.format = {}".format(fmt_name))
        if fmt_name != "ROCORDER/3":
            report({"ERROR"},
                   "This importer needs ROCORDER/3 (got '{}'). Re-record.".format(fmt_name))
            log("FATAL wrong format")
            flush_log()
            return {"CANCELLED"}

        tick_rate = float(header.get("tickRate", 30))
        roster = {p["userId"]: p for p in header.get("roster", []) if "userId" in p}
        log("tickRate={} roster_uids={}".format(tick_rate, list(roster.keys())))

        frames = []
        bad_lines = 0
        has_camera = False
        for raw in fh:
            t, players, camera = parse_frame_line_v3(raw)
            if t is None:
                bad_lines += 1
                continue
            if camera is not None:
                has_camera = True
            frames.append((t, players, camera))
        if bad_lines:
            log("WARN {} unparseable lines skipped".format(bad_lines))
        log("camera frames: {}".format("present" if has_camera else "none"))

    log("frames parsed: {}".format(len(frames)))
    if not frames:
        report({"ERROR"}, "No frames found in recording.")
        log("FATAL no frames")
        flush_log()
        return {"CANCELLED"}

    rig_data = load_rig_file(filepath, header, log)
    rig_players = (rig_data or {}).get("players", {})
    log("rig players: {}".format(list(rig_players.keys())))

    scene = context.scene
    if set_fps:
        scene.render.fps = max(1, int(round(tick_rate)))
        scene.render.fps_base = 1.0
    fps = scene.render.fps / scene.render.fps_base
    log("scene fps = {}".format(fps))

    base_name = bpy.path.display_name_from_filepath(filepath) or "ROCORDER"
    root_coll = bpy.data.collections.new("ROCORDER_" + base_name)
    scene.collection.children.link(root_coll)

    players = {}
    fallback_objs = {}
    fallback_quat = {}
    player_colls = {}

    # Lazy: only create a camera object when we actually see camera data.
    camera_obj  = None
    camera_data = None
    camera_last_quat = None

    def ensure_camera():
        nonlocal camera_obj, camera_data
        if camera_obj is not None:
            return camera_obj
        camera_data = bpy.data.cameras.new(base_name + "_camera")
        # Vertical fit so cam_data.angle == Roblox's FieldOfView (vertical FOV)
        camera_data.sensor_fit = "VERTICAL"
        camera_data.lens_unit = "FOV"
        camera_obj = bpy.data.objects.new(base_name + "_camera", camera_data)
        camera_obj.rotation_mode = "QUATERNION"
        root_coll.objects.link(camera_obj)
        log("created camera object: {} (sensor_fit=VERTICAL, FOV-driven)".format(
            camera_obj.name))
        return camera_obj

    # per-uid stats for end-of-import diagnostics
    bone_keycount = {}    # uid -> { bone: int }
    frame_seen   = {}     # uid -> set of frame_num
    part_count_mismatch = {}  # uid -> [(frame_num, got, expected), ...]

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
            log("uid {} has no rig data — going fallback".format(uid))
            return None
        log("---- building player uid={} ({}) ----".format(uid, rig.get("name")))
        log("  rigType={} parts={} joints={}".format(
            rig.get("rigType"), len(rig.get("parts", [])), len(rig.get("joints", []))))
        built = build_player(rig, _player_label(roster, uid), scale,
                             player_coll(uid), log,
                             force_standard_r6=force_standard_r6)
        if built is None:
            return None
        built["last_quat"] = {}
        built["arm"]["rocorder_user_id"] = str(uid)
        players[uid] = built
        bone_keycount[uid] = {name: 0 for name in built["R"]}
        frame_seen[uid] = set()
        part_count_mismatch[uid] = []
        return built

    use_armatures = build_armature and bool(rig_players)
    if use_armatures:
        for uid_key in rig_players:
            try:
                ensure_player(int(uid_key))
            except ValueError:
                log("WARN non-int uid_key in rig: {}".format(uid_key))

    last_frame = 1
    keyframes = 0
    bone_keys = 0

    camera_keys = 0
    for t, data, camera in frames:
        frame_num = int(round(t * fps)) + 1
        last_frame = max(last_frame, frame_num)

        if camera is not None:
            ensure_camera()
            px, py, pz, qx, qy, qz, qw, fov = camera
            cam_mat = roblox_cam_posquat_to_blender(
                px, py, pz, qx, qy, qz, qw, scale)
            loc, rot, _ = cam_mat.decompose()
            if camera_last_quat is not None and camera_last_quat.dot(rot) < 0.0:
                rot = Quaternion((-rot.w, -rot.x, -rot.y, -rot.z))
            camera_last_quat = rot
            camera_obj.location = loc
            camera_obj.rotation_quaternion = rot
            camera_obj.keyframe_insert(data_path="location", frame=frame_num)
            camera_obj.keyframe_insert(data_path="rotation_quaternion",
                                       frame=frame_num)
            # FOV: Roblox stores vertical FOV in degrees; Blender's
            # cam_data.angle is in radians. With sensor_fit=VERTICAL the
            # mapping is exact.
            camera_data.angle = math.radians(max(1e-3, float(fov)))
            camera_data.keyframe_insert(data_path="angle", frame=frame_num)
            camera_keys += 3

        for uid, part_list in data.items():
            built = ensure_player(uid) if use_armatures else None

            if built is not None:
                order = built["order"]
                R = built["R"]
                D = built["D"]
                parent_of = built["parent_of"]
                last_q = built["last_quat"]
                arm = built["arm"]

                if len(part_list) != len(order):
                    part_count_mismatch[uid].append(
                        (frame_num, len(part_list), len(order)))
                frame_seen[uid].add(frame_num)

                pose = {}
                for i, vals in enumerate(part_list):
                    if i >= len(order):
                        break
                    name = order[i]
                    if name not in R:
                        continue
                    T = roblox_posquat_to_blender(*vals, scale)
                    pose[name] = T @ D[name].inverted() @ R[name]

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
                    bone_keycount[uid][name] += 1
                continue

            # fallback (no rig / armature off)
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
    if camera_obj is not None:
        set_linear(camera_obj)
        if camera_data is not None and camera_data.animation_data \
                and camera_data.animation_data.action:
            for fc in camera_data.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"

    scene.frame_start = 1
    scene.frame_end = last_frame
    scene.frame_current = 1

    # ============ END-OF-IMPORT DIAGNOSTICS ============
    log("")
    log("=" * 30 + " DIAGNOSTICS " + "=" * 30)
    for uid, built in players.items():
        log("uid {} ({}):".format(uid, _player_label(roster, uid)))

        kc = bone_keycount[uid]
        zero_bones = [b for b, c in kc.items() if c == 0]
        if zero_bones:
            log("  *** {} BONES WITH ZERO KEYFRAMES (these are the 'missing' parts):".format(
                len(zero_bones)))
            for b in zero_bones:
                log("       - {}".format(b))
        else:
            log("  all {} bones got keyframes".format(len(kc)))

        # frame coverage / gap detection
        seen = sorted(frame_seen[uid])
        if seen:
            gaps = []
            for a, b in zip(seen, seen[1:]):
                if b - a > 1:
                    gaps.append((a, b, b - a - 1))
            log("  frame coverage: {} frames, range [{}..{}], gaps={}".format(
                len(seen), seen[0], seen[-1], len(gaps)))
            for a, b, missing in gaps[:20]:
                log("    gap: no keyframes between {} and {} ({} frames missing)".format(
                    a, b, missing))
            if len(gaps) > 20:
                log("    ... and {} more gaps".format(len(gaps) - 20))

        mis = part_count_mismatch[uid]
        if mis:
            log("  *** {} frames had wrong part count (corrupt/truncated lines):".format(
                len(mis)))
            for fnum, got, exp in mis[:10]:
                log("    frame {}: got {} parts, expected {}".format(fnum, got, exp))
            if len(mis) > 10:
                log("    ... and {} more mismatches".format(len(mis) - 10))

        # bone keyframe distribution — useful to spot bones that animated
        # only briefly (one tally per uid; bones sorted by ascending count)
        sorted_kc = sorted(kc.items(), key=lambda x: x[1])
        # only log the lowest 5 — full list is mostly noise
        log("  bones with fewest keyframes:")
        for b, c in sorted_kc[:5]:
            log("    {}: {}".format(b, c))

    log("")
    log("totals: armatures={} fallback_objs={} cameras={} frames={} "
        "keyframes={} bone_keys={} camera_keys={}".format(
            len(players), len(fallback_objs), camera_obj and 1 or 0,
            len(frames), keyframes + camera_keys, bone_keys, camera_keys))
    log("=" * 76)
    flush_log()
    if log_path:
        report({"INFO"}, "ROCORDER imported. Debug log: {}".format(log_path))
    else:
        report({"INFO"},
               "ROCORDER: {} armatures, {} frames, {} keyframes".format(
                   len(players), len(frames), keyframes))
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
        name="Scale", description="1.0 = 1 Roblox stud per Blender unit",
        default=1.0, min=0.0001, soft_max=10.0,
    )
    set_scene_fps: BoolProperty(
        name="Match scene FPS to recording",
        description="Set scene FPS to the recording's tick rate",
        default=True,
    )
    build_armature: BoolProperty(
        name="Build armature per player",
        description="Build an armature + skinned mesh per player. "
                    "Off = plain animated spheres",
        default=True,
    )
    force_standard_r6: BoolProperty(
        name="Force standard R6 rest pose",
        description="Override captured Motor6D C0/C1 with canonical R6 default "
                    "values. Many games mutate C0/C1 at runtime (shooters that "
                    "rotate the upper body to aim, look-at-cursor scripts, etc.), "
                    "which makes the captured 'rest pose' look twisted. With "
                    "this on, every R6 character starts from a clean T-pose. "
                    "No effect on R15 or custom rigs",
        default=True,
    )
    debug: BoolProperty(
        name="Write debug log",
        description="Write a verbose .import.log next to the .rec listing the "
                    "rig structure, bones created, mesh build, keyframe counts "
                    "per bone, and any anomalies. Recommended while diagnosing "
                    "rig issues",
        default=True,
    )

    def execute(self, context):
        return import_replay(
            context, self.filepath, self.scale,
            self.set_scene_fps, self.build_armature,
            self.force_standard_r6, self.debug, self.report,
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
