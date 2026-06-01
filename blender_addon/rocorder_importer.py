bl_info = {
    "name": "ROCORDER Replay Importer",
    "author": "ROCORDER",
    "version": (1, 19, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Roblox Replay (.rec)",
    "description": "Import ROCORDER .rec replays as skinned, animated armatures",
    "warning": "Alpha — file formats and options may still change",
    "category": "Import-Export",
}
ROCORDER_VERSION = "1.19.0-alpha"

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


def _add_mesh_geometry(bm, uv_layer, mesh, place_mat, part, scale, flip_v=True,
                      r6_clothing_regions=None):
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
            loop[uv_layer].uv = (u, (1.0 - v) if flip_v else v)

    # Replace mesh-authored UVs with R6 clothing-template cube projection so
    # Shirt/Pants wraps a CharacterMesh body the same way it wraps the
    # standard R6 box body. Must run BEFORE place_mat — projection is in
    # part-local space (origin at the bone), not rest pose.
    if r6_clothing_regions is not None:
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


def _add_primitive_local(bm, uv_layer, part, scale, decal_axis=None):
    """Build a box / ball / head / wedge for a classic Part at the origin in
    Blender-local coords (NOT yet placed). If decal_axis is given, the face
    pointing that way gets material slot 1 and a planar 0..1 UV (so a face
    decal / logo shows on the right side). Returns the new BMVerts."""
    shape = part.get("shape") or "Block"
    is_head = (part.get("meshType") == "Head")
    sx, sy, sz = (float(s) for s in part.get("size", [1.0, 1.0, 1.0]))

    if shape == "Ball" or is_head:
        # Classic Head is a Block part with a SpecialMesh MeshType=Head — it
        # renders as a rounded head in-game, NOT a cube. Approximate with a
        # sphere fit to the part size; project the face decal onto the front.
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


def _build_part_object(part, name, place, scale, assets, import_meshes,
                       arm_obj, collection, log, clothing=None,
                       apply_clothing=False):
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
    if import_meshes and assets is not None:
        if part.get("meshId"):
            mesh_data = assets.get_mesh(part.get("meshId"))
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
            regions, cloth_img = _clothing_for_part(name, clothing, assets)
            if regions and cloth_img:
                r6_clothing_regions = regions
                body_tex = cloth_img
        if body_tex is None:
            tref = part.get("textureId") or part.get("colorMap")
            if tref:
                body_tex = assets.get_image_path(tref)

    verts = None
    if mesh_data:
        verts = _add_mesh_geometry(bm, uv_layer, mesh_data, place, part, scale,
                                   r6_clothing_regions=r6_clothing_regions)
    if verts:
        kind = "mesh-clothed" if r6_clothing_regions else "mesh"
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
                 assets=None, import_meshes=False, apply_clothing=False):
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
                                       import_meshes, arm_obj, target, log,
                                       clothing=player_rig.get("clothing"),
                                       apply_clothing=apply_clothing)
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
                  debug, report, apply_classic_clothing=True):
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
                             apply_clothing=apply_classic_clothing)
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

    # Per-life visibility keyframes. Only players with >1 life get them: the
    # life is visible during [fromT, toT] and hidden outside. Without these,
    # all of a player's lives would render simultaneously on top of each
    # other. The visibility is keyed on hide_viewport and hide_render with
    # CONSTANT (step) interpolation so it switches instantly at the boundary.
    for (uid, life_idx), built in players.items():
        li = built.get("life_info")
        if not li or li["n_lives"] <= 1:
            continue
        arm = built["arm"]
        fromT = li["fromT"]
        toT = li["toT"]
        # Frame numbers (clamped into scene range). +1 because frame_num
        # in the frame loop is `int(round(t * fps)) + 1`.
        f_from = max(1, int(round(fromT * fps)) + 1)
        f_to = (int(round(toT * fps)) + 1) if toT is not None else (last_frame + 1)
        # Helper: keyframe both hide_viewport and hide_render at `frame` with
        # the given value, then force CONSTANT interpolation on those keys.
        def _set_vis(obj, frame, hidden):
            obj.hide_viewport = hidden
            obj.hide_render = hidden
            obj.keyframe_insert(data_path="hide_viewport", frame=frame)
            obj.keyframe_insert(data_path="hide_render", frame=frame)
        # Before life: hidden.
        if f_from > 1:
            _set_vis(arm, 1, True)
        # Life begins.
        _set_vis(arm, f_from, False)
        # Life ends — only if it actually closed (toT not None).
        if toT is not None and f_to <= last_frame + 1:
            _set_vis(arm, f_to, True)
        # Step interpolation so visibility flips instantly at the boundary.
        if arm.animation_data and arm.animation_data.action:
            for fc in arm.animation_data.action.fcurves:
                if fc.data_path in ("hide_viewport", "hide_render"):
                    for kp in fc.keyframe_points:
                        kp.interpolation = "CONSTANT"

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
