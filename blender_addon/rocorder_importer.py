bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (1, 9, 20),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replays as skinned, animated armatures",
    "warning": "Alpha — file formats and options may still change",
    "category": "Import-Export",
}
ROCORDER_VERSION = "1.9.20-alpha"

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
import re
import struct
import time
import urllib.request
import urllib.error
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
            # blend_method was removed from Material in Blender 4.3+ (EEVEE
            # Next); guard so setting it can't abort the import.
            try:
                mat.blend_method = "BLEND"
            except (AttributeError, TypeError):
                pass
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


# ============================================================================
# Roblox asset fetching + mesh parsing + textures
# ----------------------------------------------------------------------------
# The recorder captures every part's MeshId / TextureID / ColorMap. Here we
# download those assets from Roblox's CDN (no browser => no CORS), parse the
# binary mesh format, and build real geometry + UV-mapped image materials so
# the Blender scene matches what the player saw in-game. Everything is cached
# on disk so re-imports are instant and assets shared across players (or across
# imports) only download once. Any failure degrades gracefully to a box.
# ============================================================================

def _asset_id(ref):
    """Extract the numeric asset id from any Roblox content string:
    'rbxassetid://123', 'http://www.roblox.com/asset/?id=123',
    'https://assetdelivery.roblox.com/v1/asset/?id=123', or bare '123'."""
    if not ref:
        return None
    s = str(ref)
    m = re.search(r"(\d{4,})", s)  # asset ids are long integers
    return m.group(1) if m else None


class AssetFetcher:
    def __init__(self, cache_dir, cookie, log, throttle=0.05, local_dirs=None):
        self.cache_dir = cache_dir
        self.cookie = (cookie or "").strip()
        self.log = log
        self.throttle = throttle
        # Folders the recorder pre-downloaded assets into (named just '<id>').
        # Checked BEFORE the network — this is the reliable path, since the
        # executor downloads with a real authenticated session.
        self.local_dirs = [d for d in (local_dirs or []) if d and os.path.isdir(d)]
        self._mesh_cache = {}     # id -> parsed mesh dict (or False on failure)
        self._image_cache = {}    # id -> local image path (or False)
        self._last_request = 0.0
        self._auth_body_logged = False
        self.stats = {"downloads": 0, "cache_hits": 0, "fails": 0,
                      "auth_fails": 0, "local_hits": 0}
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as e:
            self.log("WARN could not create asset cache dir {}: {}".format(cache_dir, e))

    def _find_local(self, asset_id):
        """Return a path to a recorder-downloaded asset file for this id, if any
        (file named exactly '<id>' or '<id>.<ext>'). Honors a user-drop folder
        so you can hand-place any asset the executor couldn't reach."""
        for d in self.local_dirs:
            exact = os.path.join(d, asset_id)
            if os.path.isfile(exact) and os.path.getsize(exact) > 0:
                return exact
            try:
                for fn in os.listdir(d):
                    if fn == asset_id or fn.startswith(asset_id + "."):
                        p = os.path.join(d, fn)
                        if os.path.isfile(p) and os.path.getsize(p) > 0:
                            return p
            except OSError:
                pass
        return None

    def _headers(self):
        h = {"User-Agent": "Roblox/WinInet", "Accept": "*/*"}
        if self.cookie:
            h["Cookie"] = ".ROBLOSECURITY={}".format(self.cookie)
        return h

    def _throttle_wait(self):
        import time as _t
        dt = _t.monotonic() - self._last_request
        if dt < self.throttle:
            _t.sleep(self.throttle - dt)

    def _http_get(self, url, retries=2):
        """GET url -> (data_bytes_or_None, status_int). Retries only on
        network / 429 / 5xx; auth errors (401/403) and 404 fail immediately
        (retrying won't change the answer)."""
        import time as _t
        for attempt in range(retries + 1):
            self._throttle_wait()
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                self._last_request = _t.monotonic()
                return (data if data else None), 200
            except urllib.error.HTTPError as e:
                self._last_request = _t.monotonic()
                if e.code in (401, 403) and not self._auth_body_logged:
                    self._auth_body_logged = True
                    try:
                        body = e.read()[:300].decode("utf-8", "ignore")
                    except Exception:
                        body = "<unreadable>"
                    self.log("    (first auth error {} body: {})".format(e.code, body))
                if e.code in (401, 403, 404):
                    return None, e.code          # no point retrying
                if e.code == 429 or 500 <= e.code < 600:
                    _t.sleep(0.5 * (attempt + 1))  # transient: back off
                    continue
                return None, e.code
            except (urllib.error.URLError, OSError, ValueError) as e:
                self._last_request = _t.monotonic()
                if attempt == retries:
                    self.log("    GET {} failed: {}".format(url, e))
                _t.sleep(0.4 * (attempt + 1))
        return None, 0

    # ---- raw download (with on-disk cache) -------------------------------
    def _download(self, asset_id, ext):
        """Return the local cached file path for an asset id, downloading if
        needed. Tries the v1 endpoint, then the authenticated v2 CDN-location
        flow. ext is a hint for the cache filename only."""
        # 0) recorder-downloaded local asset (preferred — no network, no 401)
        local = self._find_local(asset_id)
        if local:
            self.stats["local_hits"] += 1
            return local

        cache_path = os.path.join(self.cache_dir, "{}{}".format(asset_id, ext))
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
            self.stats["cache_hits"] += 1
            return cache_path

        # 1) v1 direct
        data, status = self._http_get(
            "https://assetdelivery.roblox.com/v1/asset/?id={}".format(asset_id))

        # 2) on auth failure, try the v2 location flow (returns a signed CDN url)
        if data is None and status in (401, 403):
            data = self._fetch_via_v2(asset_id)
            if data is None:
                self.stats["auth_fails"] += 1
                self.log("    asset {} -> {} Unauthorized (needs a "
                         ".ROBLOSECURITY cookie)".format(asset_id, status))
                self.stats["fails"] += 1
                return None

        if data is None:
            self.log("    asset {} download failed (status {})".format(asset_id, status))
            self.stats["fails"] += 1
            return None

        # old "decal" assets return an XML wrapper pointing at the real image id
        if data[:5] == b"<?xml" or data.lstrip()[:7] == b"<roblox":
            inner = self._xml_inner_id(data)
            if inner and inner != asset_id:
                self.log("    asset {} is a wrapper -> {}".format(asset_id, inner))
                return self._download(inner, ext)

        try:
            with open(cache_path, "wb") as fh:
                fh.write(data)
        except OSError as e:
            self.log("    could not cache asset {}: {}".format(asset_id, e))
            return None
        self.stats["downloads"] += 1
        return cache_path

    def _fetch_via_v2(self, asset_id):
        """Authenticated metadata call -> signed CDN location -> bytes."""
        meta, status = self._http_get(
            "https://assetdelivery.roblox.com/v2/assetId/{}".format(asset_id))
        if meta is None:
            return None
        try:
            info = json.loads(meta.decode("utf-8", "ignore"))
        except Exception:
            return None
        for loc in (info.get("locations") or []):
            url = loc.get("location")
            if not url:
                continue
            data, _s = self._http_get(url)
            if data:
                return data
        return None

    @staticmethod
    def _xml_inner_id(data):
        try:
            text = data.decode("utf-8", "ignore")
        except Exception:
            return None
        m = re.search(r"(?:rbxassetid://|id=)(\d{4,})", text)
        return m.group(1) if m else None

    # ---- meshes ----------------------------------------------------------
    def get_mesh(self, ref):
        aid = _asset_id(ref)
        if not aid:
            return None
        if aid in self._mesh_cache:
            c = self._mesh_cache[aid]
            return c or None
        path = self._download(aid, ".mesh")
        if not path:
            self.log("    mesh {} unavailable (no local file + network "
                     "refused) -> box".format(aid))
            self._mesh_cache[aid] = False
            return None
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            self.log("    mesh {} read error: {} -> box".format(aid, e))
            self._mesh_cache[aid] = False
            return None
        # A saved 401/403 error page is NOT a mesh. Detect and report it instead
        # of silently falling back to a box (this was the "girly limbs / held
        # item are boxes" bug — the recorder had saved error bodies as assets).
        if data[:8] != b"version ":
            head = data[:80].decode("utf-8", "ignore").replace("\n", " ")
            self.log("    mesh {} local file is NOT a mesh (likely a saved "
                     "401/403 error page): '{}' -> box. Re-record with the "
                     "1.6.2+ recorder.".format(aid, head))
            self._mesh_cache[aid] = False
            return None
        try:
            mesh = parse_roblox_mesh(data, self.log)
        except Exception as e:
            self.log("    mesh parse error for asset {}: {}".format(aid, e))
            mesh = None
        if mesh:
            self.log("    mesh {} -> v{} verts={} faces={}".format(
                aid, mesh.get("version", "?"), len(mesh["verts"]), len(mesh["faces"])))
        self._mesh_cache[aid] = mesh or False
        return mesh

    # ---- images ----------------------------------------------------------
    def get_image_path(self, ref):
        aid = _asset_id(ref)
        if not aid:
            return None
        if aid in self._image_cache:
            c = self._image_cache[aid]
            return c or None
        path = self._download(aid, ".png")  # ext is cosmetic; Blender sniffs content
        self._image_cache[aid] = path or False
        return path


# ---- Roblox binary/text mesh parser ---------------------------------------
def parse_roblox_mesh(data, log=None):
    """Parse Roblox's mesh format into {verts, uvs, faces, version}.
    Supports v1.x (text), v2.x, v3.x (binary). v4+/skinned are best-effort.
    Returns None on unsupported/failed parse (caller falls back to a box)."""
    if data[:8] != b"version ":
        return None
    nl = data.find(b"\n")
    if nl < 0:
        return None
    ver = data[8:nl].decode("ascii", "ignore").strip()
    major = ver.split(".")[0]
    body = data[nl + 1:]
    try:
        if major == "1":
            return _parse_mesh_v1(data, ver)
        if major == "2":
            return _parse_mesh_v2(body, ver)
        if major == "3":
            return _parse_mesh_v3(body, ver)
        # v4, v5, v6, v7 — skinned/LOD formats; best effort
        return _parse_mesh_v4plus(body, ver, log)
    except Exception as e:
        if log:
            log("    mesh v{} parse exception: {}".format(ver, e))
        return None


def _parse_mesh_v1(data, ver):
    text = data.decode("ascii", "ignore")
    parts = text.split("\n", 2)
    if len(parts) < 3:
        return None
    blob = parts[2]
    groups = re.findall(r"\[([^\]]*)\]", blob)
    sc = 0.5 if ver == "1.00" else 1.0
    verts, uvs = [], []
    vcount = len(groups) // 3
    for vi in range(vcount):
        pos = groups[vi * 3].split(",")
        uv = groups[vi * 3 + 2].split(",")
        verts.append((float(pos[0]) * sc, float(pos[1]) * sc, float(pos[2]) * sc))
        uvs.append((float(uv[0]) if len(uv) > 0 else 0.0,
                    float(uv[1]) if len(uv) > 1 else 0.0))
    faces = [(i * 3, i * 3 + 1, i * 3 + 2) for i in range(vcount // 3)]
    return {"verts": verts, "uvs": uvs, "faces": faces, "version": ver}


def _read_vert_block(body, off, num, stride):
    """Read num vertices (pos@0, uv@24 if stride>=32 else @24-clamped). Returns
    (verts, uvs). pos = first 3 floats, uv = floats at byte offset 24."""
    verts, uvs = [], []
    for i in range(num):
        base = off + i * stride
        px, py, pz = struct.unpack_from("<3f", body, base)
        # uv lives after pos(12)+normal(12) = byte 24
        if stride >= 32:
            u, v = struct.unpack_from("<2f", body, base + 24)
        else:
            u, v = 0.0, 0.0
        verts.append((px, py, pz))
        uvs.append((u, v))
    return verts, uvs


def _parse_mesh_v2(body, ver):
    cb_header = struct.unpack_from("<H", body, 0)[0]
    cb_vertex = body[2]
    cb_face = body[3]
    num_verts = struct.unpack_from("<I", body, 4)[0]
    num_faces = struct.unpack_from("<I", body, 8)[0]
    off = cb_header
    verts, uvs = _read_vert_block(body, off, num_verts, cb_vertex)
    foff = off + num_verts * cb_vertex
    faces = []
    for i in range(num_faces):
        a, b, c = struct.unpack_from("<3I", body, foff + i * cb_face)
        faces.append((a, b, c))
    return {"verts": verts, "uvs": uvs, "faces": faces, "version": ver}


def _parse_mesh_v3(body, ver):
    # v3 header (16 bytes): u16 sizeof_header, u8 cbVertex, u8 cbFace,
    #   u16 sizeof_LOD, u16 numLODs, u32 numVerts, u32 numFaces.
    # (The earlier parser missed the sizeof_LOD u16 and read numVerts/numFaces
    # from the wrong offsets, which blew past the buffer on real v3 meshes.)
    cb_header = struct.unpack_from("<H", body, 0)[0]
    cb_vertex = body[2]
    cb_face = body[3]
    cb_lod = struct.unpack_from("<H", body, 4)[0]   # sizeof each LOD entry
    num_lods = struct.unpack_from("<H", body, 6)[0]
    num_verts = struct.unpack_from("<I", body, 8)[0]
    num_faces = struct.unpack_from("<I", body, 12)[0]
    off = cb_header
    verts, uvs = _read_vert_block(body, off, num_verts, cb_vertex)
    foff = off + num_verts * cb_vertex
    faces = []
    for i in range(num_faces):
        a, b, c = struct.unpack_from("<3I", body, foff + i * cb_face)
        faces.append((a, b, c))
    # LOD offset table (numLODs entries, cb_lod bytes each, usually a u32);
    # LOD 0 (highest detail) is faces[lods[0]:lods[1]].
    loff = foff + num_faces * cb_face
    try:
        step = cb_lod if cb_lod >= 4 else 4
        lods = [struct.unpack_from("<I", body, loff + i * step)[0]
                for i in range(num_lods)]
        if len(lods) >= 2 and 0 <= lods[0] < lods[1] <= num_faces:
            faces = faces[lods[0]:lods[1]]
    except Exception:
        pass
    return {"verts": verts, "uvs": uvs, "faces": faces, "version": ver}


def _parse_mesh_v4plus(body, ver, log):
    # v4 header: u16 sizeof, u16 lodType, u32 numVerts, u32 numFaces,
    #            u16 numLODs, u16 numBones, u32 sizeofBoneNames, u16 numSubsets,
    #            u8 numHQLods, u8 unused  => 24 bytes
    cb_header = struct.unpack_from("<H", body, 0)[0]
    num_verts = struct.unpack_from("<I", body, 4)[0]
    num_faces = struct.unpack_from("<I", body, 8)[0]
    num_lods = struct.unpack_from("<H", body, 12)[0]
    num_bones = struct.unpack_from("<H", body, 14)[0]
    STRIDE = 40  # v4 vertex: pos12 + normal12 + uv8 + tangent4 + rgba4
    off = cb_header
    verts, uvs = _read_vert_block(body, off, num_verts, STRIDE)
    off += num_verts * STRIDE
    if num_bones > 0:
        off += num_verts * 8  # per-vertex bone indices(4) + weights(4)
    faces = []
    for i in range(num_faces):
        a, b, c = struct.unpack_from("<3I", body, off + i * 12)
        faces.append((a, b, c))
    foff_end = off + num_faces * 12
    try:
        lods = [struct.unpack_from("<I", body, foff_end + i * 4)[0]
                for i in range(num_lods)]
        if len(lods) >= 2 and 0 <= lods[0] < lods[1] <= num_faces:
            faces = faces[lods[0]:lods[1]]
    except Exception:
        pass
    if log:
        log("    (v{} best-effort: verts={} faces={} bones={})".format(
            ver, num_verts, len(faces), num_bones))
    return {"verts": verts, "uvs": uvs, "faces": faces, "version": ver}


def _add_mesh_geometry(bm, uv_layer, mesh, place_mat, part, scale, flip_v=True):
    """Add a parsed Roblox mesh to the shared bmesh, scaled to the part's Size,
    converted Roblox-local -> Blender-local, then placed by place_mat (the
    canonical rest). Sets per-loop UVs. Returns the new BMVerts."""
    raw = mesh["verts"]
    uvs = mesh["uvs"]
    faces = mesh["faces"]
    if not raw or not faces:
        return None

    size = part.get("size", [1.0, 1.0, 1.0])
    sx, sy, sz = (float(s) for s in size)

    if part.get("shape") == "FileMesh":
        # legacy SpecialMesh: render at Scale, no auto-fit to part size
        ms = part.get("meshScale", [1.0, 1.0, 1.0])
        fx, fy, fz = float(ms[0]), float(ms[1]), float(ms[2])
        cx = cy = cz = 0.0
    else:
        # MeshPart: Roblox scales the mesh so its bbox matches Size
        xs = [v[0] for v in raw]; ys = [v[1] for v in raw]; zs = [v[2] for v in raw]
        bx = max(xs) - min(xs); by = max(ys) - min(ys); bz = max(zs) - min(zs)
        cx = (max(xs) + min(xs)) * 0.5
        cy = (max(ys) + min(ys)) * 0.5
        cz = (max(zs) + min(zs)) * 0.5
        fx = sx / bx if bx > 1e-6 else 1.0
        fy = sy / by if by > 1e-6 else 1.0
        fz = sz / bz if bz > 1e-6 else 1.0

    bmverts = []
    for (x, y, z) in raw:
        x = (x - cx) * fx; y = (y - cy) * fy; z = (z - cz) * fz
        # Roblox-local -> Blender-local axis swap (x, y, z) -> (x, -z, y)
        bmverts.append(bm.verts.new((x * scale, -z * scale, y * scale)))
    bm.verts.ensure_lookup_table()

    n = len(bmverts)
    for (a, b, c) in faces:
        if a >= n or b >= n or c >= n:
            continue
        try:
            f = bm.faces.new((bmverts[a], bmverts[b], bmverts[c]))
        except ValueError:
            continue  # duplicate/degenerate face
        for loop, vidx in zip(f.loops, (a, b, c)):
            if vidx < len(uvs):
                u, v = uvs[vidx]
                loop[uv_layer].uv = (u, (1.0 - v) if flip_v else v)

    bmesh.ops.transform(bm, matrix=place_mat, verts=bmverts)
    return bmverts


def _image_material(name, image_path, color, transparency, log):
    """Principled-BSDF material with an image-texture base color. Falls back to
    a flat color material when image_path is None."""
    if not image_path:
        return _color_material(color, transparency)
    key = "ROCORDER_TEX_" + os.path.basename(image_path)
    mat = bpy.data.materials.get(key)
    if mat is not None:
        return mat
    try:
        img = bpy.data.images.load(image_path, check_existing=True)
    except Exception as e:
        if log:
            log("    image load failed {}: {}".format(image_path, e))
        return _color_material(color, transparency)
    mat = bpy.data.materials.new(key)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.location = (-300, 200)
    if bsdf is not None:
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        # use texture alpha for transparency if the image has it
        if img.channels == 4:
            alpha_in = bsdf.inputs.get("Alpha")
            if alpha_in is not None:
                nt.links.new(tex.outputs["Alpha"], alpha_in)
            try:
                mat.blend_method = "HASHED"
            except (AttributeError, TypeError):
                pass
    return mat


# Roblox NormalId -> the Blender-local axis a decal faces, after the
# (x, y, z) -> (x, -z, y) swap:  +X->+X, +Y->+Z, +Z->-Y.
_DECAL_AXIS = {
    "Front":  Vector((0.0,  1.0,  0.0)),   # Roblox -Z
    "Back":   Vector((0.0, -1.0,  0.0)),   # Roblox +Z
    "Top":    Vector((0.0,  0.0,  1.0)),   # Roblox +Y
    "Bottom": Vector((0.0,  0.0, -1.0)),   # Roblox -Y
    "Right":  Vector((1.0,  0.0,  0.0)),   # Roblox +X
    "Left":   Vector((-1.0, 0.0,  0.0)),   # Roblox -X
}


def _add_primitive_local(bm, uv_layer, part, scale, decal_axis=None):
    """Build a box / ball / wedge for a classic Part at the origin in
    Blender-local coords (NOT yet placed). If decal_axis is given, the face
    pointing that way gets material slot 1 and a planar 0..1 UV (so a face
    decal / logo shows on the right side). Returns the new BMVerts."""
    shape = part.get("shape") or "Block"
    sx, sy, sz = (float(s) for s in part.get("size", [1.0, 1.0, 1.0]))

    if shape == "Ball":
        ret = bmesh.ops.create_uvsphere(
            bm, u_segments=20, v_segments=12,
            radius=max(min(sx, sy, sz) * 0.5 * scale, 1e-4))
        return ret["verts"]

    ret = bmesh.ops.create_cube(bm, size=1.0)
    verts = ret["verts"]
    bmesh.ops.scale(
        bm,
        vec=(max(sx * scale, 1e-4), max(sz * scale, 1e-4), max(sy * scale, 1e-4)),
        verts=verts)

    if decal_axis is not None:
        bm.normal_update()
        faces = set()
        for v in verts:
            faces.update(v.link_faces)
        best, best_dot = None, 0.5
        for f in faces:
            d = f.normal.normalized().dot(decal_axis)
            if d > best_dot:
                best, best_dot = f, d
        if best is not None:
            best.material_index = 1
            # planar UV: corners 0..1 around the quad loop
            corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
            for i, loop in enumerate(best.loops):
                loop[uv_layer].uv = corners[i % 4]
    return verts


def _build_part_object(part, name, place, scale, assets, import_meshes,
                       arm_obj, collection, log):
    """Build ONE Blender object for a single part (body part, accessory, hat,
    or tool piece), skinned 100% to its bone via an Armature modifier. Keeping
    parts as separate objects (instead of one merged mesh) lets you select /
    hide / edit each one. Returns (obj, kind) where kind is 'mesh'|'box'|
    'box+decal' for stats, or (None, 'skip')."""
    color = part.get("color", [0.7, 0.7, 0.7])
    transp = float(part.get("transparency", 0.0))
    decals = part.get("decals") or []

    mesh = bpy.data.meshes.new(name + "_mesh")
    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.verify()
    materials = []
    kind = "box"

    mesh_data = None
    body_tex = None
    if import_meshes and assets is not None:
        if part.get("meshId"):
            mesh_data = assets.get_mesh(part.get("meshId"))
        tref = part.get("textureId") or part.get("colorMap")
        if tref:
            body_tex = assets.get_image_path(tref)

    verts = None
    if mesh_data:
        verts = _add_mesh_geometry(bm, uv_layer, mesh_data, place, part, scale)
    if verts:
        kind = "mesh"
        materials.append(_image_material(name, body_tex, color, transp, log)
                         if body_tex else _color_material(color, transp))
    else:
        # primitive; optionally with a decal (face / logo) on one side
        decal_tex, decal_axis = None, None
        if import_meshes and assets is not None and decals:
            for d in decals:
                tex = assets.get_image_path(d.get("texture"))
                if tex:
                    decal_tex = tex
                    decal_axis = _DECAL_AXIS.get(d.get("face", "Front"))
                    break
        verts = _add_primitive_local(bm, uv_layer, part, scale, decal_axis)
        materials.append(_color_material(color, transp))   # slot 0
        if decal_tex:
            materials.append(_image_material(            # slot 1
                name + "_decal", decal_tex, color, transp, log))
            kind = "box+decal"
        bmesh.ops.transform(bm, matrix=place, verts=verts)

    if not verts:
        bm.free()
        return None, "skip"

    bm.to_mesh(mesh)
    bm.free()
    for m in materials:
        mesh.materials.append(m)

    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    vg = obj.vertex_groups.new(name=name)
    vg.add(list(range(len(mesh.vertices))), 1.0, "REPLACE")
    obj.parent = arm_obj
    obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True
    obj["rocorder_bone"] = name
    return obj, kind


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
def build_player(player_rig, label, scale, collection, log, force_standard_r6=True,
                 assets=None, import_meshes=False):
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

    # ---- one object per part (NOT merged) so each body part / accessory /
    # tool is independently selectable, hideable and editable ----
    # Split into Body vs Accessories sub-collections for tidiness. A part is
    # "body" if a Motor6D joint references it (or it's the root).
    jointed = set()
    for j in player_rig.get("joints", []):
        if j.get("part0"):
            jointed.add(j["part0"])
        if j.get("part1"):
            jointed.add(j["part1"])

    body_coll = bpy.data.collections.new(label + "_Body")
    acc_coll = bpy.data.collections.new(label + "_Accessories")
    collection.children.link(body_coll)
    collection.children.link(acc_coll)

    mesh_stats = {"mesh": 0, "box": 0, "box+decal": 0}
    skipped_parts = []
    part_objs = []

    for p in parts:
        name = p["name"]
        if name not in R:
            skipped_parts.append((name, "bone missing from R"))
            continue
        # Fully-transparent parts with NO mesh/decal (e.g. HumanoidRootPart) get
        # no geometry but keep their bone. Anything carrying a mesh or decal is
        # still drawn — its texture/alpha decides final visibility.
        if (float(p.get("transparency", 0.0)) >= 0.999
                and not p.get("meshId") and not p.get("decals")):
            skipped_parts.append((name, "transparency>=0.999, no mesh/decal (bone kept)"))
            continue

        place = D.get(name, Matrix.Identity(4))
        target = body_coll if name in jointed else acc_coll
        obj, kind = _build_part_object(p, name, place, scale, assets,
                                       import_meshes, arm_obj, target, log)
        if obj is None:
            skipped_parts.append((name, "no geometry produced"))
            continue
        obj["rocorder_user_id"] = arm_obj.get("rocorder_user_id", "")
        part_objs.append(obj)
        mesh_stats[kind] = mesh_stats.get(kind, 0) + 1

    if skipped_parts:
        log("  parts skipped (no geometry):")
        for name, reason in skipped_parts:
            log("    {}: {}".format(name, reason))
    log("  built {} part objects — meshes={} boxes={} box+decal={}".format(
        len(part_objs), mesh_stats.get("mesh", 0), mesh_stats.get("box", 0),
        mesh_stats.get("box+decal", 0)))

    # per-part detail so we can see WHY each part is a mesh vs a box
    log("  part detail:")
    for p in parts:
        nm = p["name"]
        bits = ["class=" + str(p.get("className", "?")),
                "shape=" + str(p.get("shape", "?"))]
        if p.get("meshId"): bits.append("meshId")
        if p.get("meshType"): bits.append("meshType=" + str(p.get("meshType")))
        if p.get("textureId"): bits.append("textureId")
        if p.get("colorMap"): bits.append("colorMap")
        if p.get("decals"): bits.append("decals={}".format(len(p["decals"])))
        log("    {:24s} {}".format(nm, " ".join(bits)))
    clothing = player_rig.get("clothing")
    if clothing:
        log("  clothing on character: {}".format(clothing))
        log("  NOTE: classic Shirt/Pants wrapping isn't applied yet (needs the "
            "classic body UV template) — bodies will show as flat-colored "
            "parts until that lands.")

    return {
        "arm": arm_obj,
        "part_objs": part_objs,
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
                  force_standard_r6, import_meshes, roblosecurity, cache_dir,
                  debug, report):
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
    log("import_meshes: {}  cookie: {}".format(
        import_meshes, "set" if (roblosecurity or "").strip() else "none"))

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

    # asset fetcher (meshes + textures). Default cache dir sits next to the .rec.
    assets = None
    if import_meshes:
        if not cache_dir or not cache_dir.strip():
            cache_dir = os.path.join(os.path.dirname(filepath), "rocorder_assets")
        rec_dir = os.path.dirname(filepath)
        # the recorder pre-downloads assets into ROCORDER/assets — look there
        # (and one level up, in case the .rec is inside ROCORDER/) before the net
        local_dirs = [
            os.path.join(rec_dir, "assets"),
            os.path.join(rec_dir, "rocorder_assets"),
            os.path.join(os.path.dirname(rec_dir), "assets"),
        ]
        found = [d for d in local_dirs if os.path.isdir(d)]
        log("asset cache dir: {}".format(cache_dir))
        log("local asset dirs found: {}".format(found or "none "
            "(Blender will try the network — expect 401s for modern assets "
            "unless you enabled 'Download Assets' in the recorder)"))
        # Surface the recorder's "couldn't fetch these" list right at the top
        # so it's obvious which assets need a manual drop.
        for d in found:
            missing_txt = os.path.join(d, "_missing.txt")
            if os.path.isfile(missing_txt):
                try:
                    with open(missing_txt, "r", encoding="utf-8") as fh:
                        ids = [ln.strip() for ln in fh
                               if ln.strip() and not ln.strip().startswith("#")]
                except OSError:
                    ids = []
                if ids:
                    log("recorder reported {} unfetchable assets (drop a file "
                        "named '<id>' into {} to use it):".format(len(ids), d))
                    for i in ids:
                        log("    {}".format(i))
        assets = AssetFetcher(cache_dir, roblosecurity, log, local_dirs=local_dirs)

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
                             force_standard_r6=force_standard_r6,
                             assets=assets, import_meshes=import_meshes)
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
            # FOV: Roblox stores vertical FOV in degrees. Blender's
            # Camera.angle is a derived property (computed from `lens` +
            # sensor size) and is NOT directly animatable — keyframe_insert
            # on it raises 'property "angle" not animatable'. So we convert
            # vertical FOV to focal length and animate `lens` instead, which
            # is the actual underlying animatable channel. With
            # sensor_fit=VERTICAL the mapping is exact:
            #     f = sensor_height / (2 * tan(fov / 2))
            fov_rad = math.radians(max(1e-3, float(fov)))
            camera_data.lens = (camera_data.sensor_height
                                / (2.0 * math.tan(fov_rad / 2.0)))
            camera_data.keyframe_insert(data_path="lens", frame=frame_num)
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
            log("  note: {} frames had fewer parts than the final count "
                "(normal for parts that appear later — e.g. an equipped tool — "
                "or a truncated line):".format(len(mis)))
            for fnum, got, exp in mis[:5]:
                log("    frame {}: {} parts (final {})".format(fnum, got, exp))
            if len(mis) > 5:
                log("    ... and {} more".format(len(mis) - 5))

        # bone keyframe distribution — useful to spot bones that animated
        # only briefly (one tally per uid; bones sorted by ascending count)
        sorted_kc = sorted(kc.items(), key=lambda x: x[1])
        # only log the lowest 5 — full list is mostly noise
        log("  bones with fewest keyframes:")
        for b, c in sorted_kc[:5]:
            log("    {}: {}".format(b, c))

    cookie_hint = False
    if assets is not None:
        log("")
        log("assets: {} local (recorder), {} downloaded, {} cache hits, "
            "{} fails ({} auth/401)".format(
                assets.stats["local_hits"], assets.stats["downloads"],
                assets.stats["cache_hits"], assets.stats["fails"],
                assets.stats["auth_fails"]))
        if assets.stats["auth_fails"] > 0:
            cookie_hint = True
            log("  *** {} assets returned 401 and weren't found locally.".format(
                assets.stats["auth_fails"]))
            log("  *** BEST FIX: enable 'Download Assets' in the recorder "
                "(Settings tab), re-record, and keep the ROCORDER/assets folder "
                "next to the .rec. The executor downloads them with a real "
                "session, which Blender can't do anonymously.")

    log("")
    log("totals: armatures={} fallback_objs={} cameras={} frames={} "
        "keyframes={} bone_keys={} camera_keys={}".format(
            len(players), len(fallback_objs), camera_obj and 1 or 0,
            len(frames), keyframes + camera_keys, bone_keys, camera_keys))
    log("=" * 76)
    flush_log()
    if cookie_hint:
        report({"WARNING"},
               "ROCORDER: {} assets couldn't be fetched (401). Enable "
               "'Download Assets' in the recorder and keep the ROCORDER/assets "
               "folder next to the .rec. See the .import.log.".format(
                   assets.stats["auth_fails"]))
    elif log_path:
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
    import_meshes: BoolProperty(
        name="Import meshes & textures",
        description="Download each part's real mesh + texture from Roblox's "
                    "CDN and build proper geometry instead of colored boxes. "
                    "Covers body MeshParts, accessories, hats, and held tools. "
                    "Assets are cached on disk so re-imports are instant. "
                    "Anything that can't be fetched falls back to a box",
        default=True,
    )
    roblosecurity: StringProperty(
        name=".ROBLOSECURITY (optional)",
        description="Optional Roblox auth cookie. Leave blank for public "
                    "assets (covers almost everything). Only needed for gated "
                    "assets that refuse anonymous download. SECURITY: this is "
                    "your account session token — only paste it if you "
                    "understand the risk; it is passed to Roblox's CDN only",
        default="",
        subtype="PASSWORD",
    )
    asset_cache_dir: StringProperty(
        name="Asset cache folder",
        description="Where downloaded meshes/textures are cached. Leave blank "
                    "to use a 'rocorder_assets' folder next to the .rec",
        default="",
        subtype="DIR_PATH",
    )
    debug: BoolProperty(
        name="Write debug log",
        description="Write a verbose .import.log next to the .rec listing the "
                    "rig structure, bones created, mesh build, asset fetches, "
                    "keyframe counts per bone, and any anomalies. Recommended "
                    "while diagnosing rig issues",
        default=True,
    )

    def execute(self, context):
        return import_replay(
            context, self.filepath, self.scale,
            self.set_scene_fps, self.build_armature,
            self.force_standard_r6, self.import_meshes,
            self.roblosecurity, self.asset_cache_dir,
            self.debug, self.report,
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
