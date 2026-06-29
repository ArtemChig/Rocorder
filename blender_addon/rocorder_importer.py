bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (1, 31, 1),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replays as skinned, animated armatures",
    "warning": "Alpha — file formats and options may still change",
    "category": "Import-Export",
}
ROCORDER_VERSION = "1.31.1-alpha"

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

import base64
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
                      "auth_fails": 0, "local_hits": 0,
                      "geom_hits": 0, "rgba_hits": 0}
        try:
            os.makedirs(cache_dir, exist_ok=True)
        except OSError as e:
            self.log("WARN could not create asset cache dir {}: {}".format(cache_dir, e))

    def _local_path(self, asset_id, ext):
        """Path to a recorder file named exactly '<id><ext>' (ext '' = bare),
        or None. Used for the typed extraction files .geom.json / .rgba and
        the bare HTTP-fallback file."""
        for d in self.local_dirs:
            p = os.path.join(d, asset_id + ext)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        return None

    def _find_local(self, asset_id):
        """Return a path to a recorder-downloaded RAW asset file for this id
        (the bare '<id>' HTTP-fallback file, or a hand-dropped '<id>.<ext>').
        Skips the typed extraction files (.geom.json / .rgba) — those are read
        directly by get_mesh / get_image_path, not fed to the binary parser."""
        for d in self.local_dirs:
            exact = os.path.join(d, asset_id)
            if os.path.isfile(exact) and os.path.getsize(exact) > 0:
                return exact
            try:
                for fn in os.listdir(d):
                    if fn.endswith(".geom.json") or fn.endswith(".rgba"):
                        continue
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
    def _parse_geom_json(self, path):
        """Parse the recorder's <id>.geom.json into the importer mesh dict.

        GEOM/2 (current): verts (flat xyz) + faces (vertex-slot triplets) +
        faceUVs (per face: 3 corner UVs = 6 floats, aligned to faces). UVs are
        per-corner so seams/islands map correctly.

        GEOM/1 (legacy): verts + per-vertex uvs + faces. Those files have
        all-zero UVs (the recorder bug this format predates), so they texture
        flat — re-record to get GEOM/2."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError) as e:
            self.log("    geom.json read error {}: {}".format(path, e))
            return None
        vf = d.get("verts") or []
        ff = d.get("faces") or []
        verts = [(vf[i], vf[i + 1], vf[i + 2]) for i in range(0, len(vf) - 2, 3)]
        faces = [(ff[i], ff[i + 1], ff[i + 2]) for i in range(0, len(ff) - 2, 3)]
        if not verts or not faces:
            return None
        fu = d.get("faceUVs")
        if fu is not None:
            return {"verts": verts, "faces": faces, "face_uvs": fu,
                    "version": "geom2"}
        uf = d.get("uvs") or []
        uvs = [(uf[i], uf[i + 1]) for i in range(0, len(uf) - 1, 2)]
        return {"verts": verts, "uvs": uvs, "faces": faces, "version": "geom"}

    def get_mesh(self, ref):
        aid = _asset_id(ref)
        if not aid:
            return None
        if aid in self._mesh_cache:
            c = self._mesh_cache[aid]
            return c or None
        # PREFERRED: engine-extracted geometry the recorder wrote as
        # <id>.geom.json. Cleaner than the CDN binary mesh, and present for
        # assets the CDN would refuse (UGC, off-sale). No parsing of Roblox's
        # versioned binary format needed.
        geom_path = self._local_path(aid, ".geom.json")
        if geom_path:
            mesh = self._parse_geom_json(geom_path)
            if mesh:
                self.stats["geom_hits"] += 1
                if mesh.get("version") == "geom":
                    self.log("    mesh {} <- local .geom.json (GEOM/1, "
                             "ZERO UVs — stale pre-1.12 file; re-record with "
                             "this avatar present to regenerate it as GEOM/2 "
                             "with correct UVs)".format(aid))
                else:
                    self.log("    mesh {} <- local .geom.json verts={} faces={}".format(
                        aid, len(mesh["verts"]), len(mesh["faces"])))
                self._mesh_cache[aid] = mesh
                return mesh
            self.log("    mesh {} .geom.json unreadable -> other sources".format(aid))
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
    def _fill_image_pixels(self, img, pix, w, h):
        """Load raw top-left-origin RGBA8 bytes into a Blender image (whose
        pixel buffer is bottom-left origin), flipping vertically. Uses numpy
        (bundled with Blender) for speed; falls back to pure Python."""
        try:
            import numpy as np
            a = np.frombuffer(pix, dtype=np.uint8).astype(np.float32) / 255.0
            a = a.reshape(h, w, 4)[::-1, :, :]      # flip rows: top-origin -> bottom-origin
            img.pixels.foreach_set(a.ravel())
            return True
        except Exception:
            pass
        try:
            out = [0.0] * (w * h * 4)
            row = w * 4
            for y in range(h):
                src = (h - 1 - y) * row
                dst = y * row
                for i in range(row):
                    out[dst + i] = pix[src + i] / 255.0
            img.pixels.foreach_set(out)
            return True
        except Exception as e:
            self.log("    pixel fill failed: {}".format(e))
            return False

    def _rgba_to_png(self, rgba_path, png_path):
        """Convert the recorder's <id>.rgba (header 'ROCORDER-RGBA8\\n<w>\\n<h>\\n'
        then raw RGBA8) into a PNG at png_path so Blender's normal image loader
        can use it. Returns True on success."""
        try:
            with open(rgba_path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            self.log("    .rgba read error {}: {}".format(rgba_path, e))
            return False
        nl1 = raw.find(b"\n")
        nl2 = raw.find(b"\n", nl1 + 1) if nl1 >= 0 else -1
        nl3 = raw.find(b"\n", nl2 + 1) if nl2 >= 0 else -1
        if nl3 < 0 or raw[:nl1] != b"ROCORDER-RGBA8":
            self.log("    .rgba header invalid: {}".format(rgba_path))
            return False
        try:
            w = int(raw[nl1 + 1:nl2]); h = int(raw[nl2 + 1:nl3])
        except ValueError:
            return False
        pix = raw[nl3 + 1:]
        need = w * h * 4
        if w <= 0 or h <= 0 or len(pix) < need:
            self.log("    .rgba size mismatch {}x{}: need {} got {}".format(
                w, h, need, len(pix)))
            return False
        img = bpy.data.images.new("_rocorder_rgba_tmp", width=w, height=h, alpha=True)
        ok = False
        try:
            if self._fill_image_pixels(img, pix[:need], w, h):
                img.filepath_raw = png_path
                img.file_format = "PNG"
                img.save()
                ok = os.path.isfile(png_path)
        except Exception as e:
            self.log("    .rgba -> png failed: {}".format(e))
        finally:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
        return ok

    def get_image_path(self, ref):
        aid = _asset_id(ref)
        if not aid:
            return None
        if aid in self._image_cache:
            c = self._image_cache[aid]
            return c or None
        # PREFERRED: engine-extracted texture the recorder wrote as <id>.rgba
        # (raw RGBA8 the client had loaded — present even for off-sale clothing
        # the CDN 401s). Convert to a PNG in the cache dir once; reuse after.
        rgba_path = self._local_path(aid, ".rgba")
        if rgba_path:
            png_path = os.path.join(self.cache_dir, aid + ".rgba.png")
            ok = (os.path.isfile(png_path) and os.path.getsize(png_path) > 0) \
                or self._rgba_to_png(rgba_path, png_path)
            if ok:
                self.stats["rgba_hits"] += 1
                self._image_cache[aid] = png_path
                return png_path
            self.log("    image {} .rgba convert failed -> other sources".format(aid))
        path = self._download(aid, ".png")  # ext is cosmetic; Blender sniffs content
        self._image_cache[aid] = path or False
        return path

    def get_baked_path(self, baked_id):
        """Stage 3a: a recorder-composited body texture stored as
        '<baked_id>.rgba' (baked_id like 'comp_12345' — not a numeric asset id,
        so it bypasses get_image_path's _asset_id logic). Returns a PNG path or
        None. None => the comp file is absent (recorder fell back / didn't
        finish), and the caller uses the per-part Stage 0 clothing path."""
        if not baked_id:
            return None
        bid = str(baked_id)
        if bid in self._image_cache:
            return self._image_cache[bid] or None
        for d in self.local_dirs:
            rgba_path = os.path.join(d, bid + ".rgba")
            if os.path.isfile(rgba_path) and os.path.getsize(rgba_path) > 0:
                png_path = os.path.join(self.cache_dir, bid + ".rgba.png")
                ok = (os.path.isfile(png_path) and os.path.getsize(png_path) > 0) \
                    or self._rgba_to_png(rgba_path, png_path)
                if ok:
                    self.stats["rgba_hits"] += 1
                    self._image_cache[bid] = png_path
                    return png_path
        self._image_cache[bid] = False
        return None


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


# ---- Bundled engine-primitive meshes ---------------------------------------
# Roblox draws primitive parts (the classic blocky body, the round head, …)
# procedurally — they have no asset id, so EditableMesh can't extract them. So
# we bundle Roblox's OWN shipped meshes (from content/avatar/…) as base64 of the
# v2 .mesh files and load them on demand. These carry the exact bevels AND the
# real R6 clothing UVs, so classic bodies need no box / projection / bevel
# modifier — clothing maps through the mesh's own UVs exactly like in-game.
# _BUNDLED_MESH_B64 (name -> base64) is defined near the bottom of this file.
_R6_BUNDLED_BODY = {
    "Torso": "torso", "Left Arm": "leftarm", "Right Arm": "rightarm",
    "Left Leg": "leftleg", "Right Leg": "rightleg",
}
_bundled_cache = {}


def _bundled_mesh(name):
    """Parsed geom {verts, uvs, faces} for a bundled engine mesh, or None."""
    if name in _bundled_cache:
        return _bundled_cache[name]
    geom = None
    b64 = _BUNDLED_MESH_B64.get(name) if "_BUNDLED_MESH_B64" in globals() else None
    if b64:
        try:
            geom = parse_roblox_mesh(base64.b64decode(b64))
        except Exception:
            geom = None
    _bundled_cache[name] = geom
    return geom


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


def _r6_cube_project_clothing_uvs(bm, uv_layer, regions):
    """For every face in the mesh, cube-project its corners into the R6
    clothing template region for that face's dominant direction. Used to
    bring back Shirt/Pants on CharacterMesh body parts: the mesh keeps its
    sculpted shape, but the texture is wrapped exactly like the standard
    R6 box body would wrap it — independent of whatever UVs the modeler
    actually authored on the mesh. Mesh verts must be in PART-LOCAL Blender
    space when this runs (i.e. before the place_mat transform). Overwrites
    `uv_layer` for every loop. Vertices outside the mesh's tight bbox in
    any axis are clamped to the cell edge."""
    if not bm.faces:
        return
    xs = [v.co.x for v in bm.verts]
    ys = [v.co.y for v in bm.verts]
    zs = [v.co.z for v in bm.verts]
    cx = (max(xs) + min(xs)) * 0.5
    cy = (max(ys) + min(ys)) * 0.5
    cz = (max(zs) + min(zs)) * 0.5
    hx = max((max(xs) - min(xs)) * 0.5, 1e-6)
    hy = max((max(ys) - min(ys)) * 0.5, 1e-6)
    hz = max((max(zs) - min(zs)) * 0.5, 1e-6)
    bm.normal_update()
    for f in bm.faces:
        n = f.normal.normalized()
        ax, ay, az = abs(n.x), abs(n.y), abs(n.z)
        if ay >= ax and ay >= az:
            key = "Front" if n.y > 0 else "Back"
        elif ax >= ay and ax >= az:
            key = "Right" if n.x > 0 else "Left"
        else:
            key = "Top" if n.z > 0 else "Bottom"
        reg = regions.get(key)
        if reg is None:
            continue
        (u0, v0, u1, v1), uax, vax = reg
        # Half-extent of the mesh along whichever axis u/v point at.
        if abs(uax.x) > 0.5:   u_half = hx
        elif abs(uax.y) > 0.5: u_half = hy
        else:                  u_half = hz
        if abs(vax.x) > 0.5:   v_half = hx
        elif abs(vax.y) > 0.5: v_half = hy
        else:                  v_half = hz
        for loop in f.loops:
            co = loop.vert.co
            X, Y, Z = co.x - cx, co.y - cy, co.z - cz
            u_coord = X * uax.x + Y * uax.y + Z * uax.z
            v_coord = X * vax.x + Y * vax.y + Z * vax.z
            u_norm = (u_coord + u_half) / (2.0 * u_half)
            v_norm = (v_coord + v_half) / (2.0 * v_half)
            if u_norm < 0.0: u_norm = 0.0
            elif u_norm > 1.0: u_norm = 1.0
            if v_norm < 0.0: v_norm = 0.0
            elif v_norm > 1.0: v_norm = 1.0
            loop[uv_layer].uv = (u0 + u_norm * (u1 - u0),
                                 v0 + v_norm * (v1 - v0))


def _cylindrical_project_clothing_uvs(bm, uv_layer, regions):
    """Stage 3b: wrap the R6 clothing template around the part as a smooth
    cylinder instead of the cube's hard per-face snapping (which seams /
    splatters on sculpted custom-UV CharacterMesh and R15 bodies). The part's
    long axis is Z in part-local Blender space (the cube projection's Top/Bottom
    caps are +-Z); the cross-section is the XY plane with Front=+Y, Back=-Y,
    Right=+X, Left=-X (matching _FACE_AXES). The angle around Z selects one of
    the four side cells (Front/Right/Back/Left, 90 deg each) per FACE (by its
    centroid, so a face never splits across two cells); the vertex angle gives U
    within that cell and height along Z gives V. Cap faces (normal along Z) get
    a planar projection into Top/Bottom. Mesh verts must be in part-local
    Blender space (before place_mat).

    APPROXIMATE + TUNABLE against a render: the front convention (atan2(x, y)),
    the cell order, the seam location, and per-cell U/V flips may need
    adjusting. A thin seam where Front meets Left is expected (no seam-vertex
    duplication in this first cut)."""
    if not bm.faces:
        return
    zs = [v.co.z for v in bm.verts]
    zmin, zmax = min(zs), max(zs)
    zspan = max(zmax - zmin, 1e-6)
    # Side cells in circumferential order; start_deg is the lower edge of each
    # cell's 90-degree arc (Front centered on 0, i.e. spans [-45, 45)).
    side_order = (("Front", -45.0), ("Right", 45.0),
                  ("Back", 135.0), ("Left", 225.0))

    def cell_for_angle(deg):
        d = deg % 360.0
        if d >= 315.0:
            d -= 360.0
        for key, start in side_order:
            if start <= d < start + 90.0:
                return key, start
        return "Front", -45.0

    bm.normal_update()
    for f in bm.faces:
        n = f.normal.normalized()
        # Caps: faces pointing mostly along the long axis -> planar Top/Bottom.
        if abs(n.z) >= 0.7:
            key = "Top" if n.z > 0 else "Bottom"
            reg = regions.get(key)
            if not reg:
                continue
            (u0, v0, u1, v1), _uax, _vax = reg
            xs = [l.vert.co.x for l in f.loops]
            ys = [l.vert.co.y for l in f.loops]
            dx = (max(xs) - min(xs)) or 1.0
            dy = (max(ys) - min(ys)) or 1.0
            x0, y0 = min(xs), min(ys)
            for loop in f.loops:
                fu = (loop.vert.co.x - x0) / dx
                fv = (loop.vert.co.y - y0) / dy
                loop[uv_layer].uv = (u0 + fu * (u1 - u0), v0 + fv * (v1 - v0))
            continue
        # Side: whole face -> the cell of its centroid angle (no intra-face
        # split); each vertex's angle gives U within the cell.
        cx = sum(l.vert.co.x for l in f.loops) / len(f.loops)
        cy = sum(l.vert.co.y for l in f.loops) / len(f.loops)
        key, start = cell_for_angle(math.degrees(math.atan2(cx, cy)))
        reg = regions.get(key)
        if not reg:
            continue
        (u0, v0, u1, v1), _uax, _vax = reg
        for loop in f.loops:
            co = loop.vert.co
            d = math.degrees(math.atan2(co.x, co.y))
            # Unwrap the vertex angle to lie near this cell's arc (seam-safe).
            while d - start < -45.0:
                d += 360.0
            while d - start > 135.0:
                d -= 360.0
            fu = max(0.0, min(1.0, (d - start) / 90.0))
            fv = (co.z - zmin) / zspan
            loop[uv_layer].uv = (u0 + fu * (u1 - u0), v0 + fv * (v1 - v0))


_ATLAS_W, _ATLAS_H = 1024, 512


def _prep_compositor_base(base_geom, atlas_w=None, atlas_h=None):
    """Prepare a compositor base mesh (vertices at composite-atlas pixels, UVs
    pointing at the Shirt/Pants template) for fast atlas-pos -> template-UV
    lookup. Returns (tris, atlas_w, atlas_h). R6 uses a shared 1024x512 atlas
    (pass it in); R15 uses per-part textures so the atlas size defaults to the
    base mesh's own position bbox."""
    V = base_geom["verts"]; U = base_geom.get("uvs"); F = base_geom["faces"]
    if not (V and U and F):
        return None
    tris = []
    for (a, b, c) in F:
        x1, y1 = V[a][0], V[a][1]; x2, y2 = V[b][0], V[b][1]; x3, y3 = V[c][0], V[c][1]
        tris.append((x1, y1, x2, y2, x3, y3,
                     U[a][0], U[a][1], U[b][0], U[b][1], U[c][0], U[c][1],
                     min(x1, x2, x3), min(y1, y2, y3), max(x1, x2, x3), max(y1, y2, y3)))
    if atlas_w is None or atlas_h is None:
        atlas_w = max(v[0] for v in V)
        atlas_h = max(v[1] for v in V)
    return (tris, atlas_w, atlas_h)


def _atlas_uv_to_template(u, v, prepped):
    """Map a body atlas UV (u,v) to a Shirt/Pants template UV via Roblox's
    compositor base mesh — the exact in-engine mapping, no projection. The atlas
    Y axis is inverted vs UV V (CompositQuad: position y=0 -> v=1). Returns
    (tu, tv), or None if outside the base's coverage."""
    tris, AW, AH = prepped
    ax = u * AW
    ay = (1.0 - v) * AH
    for (x1, y1, x2, y2, x3, y3, u1, v1, u2, v2, u3, v3,
         mnx, mny, mxx, mxy) in tris:
        if ax < mnx or ax > mxx or ay < mny or ay > mxy:
            continue
        den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        if abs(den) < 1e-9:
            continue
        l1 = ((y2 - y3) * (ax - x3) + (x3 - x2) * (ay - y3)) / den
        l2 = ((y3 - y1) * (ax - x3) + (x1 - x3) * (ay - y3)) / den
        l3 = 1.0 - l1 - l2
        if l1 >= -0.01 and l2 >= -0.01 and l3 >= -0.01:
            return (l1 * u1 + l2 * u2 + l3 * u3, l1 * v1 + l2 * v2 + l3 * v3)
    return None


def _add_mesh_geometry(bm, uv_layer, mesh, place_mat, part, scale, flip_v=True,
                      r6_clothing_regions=None, cylindrical=False,
                      compositor_base=None):
    """Add a parsed Roblox mesh to the shared bmesh, scaled to the part's Size,
    converted Roblox-local -> Blender-local, then placed by place_mat (the
    canonical rest). Sets per-loop UVs. Returns the new BMVerts."""
    raw = mesh["verts"]
    uvs = mesh.get("uvs")            # GEOM/1 + binary: per-vertex
    face_uvs = mesh.get("face_uvs")  # GEOM/2: per-face-corner (6 floats/face)
    faces = mesh["faces"]
    if not raw or not faces:
        return None

    size = part.get("size", [1.0, 1.0, 1.0])
    sx, sy, sz = (float(s) for s in size)

    if part.get("shape") in ("FileMesh", "CharacterMesh"):
        # legacy SpecialMesh + CharacterMesh: render at authored size, no
        # auto-fit to part size. CharacterMesh meshes are sculpted at
        # anatomical proportions (e.g. torso mesh bbox ~1.33×1.85×0.84 even
        # though the BasePart is the standard R6 2×2×1) — Roblox renders
        # them as-is at the part's CFrame, NOT bbox-stretched to part size.
        # Stretching them to fit the part size made every CharacterMesh
        # avatar squashed / blocky-shaped in 1.15.x.
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
    nfu = len(face_uvs) if face_uvs else 0
    for fi, (a, b, c) in enumerate(faces):
        if a >= n or b >= n or c >= n:
            continue
        try:
            f = bm.faces.new((bmverts[a], bmverts[b], bmverts[c]))
        except ValueError:
            continue  # duplicate/degenerate face
        for ci, (loop, vidx) in enumerate(zip(f.loops, (a, b, c))):
            if face_uvs is not None:
                # per-corner UVs: face fi, corner ci -> floats [fi*6 + ci*2 ..]
                base = fi * 6 + ci * 2
                if base + 1 < nfu:
                    u, v = face_uvs[base], face_uvs[base + 1]
                else:
                    u, v = 0.0, 0.0
            elif uvs and vidx < len(uvs):
                u, v = uvs[vidx]
            else:
                u, v = 0.0, 0.0
            if compositor_base is not None:
                # Remap the body's atlas UV straight to template space via
                # Roblox's compositor base mesh (exact, no projection). Loops
                # outside the base's coverage keep their authored UV.
                t = _atlas_uv_to_template(u, v, compositor_base)
                if t is not None:
                    u, v = t
            loop[uv_layer].uv = (u, (1.0 - v) if flip_v else v)

    # Replace mesh-authored UVs with an R6 clothing-template projection so
    # Shirt/Pants wraps a CharacterMesh body the same way it wraps the standard
    # R6 box body. Must run BEFORE place_mat — projection is in part-local space
    # (origin at the bone), not rest pose. Stage 3b: cylindrical (smooth wrap)
    # for custom-UV / sculpted bodies; cube (legacy) otherwise.
    if r6_clothing_regions is not None:
        if cylindrical:
            _cylindrical_project_clothing_uvs(bm, uv_layer, r6_clothing_regions)
        else:
            _r6_cube_project_clothing_uvs(bm, uv_layer, r6_clothing_regions)

    bmesh.ops.transform(bm, matrix=place_mat, verts=bmverts)
    return bmverts


def _image_material(name, image_path, color, transparency, log):
    """Principled-BSDF material with an image-texture base color blended OVER
    the part's body color via texture alpha. Matches Roblox's in-game
    rendering: a shirt with a transparent background draws the player's skin
    in the transparent regions instead of rendering see-through; a face
    decal shows the head's skin colour around the eyes/mouth; an accessory
    with a partly-transparent texture shows the accessory's flat colour
    beneath. Part.Transparency (Roblox's see-through control) is applied
    separately as actual alpha. Falls back to a flat colour material when
    image_path is None."""
    if not image_path:
        return _color_material(color, transparency)

    # Cache key includes body colour + transparency: the body colour is
    # baked into the material via the Mix node, so two parts using the same
    # texture but different colours need distinct materials. Transparent
    # parts likewise need their own.
    r = max(0.0, min(1.0, float(color[0])))
    g = max(0.0, min(1.0, float(color[1])))
    b = max(0.0, min(1.0, float(color[2])))
    color_tag = "{:02x}{:02x}{:02x}".format(
        int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))
    key = "ROCORDER_TEX_" + os.path.basename(image_path) + "_" + color_tag
    if transparency > 0.0:
        key += "_t{:02d}".format(int(transparency * 100 + 0.5))
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
    tex.location = (-600, 200)

    if bsdf is not None:
        if img.channels == 4:
            # Texture has alpha → blend texture OVER body colour with its
            # alpha as the mix factor. transparent texel ⇒ body colour,
            # opaque texel ⇒ texture colour, partial alpha smoothly blends.
            # Material stays opaque (no see-through) — that's the whole
            # point. ShaderNodeMixRGB (the legacy name) still works in
            # Blender 4.x and avoids needing the newer ShaderNodeMix.
            mix = nt.nodes.new("ShaderNodeMixRGB")
            mix.blend_type = "MIX"
            mix.location = (-300, 200)
            mix.inputs["Color1"].default_value = (r, g, b, 1.0)
            nt.links.new(tex.outputs["Color"], mix.inputs["Color2"])
            nt.links.new(tex.outputs["Alpha"], mix.inputs["Fac"])
            nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
        else:
            # No alpha channel — texture colour is the final colour.
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])

        # Part.Transparency = real see-through, independent of texture alpha.
        # Only touched when >0 so opaque parts stay fully opaque.
        if transparency > 0.0:
            alpha_in = bsdf.inputs.get("Alpha")
            if alpha_in is not None:
                alpha_in.default_value = 1.0 - transparency
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


def _project_decal_planar(bm, uv_layer, verts, axis, slot=1):
    """Project a 0..1 planar UV onto the faces of `verts` that point roughly
    along `axis`, and assign them material `slot`. Used for the face decal on
    the spherical classic head (and any non-box decal target)."""
    bm.normal_update()
    axis = axis.normalized()
    faces = set()
    for v in verts:
        faces.update(v.link_faces)
    front = [f for f in faces if f.normal.normalized().dot(axis) > 0.25]
    if not front:
        return
    up = Vector((0.0, 0.0, 1.0))
    if abs(axis.dot(up)) > 0.9:
        up = Vector((0.0, 1.0, 0.0))
    uaxis = axis.cross(up).normalized()
    vaxis = uaxis.cross(axis).normalized()
    us, vs, co = [], [], {}
    for f in front:
        for loop in f.loops:
            p = loop.vert.co
            uu, vv = p.dot(uaxis), p.dot(vaxis)
            co[loop] = (uu, vv); us.append(uu); vs.append(vv)
    umin, umax = min(us), max(us); vmin, vmax = min(vs), max(vs)
    du = (umax - umin) or 1.0; dv = (vmax - vmin) or 1.0
    for f in front:
        f.material_index = slot
        for loop in f.loops:
            uu, vv = co[loop]
            loop[uv_layer].uv = ((uu - umin) / du, (vv - vmin) / dv)


def _add_bundled_into_bm(bm, geom, sx, sy, sz, scale, fit_to_size=True):
    """Add a bundled engine mesh's geometry into bm at the origin, converting
    Roblox-local -> Blender-local (x,y,z)->(x,-z,y) and (optionally) bbox-fitting
    to the part size. Returns the new BMVerts, or None."""
    raw = geom.get("verts")
    faces = geom.get("faces")
    if not raw or not faces:
        return None
    xs = [v[0] for v in raw]; ys = [v[1] for v in raw]; zs = [v[2] for v in raw]
    bx = max(xs) - min(xs); by = max(ys) - min(ys); bz = max(zs) - min(zs)
    cx = (max(xs) + min(xs)) * 0.5
    cy = (max(ys) + min(ys)) * 0.5
    cz = (max(zs) + min(zs)) * 0.5
    if fit_to_size:
        fx = sx / bx if bx > 1e-6 else 1.0
        fy = sy / by if by > 1e-6 else 1.0
        fz = sz / bz if bz > 1e-6 else 1.0
    else:
        fx = fy = fz = 1.0
        cx = cy = cz = 0.0
    bmverts = []
    for (x, y, z) in raw:
        x = (x - cx) * fx; y = (y - cy) * fy; z = (z - cz) * fz
        bmverts.append(bm.verts.new((x * scale, -z * scale, y * scale)))
    bm.verts.ensure_lookup_table()
    n = len(bmverts)
    for (a, b, c) in faces:
        if a < n and b < n and c < n:
            try:
                bm.faces.new((bmverts[a], bmverts[b], bmverts[c]))
            except ValueError:
                pass
    return bmverts


def _add_primitive_local(bm, uv_layer, part, scale, decal_axis=None):
    """Build a box / ball / head / wedge for a classic Part at the origin in
    Blender-local coords (NOT yet placed). If decal_axis is given, the face
    pointing that way gets material slot 1 and a planar 0..1 UV (so a face
    decal / logo shows on the right side). Returns the new BMVerts."""
    shape = part.get("shape") or "Block"
    is_head = (part.get("meshType") == "Head")
    sx, sy, sz = (float(s) for s in part.get("size", [1.0, 1.0, 1.0]))

    if is_head:
        # Classic Head: use Roblox's OWN head mesh (exact rounded shape) instead
        # of a sphere approximation; project the face decal onto its front.
        # Roblox renders a SpecialMesh head at part.Size * SpecialMesh.Scale
        # (meshScale) — without the meshScale factor the head came out too small.
        head_geom = _bundled_mesh("head")
        if head_geom:
            ms = part.get("meshScale", [1.0, 1.0, 1.0])
            verts = _add_bundled_into_bm(bm, head_geom,
                                         sx * float(ms[0]), sy * float(ms[1]),
                                         sz * float(ms[2]), scale)
            if verts:
                if decal_axis is not None:
                    _project_decal_planar(bm, uv_layer, verts, decal_axis, slot=1)
                return verts
        # bundle unavailable -> fall through to the sphere approximation below

    if shape == "Ball" or is_head:
        # Ball part (or classic Head fallback): a sphere fit to the part size;
        # project the face decal onto the front for a head.
        ret = bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16,
                                        radius=0.5)
        verts = ret["verts"]
        bmesh.ops.scale(
            bm,
            vec=(max(sx * scale, 1e-4), max(sz * scale, 1e-4),
                 max(sy * scale, 1e-4)),
            verts=verts)
        if is_head and decal_axis is not None:
            _project_decal_planar(bm, uv_layer, verts, decal_axis, slot=1)
        return verts

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


# ---------------------------------------------------------------------------
# Classic R6 2D clothing (Shirt / Pants) wrapped onto the box body.
# ---------------------------------------------------------------------------
# Read directly off Roblox's official 585x559 template guide. The shirt and
# pants templates share ONE layout (shirt paints torso+arms, pants paints
# torso+legs). 64 px/stud, so torso front/back = 128x128, torso sides =
# 64x128, torso up/down = 128x64; limb side faces = 64x128, caps = 64x64.
#
# Template layout (pixels, top-left origin) from the guide image:
#   TORSO:  R·FRONT·L·BACK in a row, UP above FRONT, DOWN below FRONT.
#   RIGHT limb (bottom-left):  L·B·R·F row, U/D above/below F.
#   LEFT  limb (bottom-right): F·L·B·R row, U/D above/below F.
_TPL_W, _TPL_H = 585.0, 559.0


def _px(x, y, w, h):
    """Pixel rect (top-left origin) -> Blender UV rect (u0, v0, u1, v1),
    v flipped (Blender UV is bottom-origin)."""
    return (x / _TPL_W, 1.0 - (y + h) / _TPL_H,
            (x + w) / _TPL_W, 1.0 - y / _TPL_H)


# Per-face in-plane axes (u, v) in the box's Blender-local space, so the
# template rectangle is laid onto the face upright and un-mirrored. (If a
# specific face comes out mirrored/rotated in testing, flip that face's axes
# here — it's a one-line change per face.)
_FACE_AXES = {
    "Front":  (Vector((-1, 0, 0)), Vector((0, 0, 1))),   # +Y
    "Back":   (Vector(( 1, 0, 0)), Vector((0, 0, 1))),   # -Y
    "Right":  (Vector(( 0, 1, 0)), Vector((0, 0, 1))),   # +X
    "Left":   (Vector(( 0, -1, 0)), Vector((0, 0, 1))),  # -X
    "Top":    (Vector((-1, 0, 0)), Vector((0, 1, 0))),   # +Z
    "Bottom": (Vector((-1, 0, 0)), Vector((0, -1, 0))),  # -Z
}

# Layout reverse-engineered from official template + user-read corners.
# Cells are the standard Roblox 64×128 (sides) / 64×64 (caps) / 128×128
# (torso front/back) / 128×64 (torso up/down), separated by a 2-px gap
# between adjacent cells in each region. Anchors (user-measured):
#   torso FRONT top-left = (231, 74)
#   right-limb L top-left = (19, 355)        [F follows at 217 = 19+3*66]
#   left-limb F top-left = (308, 355)
# (F.bottomRight=(280,482) confirms F is 64×128 with inclusive-pixel reading
# 217..280 / 355..482; gap of 2 = 66 - 64.)
_TORSO_RECTS = {
    "Right":  _px(165,  74,  64, 128),    # FRONT.x - 2 - 64
    "Front":  _px(231,  74, 128, 128),    # anchor
    "Left":   _px(361,  74,  64, 128),    # FRONT.x + 128 + 2
    "Back":   _px(427,  74, 128, 128),    # L.x + 64 + 2
    "Top":    _px(231,   8, 128,  64),    # FRONT.y - 2 - 64
    "Bottom": _px(231, 204, 128,  64),    # FRONT.y + 128 + 2
}
_RIGHT_LIMB_RECTS = {
    "Left":   _px( 19, 355, 64, 128),     # anchor
    "Back":   _px( 85, 355, 64, 128),     # +66 each
    "Right":  _px(151, 355, 64, 128),
    "Front":  _px(217, 355, 64, 128),
    "Top":    _px(217, 289, 64,  64),     # F.y - 2 - 64
    "Bottom": _px(217, 485, 64,  64),     # F.y + 128 + 2
}
_LEFT_LIMB_RECTS = {
    "Front":  _px(308, 355, 64, 128),     # anchor
    "Left":   _px(374, 355, 64, 128),     # +66 each
    "Back":   _px(440, 355, 64, 128),
    "Right":  _px(506, 355, 64, 128),
    "Top":    _px(308, 289, 64,  64),
    "Bottom": _px(308, 485, 64,  64),
}


def _clothing_for_part(name, clothing, assets):
    """For a classic body part name, return (regions, template_image_path) or
    (None, None). Torso + arms use the shirt; legs use the pants. regions maps
    face -> (uv_rect, u_axis, v_axis)."""
    if not clothing or assets is None:
        return None, None
    shirt = clothing.get("shirt")
    pants = clothing.get("pants")
    if name == "Torso":
        ref, rects = (shirt or pants), _TORSO_RECTS
    elif name == "Right Arm":
        ref, rects = shirt, _RIGHT_LIMB_RECTS
    elif name == "Left Arm":
        ref, rects = shirt, _LEFT_LIMB_RECTS
    elif name == "Right Leg":
        ref, rects = pants, _RIGHT_LIMB_RECTS
    elif name == "Left Leg":
        ref, rects = pants, _LEFT_LIMB_RECTS
    else:
        return None, None
    if not ref:
        return None, None
    img = assets.get_image_path(ref)
    if not img:
        return None, None
    regions = {k: (rects[k], _FACE_AXES[k][0], _FACE_AXES[k][1]) for k in rects}
    return regions, img


def _build_clothed_box(bm, uv_layer, part, scale, regions):
    """Box for a classic body part with each face UV-mapped into its clothing-
    template rectangle, so the shirt/pants wraps like in-game. Returns verts."""
    sx, sy, sz = (float(s) for s in part.get("size", [1.0, 1.0, 1.0]))
    ret = bmesh.ops.create_cube(bm, size=1.0)
    verts = ret["verts"]
    bmesh.ops.scale(
        bm,
        vec=(max(sx * scale, 1e-4), max(sz * scale, 1e-4), max(sy * scale, 1e-4)),
        verts=verts)
    bm.normal_update()
    faces = set()
    for v in verts:
        faces.update(v.link_faces)
    for f in faces:
        n = f.normal.normalized()
        ax, ay, az = abs(n.x), abs(n.y), abs(n.z)
        if ay >= ax and ay >= az:
            key = "Front" if n.y > 0 else "Back"
        elif ax >= ay and ax >= az:
            key = "Right" if n.x > 0 else "Left"
        else:
            key = "Top" if n.z > 0 else "Bottom"
        reg = regions.get(key)
        if not reg:
            continue
        (u0, v0, u1, v1), uax, vax = reg
        coords = [(loop.vert.co.dot(uax), loop.vert.co.dot(vax)) for loop in f.loops]
        us = [c[0] for c in coords]; vs = [c[1] for c in coords]
        umin, umax = min(us), max(us); vmin, vmax = min(vs), max(vs)
        du = (umax - umin) or 1.0; dv = (vmax - vmin) or 1.0
        for loop, (cu, cv) in zip(f.loops, coords):
            fu = (cu - umin) / du; fv = (cv - vmin) / dv
            loop[uv_layer].uv = (u0 + fu * (u1 - u0), v0 + fv * (v1 - v0))
    return verts


# ---------------------------------------------------------------------------
# Real avatar-clothing composite (the exact Roblox compositor).
#
# A body mesh's UVs are coordinates in Roblox's 1024x512 composite ATLAS — not
# the raw 585x559 Shirt/Pants template. Roblox ships the mapping between them:
# the CompositXBase meshes have their VERTICES at atlas pixels and their UVs
# pointing at the template. So we reconstruct Roblox's real composite texture by
# rasterizing those base meshes (atlas space) while sampling the Shirt/Pants
# template (via the base mesh UVs). The body then samples THIS atlas through its
# own real UVs — exact in-game clothing, no projection. Works for every standard
# body (blocky, packages, …) since they all share the atlas.
# ---------------------------------------------------------------------------
_ATLAS_W, _ATLAS_H = 1024, 512
_composite_cache = {}


def _load_rgba_np(assets, ref):
    """Load a recorder .rgba (header 'ROCORDER-RGBA8\\n<w>\\n<h>\\n' + raw RGBA)
    for an asset ref into an (h, w, 4) float[0..1] numpy array, or None."""
    import numpy as np
    aid = _asset_id(ref)
    if not aid:
        return None
    path = assets._local_path(aid, ".rgba")
    if not path:
        return None
    try:
        data = open(path, "rb").read()
        n1 = data.index(b"\n"); n2 = data.index(b"\n", n1 + 1)
        n3 = data.index(b"\n", n2 + 1)
        w = int(data[n1 + 1:n2]); h = int(data[n2 + 1:n3]); px = data[n3 + 1:]
        if len(px) < w * h * 4:
            return None
        arr = np.frombuffer(px[:w * h * 4], dtype=np.uint8).astype(np.float32) / 255.0
        return arr.reshape((h, w, 4))   # row 0 = top (Roblox order)
    except Exception:
        return None


def _raster_tri(np, atlas, tmpl, P, U):
    """Rasterize one triangle into atlas (rows = Y, top-down), sampling tmpl at
    barycentric-interpolated UVs, alpha-composited over what's already there."""
    th, tw = tmpl.shape[0], tmpl.shape[1]
    ah, aw = atlas.shape[0], atlas.shape[1]
    x0 = max(0, int(np.floor(P[:, 0].min()))); x1 = min(aw - 1, int(np.ceil(P[:, 0].max())))
    y0 = max(0, int(np.floor(P[:, 1].min()))); y1 = min(ah - 1, int(np.ceil(P[:, 1].max())))
    if x1 < x0 or y1 < y0:
        return
    xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
    x1p, y1p = P[0]; x2p, y2p = P[1]; x3p, y3p = P[2]
    den = (y2p - y3p) * (x1p - x3p) + (x3p - x2p) * (y1p - y3p)
    if abs(den) < 1e-9:
        return
    a = ((y2p - y3p) * (xs - x3p) + (x3p - x2p) * (ys - y3p)) / den
    b = ((y3p - y1p) * (xs - x3p) + (x1p - x3p) * (ys - y3p)) / den
    c = 1.0 - a - b
    mask = (a >= -1e-4) & (b >= -1e-4) & (c >= -1e-4)
    if not mask.any():
        return
    u = a * U[0, 0] + b * U[1, 0] + c * U[2, 0]
    v = a * U[0, 1] + b * U[1, 1] + c * U[2, 1]
    tx = np.clip((u * tw).astype(np.int32), 0, tw - 1)
    ty = np.clip((v * th).astype(np.int32), 0, th - 1)
    samp = tmpl[ty, tx]
    al = samp[..., 3]
    m = mask
    dst = atlas[ys[m], xs[m]]
    a_m = al[m][:, None]
    atlas[ys[m], xs[m], :3] = samp[m][:, :3] * a_m + dst[:, :3] * (1.0 - a_m)
    atlas[ys[m], xs[m], 3] = 1.0


# Each R6 body part is composited into its OWN texture (Roblox does too — which
# is why their atlas UVs OVERLAP in [0,1]; they index different textures, not one
# shared atlas). Map: part name -> (base mesh, which template).
_R6_PART_BAKE = {
    "Torso": ("compTorso", "shirt"),
    "Left Arm": ("compLeftArm", "shirt"),
    "Right Arm": ("compRightArm", "shirt"),
    "Left Leg": ("compLeftLeg", "pants"),
    "Right Leg": ("compRightLeg", "pants"),
}

# R15 body part -> (compositor base mesh, template). Validated: legs reuse the
# ARM composite (same limb mapping), reading the pants template instead of the
# shirt. R15 uses per-part textures, so the atlas size is the base mesh bbox.
# Hands and feet are NOT covered by classic Shirt/Pants in Roblox (the sleeve
# ends at the wrist, pants at the ankle — hands/feet are bare skin or separate
# gloves/shoes). So they are intentionally excluded; they keep their own
# texture/skin instead of getting the sleeve/cuff region painted on.
_R15_PART_BAKE = {
    "UpperTorso": ("R15compTorso", "shirt"), "LowerTorso": ("R15compTorso", "shirt"),
    "LeftUpperArm": ("R15compLeftArm", "shirt"), "LeftLowerArm": ("R15compLeftArm", "shirt"),
    "RightUpperArm": ("R15compRightArm", "shirt"), "RightLowerArm": ("R15compRightArm", "shirt"),
    "LeftUpperLeg": ("R15compLeftArm", "pants"), "LeftLowerLeg": ("R15compLeftArm", "pants"),
    "RightUpperLeg": ("R15compRightArm", "pants"), "RightLowerLeg": ("R15compRightArm", "pants"),
}


def _bake_one_part(np, base_name, tmpl, tmpl_id, sk, cache_dir, log):
    """Bake one body part's composite texture (skin + clothing) at 1024x512 and
    return its PNG path, or None. Cached per (base mesh, template, skin)."""
    key = "bake_{}_{}_{}".format(base_name, tmpl_id,
                                 "".join("%02x" % int(c * 255 + 0.5) for c in sk))
    if key in _composite_cache:
        return _composite_cache[key]
    out_png = os.path.join(cache_dir, key + ".png")
    if os.path.isfile(out_png) and os.path.getsize(out_png) > 0:
        _composite_cache[key] = out_png
        return out_png
    geom = _bundled_mesh(base_name)
    if not geom:
        return None
    atlas = np.empty((_ATLAS_H, _ATLAS_W, 4), dtype=np.float32)
    atlas[..., 0] = sk[0]; atlas[..., 1] = sk[1]; atlas[..., 2] = sk[2]; atlas[..., 3] = 1.0
    V = geom["verts"]; U = geom["uvs"]; F = geom["faces"]
    for (a, b, c) in F:
        P = np.array([[V[a][0], V[a][1]], [V[b][0], V[b][1]], [V[c][0], V[c][1]]],
                     dtype=np.float32)
        UU = np.array([[U[a][0], U[a][1]], [U[b][0], U[b][1]], [U[c][0], U[c][1]]],
                      dtype=np.float32)
        _raster_tri(np, atlas, tmpl, P, UU)
    try:
        img = bpy.data.images.new(key, width=_ATLAS_W, height=_ATLAS_H, alpha=True)
        img.pixels = np.flipud(atlas).reshape(-1).tolist()
        img.filepath_raw = out_png
        img.file_format = "PNG"
        img.save()
    except Exception as e:
        if log:
            log("    composite bake save failed: {}".format(e))
        return None
    _composite_cache[key] = out_png
    return out_png


def _bake_avatar_composite(clothing, skin_rgb, assets, cache_dir, log):
    """Bake Roblox's real per-part composite textures (skin + Shirt/Pants) for an
    avatar. Returns {part_name: png_path}, or {} if unavailable."""
    try:
        import numpy as np
    except Exception:
        return {}
    shirt = clothing.get("shirt"); pants = clothing.get("pants")
    if not (shirt or pants):
        return {}
    sk = tuple(max(0.0, min(1.0, float(c))) for c in (skin_rgb or (0.9, 0.72, 0.57)))
    shirt_np = _load_rgba_np(assets, shirt) if shirt else None
    pants_np = _load_rgba_np(assets, pants) if pants else None
    out = {}
    for part_name, (base_name, which) in _R6_PART_BAKE.items():
        if which == "shirt":
            tmpl, tid = shirt_np, _asset_id(shirt) or 0
        else:
            tmpl, tid = pants_np, _asset_id(pants) or 0
        if tmpl is None:
            continue
        png = _bake_one_part(np, base_name, tmpl, tid, sk, cache_dir, log)
        if png:
            out[part_name] = png
    if out and log:
        log("    baked {} per-part composite textures".format(len(out)))
    return out


def _build_part_object(part, name, place, scale, assets, import_meshes,
                       arm_obj, collection, log, clothing=None,
                       apply_clothing=False, reproject=False,
                       composite_atlas=None):
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
    r6_clothing_regions = None
    compositor_base = None
    # Stage 3a (single-canvas composite) was REVERTED: the R6 template reuses
    # the same limb cells for arms (shirt) and legs (pants), so merging shirt+
    # pants onto one canvas made the pants overwrite the arm cells — arms then
    # sampled the leg texture. The per-part path below (shirt->arms, pants->legs,
    # each its own image) is correct. bakedTextureId is ignored.
    if import_meshes and assets is not None:
        if part.get("meshId"):
            mesh_data = assets.get_mesh(part.get("meshId"))
        # R15 body MeshPart with classic clothing: remap its UVs through the
        # matching R15 compositor base mesh (per-part texture, atlas = bbox).
        # Legs reuse the arm base reading the pants template.
        if (mesh_data is not None and apply_clothing and clothing
                and name in _R15_PART_BAKE):
            _bn, _which = _R15_PART_BAKE[name]
            _base = _bundled_mesh(_bn)
            _ref = clothing.get("shirt") if _which == "shirt" else clothing.get("pants")
            _tmpl = assets.get_image_path(_ref) if _ref else None
            if _base and _tmpl:
                body_tex = _tmpl
                compositor_base = _prep_compositor_base(_base)   # atlas = bbox
        # Classic blocky R6 body part (Block shape, no CharacterMesh, no mesh
        # id): use Roblox's OWN bundled body mesh — exact beveled geometry plus
        # the real R6 clothing UVs. Clothing then maps through the mesh's own
        # UVs (no box, no projection, no bevel modifier). Falls through to the
        # generated clothed-box if the bundle is unavailable.
        if (mesh_data is None and not part.get("meshId")
                and not part.get("meshType")
                and part.get("shape") == "Block"
                and name in _R6_BUNDLED_BODY):
            bundled = _bundled_mesh(_R6_BUNDLED_BODY[name])
            if bundled:
                mesh_data = bundled
                if apply_clothing and clothing:
                    # Blocky body: the classic box->template cube projection is
                    # exact for a box (its UV layout differs from CharacterMesh).
                    _rg, _cimg = _clothing_for_part(name, clothing, assets)
                    if _cimg:
                        body_tex = _cimg
                        r6_clothing_regions = _rg
        # Texture selection for body parts.
        #
        # Plain Block body parts (no CharacterMesh): the clothing-box path
        # below wraps the classic R6 Shirt/Pants template onto the box.
        #
        # CharacterMesh body parts with classic clothing: we override the
        # mesh's authored UVs with a cube projection into the R6 template
        # — same wrap a Block body would get, applied to the sculpted
        # mesh. Without this, painting Shirt/Pants via the mesh's authored
        # UVs splattered the texture (1.15.x bug — modeler-authored UVs
        # rarely follow the R6 template layout). Texture used is the
        # Shirt/Pants directly.
        #
        # CharacterMesh body part without clothing: render with the mesh's
        # own UVs and BaseTextureId (textureId in the rig record). Falls
        # back to plain color if neither is present.
        if (apply_clothing and clothing and part.get("charMesh")
                and name in ("Torso", "Left Arm", "Right Arm",
                             "Left Leg", "Right Leg")):
            # Stage 0 (1.24.5): the in-engine probe confirmed CharacterMesh
            # bodies are authored with UVs that ALREADY follow the R6 585x559
            # clothing template (sampled torso UVs landed in the canonical
            # U[0.031..0.45] V[0..0.98] band across multiple different
            # avatars). So apply the Shirt/Pants template DIRECTLY using the
            # mesh's own authored UVs, instead of overwriting them with the
            # cube projection (_r6_cube_project_clothing_uvs). That cube
            # override is what splattered clothing on sculpted bodies — the
            # authored UVs are the correct mapping. Leaving r6_clothing_regions
            # as None makes _add_mesh_geometry keep the authored faceUVs.
            _base = (_bundled_mesh(_R6_PART_BAKE[name][0])
                     if name in _R6_PART_BAKE else None)
            _regions, cloth_img = _clothing_for_part(name, clothing, assets)
            if cloth_img and _base:
                # REAL path: a CharacterMesh body's UVs are atlas coords too —
                # remap them through the compositor base mesh to the template.
                # R6 uses the shared 1024x512 atlas.
                body_tex = cloth_img
                compositor_base = _prep_compositor_base(_base, 1024, 512)
            elif cloth_img:
                body_tex = cloth_img
                if reproject and _regions:
                    r6_clothing_regions = _regions
        if body_tex is None:
            tref = part.get("textureId") or part.get("colorMap")
            if tref:
                body_tex = assets.get_image_path(tref)

    verts = None
    if mesh_data:
        verts = _add_mesh_geometry(bm, uv_layer, mesh_data, place, part, scale,
                                   r6_clothing_regions=r6_clothing_regions,
                                   cylindrical=reproject,
                                   compositor_base=compositor_base)
    if verts:
        kind = "mesh-clothed" if (r6_clothing_regions or compositor_base) else "mesh"
        materials.append(_image_material(name, body_tex, color, transp, log)
                         if body_tex else _color_material(color, transp))
    else:
        # Classic R6 2D clothing: a Block body part (Torso / arms / legs) with
        # no mesh, when the player has a Shirt/Pants, becomes a box whose faces
        # are UV-mapped into the 585x559 template so the clothing wraps like
        # in-game.
        cloth_regions, cloth_tex = (None, None)
        if apply_clothing and import_meshes and assets is not None \
                and part.get("shape") == "Block" and not part.get("meshType"):
            cloth_regions, cloth_tex = _clothing_for_part(name, clothing, assets)
        if cloth_regions and cloth_tex:
            verts = _build_clothed_box(bm, uv_layer, part, scale, cloth_regions)
            materials.append(_image_material(name + "_cloth", cloth_tex,
                                             color, transp, log))
            kind = "clothed-box"
            bmesh.ops.transform(bm, matrix=place, verts=verts)
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
    obj["rocorder_part_name"] = name   # used by the per-part visibility pass
    collection.objects.link(obj)
    vg = obj.vertex_groups.new(name=name)
    vg.add(list(range(len(mesh.vertices))), 1.0, "REPLACE")
    obj.parent = arm_obj
    obj.matrix_parent_inverse = arm_obj.matrix_world.inverted()
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj
    mod.use_vertex_groups = True
    obj["rocorder_bone"] = name

    # Roblox primitive parts have slightly beveled (chamfered) edges, not sharp
    # cube corners. Add a small angle-limited Bevel modifier to box-shaped parts
    # so they read like in-game blocks. Non-destructive (a modifier), so it
    # doesn't disturb the clothing UVs. Meshes keep their own geometry.
    if kind in ("box", "clothed-box", "box+decal"):
        bev = obj.modifiers.new("Bevel", "BEVEL")
        bev.width = 0.06 * scale
        bev.segments = 2
        bev.limit_method = "ANGLE"
        bev.angle_limit = math.radians(30.0)
        # keep the bevel ahead of the armature deform
        try:
            obj.modifiers.move(obj.modifiers.find("Bevel"),
                               obj.modifiers.find("Armature"))
        except (RuntimeError, ValueError):
            pass
    return obj, kind


def _iter_action_fcurves(action):
    """Yield every F-Curve of an Action across Blender versions. Pre-4.4 stored
    them on `action.fcurves`; Blender 4.4+/5.x "slotted actions" removed that
    attribute and keep F-Curves under layers -> strips -> channelbags -> fcurves
    (the legacy attribute now raises AttributeError, hence the getattr guard)."""
    if action is None:
        return
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        for fc in legacy:
            yield fc
        return
    for layer in getattr(action, "layers", ()):
        for strip in getattr(layer, "strips", ()):
            bags = getattr(strip, "channelbags", None)
            if bags is not None:
                for bag in bags:
                    for fc in getattr(bag, "fcurves", ()):
                        yield fc
                continue
            # Fallback: per-slot channelbag accessor (API variant)
            cb = getattr(strip, "channelbag", None)
            if callable(cb):
                for slot in getattr(action, "slots", ()):
                    bag = cb(slot)
                    if bag is not None:
                        for fc in getattr(bag, "fcurves", ()):
                            yield fc


# ----------------------------------------------------------------------------
# Per-part visibility (lifetime) — keyframe hide_viewport + hide_render so a
# part is only visible during the time windows it actually existed in-game.
# Drives three things: dead lives' parts/accessories disappear, equipped tools
# come and go, and POV viewmodel guns vanish when swapped instead of floating.
# ----------------------------------------------------------------------------
def _merge_frame_intervals(iv):
    """Merge overlapping/adjacent (f_start, f_end|None) intervals. None = open
    to the end (absorbs everything after it)."""
    if not iv:
        return iv
    iv = sorted(iv, key=lambda x: x[0])
    merged = [iv[0]]
    for s, e in iv[1:]:
        ls, le = merged[-1]
        if le is None:
            break  # already open to end
        if s <= le:
            merged[-1] = (ls, None if e is None else max(le, e))
        else:
            merged.append((s, e))
    return merged


def _windows_to_frame_intervals(windows, fps):
    """Convert [(fromT, toT|None), ...] in seconds to merged frame intervals
    [(f_start, f_end|None), ...]. f_end is the first hidden frame (exclusive);
    None means visible to the end of the recording."""
    out = []
    for wf, wt in windows:
        f_start = max(1, int(round((wf or 0.0) * fps)) + 1)
        f_end = None if wt is None else (int(round(wt * fps)) + 1)
        if f_end is not None and f_end <= f_start:
            continue  # zero/negative length — skip
        out.append((f_start, f_end))
    return _merge_frame_intervals(out)


def _keyframe_visibility(obj, windows, fps, last_frame):
    """Keyframe hide_viewport + hide_render (CONSTANT interp) so `obj` is
    visible only during `windows` (list of (fromT, toT|None) seconds)."""
    iv = _windows_to_frame_intervals(windows, fps)

    # Always-visible fast path: one interval covering [<=1 .. end]. Leave the
    # object un-keyframed (no clutter for the common whole-recording part).
    if len(iv) == 1 and iv[0][0] <= 1 and iv[0][1] is None:
        return

    def setvis(frame, hidden):
        obj.hide_viewport = hidden
        obj.hide_render = hidden
        obj.keyframe_insert(data_path="hide_viewport", frame=frame)
        obj.keyframe_insert(data_path="hide_render", frame=frame)

    if not iv:
        setvis(1, True)  # never present → hidden the whole time
    else:
        if iv[0][0] > 1:
            setvis(1, True)
        for f_start, f_end in iv:
            setvis(f_start, False)
            if f_end is not None and f_end <= last_frame:
                setvis(f_end, True)

    if obj.animation_data and obj.animation_data.action:
        for fc in _iter_action_fcurves(obj.animation_data.action):
            if fc.data_path in ("hide_viewport", "hide_render"):
                for kp in fc.keyframe_points:
                    kp.interpolation = "CONSTANT"


# ----------------------------------------------------------------------------
# Rig file
# ----------------------------------------------------------------------------
def _expand_lives(rig_players, log):
    """Flatten a RIG/3 players dict into a list of single-life records.
    Each result entry is one armature's worth of input:
        {
            "uid": int,
            "life_idx": 1-based int,
            "n_lives": total lives for this player in the rig,
            "fromT": float seconds (0 means start of recording),
            "toT": float seconds or None (None = active until end),
            "rig": single-life rig dict the build_player(...) expects
                   (rigType, parts, joints, clothing, name, displayName, userId),
            "label_suffix": "" if only one life, "_LifeN" otherwise,
        }
    Backward-compat: a RIG/2 player record (no `revisions` field) becomes
    one life spanning [0, None]."""
    lives = []
    for uid_key, p in (rig_players or {}).items():
        try:
            uid = int(uid_key)
        except (ValueError, TypeError):
            log("WARN non-int uid key in rig: {}".format(uid_key))
            continue
        name = p.get("name")
        display = p.get("displayName")
        revisions = p.get("revisions")
        if revisions:
            n_lives = len(revisions)
            for i, rev in enumerate(revisions):
                # Build a single-life rig dict the existing build_player path
                # expects (it doesn't know about revisions).
                single = {
                    "userId": uid,
                    "name": name,
                    "displayName": display,
                    "rigType": rev.get("rigType"),
                    "parts": rev.get("parts", []),
                    "partSpans": rev.get("partSpans"),
                    "joints": rev.get("joints", []),
                    "clothing": rev.get("clothing"),
                    "characterMeshes": rev.get("characterMeshes"),
                    "externalParts": rev.get("externalParts"),
                }
                lives.append({
                    "uid": uid,
                    "life_idx": i + 1,
                    "n_lives": n_lives,
                    "fromT": float(rev.get("fromT") or 0.0),
                    "toT": rev.get("toT"),
                    "rig": single,
                    "label_suffix": ("_Life{}".format(i + 1)) if n_lives > 1 else "",
                })
        else:
            # RIG/2 flat shape: whole player is one life.
            lives.append({
                "uid": uid,
                "life_idx": 1,
                "n_lives": 1,
                "fromT": 0.0,
                "toT": None,
                "rig": p,
                "label_suffix": "",
            })
    return lives


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
                 assets=None, import_meshes=False, apply_clothing=False,
                 reproject=False):
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

    # Clothing now uses a direct UV remap through Roblox's compositor base
    # meshes (per part, inside _build_part_object) — no atlas bake needed.
    composite_atlas = None

    for p in parts:
        name = p["name"]
        if name not in R:
            skipped_parts.append((name, "bone missing from R"))
            continue
        # Cull collision / hitbox volumes (bone kept so joints don't break).
        # Stage 2: the recorder marks these with rendered=false — it accounts
        # for LocalTransparencyModifier, so parts a game hides only from the
        # local view (Transparency=0 but locally invisible) are culled too,
        # instead of importing as solid boxes that override the real mesh.
        # Pre-1.25.0 rigs have no flag → fall back to the legacy transparency
        # heuristic.
        rendered = p.get("rendered")
        if rendered is False or (
                rendered is None
                and float(p.get("transparency", 0.0)) >= 0.999
                and not p.get("meshId") and not p.get("decals")):
            reason = p.get("cullReason") or "transparency>=0.999, no mesh/decal"
            skipped_parts.append((name, reason + " (bone kept)"))
            continue

        place = D.get(name, Matrix.Identity(4))
        target = body_coll if name in jointed else acc_coll
        obj, kind = _build_part_object(p, name, place, scale, assets,
                                       import_meshes, arm_obj, target, log,
                                       clothing=player_rig.get("clothing"),
                                       apply_clothing=apply_clothing,
                                       reproject=reproject,
                                       composite_atlas=composite_atlas)
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
        log("  classic R6 clothing {} — shirt wraps torso+arms, pants wraps "
            "legs via the 585x559 template UV layout".format(
                "applied" if apply_clothing else "NOT applied (option off)"))

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
def _is_viewmodel_uid(uid):
    """Negative uid is the recorder's POV viewmodel sentinel (real UserIds are
    always positive). Imported as a separate top-level Viewmodel collection."""
    try:
        return int(uid) < 0
    except (TypeError, ValueError):
        return False


def _player_label(roster, uid):
    if _is_viewmodel_uid(uid):
        return "Viewmodel" if int(uid) == -1 else "Viewmodel_{}".format(-int(uid))
    info = roster.get(uid, {})
    name = info.get("displayName") or info.get("name") or "Player"
    return "{}_{}".format(name, uid)


def _short_matrix(m):
    t = m.translation
    return "({:+.3f},{:+.3f},{:+.3f})".format(t.x, t.y, t.z)


def import_replay(context, filepath, scale, set_fps, build_armature,
                  force_standard_r6, import_meshes, roblosecurity, cache_dir,
                  debug, report, apply_classic_clothing=True,
                  clothing_reproject=False):
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
        # The recorder writes engine-extracted assets to the GLOBAL
        # ROCORDER/assets folder, but recordings now nest under
        # ROCORDER/recordings/<name>/ (1.24.0 layout) — so ROCORDER/assets is
        # TWO levels up from the .rec, not one. Search the recording's own
        # folder first, then walk a few ancestors looking for an 'assets'
        # sibling, so the global store is found regardless of nesting depth.
        local_dirs = [
            os.path.join(rec_dir, "assets"),
            os.path.join(rec_dir, "rocorder_assets"),
        ]
        _ancestor = rec_dir
        for _ in range(3):
            _ancestor = os.path.dirname(_ancestor)
            if not _ancestor or _ancestor == os.path.dirname(_ancestor):
                break  # reached filesystem root
            local_dirs.append(os.path.join(_ancestor, "assets"))
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

    # RIG/3: expand the rig into per-life entries. RIG/2 players become one
    # life each. Multi-life players get one armature per life in their own
    # sub-collection, with visibility keyframed at the life boundaries.
    all_lives = _expand_lives(rig_players, log)
    # Index for fast frame-time lookup: uid -> [life_info, ...] sorted by fromT.
    lives_by_uid = {}
    for li in all_lives:
        lives_by_uid.setdefault(li["uid"], []).append(li)
    for uid, lst in lives_by_uid.items():
        lst.sort(key=lambda li: li["fromT"])

    def player_coll(uid, life_idx=None, label_suffix=""):
        """Top-level collection per player; if life_idx is set and the player
        has multiple lives, a nested collection per life."""
        top = player_colls.get(uid)
        if top is None:
            top = bpy.data.collections.new(_player_label(roster, uid))
            root_coll.children.link(top)
            player_colls[uid] = top
        if not label_suffix:
            return top
        key = (uid, life_idx)
        sub = player_colls.get(key)
        if sub is None:
            sub = bpy.data.collections.new(
                _player_label(roster, uid) + label_suffix)
            top.children.link(sub)
            player_colls[key] = sub
        return sub

    def ensure_life(life_info):
        """Build one armature for one life. Keyed by (uid, life_idx)."""
        key = (life_info["uid"], life_info["life_idx"])
        if key in players:
            return players[key]
        rig = life_info["rig"]
        uid = life_info["uid"]
        label_base = _player_label(roster, uid)
        label = label_base + life_info["label_suffix"]
        log("---- building player uid={} ({}) life {}/{} t=[{:.2f}..{}] ----"
            .format(uid, rig.get("name"), life_info["life_idx"],
                    life_info["n_lives"], life_info["fromT"],
                    "end" if life_info["toT"] is None
                          else "{:.2f}".format(life_info["toT"])))
        log("  rigType={} parts={} joints={}".format(
            rig.get("rigType"), len(rig.get("parts", [])),
            len(rig.get("joints", []))))
        coll = player_coll(uid, life_info["life_idx"], life_info["label_suffix"])
        built = build_player(rig, label, scale, coll, log,
                             force_standard_r6=force_standard_r6,
                             assets=assets, import_meshes=import_meshes,
                             apply_clothing=apply_classic_clothing,
                             reproject=clothing_reproject)
        if built is None:
            return None
        built["last_quat"] = {}
        built["arm"]["rocorder_user_id"] = str(uid)
        built["arm"]["rocorder_life_idx"] = life_info["life_idx"]
        built["life_info"] = life_info
        players[key] = built
        # Per-life keyframe counters (one bone-count map per armature).
        bone_keycount[key] = {nm: 0 for nm in built["R"]}
        frame_seen[key] = set()
        part_count_mismatch[key] = []
        return built

    # Pick the active life for (uid, frame_time). Cached per-uid linear scan
    # is fine — usually 1-3 lives per player. Returns life_info or None.
    def active_life(uid, t):
        lst = lives_by_uid.get(uid)
        if not lst:
            return None
        for li in lst:
            to_t = li["toT"]
            if li["fromT"] <= t and (to_t is None or t < to_t):
                return li
        # If t is past all closed lives but before the start of any current
        # life: shouldn't normally happen; route to last life as best-effort.
        return lst[-1]

    use_armatures = build_armature and bool(all_lives)
    if use_armatures:
        for li in all_lives:
            ensure_life(li)

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
            # Find which life this frame belongs to (RIG/3). For RIG/2 there's
            # exactly one life so this just returns it. For RIG/3 multi-life
            # players, the frame routes to the armature whose [fromT, toT]
            # contains `t`.
            built = None
            li = None
            if use_armatures:
                li = active_life(uid, t)
                if li is not None:
                    built = ensure_life(li)
            keycount_key = (uid, li["life_idx"]) if li else None

            if built is not None:
                order = built["order"]
                R = built["R"]
                D = built["D"]
                parent_of = built["parent_of"]
                last_q = built["last_quat"]
                arm = built["arm"]

                if len(part_list) != len(order):
                    part_count_mismatch[keycount_key].append(
                        (frame_num, len(part_list), len(order)))
                frame_seen[keycount_key].add(frame_num)

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
                    bone_keycount[keycount_key][name] += 1
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
        for fc in _iter_action_fcurves(ad.action):
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
            for fc in _iter_action_fcurves(camera_data.animation_data.action):
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"

    scene.frame_start = 1
    scene.frame_end = last_frame
    scene.frame_current = 1

    # Visibility / lifetime keyframes. Each part object is shown only during
    # the presence spans the recorder captured for it (RIG/3 partSpans), so:
    #   - a dead life's parts + accessories disappear at the death boundary,
    #   - tools equipped/dropped mid-life come and go,
    #   - POV viewmodel guns vanish when swapped instead of floating forever.
    # Keyed on BOTH hide_viewport and hide_render (CONSTANT interp) so the
    # viewport isn't flooded with hidden-in-render-only junk. Older recordings
    # with no partSpans fall back to the whole-life window (so multi-life
    # players still hide correctly; single-life parts stay always-visible).
    vis_part_keyed = 0
    for (uid, life_idx), built in players.items():
        li = built.get("life_info")
        rig = (li.get("rig") if li else None) or {}
        rig_parts = rig.get("parts") or []
        part_spans = rig.get("partSpans")
        fromT = float(li["fromT"]) if li else 0.0
        toT = li["toT"] if li else None
        n_lives = li["n_lives"] if li else 1

        # Map part NAME -> its spans. parts[] and partSpans[] are parallel in
        # the rig record; mapping by name (not index) is robust against the
        # name-filtering and bone-rename steps build_player applies.
        spans_by_name = {}
        if part_spans is not None:
            for j, p in enumerate(rig_parts):
                nm = p.get("name")
                if nm is not None and j < len(part_spans):
                    spans_by_name[nm] = part_spans[j]

        for obj in built["part_objs"]:
            name = obj.get("rocorder_part_name")
            if part_spans is not None and name in spans_by_name:
                raw = spans_by_name[name] or []
                windows = [(s[0], (s[1] if len(s) > 1 else None)) for s in raw]
            else:
                windows = [(fromT, toT)]  # backward compat: whole-life window
            had_anim = bool(obj.animation_data and obj.animation_data.action)
            _keyframe_visibility(obj, windows, fps, last_frame)
            now_anim = bool(obj.animation_data and obj.animation_data.action)
            if now_anim and not had_anim:
                vis_part_keyed += 1

        # Armature object: hide its skeleton outside the life window so a dead
        # life's bones don't clutter the viewport (render ignores bare rigs).
        if n_lives > 1:
            _keyframe_visibility(built["arm"], [(fromT, toT)], fps, last_frame)

    log("visibility: {} part objects got lifetime keyframes".format(vis_part_keyed))

    # ============ END-OF-IMPORT DIAGNOSTICS ============
    log("")
    log("=" * 30 + " DIAGNOSTICS " + "=" * 30)
    for key, built in players.items():
        uid, life_idx = key
        li = built.get("life_info") or {}
        suffix = li.get("label_suffix", "")
        log("uid {} ({}){}:".format(uid, _player_label(roster, uid), suffix))

        kc = bone_keycount[key]
        zero_bones = [b for b, c in kc.items() if c == 0]
        if zero_bones:
            log("  *** {} BONES WITH ZERO KEYFRAMES (these are the 'missing' parts):".format(
                len(zero_bones)))
            for b in zero_bones:
                log("       - {}".format(b))
        else:
            log("  all {} bones got keyframes".format(len(kc)))

        # frame coverage / gap detection
        seen = sorted(frame_seen[key])
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

        mis = part_count_mismatch[key]
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
        log("assets: {} geom.json + {} rgba (engine-extracted), {} bare-local, "
            "{} downloaded, {} cache hits, {} fails ({} auth/401)".format(
                assets.stats["geom_hits"], assets.stats["rgba_hits"],
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
    apply_classic_clothing: BoolProperty(
        name="Apply classic clothing (Shirt/Pants)",
        description="Wrap classic 2D Shirt/Pants templates onto the box body "
                    "(Torso/arms/legs) via Roblox's 585x559 template UV layout. "
                    "Turn off to leave the classic body flat-colored",
        default=True,
    )
    clothing_reproject: BoolProperty(
        name="Reproject clothing (custom/sculpted bodies)",
        description="EXPERIMENTAL (Stage 3b). For sculpted CharacterMesh bodies "
                    "whose authored UVs don't follow the R6 template (clothing "
                    "comes out scrambled), wrap the Shirt/Pants around the part "
                    "cylindrically instead. Leave OFF for normal avatars — it "
                    "overrides the authored UVs and is only an approximation "
                    "that may need tuning. Only affects CharacterMesh bodies",
        default=False,
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
            apply_classic_clothing=self.apply_classic_clothing,
            clothing_reproject=self.clothing_reproject,
        )


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_rocorder.bl_idname, text="Roblox Replay (.rec)")


_classes = (IMPORT_OT_rocorder,)


# ---- Bundled engine-mesh data (base64 of Roblox's shipped v2 .mesh
# files: classic blocky body parts, classic head, and the compositor
# base meshes used to bake the real clothing atlas). Loaded by
# _bundled_mesh(). Auto-generated. ----
_BUNDLED_MESH_B64 = {
    "torso": "dmVyc2lvbiAyLjAwCgwAJAwqAAAALAAAAClcb78oXG8/AAAAPwAAAAAAAAAAAACAPwgAHj78/wA/AAAAAClcb78pXG+/AAAAPwAAAAAAAAAAAACAP97/Bz38/wA/AAAAAClcbz8pXG+/AAAAPwAAAAAAAAAAAACAP97/Bz0EAD8/AAAAAClcbz8oXG8/AAAAPwAAAAAAAAAAAACAPwgAHj4EAD8/AAAAAClcb78AAIA/UrjePgAAAAAAAIA/AAAAAPj/IT78/wA/AAAAAAAAgL8oXG8/UrjePgAAgL8AAAAAAAAAAAgAHj4IAP4+AAAAAAAAgL8pXG+/UrjePgAAgL8AAAAAAAAAAN7/Bz0IAP4+AAAAAClcb78AAIC/UrjePgAAAAAAAIC/AAAAAEMA8Dz8/wA/AAAAAClcbz8AAIC/UrjePgAAAAAAAIC/AAAAAEMA8DwEAD8/AAAAAAAAgD8pXG+/UrjePgAAgD8AAAAAAAAAAN7/Bz38/0A/AAAAAAAAgD8oXG8/UrjePgAAgD8AAAAAAAAAAAgAHj78/0A/AAAAAClcbz8AAIA/UrjePgAAAAAAAIA/AAAAAPj/IT4EAD8/AAAAAClcb78AAIA/UrjePgAAAAAAAIA/AAAAAPj/IT4IAP4+AAAAAClcb78AAIA/UrjevgAAAAAAAIA/AAAAAPj/IT74/8E+AAAAAAAAgL8oXG8/UrjevgAAgL8AAAAAAAAAAAgAHj74/8E+AAAAAAAAgL8pXG+/UrjevgAAgL8AAAAAAAAAAN7/Bz34/8E+AAAAAClcb78AAIC/UrjevgAAAAAAAIC/AAAAAEMA8Dz4/8E+AAAAAClcb78AAIC/UrjePgAAAAAAAIC/AAAAAEMA8DwIAP4+AAAAAClcb78AAIC/UrjePgAAAAAAAIC/AAAAAPZ/5j4GgEI/AAAAAClcb78AAIC/UrjevgAAAAAAAIC/AAAAAPZ/5j7+fyQ/AAAAAClcbz8AAIC/UrjevgAAAAAAAIC/AAAAAA+AqD7+fyQ/AAAAAClcbz8AAIC/UrjePgAAAAAAAIC/AAAAAA+AqD4GgEI/AAAAAClcbz8AAIC/UrjePgAAAAAAAIC/AAAAAEMA8DzO+387AAAAAClcbz8AAIC/UrjevgAAAAAAAIC/AAAAAEMA8DwiAPg9AAAAAAAAgD8pXG+/UrjevgAAgD8AAAAAAAAAAN7/Bz0iAPg9AAAAAAAAgD8pXG+/UrjePgAAgD8AAAAAAAAAAN7/Bz3O+387AAAAAAAAgD8oXG8/UrjevgAAgD8AAAAAAAAAAAgAHj4iAPg9AAAAAAAAgD8oXG8/UrjePgAAgD8AAAAAAAAAAAgAHj7O+387AAAAAClcbz8AAIA/UrjevgAAAAAAAIA/AAAAAPj/IT4iAPg9AAAAAClcbz8AAIA/UrjePgAAAAAAAIA/AAAAAPj/IT7O+387AAAAAClcbz8AAIA/UrjePgAAAAAAAIA/AAAAABkASj7+fyQ/AAAAAClcbz8AAIA/UrjevgAAAAAAAIA/AAAAABkASj4GgEI/AAAAAClcb78AAIA/UrjevgAAAAAAAIA/AAAAAPP/oj4GgEI/AAAAAClcb78AAIA/UrjePgAAAAAAAIA/AAAAAPP/oj7+fyQ/AAAAAClcb78AAIA/UrjevgAAAAAAAIA/AAAAAPj/IT4IAL4+AAAAAClcb78oXG8/AAAAvwAAAAAAAAAAAACAvwgAHj4IAL4+AAAAAClcb78pXG+/AAAAvwAAAAAAAAAAAACAv97/Bz0IAL4+AAAAAClcb78AAIC/UrjevgAAAAAAAIC/AAAAAEMA8DwIAL4+AAAAAClcbz8pXG+/AAAAvwAAAAAAAAAAAACAv97/Bz3v/wM+AAAAAClcbz8AAIC/UrjevgAAAAAAAIC/AAAAAEMA8Dzv/wM+AAAAAClcbz8oXG8/AAAAvwAAAAAAAAAAAACAvwgAHj7v/wM+AAAAAClcbz8AAIA/UrjevgAAAAAAAIA/AAAAAPj/IT7v/wM+AAAAAAAAAAABAAAAAgAAAAAAAAACAAAAAwAAAAAAAAAEAAAABQAAAAAAAAAFAAAABgAAAAAAAAAGAAAAAQAAAAEAAAAGAAAABwAAAAEAAAAHAAAACAAAAAEAAAAIAAAAAgAAAAIAAAAIAAAACQAAAAIAAAAJAAAACgAAAAIAAAAKAAAAAwAAAAMAAAAKAAAACwAAAAMAAAALAAAABAAAAAMAAAAEAAAAAAAAAAwAAAANAAAADgAAAAwAAAAOAAAABQAAAAUAAAAOAAAADwAAAAUAAAAPAAAABgAAAAYAAAAPAAAAEAAAAAYAAAAQAAAAEQAAABIAAAATAAAAFAAAABIAAAAUAAAAFQAAABYAAAAXAAAAGAAAABYAAAAYAAAAGQAAABkAAAAYAAAAGgAAABkAAAAaAAAAGwAAABsAAAAaAAAAHAAAABsAAAAcAAAAHQAAAB4AAAAfAAAAIAAAAB4AAAAgAAAAIQAAACIAAAAjAAAADgAAAA4AAAAjAAAAJAAAAA4AAAAkAAAADwAAAA8AAAAkAAAAJQAAACUAAAAkAAAAJgAAACUAAAAmAAAAJwAAACcAAAAmAAAAGAAAABgAAAAmAAAAKAAAABgAAAAoAAAAGgAAABoAAAAoAAAAKQAAACkAAAAoAAAAIwAAACkAAAAjAAAAIgAAACgAAAAmAAAAJAAAACgAAAAkAAAAIwAAAA==",
    "leftarm": "dmVyc2lvbiAyLjAwCgwAJAwqAAAALAAAAFK43r4pXG8/AAAAPwAAAAAAAAAAAACAPwKACz/4/8E+AAAAAFK43r4oXG+/AAAAPwAAAAAAAAAAAACAP/z/2D74/8E+AAAAAFK43j4oXG+/AAAAPwAAAAAAAAAAAACAP/z/2D4IAP4+AAAAAFK43j4pXG8/AAAAPwAAAAAAAAAAAACAPwKACz8IAP4+AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAP5/DD/4/8E+AAAAAAAAAL8pXG8/UrjePgAAgL8AAAAAAAAAAAKACz8IAL4+AAAAAAAAAL8oXG+/UrjePgAAgL8AAAAAAAAAAPz/2D4IAL4+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAQA1z74/8E+AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgAQA1z4IAP4+AAAAAAAAAD8oXG+/UrjePgAAgD8AAAAAAAAAAPz/2D78/wA/AAAAAAAAAD8pXG8/UrjePgAAgD8AAAAAAAAAAAKACz/8/wA/AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAAP5/DD8IAP4+AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAP5/DD8IAL4+AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAP5/DD/4/4E+AAAAAAAAAL8pXG8/UrjevgAAgL8AAAAAAAAAAAKACz/4/4E+AAAAAAAAAL8oXG+/UrjevgAAgL8AAAAAAAAAAPz/2D74/4E+AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAQA1z74/4E+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAQA1z4IAL4+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAKAXz8EAEM/AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAKAXz/8/yQ/AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgP5/UD/8/yQ/AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgP5/UD8EAEM/AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgAQA1z7O+387AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgAQA1z4iAPg9AAAAAAAAAD8oXG+/UrjevgAAgD8AAAAAAAAAAPz/2D4iAPg9AAAAAAAAAD8oXG+/UrjePgAAgD8AAAAAAAAAAPz/2D7O+387AAAAAAAAAD8pXG8/UrjevgAAgD8AAAAAAAAAAAKACz8iAPg9AAAAAAAAAD8pXG8/UrjePgAAgD8AAAAAAAAAAAKACz/O+387AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAAP5/DD8iAPg9AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAAP5/DD/O+387AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAABkASj4IAEo/AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAABkASj4AAGg/AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAPP/gj4AAGg/AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAPP/gj4IAEo/AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAP5/DD8RAHw+AAAAAFK43r4pXG8/AAAAvwAAAAAAAAAAAACAvwKACz8RAHw+AAAAAFK43r4oXG+/AAAAvwAAAAAAAAAAAACAv/z/2D4RAHw+AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAQA1z4RAHw+AAAAAFK43j4oXG+/AAAAvwAAAAAAAAAAAACAv/z/2D7v/wM+AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgAQA1z7v/wM+AAAAAFK43j4pXG8/AAAAvwAAAAAAAAAAAACAvwKACz/v/wM+AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAAP5/DD/v/wM+AAAAAAAAAAABAAAAAgAAAAAAAAACAAAAAwAAAAAAAAAEAAAABQAAAAAAAAAFAAAABgAAAAAAAAAGAAAAAQAAAAEAAAAGAAAABwAAAAEAAAAHAAAACAAAAAEAAAAIAAAAAgAAAAIAAAAIAAAACQAAAAIAAAAJAAAACgAAAAIAAAAKAAAAAwAAAAMAAAAKAAAACwAAAAMAAAALAAAABAAAAAMAAAAEAAAAAAAAAAwAAAANAAAADgAAAAwAAAAOAAAABQAAAAUAAAAOAAAADwAAAAUAAAAPAAAABgAAAAYAAAAPAAAAEAAAAAYAAAAQAAAAEQAAABIAAAATAAAAFAAAABIAAAAUAAAAFQAAABYAAAAXAAAAGAAAABYAAAAYAAAAGQAAABkAAAAYAAAAGgAAABkAAAAaAAAAGwAAABsAAAAaAAAAHAAAABsAAAAcAAAAHQAAAB4AAAAfAAAAIAAAAB4AAAAgAAAAIQAAACIAAAAjAAAADgAAAA4AAAAjAAAAJAAAAA4AAAAkAAAADwAAAA8AAAAkAAAAJQAAACUAAAAkAAAAJgAAACUAAAAmAAAAJwAAACcAAAAmAAAAGAAAABgAAAAmAAAAKAAAABgAAAAoAAAAGgAAABoAAAAoAAAAKQAAACkAAAAoAAAAIwAAACkAAAAjAAAAIgAAACgAAAAmAAAAJAAAACgAAAAkAAAAIwAAAA==",
    "leftleg": "dmVyc2lvbiAyLjAwCgwAJAwqAAAALAAAAFK43r4pXG8/AAAAPwAAAAAAAAAAAACAPwKAbz/4/8E+AAAAAFK43r4oXG+/AAAAPwAAAAAAAAAAAACAP/5/UD/4/8E+AAAAAFK43j4oXG+/AAAAPwAAAAAAAAAAAACAP/5/UD8IAP4+AAAAAFK43j4pXG8/AAAAPwAAAAAAAAAAAACAPwKAbz8IAP4+AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAP5/cD/4/8E+AAAAAAAAAL8pXG8/UrjePgAAgL8AAAAAAAAAAAKAbz8IAL4+AAAAAAAAAL8oXG+/UrjePgAAgL8AAAAAAAAAAP5/UD8IAL4+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAKATz/4/8E+AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgAKATz8IAP4+AAAAAAAAAD8oXG+/UrjePgAAgD8AAAAAAAAAAP5/UD/8/wA/AAAAAAAAAD8pXG8/UrjePgAAgD8AAAAAAAAAAAKAbz/8/wA/AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAAP5/cD8IAP4+AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAP5/cD8IAL4+AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAP5/cD/4/4E+AAAAAAAAAL8pXG8/UrjevgAAgL8AAAAAAAAAAAKAbz/4/4E+AAAAAAAAAL8oXG+/UrjevgAAgL8AAAAAAAAAAP5/UD/4/4E+AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAKATz/4/4E+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAKATz8IAL4+AAAAAFK43r4AAIC/UrjePgAAAIAAAIC/AAAAgAKAOz8EAEM/AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAKAOz/8/yQ/AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgP5/LD/8/yQ/AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgP5/LD8EAEM/AAAAAFK43j4AAIC/UrjePgAAAIAAAIC/AAAAgAKATz/O+387AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgAKATz8iAPg9AAAAAAAAAD8oXG+/UrjevgAAgD8AAAAAAAAAAP5/UD8iAPg9AAAAAAAAAD8oXG+/UrjePgAAgD8AAAAAAAAAAP5/UD/O+387AAAAAAAAAD8pXG8/UrjevgAAgD8AAAAAAAAAAAKAbz8iAPg9AAAAAAAAAD8pXG8/UrjePgAAgD8AAAAAAAAAAAKAbz/O+387AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAAP5/cD8iAPg9AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAAP5/cD/O+387AAAAAFK43j4AAIA/UrjePgAAAAAAAIA/AAAAAP5/CD/8/yQ/AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAAP5/CD8EAEM/AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAAKAFz8EAEM/AAAAAFK43r4AAIA/UrjePgAAAAAAAIA/AAAAAAKAFz/8/yQ/AAAAAFK43r4AAIA/UrjevgAAAAAAAIA/AAAAAP5/cD8RAHw+AAAAAFK43r4pXG8/AAAAvwAAAAAAAAAAAACAvwKAbz8RAHw+AAAAAFK43r4oXG+/AAAAvwAAAAAAAAAAAACAv/5/UD8RAHw+AAAAAFK43r4AAIC/UrjevgAAAIAAAIC/AAAAgAKATz8RAHw+AAAAAFK43j4oXG+/AAAAvwAAAAAAAAAAAACAv/5/UD/v/wM+AAAAAFK43j4AAIC/UrjevgAAAIAAAIC/AAAAgAKATz/v/wM+AAAAAFK43j4pXG8/AAAAvwAAAAAAAAAAAACAvwKAbz/v/wM+AAAAAFK43j4AAIA/UrjevgAAAAAAAIA/AAAAAP5/cD/v/wM+AAAAAAAAAAABAAAAAgAAAAAAAAACAAAAAwAAAAAAAAAEAAAABQAAAAAAAAAFAAAABgAAAAAAAAAGAAAAAQAAAAEAAAAGAAAABwAAAAEAAAAHAAAACAAAAAEAAAAIAAAAAgAAAAIAAAAIAAAACQAAAAIAAAAJAAAACgAAAAIAAAAKAAAAAwAAAAMAAAAKAAAACwAAAAMAAAALAAAABAAAAAMAAAAEAAAAAAAAAAwAAAANAAAADgAAAAwAAAAOAAAABQAAAAUAAAAOAAAADwAAAAUAAAAPAAAABgAAAAYAAAAPAAAAEAAAAAYAAAAQAAAAEQAAABIAAAATAAAAFAAAABIAAAAUAAAAFQAAABYAAAAXAAAAGAAAABYAAAAYAAAAGQAAABkAAAAYAAAAGgAAABkAAAAaAAAAGwAAABsAAAAaAAAAHAAAABsAAAAcAAAAHQAAAB4AAAAfAAAAIAAAAB4AAAAgAAAAIQAAACIAAAAjAAAADgAAAA4AAAAjAAAAJAAAAA4AAAAkAAAADwAAAA8AAAAkAAAAJQAAACUAAAAkAAAAJgAAACUAAAAmAAAAJwAAACcAAAAmAAAAGAAAABgAAAAmAAAAKAAAABgAAAAoAAAAGgAAABoAAAAoAAAAKQAAACkAAAAoAAAAIwAAACkAAAAjAAAAIgAAACgAAAAmAAAAJAAAACgAAAAkAAAAIwAAAA==",
    "rightarm": "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",
    "rightleg": "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",
    "head": "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AAcAAAAFAAAAFgAAABYAAAAXAAAABwAAAAkAAAAHAAAAFwAAABcAAAAYAAAACQAAAAsAAAAJAAAAGAAAABgAAAAZAAAACwAAAA0AAAALAAAAGQAAABkAAAAaAAAADQAAAA8AAAANAAAAGgAAABoAAAAbAAAADwAAABEAAAAPAAAAGwAAABsAAAAcAAAAEQAAABMAAAARAAAAHAAAABwAAAAdAAAAEwAAABUAAAAUAAAAHgAAAB4AAAAfAAAAFQAAABYAAAAVAAAAHwAAAB8AAAAgAAAAFgAAABcAAAAWAAAAIAAAACAAAAAhAAAAFwAAABgAAAAXAAAAIQAAACEAAAAiAAAAGAAAABkAAAAYAAAAIgAAACIAAAAjAAAAGQAAABoAAAAZAAAAIwAAACMAAAAkAAAAGgAAABsAAAAaAAAAJAAAACQAAAAlAAAAGwAAABwAAAAbAAAAJQAAACUAAAAmAAAAHAAAAB0AAAAcAAAAJgAAACYAAAAnAAAAHQAAAB8AAAAeAAAAKAAAACgAAAApAAAAHwAAACAAAAAfAAAAKQAAACkAAAAqAAAAIAAAACEAAAAgAAAAKgAAACoAAAArAAAAIQAAACIAAAAhAAAAKwAAACsAAAAsAAAAIgAAACMAAAAiAAAALAAAACwAAAAtAAAAIwAAACQAAAAjAAAALQAAAC0AAAAuAAAAJAAAACUAAAAkAAAALgAAAC4AAAAvAAAAJQAAACYAAAAlAAAALwAAAC8AAAAwAAAAJgAAACcAAAAmAAAAMAAAADAAAAAxAAAAJwAAACkAAAAoAAAAMgAAADIAAAAzAAAAKQAAACoAAAApAAAAMwAAADMAAAA0AAAAKgAAACsAAAAqAAAANAAAADQAAAA1AAAAKwAAACwAAAArAAAANQAAADUAAAA2AAAALAAAAC0AAAAsAAAANgAAADYAAAA3AAAALQAAAC4AAAAtAAAANwAAADcAAAA4AAAALgAAAC8AAAAuAAAAOAAAADgAAAA5AAAALwAAADAAAAAvAAAAOQAAADkAAAA6AAAAMAAAADEAAAAwAAAAOgAAADoAAAA7AAAAMQAAADMAAAAyAAAAPAAAADwAAAA9AAAAMwAAADQAAAAzAAAAPQAAAD0AAAA+AAAANAAAADUAAAA0AAAAPgAAAD4AAAA/AAAANQAAADYAAAA1AAAAPwAAAD8AAABAAAAANgAAADcAAAA2AAAAQAAAAEAAAABBAAAANwAAADgAAAA3AAAAQQAAAEEAAABCAAAAOAAAADkAAAA4AAAAQgAAAEIAAABDAAAAOQAAADoAAAA5AAAAQwAAAEMAAABEAAAAOgAAADsAAAA6AAAARAAAAEQAAABFAAAAOwAAAD0AAAA8AAAARgAAAEYAAABHAAAAPQAAAD4AAAA9AAAARwAAAEcAAABIAAAAPgAAAD8AAAA+AAAASAAAAEgAAABJAAAAPwAAAEAAAAA/AAAASQAAAEkAAABKAAAAQAAAAEEAAABAAAAASgAAAEoAAABLAAAAQQAAAEIAAABBAAAASwAAAEsAAABMAAAAQgAAAEMAAABCAAAATAAAAEwAAABNAAAAQwAAAEQAAABDAAAATQAAAE0AAABOAAAARAAAAEUAAABEAAAATgAAAE4AAABPAAAARQAAAEcAAABGAAAAUAAAAFAAAABRAAAARwAAAEgAAABHAAAAUQAAAFEAAABSAAAASAAAAEkAAABIAAAAUgAAAFIAAABTAAAASQAAAEoAAABJAAAAUwAAAFMAAABUAAAASgAAAEsAAABKAAAAVAAAAFQAAABVAAAASwAAAEwAAABLAAAAVQAAAFUAAABWAAAATAAAAE0AAABMAAAAVgAAAFYAAABXAAAATQAAAE4AAABNAAAAVwAAAFcAAABYAAAATgAAAE8AAABOAAAAWAAAAFgAAABZAAAATwAAAFEAAABQAAAAWgAAAFoAAABbAAAAUQAAAFIAAABRAAAAWwAAAFsAAABcAAAAUgAAAFMAAABSAAAAXAAAAFwAAABdAAAAUwAAAFQAAABTAAAAXQAAAF0AAABeAAAAVAAAAFUAAABUAAAAXgAAAF4AAABfAAAAVQAAAFYAAABVAAAAXwAAAF8AAABgAAAAVgAAAFcAAABWAAAAYAAAAGAAAABhAAAAVwAAAFgAAABXAAAAYQAAAGEAAABiAAAAWAAAAFkAAABYAAAAYgAAAGIAAABjAAAAWQAAAGQAAABlAAAAZgAAAGQAAABmAAAAZwAAAGQAAABnAAAAaAAAAGQAAABoAAAAaQAAAGQAAABpAAAAagAAAGQAAABqAAAAawAAAGQAAABrAAAAbAAAAGQAAABsAAAAbQAAAGQAAABtAAAAbgAAAGQAAABuAAAAbwAAAGQAAABvAAAAcAAAAGQAAABwAAAAcQAAAGQAAABxAAAAcgAAAGQAAAByAAAAcwAAAGQAAABzAAAAdAAAAGQAAAB0AAAAdQAAAGQAAAB1AAAAdgAAAGQAAAB2AAAAdwAAAGQAAAB3AAAAeAAAAGQAAAB4AAAAeQAAAGQAAAB5AAAAegAAAGQAAAB6AAAAewAAAGQAAAB7AAAAfAAAAGQAAAB8AAAAfQAAAGQAAAB9AAAAfgAAAGQAAAB+AAAAfwAAAGQAAAB/AAAAgAAAAGQAAACAAAAAgQAAAGQAAACBAAAAggAAAGQAAACCAAAAgwAAAGQAAACDAAAAhAAAAGQAAACEAAAAhQAAAGQAAACFAAAAhgAAAGQAAACGAAAAhwAAAGQAAACHAAAAiAAAAGQAAACIAAAAZQAAAIkAAACKAAAAiwAAAIsAAACMAAAAiQAAAI0AAACJAAAAjAAAAIwAAACOAAAAjQAAAI8AAACNAAAAjgAAAI4AAACQAAAAjwAAAJEAAACPAAAAkAAAAJAAAACSAAAAkQAAAJMAAACRAAAAkgAAAJIAAACUAAAAkwAAAJUAAACTAAAAlAAAAJQAAACWAAAAlQAAAJcAAACVAAAAlgAAAJYAAACYAAAAlwAAAJkAAACXAAAAmAAAAJgAAACaAAAAmQAAAJsAAACZAAAAmgAAAJoAAACcAAAAmwAAAIwAAACLAAAAnQAAAJ0AAACeAAAAjAAAAI4AAACMAAAAngAAAJ4AAACfAAAAjgAAAJAAAACOAAAAnwAAAJ8AAACgAAAAkAAAAJIAAACQAAAAoAAAAKAAAAChAAAAkgAAAJQAAACSAAAAoQAAAKEAAACiAAAAlAAAAJYAAACUAAAAogAAAKIAAACjAAAAlgAAAJgAAACWAAAAowAAAKMAAACkAAAAmAAAAJoAAACYAAAApAAAAKQAAAClAAAAmgAAAJwAAACaAAAApQAAAKUAAACmAAAAnAAAAJ4AAACdAAAApwAAAKcAAACoAAAAngAAAJ8AAACeAAAAqAAAAKgAAACpAAAAnwAAAKAAAACfAAAAqQAAAKkAAACqAAAAoAAAAKEAAACgAAAAqgAAAKoAAACrAAAAoQAAAKIAAAChAAAAqwAAAKsAAACsAAAAogAAAKMAAACiAAAArAAAAKwAAACtAAAAowAAAKQAAACjAAAArQAAAK0AAACuAAAApAAAAKUAAACkAAAArgAAAK4AAACvAAAApQAAAKYAAAClAAAArwAAAK8AAACwAAAApgAAAKgAAACnAAAAsQAAALEAAACyAAAAqAAAAKkAAACoAAAAsgAAALIAAACzAAAAqQAAAKoAAACpAAAAswAAALMAAAC0AAAAqgAAAKsAAACqAAAAtAAAALQAAAC1AAAAqwAAAKwAAACrAAAAtQAAALUAAAC2AAAArAAAAK0AAACsAAAAtgAAALYAAAC3AAAArQAAAK4AAACtAAAAtwAAALcAAAC4AAAArgAAAK8AAACuAAAAuAAAALgAAAC5AAAArwAAALAAAACvAAAAuQAAALkAAAC6AAAAsAAAALIAAACxAAAAuwAAALsAAAC8AAAAsgAAALMAAACyAAAAvAAAALwAAAC9AAAAswAAALQAAACzAAAAvQAAAL0AAAC+AAAAtAAAALUAAAC0AAAAvgAAAL4AAAC/AAAAtQAAALYAAAC1AAAAvwAAAL8AAADAAAAAtgAAALcAAAC2AAAAwAAAAMAAAADBAAAAtwAAALgAAAC3AAAAwQAAAMEAAADCAAAAuAAAALkAAAC4AAAAwgAAAMIAAADDAAAAuQAAALoAAAC5AAAAwwAAAMMAAADEAAAAugAAALwAAAC7AAAAxQAAAMUAAADGAAAAvAAAAL0AAAC8AAAAxgAAAMYAAADHAAAAvQAAAL4AAAC9AAAAxwAAAMcAAADIAAAAvgAAAL8AAAC+AAAAyAAAAMgAAADJAAAAvwAAAMAAAAC/AAAAyQAAAMkAAADKAAAAwAAAAMEAAADAAAAAygAAAMoAAADLAAAAwQAAAMIAAADBAAAAywAAAMsAAADMAAAAwgAAAMMAAADCAAAAzAAAAMwAAADNAAAAwwAAAMQAAADDAAAAzQAAAM0AAADOAAAAxAAAAMYAAADFAAAAzwAAAM8AAADQAAAAxgAAAMcAAADGAAAA0AAAANAAAADRAAAAxwAAAMgAAADHAAAA0QAAANEAAADSAAAAyAAAAMkAAADIAAAA0gAAANIAAADTAAAAyQAAAMoAAADJAAAA0wAAANMAAADUAAAAygAAAMsAAADKAAAA1AAAANQAAADVAAAAywAAAMwAAADLAAAA1QAAANUAAADWAAAAzAAAAM0AAADMAAAA1gAAANYAAADXAAAAzQAA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ACIBAAAhAQAAKwEAACsBAAAsAQAAIgEAACMBAAAiAQAALAEAACwBAAAtAQAAIwEAACQBAAAjAQAALQEAAC0BAAAuAQAAJAEAACUBAAAkAQAALgEAAC4BAAAvAQAAJQEAACYBAAAlAQAALwEAAC8BAAAwAQAAJgEAACcBAAAmAQAAMAEAADABAAAxAQAAJwEAACgBAAAnAQAAMQEAADEBAAAyAQAAKAEAACoBAAApAQAAMwEAADMBAAA0AQAAKgEAACsBAAAqAQAANAEAADQBAAA1AQAAKwEAACwBAAArAQAANQEAADUBAAA2AQAALAEAAC0BAAAsAQAANgEAADYBAAA3AQAALQEAAC4BAAAtAQAANwEAADcBAAA4AQAALgEAAC8BAAAuAQAAOAEAADgBAAA5AQAALwEAADABAAAvAQAAOQEAADkBAAA6AQAAMAEAADEBAAAwAQAAOgEAADoBAAA7AQAAMQEAADIBAAAxAQAAOwEAADsBAAA8AQAAMgEAADQBAAAzAQAAigAAAIoAAACJAAAANAEAADUBAAA0AQAAiQAAAIkAAACNAAAANQEAADYBAAA1AQAAjQAAAI0AAACPAAAANgEAADcBAAA2AQAAjwAAAI8AAACRAAAANwEAADgBAAA3AQAAkQAAAJEAAACTAAAAOAEAADkBAAA4AQAAkwAAAJMAAACVAAAAOQEAADoBAAA5AQAAlQAAAJUAAACXAAAAOgEAADsBAAA6AQAAlwAAAJcAAACZAAAAOwEAADwBAAA7AQAAmQAAAJkAAACbAAAAPAEAAD0BAAA+AQAAPwEAAD8BAABAAQAAPQEAAEEBAAA9AQAAQAEAAEABAABCAQAAQQEAAEMBAABBAQAAQgEAAEIBAABEAQAAQwEAAEUBAABDAQAARAEAAEQBAABGAQAARQEAAEcBAABFAQAARgEAAEYBAABIAQAARwEAAEkBAABHAQAASAEAAEgBAABKAQAASQEAAEsBAABJAQAASgEAAEoBAABMAQAASwEAAE0BAABLAQAATAEAAEwBAABOAQAATQEAAE8BAABNAQAATgEAAE4BAABQAQAATwEAAEABAAA/AQAAUQEAAFEBAABSAQAAQAEAAEIBAABAAQAAUgEAAFIBAABTAQAAQgEAAEQBAABCAQAAUwEAAFMBAABUAQAARAEAAEYBAABEAQAAVAEAAFQBAABVAQAARgEAAEgBAABGAQAAVQEAAFUBAABWAQAASAEAAEoBAABIAQAAVgEAAFYBAABXAQAASgEAAEwBAABKAQAAVwEAAFcBAABYAQAATAEAAE4BAABMAQAAWAEAAFgBAABZAQAATgEAAFABAABOAQAAWQEAAFkBAABaAQAAUAEAAFIBAABRAQAAWwEAAFsBAABcAQAAUgEAAFMBAABSAQAAXAEAAFwBAABdAQAAUwEAAFQBAABTAQAAXQEAAF0BAABeAQAAVAEAAFUBAABUAQAAXgEAAF4BAABfAQAAVQEAAFYBAABVAQAAXwEAAF8BAABgAQAAVgEAAFcBAABWAQAAYAEAAGABAABhAQAAVwEAAFgBAABXAQAAYQEAAGEBAABiAQAAWAEAAFkBAABYAQAAYgEAAGIBAABjAQAAWQEAAFoBAABZAQAAYwEAAGMBAABkAQAAWgEAAFwBAABbAQAAZQEAAGUBAABmAQAAXAEAAF0BAABcAQAAZgEAAGYBAABnAQAAXQEAAF4BAABdAQAAZwEAAGcBAABoAQAAXgEAAF8BAABeAQAAaAEAAGgBAABpAQAAXwEAAGABAABfAQAAaQEAAGkBAABqAQAAYAEAAGEBAABgAQAAagEAAGoBAABrAQAAYQEAAGIBAABhAQAAawEAAGsBAABsAQAAYgEAAGMBAABiAQAAbAEAAGwBAABtAQAAYwEAAGQBAABjAQAAbQEAAG0BAABuAQAAZAEAAGYBAABlAQAAbwEAAG8BAABwAQAAZgEAAGcBAABmAQAAcAEAAHABAABxAQAAZwEAAGgBAABnAQAAcQEAAHEBAAByAQAAaAEAAGkBAABoAQAAcgEAAHIBAABzAQAAaQEAAGoBAABpAQAAcwEAAHMBAAB0AQAAagEAAGsBAABqAQAAdAEAAHQBAAB1AQAAawEAAGwBAABrAQAAdQEAAHUBAAB2AQAAbAEAAG0BAABsAQAAdgEAAHYBAAB3AQAAbQEAAG4BAABtAQAAdwEAAHcBAAB4AQAAbgEAAHABAABvAQAAeQEAAHkBAAB6AQAAcAEAAHEBAABwAQAAegEAAHoBAAB7AQAAcQEAAHIBAABxAQAAewEAAHsBAAB8AQAAcgEAAHMBAAByAQAAfAEAAHwBAAB9AQAAcwEAAHQBAABzAQAAfQEAAH0BAAB+AQAAdAEAAHUBAAB0AQAAfgEAAH4BAAB/AQAAdQEAAHYBAAB1AQAAfwEAAH8BAACAAQAAdgEAAHcBAAB2AQAAgAEAAIABAACBAQAAdwEAAHgBAAB3AQAAgQEAAIEBAACCAQAAeAEAAHoBAAB5AQAAgwEAAIMBAACEAQAAegEAAHsBAAB6AQAAhAEAAIQBAACFAQAAewEAAHwBAAB7AQAAhQEAAIUBAACGAQAAfAEAAH0BAAB8AQAAhgEAAIYBAACHAQAAfQEAAH4BAAB9AQAAhwEAAIcBAACIAQAAfgEAAH8BAAB+AQAAiAEAAIgBAACJAQAAfwEAAIABAAB/AQAAiQEAAIkBAACKAQAAgAEAAIEBAACAAQAAigEAAIoBAACLAQAAgQEAAIIBAACBAQAAiwEAAIsBAACMAQAAggEAAIQBAACDAQAAjQEAAI0BAACOAQAAhAEAAIUBAACEAQAAjgEAAI4BAACPAQAAhQEAAIYBAACFAQAAjwEAAI8BAACQAQAAhgEAAIcBAACGAQAAkAEAAJABAACRAQAAhwEAAIgBAACHAQAAkQEAAJEBAACSAQAAiAEAAIkBAACIAQAAkgEAAJIBAACTAQAAiQEAAIoBAACJAQAAkwEAAJMBAACUAQAAigEAAIsBAACKAQAAlAEAAJQBAACVAQAAiwEAAIwBAACLAQAAlQEAAJUBAACWAQAAjAEAAI4BAACNAQAAlwEAAJcBAACYAQAAjgEAAI8BAACOAQAAmAEAAJgBAACZAQAAjwEAAJABAACPAQAAmQEAAJkBAACaAQAAkAEAAJEBAACQAQAAmgEAAJoBAACbAQAAkQEAAJIBAACRAQAAmwEAAJsBAACcAQAAkgEAAJMBAACSAQAAnAEAAJwBAACdAQAAkwEAAJQBAACTAQAAnQEAAJ0BAACeAQAAlAEAAJUBAACUAQAAngEAAJ4BAACfAQAAlQEAAJYBAACVAQAAnwEAAJ8BAACgAQAAlgEAAKEBAACiAQAAowEAAKMBAACkAQAAoQEAAKUBAAChAQAApAEAAKQBAACmAQAApQEAAKcBAAClAQAApgEAAKYBAACoAQAApwEAAKkBAACnAQAAqAEAAKgBAACqAQAAqQEAAKsBAACpAQAAqgEAAKoBAACsAQAAqwEAAK0BAACrAQAArAEAAKwBAACuAQAArQEAAK8BAACtAQAArgEAAK4BAACwAQAArwEAALEBAACvAQAAsAEAALABAACyAQAAsQEAALMBAACxAQAAsgEAALIBAAC0AQAAswEAAKQBAACjAQAAtQEAALUBAAC2AQAApAEAAKYBAACkAQAAtgEAALYBAAC3AQAApgEAAKgBAACmAQAAtwEAALcBAAC4AQAAqAEAAKoBAACoAQAAuAEAALgBAAC5AQAAqgEAAKwBAACqAQAAuQEAALkBAAC6AQAArAEAAK4BAACsAQAAugEAALoBAAC7AQAArgEAALABAACuAQAAuwEAALsBAAC8AQAAsAEAALIBAACwAQAAvAEAALwBAAC9AQAAsgEAALQBAACyAQAAvQEAAL0BAAC+AQAAtAEAALYBAAC1AQAAvwEAAL8BAADAAQAAtgEAALcBAAC2AQAAwAEAAMABAADBAQAAtwEAALgBAAC3AQAAwQEAAMEBAADCAQAAuAEAALkBAAC4AQAAwgEAAMIBAADDAQAAuQEAALoBAAC5AQAAwwEAAMMBAADEAQAAugEAALsBAAC6AQAAxAEAAMQBAADFAQAAuwEAALwBAAC7AQAAxQEAAMUBAADGAQAAvAEAAL0BAAC8AQAAxgEAAMYBAADHAQAAvQEAAL4BAAC9AQAAxwEAAMcBAADIAQAAvgEAAMABAAC/AQAAyQEAAMkBAADKAQAAwAEAAMEBAADAAQAAygEAAMoBAADLAQAAwQEAAMIBAADBAQAAywEAAMsBAADMAQAAwgEAAMMBAADCAQAAzAEAAMwBAADNAQAAwwEAAMQBAADDAQAAzQEAAM0BAADOAQAAxAEAAMUBAADEAQAAzgEAAM4BAADPAQAAxQEAAMYBAADFAQAAzwEAAM8BAADQAQAAxgEAAMcBAADGAQAA0AEAANABAADRAQAAxwEAAMgBAADHAQAA0QEAANEBAADSAQAAyAEAAMoBAADJAQAA0wEAANMBAADUAQAAygEAAMsBAADKAQAA1AEAANQBAADVAQAAywEAAMwBAADLAQAA1QEAANUBAADWAQAAzAEAAM0BAADMAQAA1gEAANYBAADXAQAAzQEAAM4BAADNAQAA1wEAANcBAADYAQAAzgEAAM8BAADOAQAA2AEAANgBAADZAQAAzwEAANABAADPAQAA2QEAANkBAADaAQAA0AEAANEBAADQAQAA2gEAANoBAADbAQAA0QEAANIBAADRAQAA2wEAANsBAADcAQAA0gEAANQBAADTAQAA3QEAAN0BAADeAQAA1AEAANUBAADUAQAA3gEAAN4BAADfAQAA1QEAANYBAADVAQAA3wEAAN8BAADgAQAA1gEAANcBAADWAQAA4AEAAOABAADhAQAA1wEAANgBAADXAQAA4QEAAOEBAADiAQAA2AEAANkBAADYAQAA4gEAAOIBAADjAQAA2QEAANoBAADZAQAA4wEAAOMBAADkAQAA2gEAANsBAADaAQAA5AEAAOQBAADlAQAA2wEAANwBAADbAQAA5QEAAOUBAADmAQAA3AEAAN4BAADdAQAA5wEAAOcBAADoAQAA3gEAAN8BAADeAQAA6AEAAOgBAADpAQAA3wEAAOABAADfAQAA6QEAAOkBAADqAQAA4AEAAOEBAADgAQAA6gEAAOoBAADrAQAA4QEAAOIBAADhAQAA6wEAAOsBAADsAQAA4gEAAOMBAADiAQAA7AEAAOwBAADtAQAA4wEAAOQBAADjAQAA7QEAAO0BAADuAQAA5AEAAOUBAADkAQAA7gEAAO4BAADvAQAA5QEAAOYBAADlAQAA7wEAAO8BAADwAQAA5gEAAOgBAADnAQAA8QEAAPEBAADyAQAA6AEAAOkBAADoAQAA8gEAAPIBAADzAQAA6QEAAOoBAADpAQAA8wEAAPMBAAD0AQAA6gEAAOsBAADqAQAA9AEAAPQBAAD1AQAA6wEAAOwBAADrAQAA9QEAAPUBAAD2AQAA7AEAAO0BAADsAQAA9gEAAPYBAAD3AQAA7QEAAO4BAADtAQAA9wEAAPcBAAD4AQAA7gEAAO8BAADuAQAA+AEAAPgBAAD5AQAA7wEAAPABAADvAQAA+QEAAPkBAAD6AQAA8AEAAPIBAADxAQAA+wEAAPsBAAD8AQAA8gEAAPMBAADyAQAA/AEAAPwBAAD9AQAA8wEAAPQBAADzAQAA/QEAAP0BAAD+AQAA9AEAAPUBAAD0AQAA/gEAAP4BAAD/AQAA9QEAAPYBAAD1AQAA/wEAAP8BAAAAAgAA9gEAAPcBAAD2AQAAAAIAAAACAAABAgAA9wEAAPgBAAD3AQAAAQIAAAECAAACAgAA+AEAAPkBAAD4AQAAAgIAAAICAAADAgAA+QEAAPoBAAD5AQAAAwIAAAMCAAAEAgAA+gEAAA==",
    "compTorso": "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QQAAAAAAAAAAAACAP9/5HT9N268+AAAAAOP96kPi/TxD0A18QQAAAAAAAAAAAACAP9/5HT9N268+AAAAANj9p0Pg/TxD0A18QQAAAAAAAAAAAACAP6osyj5N268+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP6osyj4uBLk+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP84ZHT8uBLk+AAAAANf950Pi/TxD0A18QQAAAAAAAAAAAACAP84ZHT9N268+AAAAANj9p0OA++9C0A18QQAAAAAAAAAAAACAPy/fcj8uBLk+AAAAANj9p0NQ++NC0A18QQAAAAAAAAAAAACAPy/fcj9N268+AAAAANj950NM++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP7bbOj8uBLk+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP9/5HT8uBLk+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP6T7OT8uBLk+AAAAAOP96kOE++9C0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOP96kPC/TdD0A18QQAAAAAAAAAAAACAP9/5HT9N268+AAAAAMh9pUPA/TdD0A18QQAAAAAAAAAAAACAP4dsyD5N268+AAAAAMh9pUOA++9C0A18QQAAAAAAAAAAAACAP/1okD5N268+AAAAANj9p0OA++9C0A18QQAAAAAAAAAAAACAP/1okD4uBLk+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP4dsyD4uBLk+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP6T7OT8uBLk+AAAAAOT96kNM++NC0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOP96kOE++9C0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOT96kNQ++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP7bbOj8uBLk+AAAAANj950NQ++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAAMh9pUPg/TxD0A18QQAAAAAAAAAAAACAP6osyj4uBLk+AAAAANj9p0PA/TdD0A18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QQAAAAAAAAAAAACAPxoYAT89KNg+AAAAANFArDxVBKhD0A18QQAAAAAAAAAAAACAPxoYAT89KNg+AAAAAKr+h0M9/91C0A18QQAAAAAAAAAAAACAP6osyj4G2bo+AAAAAKr+h0OSFOBA0A18QQAAAAAAAAAAAACAP6osyj6Wd/U+AAAAAGj+0UPTFOBA0A18QQAAAAAAAAAAAACAP84ZHT+Wd/U+AAAAAGj+0UNB/91C0A18QQAAAAAAAAAAAACAP84ZHT8G2bo+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP9/5HT8uBLk+AAAAAOP96kPC/TdD0A18QQAAAAAAAAAAAACAP9/5HT9N268+AAAAAOP96kPi/TxD0A18QQAAAAAAAAAAAACAP9/5HT9N268+AAAAANj9p0Pg/TxD0A18QQAAAAAAAAAAAACAP6osyj5N268+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP6osyj4uBLk+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP84ZHT8uBLk+AAAAANf950Pi/TxD0A18QQAAAAAAAAAAAACAP84ZHT9N268+AAAAANj9p0OA++9C0A18QQAAAAAAAAAAAACAPy/fcj8uBLk+AAAAANj9p0NQ++NC0A18QQAAAAAAAAAAAACAPy/fcj9N268+AAAAANj950NM++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP7bbOj8uBLk+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP9/5HT8uBLk+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP6T7OT8uBLk+AAAAAOP96kOE++9C0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOP96kPC/TdD0A18QQAAAAAAAAAAAACAP9/5HT9N268+AAAAAMh9pUPA/TdD0A18QQAAAAAAAAAAAACAP4dsyD5N268+AAAAAMh9pUOA++9C0A18QQAAAAAAAAAAAACAP/1okD5N268+AAAAANj9p0OA++9C0A18QQAAAAAAAAAAAACAP/1okD4uBLk+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP4dsyD4uBLk+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP6T7OT8uBLk+AAAAAOT96kNM++NC0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOP96kOE++9C0A18QQAAAAAAAAAAAACAP6T7OT9N268+AAAAAOT96kNQ++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAANf950OE++9C0A18QQAAAAAAAAAAAACAP7bbOj8uBLk+AAAAANj950NQ++NC0A18QQAAAAAAAAAAAACAP7bbOj9N268+AAAAAMh9pUPg/TxD0A18QQAAAAAAAAAAAACAP6osyj4uBLk+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP6osyj4uBLk+AAAAANj9p0Pg/TxD0A18QQAAAAAAAAAAAACAP6osyj5N268+AAAAANj9p0PA/TdD0A18QQAAAAAAAAAAAACAP4dsyD4uBLk+AAAAAMl9pUPg/TxD0A18QQAAAAAAAAAAAACAP4dsyD5N268+AAAAAMh9pUPA/TdD0A18QQAAAAAAAAAAAACAP4dsyD5N268+AAAAAMh9pUNQ++NC0A18QQAAAAAAAAAAAACAPy/fcj8uBLk+AAAAANj9p0NQ++NC0A18QQAAAAAAAAAAAACAPy/fcj8uBLk+AAAAANj9p0OE++9C0A18QQAAAAAAAAAAAACAPy/fcj9N268+AAAAANj9p0OA++9C0A18QQAAAAAAAAAAAACAP/1okD4uBLk+AAAAAMh9pUOA++9C0A18QQAAAAAAAAAAAACAP/1okD5N268+AAAAAMh9pUNQ++NC0A18QQAAAAAAAAAAAACAP/1okD5N268+AAAAAOP96kPi/TxD0A18QQAAAAAAAAAAAACAP84ZHT9N268+AAAAANf950Pi/TxD0A18QQAAAAAAAAAAAACAP84ZHT9N268+AAAAANf950PC/TdD0A18QQAAAAAAAAAAAACAP84ZHT8uBLk+AAAAAEX7R0PC/TdD0A18QQAAAAAAAAAAAACAP6osyj6veGo8AAAAAEf7R0OE++9C0A18QQAAAAAAAAAAAACAP6osyj5p5AM+AAAAAKP9o0OI++9C0A18QQAAAAAAAAAAAACAP84ZHT9p5AM+AAAAAKL9o0PE/TdD0A18QQAAAAAAAAAAAACAP84ZHT+veGo8AAAAAK/9pkPg/T1D0A18QQAAAAAAAAAAAACAP7bbOj/b3xk+AAAAAKL9o0Pg/T1D0A18QQAAAAAAAAAAAACAP7bbOj/b3xk+AAAAAKL9o0PE/TdD0A18QQAAAAAAAAAAAACAP7bbOj9cjgc+AAAAAEX7R0Pe/T1D0A18QQAAAAAAAAAAAACAPy/fcj/b3xk+AAAAAEX7R0PC/TdD0A18QQAAAAAAAAAAAACAPy/fcj9cjgc+AAAAAKL9o0PE/TdD0A18QQAAAAAAAAAAAACAP7bbOj9cjgc+AAAAAKL9o0Pg/T1D0A18QQAAAAAAAAAAAACAP7bbOj/b3xk+AAAAAEf7R0OE++9C0A18QQAAAAAAAAAAAACAP6osyj5cjgc+AAAAAEf7R0NM++NC0A18QQAAAAAAAAAAAACAP6osyj7b3xk+AAAAAKP9o0NQ++NC0A18QQAAAAAAAAAAAACAP84ZHT/b3xk+AAAAAKP9o0OI++9C0A18QQAAAAAAAAAAAACAP84ZHT9cjgc+AAAAAKL9o0PE/TdD0A18QQAAAAAAAAAAAACAP6T7OT9cjgc+AAAAAKP9o0OI++9C0A18QQAAAAAAAAAAAACAP9/5HT9cjgc+AAAAALD9pkOI++9C0A18QQAAAAAAAAAAAACAP9/5HT/b3xk+AAAAAK/9pkPE/TdD0A18QQAAAAAAAAAAAACAP6T7OT/b3xk+AAAAACz7QUPA/TdD0A18QQAAAAAAAAAAAACAP/1okD7b3xk+AAAAAC37QUOA++9C0A18QQAAAAAAAAAAAACAP4dsyD7b3xk+AAAAAEf7R0OE++9C0A18QQAAAAAAAAAAAACAP4dsyD5cjgc+AAAAAEX7R0PC/TdD0A18QQAAAAAAAAAAAACAP/1okD5cjgc+AAAAAKP9o0OI++9C0A18QQAAAAAAAAAAAACAP9/5HT9cjgc+AAAAALD9pkNQ++NC0A18QQAAAAAAAAAAAACAP9/5HT/b3xk+AAAAALD9pkOI++9C0A18QQAAAAAAAAAAAACAP9/5HT/b3xk+AAAAALD9pkNU++NC0A18QQAAAAAAAAAAAACAP84ZHT/b3xk+AAAAAKP9o0OI++9C0A18QQAAAAAAAAAAAACAP84ZHT9cjgc+AAAAAKP9o0NU++NC0A18QQAAAAAAAAAAAACAP84ZHT/b3xk+AAAAAEX7R0PC/TdD0A18QQAAAAAAAAAAAACAP/1okD5cjgc+AAAAACz7QUPc/T1D0A18QQAAAAAAAAAAAACAP/1okD7b3xk+AAAAACz7QUPA/TdD0A18QQAAAAAAAAAAAACAP/1okD7b3xk+AAAAACz7QUPc/T1D0A18QQAAAAAAAAAAAACAPy/fcj/b3xk+AAAAAEX7R0PC/TdD0A18QQAAAAAAAAAAAACAPy/fcj9cjgc+AAAAAEX7R0Pe/T1D0A18QQAAAAAAAAAAAACAPy/fcj/b3xk+AAAAAEf7R0OE++9C0A18QQAAAAAAAAAAAACAP4dsyD5cjgc+AAAAAC37QUOA++9C0A18QQAAAAAAAAAAAACAP4dsyD7b3xk+AAAAAC37QUNI++NC0A18QQAAAAAAAAAAAACAP4dsyD7b3xk+AAAAAC37QUNI++NC0A18QQAAAAAAAAAAAACAP6osyj7b3xk+AAAAAEf7R0NQ++NC0A18QQAAAAAAAAAAAACAP6osyj7b3xk+AAAAAEf7R0OE++9C0A18QQAAAAAAAAAAAACAP6osyj5cjgc+AAAAAKL9o0PE/TdD0A18QQAAAAAAAAAAAACAP6T7OT9cjgc+AAAAAK/9pkPE/TdD0A18QQAAAAAAAAAAAACAP6T7OT/b3xk+AAAAAK/9pkPg/T1D0A18QQAAAAAAAAAAAACAP6T7OT/b3xk+AAAAAAAAAAABAAAAAgAAAAMAAAAAAAAAAgAAAAQAAAAFAAAABgAAAAcAAAAEAAAABgAAAAgAAAAJAAAACgAAAAsAAAAIAAAACgAAAAwAAAANAAAADgAAAA8AAAAMAAAADgAAABAAAAARAAAAEgAAABMAAAAQAAAAEgAAABQAAAAVAAAAFgAAABcAAAAUAAAAFgAAABgAAAAZAAAAGgAAABsAAAAcAAAAHQAAAB4AAAAfAAAAIAAAACEAAAAiAAAAIwAAACIAAAAkAAAAJQAAACQAAAAmAAAAJQAAACYAAAAnAAAAKAAAACcAAAApAAAAKAAAACkAAAAqAAAAKwAAACoAAAAbAAAAKwAAACMAAAAiAAAALAAAACUAAAAmAAAALQAAACIAAAAlAAAALgAAACgAAAApAAAALwAAACYAAAAoAAAAMAAAACsAAAAbAAAAMQAAACkAAAArAAAAMgAAADMAAAAYAAAANAAAADUAAAAzAAAANgAAADcAAAA1AAAAOAAAADkAAAA3AAAAOgAAADsAAAA5AAAAPAAAAD0AAAA7AAAAPgAAAD8AAAA9AAAAQAAAAEEAAAAeAAAAQgAAAEMAAABBAAAARAAAAEUAAABDAAAARgAAAEcAAABFAAAASAAAAEkAAABHAAAASgAAAEsAAABJAAAATAAAAE0AAABLAAAATgAAABsAAABPAAAAHAAAABgAAABQAAAAGQAAADUAAABRAAAAMwAAAFEAAAAYAAAAMwAAADkAAABSAAAANwAAAFIAAAA1AAAANwAAAD0AAABTAAAAOwAAAFMAAAA5AAAAOwAAAEAAAAA9AAAAVAAAAB4AAABVAAAAHwAAAEMAAABWAAAAQQAAAFYAAAAeAAAAQQAAAEcAAABXAAAARQAAAFcAAABDAAAARQAAAEsAAABYAAAASQAAAFgAAABHAAAASQAAAE4AAABLAAAAWQAAAFoAAABbAAAAXAAAAF0AAABeAAAAXwAAAGAAAABhAAAAYgAAAGMAAABkAAAAZQAAAGQAAABmAAAAZwAAAGYAAABoAAAAZwAAAGgAAABpAAAAagAAAGkAAABrAAAAagAAAGsAAABsAAAAbQAAAGwAAABdAAAAbQAAAGUAAABkAAAAbgAAAGcAAABoAAAAbwAAAGQAAABnAAAAcAAAAGoAAABrAAAAcQAAAGgAAABqAAAAcgAAAG0AAABdAAAAcwAAAGsAAABtAAAAdAAAAHUAAABaAAAAdgAAAHcAAAB1AAAAeAAAAHkAAAB3AAAAegAAAHsAAAB5AAAAfAAAAH0AAAB7AAAAfgAAAH8AAAB9AAAAgAAAAIEAAAB/AAAAggAAAIMAAABgAAAAhAAAAIUAAACDAAAAhgAAAIcAAACFAAAAiAAAAIkAAACHAAAAigAAAIsAAACJAAAAjAAAAI0AAACLAAAAjgAAAI8AAACNAAAAkAAAAF0AAACRAAAAXgAAAFoAAACSAAAAWwAAAHcAAACTAAAAdQAAAJMAAABaAAAAdQAAAHsAAACUAAAAeQAAAJQAAAB3AAAAeQAAAH8AAACVAAAAfQAAAJUAAAB7AAAAfQAAAIIAAAB/AAAAlgAAAGAAAACXAAAAYQAAAIUAAACYAAAAgwAAAJgAAABgAAAAgwAAAIkAAACZAAAAhwAAAJkAAACFAAAAhwAAAI0AAACaAAAAiwAAAJoAAACJAAAAiwAAAJAAAACNAAAAmwAAAJwAAACdAAAAngAAAJ8AAACcAAAAoAAAAKEAAACfAAAAogAAAKMAAAChAAAApAAAAKUAAACjAAAApgAAAKcAAAClAAAAqAAAAKkAAACnAAAAqgAAAKsAAACpAAAArAAAAJwAAACtAAAAnQAAAKEAAACuAAAAnwAAAK4AAACcAAAAnwAAAKUAAACvAAAAowAAAK8AAAChAAAAowAAAKkAAACwAAAApwAAALAAAAClAAAApwAAAKwAAACpAAAAsQAAALIAAACzAAAAtAAAALUAAAC2AAAAtwAAALYAAAC4AAAAuQAAALoAAAC7AAAAvAAAALgAAAC6AAAAuQAAALsAAAC9AAAAvAAAAL0AAAC+AAAAvwAAAL4AAACyAAAAvwAAALIAAADAAAAAswAAAL8AAACyAAAAwQAAAL0AAAC/AAAAwgAAALwAAAC9AAAAwwAAALoAAAC8AAAAxAAAALkAAAC6AAAAxQAAALYAAAC5AAAAxgAAALcAAAC2AAAAxwAAAMgAAADJAAAAygAAAMsAAADIAAAAzAAAAM0AAADLAAAAzgAAAM8AAADNAAAA0AAAANEAAADPAAAA0gAAANMAAADRAAAA1AAAANUAAADTAAAA1gAAANcAAADVAAAA2AAAAMgAAADZAAAAyQAAAM0AAADaAAAAywAAANoAAADIAAAAywAAANEAAADbAAAAzwAAANsAAADNAAAAzwAAANUAAADcAAAA0wAAANwAAADRAAAA0wAAANgAAADVAAAA3QAAAN4AAADfAAAA4AAAAOEAAADiAAAA4wAAAOIAAADkAAAA5QAAAOYAAADnAAAA6AAAAOQAAADmAAAA5QAAAOcAAADpAAAA6AAAAOkAAADqAAAA6wAAAOoAAADeAAAA6wAAAN4AAADsAAAA3wAAAOsAAADeAAAA7QAAAOkAAADrAAAA7gAAAOgAAADpAAAA7wAAAOYAAADoAAAA8AAAAOUAAADmAAAA8QAAAOIAAADlAAAA8gAAAOMAAADiAAAA8wAAAPQAAAD1AAAA9gAAAPcAAAD0AAAA9gAAAPgAAAD5AAAA+gAAAPsAAAD8AAAA/QAAAP4AAAD7AAAA/QAAAP8AAAAAAQAAAQEAAAIBAAD/AAAAAQEAAAMBAAAEAQAABQEAAAMBAAAFAQAABgEAAAcBAAAIAQAACQEAAAcBAAAJAQAACgEAAAsBAAAMAQAADQEAAA4BAAAPAQAAEAEAABEBAAASAQAAEwEAABQBAAAVAQAAFgEAABcBAAAYAQAAGQEAABoBAAAbAQAAHAEAAB0BAAAeAQAAHwEAACABAAAhAQAAIgEAACMBAAAgAQAAIgEAACQBAAAlAQAAJgEAACcBAAAoAQAAKQEAACoBAAAnAQAAKQEAACsBAAAsAQAALQEAAC4BAAArAQAALQEAAC8BAAAwAQAAMQEAAC8BAAAxAQAAMgEAADMBAAA0AQAANQEAADMBAAA1AQAANgEAADcBAAA4AQAAOQEAADoBAAA7AQAAPAEAAD0BAAA+AQAAPwEAAEABAABBAQAAQgEAAEMBAABEAQAARQEAAEYBAABHAQAASAEAAEkBAABKAQAASwEAAEwBAABNAQAATgEAAE8BAABMAQAATgEAAFABAABRAQAAUgEAAFMBAABQAQAAUgEAAFQBAABVAQAAVgEAAFcBAABUAQAAVgEAAFgBAABZAQAAWgEAAFsBAABYAQAAWgEAAFwBAABdAQAAXgEAAF8BAABcAQAAXgEAAGABAABhAQAAYgEAAGMBAABgAQAAYgEAAGQBAABlAQAAZgEAAGcBAABoAQAAaQEAAGoBAABrAQAAbAEAAG0BAABuAQAAbwEAAG4BAABwAQAAcQEAAHABAAByAQAAcQEAAHIBAABzAQAAdAEAAHMBAAB1AQAAdAEAAHUBAAB2AQAAdwEAAHYBAABnAQAAdwEAAG8BAABuAQAAeAEAAHEBAAByAQAAeQEAAG4BAABxAQAAegEAAHQBAAB1AQAAewEAAHIBAAB0AQAAfAEAAHcBAABnAQAAfQEAAHUBAAB3AQAAfgEAAH8BAABkAQAAgAEAAIEBAAB/AQAAggEAAIMBAACBAQAAhAEAAIUBAACDAQAAhgEAAIcBAACFAQAAiAEAAIkBAACHAQAAigEAAIsBAACJAQAAjAEAAI0BAABqAQAAjgEAAI8BAACNAQAAkAEAAJEBAACPAQAAkgEAAJMBAACRAQAAlAEAAJUBAACTAQAAlgEAAJcBAACVAQAAmAEAAJkBAACXAQAAmgEAAGcBAACbAQAAaAEAAGQBAACcAQAAZQEAAIEBAACdAQAAfwEAAJ0BAABkAQAAfwEAAIUBAACeAQAAgwEAAJ4BAACBAQAAgwEAAIkBAACfAQAAhwEAAJ8BAACFAQAAhwEAAIwBAACJAQAAoAEAAGoBAAChAQAAawEAAI8BAACiAQAAjQEAAKIBAABqAQAAjQEAAJMBAACjAQAAkQEAAKMBAACPAQAAkQEAAJcBAACkAQAAlQEAAKQBAACTAQAAlQEAAJoBAACXAQAApQEAAKYBAACnAQAAqAEAAKkBAACqAQAAqwEAAKwBAACtAQAArgEAAK8BAACwAQAAsQEAALABAACyAQAAswEAALIBAAC0AQAAswEAALQBAAC1AQAAtgEAALUBAAC3AQAAtgEAALcBAAC4AQAAuQEAALgBAACpAQAAuQEAALEBAACwAQAAugEAALMBAAC0AQAAuwEAALABAACzAQAAvAEAALYBAAC3AQAAvQEAALQBAAC2AQAAvgEAALkBAACpAQAAvwEAALcBAAC5AQAAwAEAAMEBAACmAQAAwgEAAMMBAADBAQAAxAEAAMUBAADDAQAAxgEAAMcBAADFAQAAyAEAAMkBAADHAQAAygEAAMsBAADJAQAAzAEAAM0BAADLAQAAzgEAAM8BAACsAQAA0AEAANEBAADPAQAA0gEAANMBAADRAQAA1AEAANUBAADTAQAA1gEAANcBAADVAQAA2AEAANkBAADXAQAA2gEAANsBAADZAQAA3AEAAKkBAADdAQAAqgEAAKYBAADeAQAApwEAAMMBAADfAQAAwQEAAN8BAACmAQAAwQEAAMcBAADgAQAAxQEAAOABAADDAQAAxQEAAMsBAADhAQAAyQEAAOEBAADHAQAAyQEAAM4BAADLAQAA4gEAAKwBAADjAQAArQEAANEBAADkAQAAzwEAAOQBAACsAQAAzwEAANUBAADlAQAA0wEAAOUBAADRAQAA0wEAANkBAADmAQAA1wEAAOYBAADVAQAA1wEAANwBAADZAQAA5wEAAOgBAADpAQAA6gEAAOsBAADoAQAA7AEAAO0BAADrAQAA7gEAAO8BAADtAQAA8AEAAPEBAADvAQAA8gEAAPMBAADxAQAA9AEAAPUBAADzAQAA9gEAAPcBAAD1AQAA+AEAAOgBAAD5AQAA6QEAAO0BAAD6AQAA6wEAAPoBAADoAQAA6wEAAPEBAAD7AQAA7wEAAPsBAADtAQAA7wEAAPUBAAD8AQAA8wEAAPwBAADxAQAA8wEAAPgBAAD1AQAA/QEAAP4BAAD/AQAAAAIAAAECAAACAgAAAwIAAAICAAAEAgAABQIAAAYCAAAHAgAACAIAAAQCAAAGAgAABQIAAAcCAAAJAgAACAIAAAkCAAAKAgAACwIAAAoCAAD+AQAACwIAAP4BAAAMAgAA/wEAAAsCAAD+AQAADQIAAAkCAAALAgAADgIAAAgCAAAJAgAADwIAAAYCAAAIAgAAEAIAAAUCAAAGAgAAEQIAAAICAAAFAgAAEgIAAAMCAAACAgAAEwIAABQCAAAVAgAAFgIAABcCAAAUAgAAGAIAABkCAAAXAgAAGgIAABsCAAAZAgAAHAIAAB0CAAAbAgAAHgIAAB8CAAAdAgAAIAIAACECAAAfAgAAIgIAACMCAAAhAgAAJAIAABQCAAAlAgAAFQIAABkCAAAmAgAAFwIAACYCAAAUAgAAFwIAAB0CAAAnAgAAGwIAACcCAAAZAgAAGwIAACECAAAoAgAAHwIAACgCAAAdAgAAHwIAACQCAAAhAgAAKQIAACoCAAArAgAALAIAAC0CAAAuAgAALwIAAC4CAAAwAgAAMQIAADICAAAzAgAANAIAADACAAAyAgAAMQIAADMCAAA1AgAANAIAADUCAAA2AgAANwIAADYCAAAqAgAANwIAACoCAAA4AgAAKwIAADcCAAAqAgAAOQIAADUCAAA3AgAAOgIAADQCAAA1AgAAOwIAADICAAA0AgAAPAIAADECAAAyAgAAPQIAAC4CAAAxAgAAPgIAAC8CAAAuAgAAPwIAAEACAABBAgAAQgIAAEMCAABAAgAAQgIAAEQCAABFAgAARgIAAEcCAABIAgAASQIAAEoCAABHAgAASQIAAEsCAABMAgAATQIAAE4CAABLAgAATQIAAE8CAABQAgAAUQIAAE8CAABRAgAAUgIAAFMCAABUAgAAVQIAAFMCAABVAgAAVgIAAFcCAABYAgAAWQIAAFoCAABbAgAAXAIAAF0CAABeAgAAXwIAAGACAABhAgAAYgIAAGMCAABkAgAAZQIAAGYCAABnAgAAaAIAAGkCAABqAgAAawIAAGwCAABtAgAAbgIAAG8CAABsAgAAbgIAAHACAABxAgAAcgIAAHMCAAB0AgAAdQIAAHYCAABzAgAAdQIAAHcCAAB4AgAAeQIAAHoCAAB3AgAAeQIAAHsCAAB8AgAAfQIAAHsCAAB9AgAAfgIAAH8CAACAAgAAgQIAAH8CAACBAgAAggIAAIMCAACEAgAAhQIAAIYCAACHAgAAiAIAAIkCAACKAgAAiwIAAIwCAACNAgAAjgIAAI8CAACQAgAAkQIAAJICAACTAgAAlAIAAJUCAACWAgAAlwIAAA==",
    "compLeftArm": "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AEkAAABKAAAASwAAAEwAAABNAAAATgAAAE8AAABQAAAAUQAAAFIAAABTAAAAVAAAAFUAAABWAAAAVwAAAFgAAABZAAAAWgAAAFsAAABYAAAAWgAAAFwAAABdAAAAXgAAAF8AAABcAAAAXgAAAGAAAABhAAAAYgAAAGMAAABgAAAAYgAAAGQAAABlAAAAZgAAAGcAAABkAAAAZgAAAGgAAABpAAAAagAAAGsAAABoAAAAagAAAGwAAABtAAAAbgAAAG8AAABsAAAAbgAAAHAAAABxAAAAcgAAAHMAAAB0AAAAdQAAAHYAAAB3AAAAeAAAAHkAAAB6AAAAewAAAHwAAAB9AAAAfgAAAH8AAACAAAAAgQAAAIAAAACCAAAAgwAAAIIAAACEAAAAgwAAAIQAAACFAAAAhgAAAIUAAACHAAAAhgAAAIcAAACIAAAAiQAAAIgAAAB5AAAAiQAAAIEAAACAAAAAigAAAIMAAACEAAAAiwAAAIAAAACDAAAAjAAAAIYAAACHAAAAjQAAAIQAAACGAAAAjgAAAIkAAAB5AAAAjwAAAIcAAACJAAAAkAAAAJEAAACSAAAAkwAAAJIAAACUAAAAlQAAAJYAAACXAAAAmAAAAJQAAACWAAAAlQAAAJcAAACZAAAAmAAAAJkAAACaAAAAmwAAAJoAAAB2AAAAmwAAAJwAAABwAAAAnQAAAJ4AAACcAAAAnwAAAKAAAACeAAAAoQAAAKIAAACgAAAAowAAAKQAAACiAAAApQAAAKYAAACkAAAApwAAAKgAAACmAAAAqQAAAKoAAABzAAAAqwAAAKwAAACqAAAArQAAAK4AAACsAAAArwAAALAAAACuAAAAsQAAALIAAACwAAAAswAAALQAAACyAAAAtQAAALYAAAC0AAAAtwAAALgAAAB8AAAAuQAAALoAAAC4AAAAuwAAALwAAAC6AAAAvQAAAL4AAAC8AAAAvwAAAMAAAAC+AAAAwQAAAMIAAADAAAAAwwAAAMQAAADCAAAAxQAAAHkAAADGAAAAegAAAHYAAADHAAAAdwAAAJsAAAB2AAAAyAAAAJkAAACbAAAAyQAAAJgAAACZAAAAygAAAJYAAACYAAAAywAAAJUAAACWAAAAzAAAAJIAAACVAAAAzQAAAJMAAACSAAAAzgAAAHAAAADPAAAAcQAAAJ4AAADQAAAAnAAAANAAAABwAAAAnAAAAKIAAADRAAAAoAAAANEAAACeAAAAoAAAAKYAAADSAAAApAAAANIAAACiAAAApAAAAKkAAACmAAAA0wAAAHMAAADUAAAAdAAAAKwAAADVAAAAqgAAANUAAABzAAAAqgAAALAAAADWAAAArgAAANYAAACsAAAArgAAALQAAADXAAAAsgAAANcAAACwAAAAsgAAALcAAAC0AAAA2AAAAHwAAADZAAAAfQAAALoAAADaAAAAuAAAANoAAAB8AAAAuAAAAL4AAADbAAAAvAAAANsAAAC6AAAAvAAAAMIAAADcAAAAwAAAANwAAAC+AAAAwAAAAMUAAADCAAAA3QAAAN4AAADfAAAA4AAAAOEAAADiAAAA4wAAAOQAAADlAAAA5gAAAOcAAADoAAAA6QAAAOoAAADrAAAA7AAAAO0AAADuAAAA7wAAAO4AAADwAAAA8QAAAPAAAADyAAAA8QAAAPIAAADzAAAA9AAAAPMAAAD1AAAA9AAAAPUAAAD2AAAA9wAAAPYAAADnAAAA9wAAAO8AAADuAAAA+AAAAPEAAADyAAAA+QAAAO4AAADxAAAA+gAAAPQAAAD1AAAA+wAAAPIAAAD0AAAA/AAAAPcAAADnAAAA/QAAAPUAAAD3AAAA/gAAAP8AAAAAAQAAAQEAAAABAAACAQAAAwEAAAQBAAAFAQAABgEAAAIBAAAEAQAAAwEAAAUBAAAHAQAABgEAAAcBAAAIAQAACQEAAAgBAADkAAAACQEAAAoBAADeAAAACwEAAAwBAAAKAQAADQEAAA4BAAAMAQAADwEAABABAAAOAQAAEQEAABIBAAAQAQAAEwEAABQBAAASAQAAFQEAABYBAAAUAQAAFwEAABgBAADhAAAAGQEAABoBAAAYAQAAGwEAABwBAAAaAQAAHQEAAB4BAAAcAQAAHwEAACABAAAeAQAAIQEAACIBAAAgAQAAIwEAACQBAAAiAQAAJQEAACYBAADqAAAAJwEAACgBAAAmAQAAKQEAACoBAAAoAQAAKwEAACwBAAAqAQAALQEAAC4BAAAsAQAALwEAADABAAAuAQAAMQEAADIBAAAwAQAAMwEAAOcAAAA0AQAA6AAAAOQAAAA1AQAA5QAAAAkBAADkAAAANgEAAAcBAAAJAQAANwEAAAYBAAAHAQAAOAEAAAQBAAAGAQAAOQEAAAMBAAAEAQAAOgEAAAABAAADAQAAOwEAAAEBAAAAAQAAPAEAAN4AAAA9AQAA3wAAAAwBAAA+AQAACgEAAD4BAADeAAAACgEAABABAAA/AQAADgEAAD8BAAAMAQAADgEAABQBAABAAQAAEgEAAEABAAAQAQAAEgEAABcBAAAUAQAAQQEAAOEAAABCAQAA4gAAABoBAABDAQAAGAEAAEMBAADhAAAAGAEAAB4BAABEAQAAHAEAAEQBAAAaAQAAHAEAACIBAABFAQAAIAEAAEUBAAAeAQAAIAEAACUBAAAiAQAARgEAAOoAAABHAQAA6wAAACgBAABIAQAAJgEAAEgBAADqAAAAJgEAACwBAABJAQAAKgEAAEkBAAAoAQAAKgEAADABAABKAQAALgEAAEoBAAAsAQAALgEAADMBAAAwAQAASwEAAEwBAABNAQAATgEAAE8BAABMAQAATgEAAFABAABRAQAAUgEAAFMBAABUAQAAVQEAAFYBAABTAQAAVQEAAFcBAABYAQAAWQEAAFoBAABXAQAAWQEAAFsBAABcAQAAXQEAAFsBAABdAQAAXgEAAF8BAABgAQAAYQEAAF8BAABhAQAAYgEAAGMBAABkAQAAZQEAAGYBAABnAQAAaAEAAGkBAABqAQAAawEAAGwBAABtAQAAbgEAAG8BAABwAQAAcQEAAHIBAABzAQAAdAEAAHUBAAB2AQAAdwEAAHgBAAB5AQAAegEAAHsBAAB4AQAAegEAAHwBAAB9AQAAfgEAAH8BAACAAQAAgQEAAIIBAAB/AQAAgQEAAIMBAACEAQAAhQEAAIYBAACDAQAAhQEAAIcBAACIAQAAiQEAAIcBAACJAQAAigEAAIsBAACMAQAAjQEAAIsBAACNAQAAjgEAAI8BAACQAQAAkQEAAJIBAACTAQAAlAEAAJUBAACWAQAAlwEAAJgBAACZAQAAmgEAAJsBAACcAQAAnQEAAJ4BAACfAQAAoAEAAKEBAACiAQAAowEAAKQBAAClAQAApgEAAKcBAACkAQAApgEAAKgBAACpAQAAqgEAAKsBAACoAQAAqgEAAKwBAACtAQAArgEAAK8BAACsAQAArgEAALABAACxAQAAsgEAALMBAACwAQAAsgEAALQBAAC1AQAAtgEAALcBAAC0AQAAtgEAALgBAAC5AQAAugEAALsBAAC4AQAAugEAALwBAAC9AQAAvgEAAL8BAADAAQAAwQEAAMIBAADDAQAAxAEAAMUBAADGAQAAxwEAAMgBAADJAQAAygEAAMsBAADMAQAAzQEAAMwBAADOAQAAzwEAAM4BAADQAQAAzwEAANABAADRAQAA0gEAANEBAADTAQAA0gEAANMBAADUAQAA1QEAANQBAADFAQAA1QEAAM0BAADMAQAA1gEAAM8BAADQAQAA1wEAAMwBAADPAQAA2AEAANIBAADTAQAA2QEAANABAADSAQAA2gEAANUBAADFAQAA2wEAANMBAADVAQAA3AEAAN0BAADeAQAA3wEAAN4BAADgAQAA4QEAAOIBAADjAQAA5AEAAOABAADiAQAA4QEAAOMBAADlAQAA5AEAAOUBAADmAQAA5wEAAOYBAADCAQAA5wEAAOgBAAC8AQAA6QEAAOoBAADoAQAA6wEAAOwBAADqAQAA7QEAAO4BAADsAQAA7wEAAPABAADuAQAA8QEAAPIBAADwAQAA8wEAAPQBAADyAQAA9QEAAPYBAAC/AQAA9wEAAPgBAAD2AQAA+QEAAPoBAAD4AQAA+wEAAPwBAAD6AQAA/QEAAP4BAAD8AQAA/wEAAAACAAD+AQAAAQIAAAICAAAAAgAAAwIAAAQCAADIAQAABQIAAAYCAAAEAgAABwIAAAgCAAAGAgAACQIAAAoCAAAIAgAACwIAAAwCAAAKAgAADQIAAA4CAAAMAgAADwIAABACAAAOAgAAEQIAAMUBAAASAgAAxgEAAMIBAAATAgAAwwEAAOcBAADCAQAAFAIAAOUBAADnAQAAFQIAAOQBAADlAQAAFgIAAOIBAADkAQAAFwIAAOEBAADiAQAAGAIAAN4BAADhAQAAGQIAAN8BAADeAQAAGgIAALwBAAAbAgAAvQEAAOoBAAAcAgAA6AEAABwCAAC8AQAA6AEAAO4BAAAdAgAA7AEAAB0CAADqAQAA7AEAAPIBAAAeAgAA8AEAAB4CAADuAQAA8AEAAPUBAADyAQAAHwIAAL8BAAAgAgAAwAEAAPgBAAAhAgAA9gEAACECAAC/AQAA9gEAAPwBAAAiAgAA+gEAACICAAD4AQAA+gEAAAACAAAjAgAA/gEAACMCAAD8AQAA/gEAAAMCAAAAAgAAJAIAAMgBAAAlAgAAyQEAAAYCAAAmAgAABAIAACYCAADIAQAABAIAAAoCAAAnAgAACAIAACcCAAAGAgAACAIAAA4CAAAoAgAADAIAACgCAAAKAgAADAIAABECAAAOAgAAKQIAACoCAAArAgAALAIAAC0CAAAuAgAALwIAADACAAAxAgAAMgIAADMCAAA0AgAANQIAADYCAAA3AgAAOAIAADkCAAA6AgAAOwIAADoCAAA8AgAAPQIAADwCAAA+AgAAPQIAAD4CAAA/AgAAQAIAAD8CAABBAgAAQAIAAEECAABCAgAAQwIAAEICAAAzAgAAQwIAADsCAAA6AgAARAIAAD0CAAA+AgAARQIAADoCAAA9AgAARgIAAEACAABBAgAARwIAAD4CAABAAgAASAIAAEMCAAAzAgAASQIAAEECAABDAgAASgIAAEsCAABMAgAATQIAAEwCAABOAgAATwIAAFACAABRAgAAUgIAAE4CAABQAgAATwIAAFECAABTAgAAUgIAAFMCAABUAgAAVQIAAFQCAAAwAgAAVQIAAFYCAAAqAgAAVwIAAFgCAABWAgAAWQIAAFoCAABYAgAAWwIAAFwCAABaAgAAXQIAAF4CAABcAgAAXwIAAGACAABeAgAAYQIAAGICAABgAgAAYwIAAGQCAAAtAgAAZQIAAGYCAABkAgAAZwIAAGgCAABmAgAAaQIAAGoCAABoAgAAawIAAGwCAABqAgAAbQIAAG4CAABsAgAAbwIAAHACAABuAgAAcQIAAHICAAA2AgAAcwIAAHQCAAByAgAAdQIAAHYCAAB0AgAAdwIAAHgCAAB2AgAAeQIAAHoCAAB4AgAAewIAAHwCAAB6AgAAfQIAAH4CAAB8AgAAfwIAADMCAACAAgAANAIAADACAACBAgAAMQIAAFUCAAAwAgAAggIAAFMCAABVAgAAgwIAAFICAABTAgAAhAIAAFACAABSAgAAhQIAAE8CAABQAgAAhgIAAEwCAABPAgAAhwIAAE0CAABMAgAAiAIAACoCAACJAgAAKwIAAFgCAACKAgAAVgIAAIoCAAAqAgAAVgIAAFwCAACLAgAAWgIAAIsCAABYAgAAWgIAAGACAACMAgAAXgIAAIwCAABcAgAAXgIAAGMCAABgAgAAjQIAAC0CAACOAgAALgIAAGYCAACPAgAAZAIAAI8CAAAtAgAAZAIAAGoCAACQAgAAaAIAAJACAABmAgAAaAIAAG4CAACRAgAAbAIAAJECAABqAgAAbAIAAHECAABuAgAAkgIAADYCAACTAgAANwIAAHQCAACUAgAAcgIAAJQCAAA2AgAAcgIAAHgCAACVAgAAdgIAAJUCAAB0AgAAdgIAAHwCAACWAgAAegIAAJYCAAB4AgAAegIAAH8CAAB8AgAAlwIAAA==",
    "compRightArm": "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AIEAAACDAAAAggAAAIMAAACEAAAAhQAAAIQAAABxAAAAhQAAAH0AAAB8AAAAhgAAAH8AAACAAAAAhwAAAHwAAAB/AAAAiAAAAIIAAACDAAAAiQAAAIAAAACCAAAAigAAAIUAAABxAAAAiwAAAIMAAACFAAAAjAAAAI0AAACOAAAAjwAAAI4AAACQAAAAkQAAAJIAAACTAAAAlAAAAJAAAACSAAAAkQAAAJMAAACVAAAAlAAAAJUAAACWAAAAlwAAAJYAAABqAAAAlwAAAJgAAABkAAAAmQAAAJoAAACYAAAAmwAAAJwAAACaAAAAnQAAAJ4AAACcAAAAnwAAAKAAAACeAAAAoQAAAKIAAACgAAAAowAAAKQAAACiAAAApQAAAKYAAABnAAAApwAAAKgAAACmAAAAqQAAAKoAAACoAAAAqwAAAKwAAACqAAAArQAAAK4AAACsAAAArwAAALAAAACuAAAAsQAAALIAAACwAAAAswAAALQAAAB4AAAAtQAAALYAAAC0AAAAtwAAALgAAAC2AAAAuQAAALoAAAC4AAAAuwAAALwAAAC6AAAAvQAAAL4AAAC8AAAAvwAAAMAAAAC+AAAAwQAAAHEAAADCAAAAcgAAAGoAAADDAAAAawAAAJcAAABqAAAAxAAAAJUAAACXAAAAxQAAAJQAAACVAAAAxgAAAJIAAACUAAAAxwAAAJEAAACSAAAAyAAAAI4AAACRAAAAyQAAAI8AAACOAAAAygAAAGQAAADLAAAAZQAAAJoAAADMAAAAmAAAAMwAAABkAAAAmAAAAJ4AAADNAAAAnAAAAM0AAACaAAAAnAAAAKIAAADOAAAAoAAAAM4AAACeAAAAoAAAAKUAAACiAAAAzwAAAGcAAADQAAAAaAAAAKgAAADRAAAApgAAANEAAABnAAAApgAAAKwAAADSAAAAqgAAANIAAACoAAAAqgAAALAAAADTAAAArgAAANMAAACsAAAArgAAALMAAACwAAAA1AAAAHgAAADVAAAAeQAAALYAAADWAAAAtAAAANYAAAB4AAAAtAAAALoAAADXAAAAuAAAANcAAAC2AAAAuAAAAL4AAADYAAAAvAAAANgAAAC6AAAAvAAAAMEAAAC+AAAA2QAAANoAAADbAAAA3AAAAN0AAADeAAAA3wAAAOAAAADhAAAA4gAAAOMAAADkAAAA5QAAAOYAAADnAAAA6AAAAOkAAADqAAAA6wAAAOoAAADsAAAA7QAAAOwAAADuAAAA7QAAAO4AAADvAAAA8AAAAO8AAADxAAAA8AAAAPEAAADyAAAA8wAAAPIAAADjAAAA8wAAAOsAAADqAAAA9AAAAO0AAADuAAAA9QAAAOoAAADtAAAA9gAAAPAAAADxAAAA9wAAAO4AAADwAAAA+AAAAPMAAADjAAAA+QAAAPEAAADzAAAA+gAAAPsAAAD8AAAA/QAAAPwAAAD+AAAA/wAAAAABAAABAQAAAgEAAP4AAAAAAQAA/wAAAAEBAAADAQAAAgEAAAMBAAAEAQAABQEAAAQBAADgAAAABQEAAAYBAADaAAAABwEAAAgBAAAGAQAACQEAAAoBAAAIAQAACwEAAAwBAAAKAQAADQEAAA4BAAAMAQAADwEAABABAAAOAQAAEQEAABIBAAAQAQAAEwEAABQBAADdAAAAFQEAABYBAAAUAQAAFwEAABgBAAAWAQAAGQEAABoBAAAYAQAAGwEAABwBAAAaAQAAHQEAAB4BAAAcAQAAHwEAACABAAAeAQAAIQEAACIBAADmAAAAIwEAACQBAAAiAQAAJQEAACYBAAAkAQAAJwEAACgBAAAmAQAAKQEAACoBAAAoAQAAKwEAACwBAAAqAQAALQEAAC4BAAAsAQAALwEAAOMAAAAwAQAA5AAAAOAAAAAxAQAA4QAAAAUBAADgAAAAMgEAAAMBAAAFAQAAMwEAAAIBAAADAQAANAEAAAABAAACAQAANQEAAP8AAAAAAQAANgEAAPwAAAD/AAAANwEAAP0AAAD8AAAAOAEAANoAAAA5AQAA2wAAAAgBAAA6AQAABgEAADoBAADaAAAABgEAAAwBAAA7AQAACgEAADsBAAAIAQAACgEAABABAAA8AQAADgEAADwBAAAMAQAADgEAABMBAAAQAQAAPQEAAN0AAAA+AQAA3gAAABYBAAA/AQAAFAEAAD8BAADdAAAAFAEAABoBAABAAQAAGAEAAEABAAAWAQAAGAEAAB4BAABBAQAAHAEAAEEBAAAaAQAAHAEAACEBAAAeAQAAQgEAAOYAAABDAQAA5wAAACQBAABEAQAAIgEAAEQBAADmAAAAIgEAACgBAABFAQAAJgEAAEUBAAAkAQAAJgEAACwBAABGAQAAKgEAAEYBAAAoAQAAKgEAAC8BAAAsAQAARwEAAEgBAABJAQAASgEAAEsBAABIAQAASgEAAEwBAABNAQAATgEAAE8BAABQAQAAUQEAAFIBAABPAQAAUQEAAFMBAABUAQAAVQEAAFYBAABTAQAAVQEAAFcBAABYAQAAWQEAAFcBAABZAQAAWgEAAFsBAABcAQAAXQEAAFsBAABdAQAAXgEAAF8BAABgAQAAYQEAAGIBAABjAQAAZAEAAGUBAABmAQAAZwEAAGgBAABpAQAAagEAAGsBAABsAQAAbQEAAG4BAABvAQAAcAEAAHEBAAByAQAAcwEAAHQBAAB1AQAAdgEAAHcBAAB0AQAAdgEAAHgBAAB5AQAAegEAAHsBAAB8AQAAfQEAAH4BAAB7AQAAfQEAAH8BAACAAQAAgQEAAIIBAAB/AQAAgQEAAIMBAACEAQAAhQEAAIMBAACFAQAAhgEAAIcBAACIAQAAiQEAAIcBAACJAQAAigEAAIsBAACMAQAAjQEAAI4BAACPAQAAkAEAAJEBAACSAQAAkwEAAJQBAACVAQAAlgEAAJcBAACYAQAAmQEAAJoBAACbAQAAnAEAAJ0BAACeAQAAnwEAAKABAAChAQAAogEAAKMBAACgAQAAogEAAKQBAAClAQAApgEAAKcBAACkAQAApgEAAKgBAACpAQAAqgEAAKsBAACoAQAAqgEAAKwBAACtAQAArgEAAK8BAACwAQAAsQEAALIBAACzAQAAtAEAALUBAAC2AQAAtwEAALgBAAC1AQAAtwEAALkBAAC6AQAAuwEAALwBAAC9AQAAvgEAAL8BAAC8AQAAvgEAAMABAADBAQAAwgEAAMMBAADEAQAAxQEAAMQBAADGAQAAxwEAAMYBAADIAQAAxwEAAMgBAADJAQAAygEAAMkBAADLAQAAygEAAMsBAADMAQAAzQEAAMwBAAC5AQAAzQEAAMUBAADEAQAAzgEAAMcBAADIAQAAzwEAAMQBAADHAQAA0AEAAMoBAADLAQAA0QEAAMgBAADKAQAA0gEAAM0BAAC5AQAA0wEAAMsBAADNAQAA1AEAANUBAADWAQAA1wEAANYBAADYAQAA2QEAANoBAADbAQAA3AEAANgBAADaAQAA2QEAANsBAADdAQAA3AEAAN0BAADeAQAA3wEAAN4BAACyAQAA3wEAAOABAACsAQAA4QEAAOIBAADgAQAA4wEAAOQBAADiAQAA5QEAAOYBAADkAQAA5wEAAOgBAADmAQAA6QEAAOoBAADoAQAA6wEAAOwBAADqAQAA7QEAAO4BAACvAQAA7wEAAPABAADuAQAA8QEAAPIBAADwAQAA8wEAAPQBAADyAQAA9QEAAPYBAAD0AQAA9wEAAPgBAAD2AQAA+QEAAPoBAAD4AQAA+wEAAPwBAADAAQAA/QEAAP4BAAD8AQAA/wEAAAACAAD+AQAAAQIAAAICAAAAAgAAAwIAAAQCAAACAgAABQIAAAYCAAAEAgAABwIAAAgCAAAGAgAACQIAALkBAAAKAgAAugEAALIBAAALAgAAswEAAN8BAACyAQAADAIAAN0BAADfAQAADQIAANwBAADdAQAADgIAANoBAADcAQAADwIAANkBAADaAQAAEAIAANYBAADZAQAAEQIAANcBAADWAQAAEgIAAKwBAAATAgAArQEAAOIBAAAUAgAA4AEAABQCAACsAQAA4AEAAOYBAAAVAgAA5AEAABUCAADiAQAA5AEAAOoBAAAWAgAA6AEAABYCAADmAQAA6AEAAO0BAADqAQAAFwIAAK8BAAAYAgAAsAEAAPABAAAZAgAA7gEAABkCAACvAQAA7gEAAPQBAAAaAgAA8gEAABoCAADwAQAA8gEAAPgBAAAbAgAA9gEAABsCAAD0AQAA9gEAAPsBAAD4AQAAHAIAAMABAAAdAgAAwQEAAP4BAAAeAgAA/AEAAB4CAADAAQAA/AEAAAICAAAfAgAAAAIAAB8CAAD+AQAAAAIAAAYCAAAgAgAABAIAACACAAACAgAABAIAAAkCAAAGAgAAIQIAACICAAAjAgAAJAIAACUCAAAmAgAAJwIAACgCAAApAgAAKgIAACsCAAAsAgAALQIAAC4CAAAvAgAAMAIAADECAAAyAgAAMwIAADICAAA0AgAANQIAADQCAAA2AgAANQIAADYCAAA3AgAAOAIAADcCAAA5AgAAOAIAADkCAAA6AgAAOwIAADoCAAArAgAAOwIAADMCAAAyAgAAPAIAADUCAAA2AgAAPQIAADICAAA1AgAAPgIAADgCAAA5AgAAPwIAADYCAAA4AgAAQAIAADsCAAArAgAAQQIAADkCAAA7AgAAQgIAAEMCAABEAgAARQIAAEQCAABGAgAARwIAAEgCAABJAgAASgIAAEYCAABIAgAARwIAAEkCAABLAgAASgIAAEsCAABMAgAATQIAAEwCAAAoAgAATQIAAE4CAAAiAgAATwIAAFACAABOAgAAUQIAAFICAABQAgAAUwIAAFQCAABSAgAAVQIAAFYCAABUAgAAVwIAAFgCAABWAgAAWQIAAFoCAABYAgAAWwIAAFwCAAAlAgAAXQIAAF4CAABcAgAAXwIAAGACAABeAgAAYQIAAGICAABgAgAAYwIAAGQCAABiAgAAZQIAAGYCAABkAgAAZwIAAGgCAABmAgAAaQIAAGoCAAAuAgAAawIAAGwCAABqAgAAbQIAAG4CAABsAgAAbwIAAHACAABuAgAAcQIAAHICAABwAgAAcwIAAHQCAAByAgAAdQIAAHYCAAB0AgAAdwIAACsCAAB4AgAALAIAACgCAAB5AgAAKQIAAE0CAAAoAgAAegIAAEsCAABNAgAAewIAAEoCAABLAgAAfAIAAEgCAABKAgAAfQIAAEcCAABIAgAAfgIAAEQCAABHAgAAfwIAAEUCAABEAgAAgAIAACICAACBAgAAIwIAAFACAACCAgAATgIAAIICAAAiAgAATgIAAFQCAACDAgAAUgIAAIMCAABQAgAAUgIAAFgCAACEAgAAVgIAAIQCAABUAgAAVgIAAFsCAABYAgAAhQIAACUCAACGAgAAJgIAAF4CAACHAgAAXAIAAIcCAAAlAgAAXAIAAGICAACIAgAAYAIAAIgCAABeAgAAYAIAAGYCAACJAgAAZAIAAIkCAABiAgAAZAIAAGkCAABmAgAAigIAAC4CAACLAgAALwIAAGwCAACMAgAAagIAAIwCAAAuAgAAagIAAHACAACNAgAAbgIAAI0CAABsAgAAbgIAAHQCAACOAgAAcgIAAI4CAABwAgAAcgIAAHcCAAB0AgAAjwIAAA==",
    "compLeftLeg": "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ADkAAAA6AAAAOwAAADoAAAA8AAAAPQAAAD4AAAA/AAAAQAAAADwAAAA+AAAAPQAAAD8AAABBAAAAQAAAAEEAAABCAAAAQwAAAEIAAAAeAAAAQwAAAEQAAAAYAAAARQAAAEYAAABEAAAARwAAAEgAAABGAAAASQAAAEoAAABIAAAASwAAAEwAAABKAAAATQAAAE4AAABMAAAATwAAAFAAAABOAAAAUQAAAFIAAAAbAAAAUwAAAFQAAABSAAAAVQAAAFYAAABUAAAAVwAAAFgAAABWAAAAWQAAAFoAAABYAAAAWwAAAFwAAABaAAAAXQAAAF4AAABcAAAAXwAAAGAAAAAkAAAAYQAAAGIAAABgAAAAYwAAAGQAAABiAAAAZQAAAGYAAABkAAAAZwAAAGgAAABmAAAAaQAAAGoAAABoAAAAawAAAGwAAABqAAAAbQAAACEAAABuAAAAIgAAAB4AAABvAAAAHwAAAEMAAAAeAAAAcAAAAEEAAABDAAAAcQAAAEAAAABBAAAAcgAAAD4AAABAAAAAcwAAAD0AAAA+AAAAdAAAADoAAAA9AAAAdQAAADsAAAA6AAAAdgAAABgAAAB3AAAAGQAAAEYAAAB4AAAARAAAAHgAAAAYAAAARAAAAEoAAAB5AAAASAAAAHkAAABGAAAASAAAAE4AAAB6AAAATAAAAHoAAABKAAAATAAAAFEAAABOAAAAewAAABsAAAB8AAAAHAAAAFQAAAB9AAAAUgAAAH0AAAAbAAAAUgAAAFgAAAB+AAAAVgAAAH4AAABUAAAAVgAAAFwAAAB/AAAAWgAAAH8AAABYAAAAWgAAAF8AAABcAAAAgAAAACQAAACBAAAAJQAAAGIAAACCAAAAYAAAAIIAAAAkAAAAYAAAAGYAAACDAAAAZAAAAIMAAABiAAAAZAAAAGoAAACEAAAAaAAAAIQAAABmAAAAaAAAAG0AAABqAAAAhQAAAIYAAACHAAAAiAAAAIkAAACKAAAAiwAAAIwAAACNAAAAjgAAAI8AAACQAAAAkQAAAJIAAACTAAAAlAAAAJUAAACWAAAAlwAAAJYAAACYAAAAmQAAAJgAAACaAAAAmQAAAJoAAACbAAAAnAAAAJsAAACdAAAAnAAAAJ0AAACeAAAAnwAAAJ4AAACPAAAAnwAAAJcAAACWAAAAoAAAAJkAAACaAAAAoQAAAJYAAACZAAAAogAAAJwAAACdAAAAowAAAJoAAACcAAAApAAAAJ8AAACPAAAApQAAAJ0AAACfAAAApgAAAKcAAACoAAAAqQAAAKgAAACqAAAAqwAAAKwAAACtAAAArgAAAKoAAACsAAAAqwAAAK0AAACvAAAArgAAAK8AAACwAAAAsQAAALAAAACMAAAAsQAAALIAAACGAAAAswAAALQAAACyAAAAtQAAALYAAAC0AAAAtwAAALgAAAC2AAAAuQAAALoAAAC4AAAAuwAAALwAAAC6AAAAvQAAAL4AAAC8AAAAvwAAAMAAAACJAAAAwQAAAMIAAADAAAAAwwAAAMQAAADCAAAAxQAAAMYAAADEAAAAxwAAAMgAAADGAAAAyQAAAMoAAADIAAAAywAAAMwAAADKAAAAzQAAAM4AAACSAAAAzwAAANAAAADOAAAA0QAAANIAAADQAAAA0wAAANQAAADSAAAA1QAAANYAAADUAAAA1wAAANgAAADWAAAA2QAAANoAAADYAAAA2wAAAI8AAADcAAAAkAAAAIwAAADdAAAAjQAAALEAAACMAAAA3gAAAK8AAACxAAAA3wAAAK4AAACvAAAA4AAAAKwAAACuAAAA4QAAAKsAAACsAAAA4gAAAKgAAACrAAAA4wAAAKkAAACoAAAA5AAAAIYAAADlAAAAhwAAALQAAADmAAAAsgAAAOYAAACGAAAAsgAAALgAAADnAAAAtgAAAOcAAAC0AAAAtgAAALwAAADoAAAAugAAAOgAAAC4AAAAugAAAL8AAAC8AAAA6QAAAIkAAADqAAAAigAAAMIAAADrAAAAwAAAAOsAAACJAAAAwAAAAMYAAADsAAAAxAAAAOwAAADCAAAAxAAAAMoAAADtAAAAyAAAAO0AAADGAAAAyAAAAM0AAADKAAAA7gAAAJIAAADvAAAAkwAAANAAAADwAAAAzgAAAPAAAACSAAAAzgAAANQAAADxAAAA0gAAAPEAAADQAAAA0gAAANgAAADyAAAA1gAAAPIAAADUAAAA1gAAANsAAADYAAAA8wAAAPQAAAD1AAAA9gAAAPcAAAD0AAAA9gAAAPgAAAD5AAAA+gAAAPsAAAD8AAAA/QAAAP4AAAD7AAAA/QAAAP8AAAAAAQAAAQEAAAIBAAD/AAAAAQEAAAMBAAAEAQAABQEAAAMBAAAFAQAABgEAAAcBAAAIAQAACQEAAAcBAAAJAQAACgEAAAsBAAAMAQAADQEAAA4BAAAPAQAAEAEAABEBAAASAQAAEwEAABQBAAAVAQAAFgEAABcBAAAYAQAAGQEAABoBAAAbAQAAHAEAAB0BAAAeAQAAHwEAACABAAAhAQAAIgEAACMBAAAgAQAAIgEAACQBAAAlAQAAJgEAACcBAAAoAQAAKQEAACoBAAAnAQAAKQEAACsBAAAsAQAALQEAAC4BAAArAQAALQEAAC8BAAAwAQAAMQEAAC8BAAAxAQAAMgEAADMBAAA0AQAANQEAADMBAAA1AQAANgEAADcBAAA4AQAAOQEAADoBAAA7AQAAPAEAAD0BAAA+AQAAPwEAAEABAABBAQAAQgEAAEMBAABEAQAARQEAAEYBAABHAQAASAEAAEkBAABKAQAASwEAAEwBAABNAQAATgEAAE8BAABMAQAATgEAAFABAABRAQAAUgEAAFMBAABQAQAAUgEAAFQBAABVAQAAVgEAAFcBAABUAQAAVgEAAFgBAABZAQAAWgEAAFsBAABYAQAAWgEAAFwBAABdAQAAXgEAAF8BAABcAQAAXgEAAGABAABhAQAAYgEAAGMBAABgAQAAYgEAAGQBAABlAQAAZgEAAGcBAABoAQAAaQEAAGoBAABrAQAAbAEAAG0BAABuAQAAbwEAAHABAABxAQAAcgEAAHMBAAB0AQAAdQEAAHQBAAB2AQAAdwEAAHYBAAB4AQAAdwEAAHgBAAB5AQAAegEAAHkBAAB7AQAAegEAAHsBAAB8AQAAfQEAAHwBAABtAQAAfQEAAHUBAAB0AQAAfgEAAHcBAAB4AQAAfwEAAHQBAAB3AQAAgAEAAHoBAAB7AQAAgQEAAHgBAAB6AQAAggEAAH0BAABtAQAAgwEAAHsBAAB9AQAAhAEAAIUBAACGAQAAhwEAAIYBAACIAQAAiQEAAIoBAACLAQAAjAEAAIgBAACKAQAAiQEAAIsBAACNAQAAjAEAAI0BAACOAQAAjwEAAI4BAABqAQAAjwEAAJABAABkAQAAkQEAAJIBAACQAQAAkwEAAJQBAACSAQAAlQEAAJYBAACUAQAAlwEAAJgBAACWAQAAmQEAAJoBAACYAQAAmwEAAJwBAACaAQAAnQEAAJ4BAABnAQAAnwEAAKABAACeAQAAoQEAAKIBAACgAQAAowEAAKQBAACiAQAApQEAAKYBAACkAQAApwEAAKgBAACmAQAAqQEAAKoBAACoAQAAqwEAAKwBAABwAQAArQEAAK4BAACsAQAArwEAALABAACuAQAAsQEAALIBAACwAQAAswEAALQBAACyAQAAtQEAALYBAAC0AQAAtwEAALgBAAC2AQAAuQEAAG0BAAC6AQAAbgEAAGoBAAC7AQAAawEAAI8BAABqAQAAvAEAAI0BAACPAQAAvQEAAIwBAACNAQAAvgEAAIoBAACMAQAAvwEAAIkBAACKAQAAwAEAAIYBAACJAQAAwQEAAIcBAACGAQAAwgEAAGQBAADDAQAAZQEAAJIBAADEAQAAkAEAAMQBAABkAQAAkAEAAJYBAADFAQAAlAEAAMUBAACSAQAAlAEAAJoBAADGAQAAmAEAAMYBAACWAQAAmAEAAJ0BAACaAQAAxwEAAGcBAADIAQAAaAEAAKABAADJAQAAngEAAMkBAABnAQAAngEAAKQBAADKAQAAogEAAMoBAACgAQAAogEAAKgBAADLAQAApgEAAMsBAACkAQAApgEAAKsBAACoAQAAzAEAAHABAADNAQAAcQEAAK4BAADOAQAArAEAAM4BAABwAQAArAEAALIBAADPAQAAsAEAAM8BAACuAQAAsAEAALYBAADQAQAAtAEAANABAACyAQAAtAEAALkBAAC2AQAA0QEAANIBAADTAQAA1AEAANUBAADWAQAA1wEAANgBAADZAQAA2gEAANsBAADcAQAA3QEAAN4BAADfAQAA4AEAAOEBAADiAQAA4wEAAOIBAADkAQAA5QEAAOQBAADmAQAA5QEAAOYBAADnAQAA6AEAAOcBAADpAQAA6AEAAOkBAADqAQAA6wEAAOoBAADbAQAA6wEAAOMBAADiAQAA7AEAAOUBAADmAQAA7QEAAOIBAADlAQAA7gEAAOgBAADpAQAA7wEAAOYBAADoAQAA8AEAAOsBAADbAQAA8QEAAOkBAADrAQAA8gEAAPMBAAD0AQAA9QEAAPQBAAD2AQAA9wEAAPgBAAD5AQAA+gEAAPYBAAD4AQAA9wEAAPkBAAD7AQAA+gEAAPsBAAD8AQAA/QEAAPwBAADYAQAA/QEAAP4BAADSAQAA/wEAAAACAAD+AQAAAQIAAAICAAAAAgAAAwIAAAQCAAACAgAABQIAAAYCAAAEAgAABwIAAAgCAAAGAgAACQIAAAoCAAAIAgAACwIAAAwCAADVAQAADQIAAA4CAAAMAgAADwIAABACAAAOAgAAEQIAABICAAAQAgAAEwIAABQCAAASAgAAFQIAABYCAAAUAgAAFwIAABgCAAAWAgAAGQIAABoCAADeAQAAGwIAABwCAAAaAgAAHQIAAB4CAAAcAgAAHwIAACACAAAeAgAAIQIAACICAAAgAgAAIwIAACQCAAAiAgAAJQIAACYCAAAkAgAAJwIAANsBAAAoAgAA3AEAANgBAAApAgAA2QEAAP0BAADYAQAAKgIAAPsBAAD9AQAAKwIAAPoBAAD7AQAALAIAAPgBAAD6AQAALQIAAPcBAAD4AQAALgIAAPQBAAD3AQAALwIAAPUBAAD0AQAAMAIAANIBAAAxAgAA0wEAAAACAAAyAgAA/gEAADICAADSAQAA/gEAAAQCAAAzAgAAAgIAADMCAAAAAgAAAgIAAAgCAAA0AgAABgIAADQCAAAEAgAABgIAAAsCAAAIAgAANQIAANUBAAA2AgAA1gEAAA4CAAA3AgAADAIAADcCAADVAQAADAIAABICAAA4AgAAEAIAADgCAAAOAgAAEAIAABYCAAA5AgAAFAIAADkCAAASAgAAFAIAABkCAAAWAgAAOgIAAN4BAAA7AgAA3wEAABwCAAA8AgAAGgIAADwCAADeAQAAGgIAACACAAA9AgAAHgIAAD0CAAAcAgAAHgIAACQCAAA+AgAAIgIAAD4CAAAgAgAAIgIAACcCAAAkAgAAPwIAAEACAABBAgAAQgIAAEMCAABAAgAAQgIAAEQCAABFAgAARgIAAEcCAABIAgAASQIAAEoCAABHAgAASQIAAEsCAABMAgAATQIAAE4CAABLAgAATQIAAE8CAABQAgAAUQIAAE8CAABRAgAAUgIAAFMCAABUAgAAVQIAAFMCAABVAgAAVgIAAFcCAABYAgAAWQIAAFoCAABbAgAAXAIAAF0CAABeAgAAXwIAAGACAABhAgAAYgIAAGMCAABkAgAAZQIAAGYCAABnAgAAaAIAAGkCAABqAgAAawIAAGwCAABtAgAAbgIAAG8CAABsAgAAbgIAAHACAABxAgAAcgIAAHMCAAB0AgAAdQIAAHYCAABzAgAAdQIAAHcCAAB4AgAAeQIAAHoCAAB3AgAAeQIAAHsCAAB8AgAAfQIAAHsCAAB9AgAAfgIAAH8CAACAAgAAgQIAAH8CAACBAgAAggIAAIMCAACEAgAAhQIAAIYCAACHAgAAiAIAAIkCAACKAgAAiwIAAIwCAACNAgAAjgIAAI8CAACQAgAAkQIAAJICAACTAgAAlAIAAJUCAACWAgAAlwIAAA==",
    "compRightLeg": "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AGYAAABkAAAAZwAAAGgAAABmAAAAaQAAABkAAABqAAAAGgAAABIAAABrAAAAEwAAAD8AAAASAAAAbAAAAD0AAAA/AAAAbQAAADwAAAA9AAAAbgAAADoAAAA8AAAAbwAAADkAAAA6AAAAcAAAADYAAAA5AAAAcQAAADcAAAA2AAAAcgAAAAwAAABzAAAADQAAAEIAAAB0AAAAQAAAAHQAAAAMAAAAQAAAAEYAAAB1AAAARAAAAHUAAABCAAAARAAAAEoAAAB2AAAASAAAAHYAAABGAAAASAAAAE0AAABKAAAAdwAAAA8AAAB4AAAAEAAAAFAAAAB5AAAATgAAAHkAAAAPAAAATgAAAFQAAAB6AAAAUgAAAHoAAABQAAAAUgAAAFgAAAB7AAAAVgAAAHsAAABUAAAAVgAAAFsAAABYAAAAfAAAACAAAAB9AAAAIQAAAF4AAAB+AAAAXAAAAH4AAAAgAAAAXAAAAGIAAAB/AAAAYAAAAH8AAABeAAAAYAAAAGYAAACAAAAAZAAAAIAAAABiAAAAZAAAAGkAAABmAAAAgQAAAIIAAACDAAAAhAAAAIUAAACGAAAAhwAAAIgAAACJAAAAigAAAIsAAACMAAAAjQAAAI4AAACPAAAAkAAAAJEAAACSAAAAkwAAAJIAAACUAAAAlQAAAJQAAACWAAAAlQAAAJYAAACXAAAAmAAAAJcAAACZAAAAmAAAAJkAAACaAAAAmwAAAJoAAACLAAAAmwAAAJMAAACSAAAAnAAAAJUAAACWAAAAnQAAAJIAAACVAAAAngAAAJgAAACZAAAAnwAAAJYAAACYAAAAoAAAAJsAAACLAAAAoQAAAJkAAACbAAAAogAAAKMAAACkAAAApQAAAKQAAACmAAAApwAAAKgAAACpAAAAqgAAAKYAAACoAAAApwAAAKkAAACrAAAAqgAAAKsAAACsAAAArQAAAKwAAACIAAAArQAAAK4AAACCAAAArwAAALAAAACuAAAAsQAAALIAAACwAAAAswAAALQAAACyAAAAtQAAALYAAAC0AAAAtwAAALgAAAC2AAAAuQAAALoAAAC4AAAAuwAAALwAAACFAAAAvQAAAL4AAAC8AAAAvwAAAMAAAAC+AAAAwQAAAMIAAADAAAAAwwAAAMQAAADCAAAAxQAAAMYAAADEAAAAxwAAAMgAAADGAAAAyQAAAMoAAACOAAAAywAAAMwAAADKAAAAzQAAAM4AAADMAAAAzwAAANAAAADOAAAA0QAAANIAAADQAAAA0wAAANQAAADSAAAA1QAAANYAAADUAAAA1wAAAIsAAADYAAAAjAAAAIgAAADZAAAAiQAAAK0AAACIAAAA2gAAAKsAAACtAAAA2wAAAKoAAACrAAAA3AAAAKgAAACqAAAA3QAAAKcAAACoAAAA3gAAAKQAAACnAAAA3wAAAKUAAACkAAAA4AAAAIIAAADhAAAAgwAAALAAAADiAAAArgAAAOIAAACCAAAArgAAALQAAADjAAAAsgAAAOMAAACwAAAAsgAAALgAAADkAAAAtgAAAOQAAAC0AAAAtgAAALsAAAC4AAAA5QAAAIUAAADmAAAAhgAAAL4AAADnAAAAvAAAAOcAAACFAAAAvAAAAMIAAADoAAAAwAAAAOgAAAC+AAAAwAAAAMYAAADpAAAAxAAAAOkAAADCAAAAxAAAAMkAAADGAAAA6gAAAI4AAADrAAAAjwAAAMwAAADsAAAAygAAAOwAAACOAAAAygAAANAAAADtAAAAzgAAAO0AAADMAAAAzgAAANQAAADuAAAA0gAAAO4AAADQAAAA0gAAANcAAADUAAAA7wAAAPAAAADxAAAA8gAAAPMAAADwAAAA8gAAAPQAAAD1AAAA9gAAAPcAAAD4AAAA+QAAAPoAAAD3AAAA+QAAAPsAAAD8AAAA/QAAAP4AAAD7AAAA/QAAAP8AAAAAAQAAAQEAAP8AAAABAQAAAgEAAAMBAAAEAQAABQEAAAMBAAAFAQAABgEAAAcBAAAIAQAACQEAAAoBAAALAQAADAEAAA0BAAAOAQAADwEAABABAAARAQAAEgEAABMBAAAUAQAAFQEAABYBAAAXAQAAGAEAABkBAAAaAQAAGwEAABwBAAAdAQAAHgEAAB8BAAAcAQAAHgEAACABAAAhAQAAIgEAACMBAAAkAQAAJQEAACYBAAAjAQAAJQEAACcBAAAoAQAAKQEAACoBAAAnAQAAKQEAACsBAAAsAQAALQEAACsBAAAtAQAALgEAAC8BAAAwAQAAMQEAAC8BAAAxAQAAMgEAADMBAAA0AQAANQEAADYBAAA3AQAAOAEAADkBAAA6AQAAOwEAADwBAAA9AQAAPgEAAD8BAABAAQAAQQEAAEIBAABDAQAARAEAAEUBAABGAQAARwEAAEgBAABJAQAASgEAAEsBAABIAQAASgEAAEwBAABNAQAATgEAAE8BAABMAQAATgEAAFABAABRAQAAUgEAAFMBAABQAQAAUgEAAFQBAABVAQAAVgEAAFcBAABYAQAAWQEAAFoBAABbAQAAXAEAAF0BAABeAQAAXwEAAGABAABdAQAAXwEAAGEBAABiAQAAYwEAAGQBAABlAQAAZgEAAGcBAABkAQAAZgEAAGgBAABpAQAAagEAAGsBAABsAQAAbQEAAGwBAABuAQAAbwEAAG4BAABwAQAAbwEAAHABAABxAQAAcgEAAHEBAABzAQAAcgEAAHMBAAB0AQAAdQEAAHQBAABhAQAAdQEAAG0BAABsAQAAdgEAAG8BAABwAQAAdwEAAGwBAABvAQAAeAEAAHIBAABzAQAAeQEAAHABAAByAQAAegEAAHUBAABhAQAAewEAAHMBAAB1AQAAfAEAAH0BAAB+AQAAfwEAAH4BAACAAQAAgQEAAIIBAACDAQAAhAEAAIABAACCAQAAgQEAAIMBAACFAQAAhAEAAIUBAACGAQAAhwEAAIYBAABaAQAAhwEAAIgBAABUAQAAiQEAAIoBAACIAQAAiwEAAIwBAACKAQAAjQEAAI4BAACMAQAAjwEAAJABAACOAQAAkQEAAJIBAACQAQAAkwEAAJQBAACSAQAAlQEAAJYBAABXAQAAlwEAAJgBAACWAQAAmQEAAJoBAACYAQAAmwEAAJwBAACaAQAAnQEAAJ4BAACcAQAAnwEAAKABAACeAQAAoQEAAKIBAACgAQAAowEAAKQBAABoAQAApQEAAKYBAACkAQAApwEAAKgBAACmAQAAqQEAAKoBAACoAQAAqwEAAKwBAACqAQAArQEAAK4BAACsAQAArwEAALABAACuAQAAsQEAAGEBAACyAQAAYgEAAFoBAACzAQAAWwEAAIcBAABaAQAAtAEAAIUBAACHAQAAtQEAAIQBAACFAQAAtgEAAIIBAACEAQAAtwEAAIEBAACCAQAAuAEAAH4BAACBAQAAuQEAAH8BAAB+AQAAugEAAFQBAAC7AQAAVQEAAIoBAAC8AQAAiAEAALwBAABUAQAAiAEAAI4BAAC9AQAAjAEAAL0BAACKAQAAjAEAAJIBAAC+AQAAkAEAAL4BAACOAQAAkAEAAJUBAACSAQAAvwEAAFcBAADAAQAAWAEAAJgBAADBAQAAlgEAAMEBAABXAQAAlgEAAJwBAADCAQAAmgEAAMIBAACYAQAAmgEAAKABAADDAQAAngEAAMMBAACcAQAAngEAAKMBAACgAQAAxAEAAGgBAADFAQAAaQEAAKYBAADGAQAApAEAAMYBAABoAQAApAEAAKoBAADHAQAAqAEAAMcBAACmAQAAqAEAAK4BAADIAQAArAEAAMgBAACqAQAArAEAALEBAACuAQAAyQEAAMoBAADLAQAAzAEAAM0BAADOAQAAzwEAANABAADRAQAA0gEAANMBAADUAQAA1QEAANYBAADXAQAA2AEAANkBAADaAQAA2wEAANoBAADcAQAA3QEAANwBAADeAQAA3QEAAN4BAADfAQAA4AEAAN8BAADhAQAA4AEAAOEBAADiAQAA4wEAAOIBAADTAQAA4wEAANsBAADaAQAA5AEAAN0BAADeAQAA5QEAANoBAADdAQAA5gEAAOABAADhAQAA5wEAAN4BAADgAQAA6AEAAOMBAADTAQAA6QEAAOEBAADjAQAA6gEAAOsBAADsAQAA7QEAAOwBAADuAQAA7wEAAPABAADxAQAA8gEAAO4BAADwAQAA7wEAAPEBAADzAQAA8gEAAPMBAAD0AQAA9QEAAPQBAADQAQAA9QEAAPYBAADKAQAA9wEAAPgBAAD2AQAA+QEAAPoBAAD4AQAA+wEAAPwBAAD6AQAA/QEAAP4BAAD8AQAA/wEAAAACAAD+AQAAAQIAAAICAAAAAgAAAwIAAAQCAADNAQAABQIAAAYCAAAEAgAABwIAAAgCAAAGAgAACQIAAAoCAAAIAgAACwIAAAwCAAAKAgAADQIAAA4CAAAMAgAADwIAABACAAAOAgAAEQIAABICAADWAQAAEwIAABQCAAASAgAAFQIAABYCAAAUAgAAFwIAABgCAAAWAgAAGQIAABoCAAAYAgAAGwIAABwCAAAaAgAAHQIAAB4CAAAcAgAAHwIAANMBAAAgAgAA1AEAANABAAAhAgAA0QEAAPUBAADQAQAAIgIAAPMBAAD1AQAAIwIAAPIBAADzAQAAJAIAAPABAADyAQAAJQIAAO8BAADwAQAAJgIAAOwBAADvAQAAJwIAAO0BAADsAQAAKAIAAMoBAAApAgAAywEAAPgBAAAqAgAA9gEAACoCAADKAQAA9gEAAPwBAAArAgAA+gEAACsCAAD4AQAA+gEAAAACAAAsAgAA/gEAACwCAAD8AQAA/gEAAAMCAAAAAgAALQIAAM0BAAAuAgAAzgEAAAYCAAAvAgAABAIAAC8CAADNAQAABAIAAAoCAAAwAgAACAIAADACAAAGAgAACAIAAA4CAAAxAgAADAIAADECAAAKAgAADAIAABECAAAOAgAAMgIAANYBAAAzAgAA1wEAABQCAAA0AgAAEgIAADQCAADWAQAAEgIAABgCAAA1AgAAFgIAADUCAAAUAgAAFgIAABwCAAA2AgAAGgIAADYCAAAYAgAAGgIAAB8CAAAcAgAANwIAADgCAAA5AgAAOgIAADsCAAA4AgAAOgIAADwCAAA9AgAAPgIAAD8CAABAAgAAQQIAAEICAAA/AgAAQQIAAEMCAABEAgAARQIAAEYCAABDAgAARQIAAEcCAABIAgAASQIAAEcCAABJAgAASgIAAEsCAABMAgAATQIAAEsCAABNAgAATgIAAE8CAABQAgAAUQIAAFICAABTAgAAVAIAAFUCAABWAgAAVwIAAFgCAABZAgAAWgIAAFsCAABcAgAAXQIAAF4CAABfAgAAYAIAAGECAABiAgAAYwIAAGQCAABlAgAAZgIAAGcCAABkAgAAZgIAAGgCAABpAgAAagIAAGsCAABsAgAAbQIAAG4CAABrAgAAbQIAAG8CAABwAgAAcQIAAHICAABvAgAAcQIAAHMCAAB0AgAAdQIAAHMCAAB1AgAAdgIAAHcCAAB4AgAAeQIAAHcCAAB5AgAAegIAAHsCAAB8AgAAfQIAAH4CAAB/AgAAgAIAAIECAACCAgAAgwIAAIQCAACFAgAAhgIAAIcCAACIAgAAiQIAAIoCAACLAgAAjAIAAI0CAACOAgAAjwIAAA==",
    "R15compTorso": "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AB4AAAAgAAAAIQAAACIAAAAjAAAAJAAAACUAAAAmAAAAJwAAACUAAAAnAAAAKAAAACkAAAAqAAAAKwAAACwAAAAtAAAALgAAAC8AAAAwAAAAMQAAADIAAAAzAAAANAAAADUAAAA2AAAANwAAADgAAAA5AAAAOgAAADsAAAA8AAAAPQAAAD4AAAA/AAAAQAAAAD4AAABAAAAAQQAAAEIAAABDAAAARAAAAEIAAABEAAAARQAAAEYAAABHAAAASAAAAEYAAABIAAAASQAAAEoAAABLAAAATAAAAE0AAABOAAAATwAAAFAAAABRAAAAUgAAAFAAAABSAAAAUwAAAFQAAABVAAAAVgAAAFQAAABWAAAAVwAAAFgAAABZAAAAWgAAAFgAAABaAAAAWwAAAFwAAABdAAAAXgAAAF8AAABgAAAAYQAAAF8AAABhAAAAYgAAAGMAAABkAAAAZQAAAGMAAABlAAAAZgAAAGcAAABoAAAAaQAAAGcAAABpAAAAagAAAGsAAABsAAAAbQAAAGsAAABtAAAAbgAAAG8AAABwAAAAcQAAAG8AAABxAAAAcgAAAHMAAAB0AAAAdQAAAHYAAAB3AAAAeAAAAHYAAAB4AAAAeQAAAHoAAAB7AAAAfAAAAH0AAAB+AAAAfwAAAH0AAAB/AAAAgAAAAIEAAACCAAAAgwAAAIEAAACDAAAAhAAAAIUAAACGAAAAhwAAAIUAAACHAAAAiAAAAIkAAACKAAAAiwAAAIwAAACNAAAAjgAAAI8AAACQAAAAkQAAAJIAAACTAAAAlAAAAJIAAACUAAAAlQAAAJYAAACXAAAAmAAAAJYAAACYAAAAmQAAAJoAAACbAAAAnAAAAJoAAACcAAAAnQAAAJ4AAACfAAAAoAAAAKEAAACiAAAAowAAAKEAAACjAAAApAAAAKUAAACmAAAApwAAAKgAAACpAAAAqgAAAKgAAACqAAAAqwAAAKwAAACtAAAArgAAAK8AAACwAAAAsQAAAK8AAACxAAAAsgAAALMAAAC0AAAAtQAAALMAAAC1AAAAtgAAALcAAAC4AAAAuQAAALcAAAC5AAAAugAAALsAAAC8AAAAvQAAAL4AAAC/AAAAwAAAAMEAAADCAAAAwwAAAMQAAADFAAAAxgAAAMcAAADIAAAAyQAAAMoAAADLAAAAzAAAAM0AAADOAAAAzwAAANAAAADRAAAA0gAAANMAAADUAAAA1QAAANYAAADXAAAA2AAAANkAAADaAAAA2wAAAA==",
    "R15compLeftArm": "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AAgBAAAJAQAACgEAAAgBAAAKAQAACwEAAAwBAAANAQAADgEAAAwBAAAOAQAADwEAABABAAARAQAAEgEAABABAAASAQAAEwEAABQBAAAVAQAAFgEAABcBAAAYAQAAGQEAABoBAAAbAQAAHAEAAB0BAAAeAQAAHwEAAB0BAAAfAQAAIAEAACEBAAAiAQAAIwEAACEBAAAjAQAAJAEAACUBAAAmAQAAJwEAACUBAAAnAQAAKAEAACkBAAAqAQAAKwEAACwBAAAtAQAALgEAACwBAAAuAQAALwEAADABAAAxAQAAMgEAADMBAAA0AQAANQEAADYBAAA3AQAAOAEAADYBAAA4AQAAOQEAADoBAAA7AQAAPAEAAD0BAAA+AQAAPwEAAEABAABBAQAAQgEAAEMBAABEAQAARQEAAEYBAABHAQAASAEAAEkBAABKAQAASwEAAEwBAABNAQAATgEAAEwBAABOAQAATwEAAFABAABRAQAAUgEAAFABAABSAQAAUwEAAFQBAABVAQAAVgEAAFcBAABYAQAAWQEAAFoBAABbAQAAXAEAAF0BAABeAQAAXwEAAGABAABhAQAAYgEAAGMBAABkAQAAZQEAAGYBAABnAQAAaAEAAGkBAABqAQAAawEAAGwBAABtAQAAbgEAAG8BAABwAQAAcQEAAHIBAABzAQAAdAEAAHIBAAB0AQAAdQEAAHYBAAB3AQAAeAEAAHYBAAB4AQAAeQEAAHoBAAB7AQAAfAEAAHoBAAB8AQAAfQEAAH4BAAB/AQAAgAEAAIEBAACCAQAAgwEAAIEBAACDAQAAhAEAAIUBAACGAQAAhwEAAIgBAACJAQAAigEAAIsBAACMAQAAjQEAAI4BAACPAQAAkAEAAJEBAACSAQAAkwEAAJQBAACVAQAAlgEAAJcBAACYAQAAmQEAAJoBAACbAQAAnAEAAJ0BAACeAQAAnwEAAKABAAChAQAAogEAAKMBAACkAQAApQEAAKYBAACnAQAAqAEAAKYBAACoAQAAqQEAAKoBAACrAQAArAEAAKoBAACsAQAArQEAAK4BAACvAQAAsAEAAK4BAACwAQAAsQEAALIBAACzAQAAtAEAALUBAAC2AQAAtwEAALUBAAC3AQAAuAEAALkBAAC6AQAAuwEAALwBAAC9AQAAvgEAAL8BAADAAQAAwQEAAMIBAADDAQAAxAEAAMUBAADGAQAAxwEAAMgBAADJAQAAygEAAMsBAADMAQAAzQEAAA==",
    "R15compRightArm": "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ABIBAAATAQAAFAEAABIBAAAUAQAAFQEAABYBAAAXAQAAGAEAABYBAAAYAQAAGQEAABoBAAAbAQAAHAEAABoBAAAcAQAAHQEAAB4BAAAfAQAAIAEAAB4BAAAgAQAAIQEAACIBAAAjAQAAJAEAACIBAAAkAQAAJQEAACYBAAAnAQAAKAEAACkBAAAqAQAAKwEAACkBAAArAQAALAEAAC0BAAAuAQAALwEAAC0BAAAvAQAAMAEAADEBAAAyAQAAMwEAADEBAAAzAQAANAEAADUBAAA2AQAANwEAADgBAAA5AQAAOgEAADgBAAA6AQAAOwEAADwBAAA9AQAAPgEAAD8BAABAAQAAQQEAAEIBAABDAQAARAEAAEUBAABGAQAARwEAAEgBAABJAQAASgEAAEgBAABKAQAASwEAAEwBAABNAQAATgEAAEwBAABOAQAATwEAAFABAABRAQAAUgEAAFABAABSAQAAUwEAAFQBAABVAQAAVgEAAFcBAABYAQAAWQEAAFcBAABZAQAAWgEAAFsBAABcAQAAXQEAAF4BAABfAQAAYAEAAGEBAABiAQAAYwEAAGQBAABlAQAAZgEAAGQBAABmAQAAZwEAAGgBAABpAQAAagEAAGgBAABqAQAAawEAAGwBAABtAQAAbgEAAG8BAABwAQAAcQEAAG8BAABxAQAAcgEAAHMBAAB0AQAAdQEAAHMBAAB1AQAAdgEAAHcBAAB4AQAAeQEAAHcBAAB5AQAAegEAAHsBAAB8AQAAfQEAAHsBAAB9AQAAfgEAAH8BAACAAQAAgQEAAIIBAACDAQAAhAEAAIIBAACEAQAAhQEAAIYBAACHAQAAiAEAAIkBAACKAQAAiwEAAIwBAACNAQAAjgEAAIwBAACOAQAAjwEAAJABAACRAQAAkgEAAJABAACSAQAAkwEAAJQBAACVAQAAlgEAAJQBAACWAQAAlwEAAJgBAACZAQAAmgEAAJsBAACcAQAAnQEAAJ4BAACfAQAAoAEAAKEBAACiAQAAowEAAKQBAAClAQAApgEAAKcBAACoAQAAqQEAAKoBAACrAQAArAEAAK0BAACuAQAArwEAALABAACxAQAAsgEAALMBAAC0AQAAtQEAALYBAAC3AQAAuAEAALkBAAC6AQAAuwEAALwBAAC9AQAAvgEAAL8BAADAAQAAwQEAAMIBAADDAQAAxAEAAMUBAADGAQAAxwEAAMgBAADJAQAAygEAAMsBAADMAQAAzQEAAA==",
}

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
