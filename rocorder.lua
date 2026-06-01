--!nocheck
-- ROCORDER — replay recorder for Xeno (and other executors with file IO).
--
-- Tk-style in-game UI: record/instant-replay/settings/files/sources tabs.
-- Single-source-of-truth settings live on disk in ROCORDER/settings.json so
-- they survive reloads.
--
-- File format identifiers (unchanged from 1.0):
--   .rec       ROCORDER/3      — line 1 JSON header, then
--                                 t=<sec>;<uid>:p0|p1|...;<uid>:...
--                                 each pK = px,py,pz,qx,qy,qz,qw (positional)
--   .rig.json  ROCORDER-RIG/2  — per-player rig (parts ordered + Motor6D C0/C1)
--   .debug.log diagnostic events (toggle via Settings > Capture > Debug)

local ROCORDER_VERSION = "1.9.19-alpha"

if _G.ROCORDER then
    print("[ROCORDER] reload guard: tearing down previous instance v"
        .. tostring(_G.ROCORDER.version or "?"))
    if _G.ROCORDER.Stop then pcall(function() _G.ROCORDER:Stop() end) end
    if _G.ROCORDER._destroy then pcall(function() _G.ROCORDER:_destroy() end) end
    _G.ROCORDER = nil
end

local Players            = game:GetService("Players")
local RunService         = game:GetService("RunService")
local UserInputService   = game:GetService("UserInputService")
local StarterGui         = game:GetService("StarterGui")
local HttpService        = game:GetService("HttpService")
local TweenService       = game:GetService("TweenService")
local MarketplaceService = game:GetService("MarketplaceService")
local AssetService       = game:GetService("AssetService")
local ContentProvider    = game:GetService("ContentProvider")

local fmt = string.format

----------------------------------------------------------------
-- Executor IO probes
----------------------------------------------------------------
local writefile  = writefile or (syn and syn.writefile)
local appendfile = appendfile
local readfile   = readfile or (syn and syn.readfile)
local isfile     = isfile
local isfolder   = isfolder
local makefolder = makefolder
local listfiles  = listfiles
local delfile    = delfile

local function ioOK(...)
    for _, fn in ipairs({...}) do if not fn then return false end end
    return true
end

if not ioOK(writefile, appendfile, makefolder, isfolder) then
    warn("[ROCORDER] Executor missing required file IO.")
    return
end

local FOLDER = "ROCORDER"
local ASSETS_FOLDER = FOLDER .. "/assets"
if not isfolder(FOLDER) then makefolder(FOLDER) end

-- Executor HTTP request function (for authenticated asset downloads). These
-- run in the real client session, so they reach assets Blender's anonymous
-- urllib can't (it gets 401). Names vary across executors.
local httpRequest = (syn and syn.request)
    or (http and http.request)
    or http_request
    or request
    or (fluxus and fluxus.request)

-- Names of executor functions that return the bytes of an asset the client
-- already has loaded. Different executors expose different names; we probe all.
local getCustomAsset  = getcustomasset or get_custom_asset
    or (syn and syn.protect_gui and syn.getcustomasset)
local getSynAsset     = getsynasset
local readCustomAsset = readcustomasset    -- direct bytes (rare)
local readAsset       = readasset           -- some forks
local getAssetBytes   = (Xeno and Xeno.getAssetBytes) or get_asset_bytes
local _diagPrinted    = false

-- Try every in-memory path we know about. The DIAG on first call now ALWAYS
-- prints — success, error, or wrong type — so we can see what the executor
-- actually does. The previous version hid failures, which is why the log
-- showed no DIAG line at all even though getcustomasset=yes.
local function readFromContentProvider(assetId, dbg)
    local rbxUrl = "rbxassetid://" .. assetId

    -- 1) Direct bytes getters.
    for fname, fn in pairs({
        readcustomasset = readCustomAsset, readasset = readAsset,
        getAssetBytes   = getAssetBytes,
    }) do
        if fn then
            local ok, data = pcall(fn, rbxUrl)
            if not _diagPrinted and dbg then
                _diagPrinted = true
                if ok then
                    dbg(fmt("  DIAG: %s(%s) -> type=%s len=%s",
                        fname, rbxUrl, type(data),
                        tostring(type(data) == "string" and #data)))
                else
                    dbg(fmt("  DIAG: %s(%s) -> ERRORED: %s",
                        fname, rbxUrl, tostring(data)))
                end
            end
            if ok and type(data) == "string" and #data > 0 then
                return data, fname
            end
        end
    end

    -- 2) Path-returning forms.
    for fname, fn in pairs({
        getcustomasset = getCustomAsset, getsynasset = getSynAsset,
    }) do
        if fn then
            local ok, localPath = pcall(fn, rbxUrl)
            if not _diagPrinted and dbg then
                _diagPrinted = true
                if ok then
                    local typ = type(localPath)
                    local sample = (typ == "string") and localPath:sub(1, 200) or "<not string>"
                    dbg(fmt("  DIAG: %s(%s) -> type=%s value='%s'",
                        fname, rbxUrl, typ, sample))
                else
                    dbg(fmt("  DIAG: %s(%s) -> ERRORED: %s",
                        fname, rbxUrl, tostring(localPath)))
                end
            end
            if ok and type(localPath) == "string" and #localPath > 0 then
                if readfile then
                    -- The returned path could be:
                    --   "rbxasset://Xeno/asset_<id>.bin"
                    --   "rbxasset://temp/<file>"
                    --   "rbxasset://<file>"
                    --   "content://..."
                    --   raw filesystem path
                    -- Try all common shapes — first reader to succeed wins.
                    local candidates = { localPath }
                    local stripped = localPath:gsub("^rbxasset://", "")
                    if stripped ~= localPath then
                        candidates[#candidates + 1] = stripped
                    end
                    candidates[#candidates + 1] = "asset_" .. assetId .. ".bin"
                    candidates[#candidates + 1] = assetId
                    for _, p in ipairs(candidates) do
                        local okr, data = pcall(readfile, p)
                        if okr and type(data) == "string" and #data > 0 then
                            if dbg then
                                dbg(fmt("  DIAG: SUCCESS via %s using readfile('%s') %d bytes",
                                    fname, p:sub(1, 80), #data))
                            end
                            return data, fname .. ":" .. p:sub(1, 60)
                        end
                    end
                    if dbg then
                        dbg(fmt("  DIAG: %s returned '%s' but readfile failed "
                            .. "for all %d candidate shapes for asset %s",
                            fname, localPath:sub(1, 80), #candidates, assetId))
                    end
                end
            end
        end
    end
    return nil
end

-- Forward-declared so rec methods (defined before the UI section) can call
-- Indicator:refresh() — the actual table is assigned later in the UI section.
local Indicator

----------------------------------------------------------------
-- Settings: defs, defaults, load/save
----------------------------------------------------------------
-- Each def: key, type (number/bool/key/choice), default, label, desc, group,
--           plus per-type extras (min, max, int for numbers; choices for choice).
-- An item flagged advanced=true is hidden behind the Settings tab's
-- "Show advanced settings" toggle so casual users aren't drowned in knobs.
local SETTING_DEFS = {
    -- Capture: basics first, advanced last
    { key="TICK_RATE",       type="number", default=30,   label="Tick Rate (Hz)",
      desc="Samples per second.",                                  group="Capture",
      min=1,   max=240, int=true },
    { key="MAX_DISTANCE",    type="number", default=0,    label="Max Distance (studs)",
      desc="Skip players beyond this distance from the camera. 0 = unlimited.",
      group="Capture",
      min=0,   max=100000, int=true },
    { key="INCLUDE_LOCAL",   type="bool",   default=true, label="Include Local Player",
      desc="Record yourself too.",                                 group="Capture" },
    { key="DEBUG",           type="bool",   default=true, label="Write Debug Log",
      desc="Write a verbose .debug.log next to each recording.",   group="Capture" },
    { key="DOWNLOAD_ASSETS", type="bool",   default=true, label="Download Assets",
      desc="At Stop, download every mesh/texture the characters use into "
        .. "ROCORDER/assets so Blender can build real models offline. Uses "
        .. "the executor's authenticated session (works where Blender's "
        .. "anonymous downloads get 401'd).",
      group="Capture" },
    { key="POS_PRECISION",   type="number", default=3,    label="Position Decimals",
      desc="Decimal places for positions (studs).",                group="Capture",
      min=0,   max=6,   int=true, advanced=true },
    { key="ROT_PRECISION",   type="number", default=5,    label="Rotation Decimals",
      desc="Decimal places for quaternion components.",            group="Capture",
      min=0,   max=8,   int=true, advanced=true },
    { key="FLUSH_INTERVAL",  type="number", default=0.5,  label="Flush Interval (s)",
      desc="Seconds between disk writes.",                         group="Capture",
      min=0.05, max=10, advanced=true },
    { key="MAX_CATCHUP_SEC", type="number", default=5.0,  label="Max Catchup (s)",
      desc="Cap on backfilled frames after a stall.",              group="Capture",
      min=0,   max=60, advanced=true },

    -- Instant Replay
    { key="IR_ENABLED",      type="bool",   default=false, label="Instant Replay",
      desc="Continuously buffer recent data in memory. Press the Save Replay "
        .. "hotkey (or the button) to dump the last N seconds to disk.",
      group="Instant Replay" },
    { key="IR_BUFFER_SEC",   type="number", default=30,   label="Buffer Length (s)",
      desc="How many seconds to keep in the rolling buffer.",      group="Instant Replay",
      min=5,   max=600, int=true },

    -- Indicator overlay (small dot in a screen corner while capturing)
    { key="INDICATOR_ENABLED", type="bool",   default=true, label="Show indicator",
      desc="A small dot in a screen corner — red while recording, white while "
        .. "Instant Replay is buffering. Sits at low opacity to stay out of the way.",
      group="Indicator" },
    { key="INDICATOR_CORNER",  type="choice", default="TopRight", label="Corner",
      desc="Which corner of the screen the indicator appears in.",
      group="Indicator",
      choices={ "TopLeft", "TopRight", "BottomLeft", "BottomRight" } },

    -- Hotkeys
    { key="HOTKEY_RECORD",       type="key", default="F8",        label="Record Toggle",
      desc="Starts / stops a full recording.",                    group="Hotkeys" },
    { key="HOTKEY_UI",           type="key", default="RightShift",label="Open UI",
      desc="Shows / hides this window.",                          group="Hotkeys" },
    { key="HOTKEY_SAVE_REPLAY",  type="key", default="F7",        label="Save Instant Replay",
      desc="Dumps the rolling buffer (when Instant Replay is on).",group="Hotkeys" },

    -- Sources (managed via the Sources tab, not the Settings tab)
    { key="SRC_PLAYER_PARTS", type="bool", default=true,  label="Player parts",
      desc="Per-tick world position + quaternion of every BasePart in each character.",
      group="Sources" },
    { key="SRC_CAMERA",       type="bool", default=false, label="Player camera",
      desc="Per-tick CFrame + FOV of the local camera. Imports as a Blender camera.",
      group="Sources" },
}

-- Groups that do NOT belong on the Settings tab (managed elsewhere).
local SETTINGS_TAB_OMIT = { Sources = true }

local SETTINGS_PATH = FOLDER .. "/settings.json"

local function loadSettings()
    local cfg = {}
    for _, d in ipairs(SETTING_DEFS) do cfg[d.key] = d.default end
    if isfile and readfile and isfile(SETTINGS_PATH) then
        local ok, body = pcall(readfile, SETTINGS_PATH)
        if ok and type(body) == "string" then
            local ok2, data = pcall(function() return HttpService:JSONDecode(body) end)
            if ok2 and type(data) == "table" then
                for _, d in ipairs(SETTING_DEFS) do
                    local v = data[d.key]
                    if v ~= nil and type(v) == type(d.default) then
                        if d.type == "choice" then
                            -- only accept values that are still in the allowed list
                            for _, c in ipairs(d.choices or {}) do
                                if c == v then cfg[d.key] = v; break end
                            end
                        else
                            cfg[d.key] = v
                        end
                    end
                end
            end
        end
    end
    return cfg
end

local function saveSettings(cfg)
    pcall(function()
        writefile(SETTINGS_PATH, HttpService:JSONEncode(cfg))
    end)
end

local function defByKey(key)
    for _, d in ipairs(SETTING_DEFS) do if d.key == key then return d end end
end

local function settingsByGroup(omit)
    omit = omit or {}
    local groups = {}
    local order = {}
    for _, d in ipairs(SETTING_DEFS) do
        if not omit[d.group] then
            if not groups[d.group] then
                groups[d.group] = {}
                order[#order + 1] = d.group
            end
            table.insert(groups[d.group], d)
        end
    end
    return order, groups
end

local function settingsForGroup(name)
    local list = {}
    for _, d in ipairs(SETTING_DEFS) do
        if d.group == name then list[#list + 1] = d end
    end
    return list
end

----------------------------------------------------------------
-- Encoding helpers
----------------------------------------------------------------
local function sanitizeName(s)
    return (s:gsub("[;:=,|]", "_"))
end

-- MarketplaceService:GetProductInfo yields and makes an HTTP call. Cache the
-- result so we don't refetch on every recording, replay save, or Files refresh.
local gameNameCache = {}
local function lookupGameName(placeId)
    if gameNameCache[placeId] ~= nil then
        local v = gameNameCache[placeId]
        if v == false then return nil end
        return v
    end
    local ok, info = pcall(function()
        return MarketplaceService:GetProductInfo(placeId)
    end)
    if ok and type(info) == "table" and info.Name and info.Name ~= "" then
        gameNameCache[placeId] = info.Name
        return info.Name
    end
    gameNameCache[placeId] = false  -- negative cache (don't keep retrying)
    return nil
end

local function matrixToQuat(m00,m01,m02,m10,m11,m12,m20,m21,m22)
    local trace = m00 + m11 + m22
    local qw,qx,qy,qz
    if trace > 0 then
        local s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25*s; qx = (m21-m12)/s; qy = (m02-m20)/s; qz = (m10-m01)/s
    elseif m00 > m11 and m00 > m22 then
        local s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        qw = (m21-m12)/s; qx = 0.25*s; qy = (m01+m10)/s; qz = (m02+m20)/s
    elseif m11 > m22 then
        local s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        qw = (m02-m20)/s; qx = (m01+m10)/s; qy = 0.25*s; qz = (m12+m21)/s
    else
        local s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        qw = (m10-m01)/s; qx = (m02+m20)/s; qy = (m12+m21)/s; qz = 0.25*s
    end
    return qx,qy,qz,qw
end

-- Camera source: encodes workspace.CurrentCamera as a single 'cam:' chunk
-- appended to a frame line. Backward compatible — old importers (and the
-- 1.0 importer) skip unknown chunks silently.
local function captureCameraStr(encode, posPrec)
    local cam = workspace.CurrentCamera
    if not cam then return nil end
    local cfStr = encode(cam.CFrame)
    return fmt("cam:%s,%." .. posPrec .. "f", cfStr, cam.FieldOfView)
end

----------------------------------------------------------------
-- Asset extractor — pull mesh/image bytes from the engine's loaded copy
-- (EditableMesh / EditableImage) instead of asking the CDN. This works for
-- any asset currently rendering in-game, including UGC the CDN refuses.
----------------------------------------------------------------
local EXTRACT_OK = typeof(AssetService.CreateEditableMeshAsync) == "function"
              and typeof(AssetService.CreateEditableImageAsync) == "function"

-- Asset IDs we've already attempted to extract this session (success or fail).
-- Persisted in _G so it survives reloads.
_G.ROCORDER_EXTRACTED = _G.ROCORDER_EXTRACTED or {}
local EXTRACTED = _G.ROCORDER_EXTRACTED

local function _assetIdFromRef(ref)
    if not ref then return nil end
    local s = tostring(ref)
    return s:match("(%d%d%d%d+)")
end

local function _geomPath(id) return ASSETS_FOLDER .. "/" .. id .. ".geom.json" end
local function _imgPath(id)  return ASSETS_FOLDER .. "/" .. id .. ".rgba"      end
local function _binPath(id)  return ASSETS_FOLDER .. "/" .. id              end

-- True if any form of this asset is already cached on disk OR has been
-- extracted during this session (the in-memory flag covers the brief race
-- where the queue worker has written the file but a downstream pass checks
-- before the filesystem cache catches up).
local function _isCached(id)
    if not id then return false end
    if EXTRACTED[id] then return true end
    if not isfile then return false end
    return isfile(_geomPath(id)) or isfile(_imgPath(id)) or isfile(_binPath(id))
end

-- ============================================================================
-- Frame-rate-aware pacing for the extractor.
--
-- The earlier "yield every 500 verts / every entry" policy meant a single
-- ReadPixelsBuffer of a 1024x1024 texture (4 MB pulled atomically) or a 5000-
-- vertex mesh could chew ~50 ms in one go — visible as stutters even on a 144
-- fps client. We now:
--
--   1. Sample the actual frame delta via Heartbeat. A single global handler
--      under _G survives reloads (each load cleans up the previous one).
--   2. Before each chunk, ask paceExtractor() whether to yield. If the
--      previous frame was below ~45 fps, yield TWO frames to give the game
--      breathing room. Otherwise, take ~3 ms slices.
--
-- The trade-off: extraction takes longer in wall-clock (often 2-3x), but the
-- game doesn't stutter. Per user preference: smooth game > fast extraction.
-- ============================================================================
_G.ROCORDER_FRAME_DELTA = _G.ROCORDER_FRAME_DELTA or (1/60)
if _G.ROCORDER_FRAME_DELTA_HOOK then
    pcall(_G.ROCORDER_FRAME_DELTA_HOOK)
end
do
    local conn = RunService.Heartbeat:Connect(function(dt)
        _G.ROCORDER_FRAME_DELTA = dt
    end)
    _G.ROCORDER_FRAME_DELTA_HOOK = function()
        pcall(function() conn:Disconnect() end)
    end
end

-- ============================================================================
-- Actor-based extractor scaffolding.
--
-- The 1.9.15-1.9.17 saga proved a coroutine that desyncs isn't enough —
-- Roblox's editable APIs check whether the calling Script is parented to
-- an Actor specifically, not just whether the thread is desynchronized.
-- So we need a real Actor + Script setup. That requires writing to
-- `Script.Source`, which is normally read-only — but most executors
-- (including Xeno) patch that security check.
--
-- This block is a STAGED PROBE (1.9.18+): it only attempts to set up the
-- scaffold and verify communication round-trips. Actual extraction
-- offloading happens in a follow-up version once we confirm the probe
-- works. Probe steps:
--   1. Create Actor + worker LocalScript + two BindableEvents (job in,
--      result out) under it.
--   2. Try to write a known source to Script.Source.
--   3. Verify the write stuck by reading it back.
--   4. Parent the script to start it.
--   5. Send a "ping" job.
--   6. Wait up to 2 s for a "pong" response.
--   7. Print the result.
--
-- On any failure, the partial scaffold is destroyed and we fall back to
-- serial extraction. Sets _G.ROCORDER_ACTOR_OK = true iff the round-trip
-- worked.
-- ============================================================================
_G.ROCORDER_ACTOR_OK = false
_G.ROCORDER_ACTOR_ERR = nil
_G.ROCORDER_ACTOR_DETACH = _G.ROCORDER_ACTOR_DETACH or function() end
do
    -- Clean up any actor from a prior script load (Xeno re-execute case).
    pcall(_G.ROCORDER_ACTOR_DETACH)
    local stale = workspace:FindFirstChild("_ROCORDER_ExtractorActor")
    if stale then pcall(function() stale:Destroy() end) end

    local actor = Instance.new("Actor")
    actor.Name = "_ROCORDER_ExtractorActor"

    local jobEvent = Instance.new("BindableEvent")
    jobEvent.Name = "Job"
    jobEvent.Parent = actor

    local resultEvent = Instance.new("BindableEvent")
    resultEvent.Name = "Result"
    resultEvent.Parent = actor

    local readyEvent = Instance.new("BindableEvent")
    readyEvent.Name = "Ready"
    readyEvent.Parent = actor

    actor.Parent = workspace

    local workerScript = Instance.new("LocalScript")
    workerScript.Name = "Worker"

    -- Worker source. Inside an Actor's script, `task.desynchronize()` is
    -- a legitimate parallel context — and the editable APIs accept it.
    -- The worker:
    --   1. Signals "ready" on first run so the main thread knows the
    --      Source took.
    --   2. Listens for jobs on Job event.
    --   3. Handles ping (returns "pong" immediately) and extract jobs
    --      (does CreateEditable*Async + reads in desync, posts result).
    local workerSource = [==[
        local actor = script.Parent
        local jobEvent = actor:WaitForChild("Job")
        local resultEvent = actor:WaitForChild("Result")
        local readyEvent = actor:WaitForChild("Ready")
        local AssetService = game:GetService("AssetService")

        -- Signal ready BEFORE any heavy work so main thread knows the
        -- Source successfully ran. main thread waits up to 2 s for this.
        readyEvent:Fire("ready")

        jobEvent.Event:Connect(function(jobId, kind, payload)
            if kind == "ping" then
                resultEvent:Fire(jobId, true, "pong")
                return
            end

            if kind == "image" then
                local content = payload
                local ok, ei
                task.desynchronize()
                ok, ei = pcall(function()
                    return AssetService:CreateEditableImageAsync(content)
                end)
                if not ok or not ei then
                    task.synchronize()
                    resultEvent:Fire(jobId, false,
                        "CreateEditableImageAsync: " .. tostring(ei))
                    return
                end
                local sz = ei.Size
                local w = math.floor(sz.X); local h = math.floor(sz.Y)
                if w <= 0 or h <= 0 then
                    task.synchronize()
                    resultEvent:Fire(jobId, false, "zero-size image")
                    return
                end
                local buf
                local okBuf = pcall(function()
                    buf = ei:ReadPixelsBuffer(Vector2.new(0, 0), sz)
                end)
                task.synchronize()
                if not okBuf or not buf then
                    resultEvent:Fire(jobId, false, "ReadPixelsBuffer failed")
                    return
                end
                local data
                pcall(function() data = buffer.tostring(buf) end)
                if not data then
                    resultEvent:Fire(jobId, false, "buffer.tostring failed")
                    return
                end
                resultEvent:Fire(jobId, true,
                    string.format("ROCORDER-RGBA8\n%d\n%d\n", w, h) .. data)
                return
            end

            if kind == "mesh" then
                local content = payload
                local ok, em
                task.desynchronize()
                ok, em = pcall(function()
                    return AssetService:CreateEditableMeshAsync(content)
                end)
                if not ok or not em then
                    task.synchronize()
                    resultEvent:Fire(jobId, false,
                        "CreateEditableMeshAsync: " .. tostring(em))
                    return
                end
                local vids = em:GetVertices()
                if not vids or #vids == 0 then
                    task.synchronize()
                    resultEvent:Fire(jobId, false, "no vertices")
                    return
                end
                local verts, uvs, normals = {}, {}, {}
                local idMap = {}
                for i, vid in ipairs(vids) do
                    idMap[vid] = i - 1
                    local p = em:GetPosition(vid)
                    verts[#verts+1] = p.X; verts[#verts+1] = p.Y; verts[#verts+1] = p.Z
                    local okUV, uv = pcall(em.GetUV, em, vid)
                    if okUV and uv then
                        uvs[#uvs+1] = uv.X; uvs[#uvs+1] = uv.Y
                    else
                        uvs[#uvs+1] = 0; uvs[#uvs+1] = 0
                    end
                    local okN, n = pcall(em.GetNormal, em, vid)
                    if okN and n then
                        normals[#normals+1] = n.X; normals[#normals+1] = n.Y; normals[#normals+1] = n.Z
                    end
                    -- pace lightly inside actor too
                    if i % 500 == 0 then task.wait() end
                end
                local faces = {}
                local fids
                local okF = pcall(function() fids = em:GetFaces() end)
                if not okF or not fids then
                    okF = pcall(function() fids = em:GetTriangles() end)
                end
                if okF and fids then
                    for i, fid in ipairs(fids) do
                        local fv
                        local okFV = pcall(function() fv = em:GetFaceVertices(fid) end)
                        if not okFV or not fv then
                            okFV = pcall(function() fv = { em:GetTriangleVertices(fid) } end)
                        end
                        if okFV and fv and #fv >= 3 then
                            local a, b, c = idMap[fv[1]], idMap[fv[2]], idMap[fv[3]]
                            if a and b and c then
                                faces[#faces+1] = a; faces[#faces+1] = b; faces[#faces+1] = c
                            end
                        end
                        if i % 500 == 0 then task.wait() end
                    end
                end
                task.synchronize()
                if #faces == 0 then
                    resultEvent:Fire(jobId, false, "no faces produced")
                    return
                end
                -- Pass back a table. BindableEvents can carry tables across
                -- actor boundaries.
                resultEvent:Fire(jobId, true, {
                    format = "ROCORDER-GEOM/1",
                    verts = verts, uvs = uvs, normals = normals, faces = faces,
                })
                return
            end

            resultEvent:Fire(jobId, false, "unknown kind " .. tostring(kind))
        end)
    ]==]

    -- Try writing the Source. Most executors patch the security check
    -- that makes this read-only.
    local writeOk = pcall(function() workerScript.Source = workerSource end)
    local readBack
    pcall(function() readBack = workerScript.Source end)
    local stuck = writeOk and (readBack == workerSource)

    if not stuck then
        _G.ROCORDER_ACTOR_ERR =
            "Script.Source write " ..
            (writeOk and "appeared to succeed but didn't stick (" ..
                (readBack and (#readBack > 60 and (#readBack .. " bytes")
                    or readBack:sub(1, 60)) or "nil") .. ")"
                or "threw")
        pcall(function() workerScript:Destroy() end)
        pcall(function() actor:Destroy() end)
        print("[ROCORDER] Actor scaffold unavailable: "
            .. _G.ROCORDER_ACTOR_ERR)
    else
        -- Start the script. Listen for the "ready" signal.
        local ready = false
        local readyConn
        readyConn = readyEvent.Event:Connect(function() ready = true end)
        workerScript.Parent = actor

        local waited = 0
        while not ready and waited < 2.0 do
            task.wait(0.05); waited = waited + 0.05
        end
        if readyConn then readyConn:Disconnect() end

        if not ready then
            _G.ROCORDER_ACTOR_ERR = "Actor worker script never signaled ready "
                .. "(maybe Source.set silently failed, or scripts under "
                .. "Actors are disabled in this executor)"
            pcall(function() workerScript:Destroy() end)
            pcall(function() actor:Destroy() end)
            print("[ROCORDER] Actor scaffold unavailable: "
                .. _G.ROCORDER_ACTOR_ERR)
        else
            -- Round-trip ping test
            local pong = nil
            local pongConn
            pongConn = resultEvent.Event:Connect(function(jobId, ok, data)
                if jobId == "probe-ping" then
                    pong = { ok = ok, data = data }
                end
            end)
            jobEvent:Fire("probe-ping", "ping", nil)
            waited = 0
            while pong == nil and waited < 2.0 do
                task.wait(0.05); waited = waited + 0.05
            end
            if pongConn then pongConn:Disconnect() end

            if pong and pong.ok and pong.data == "pong" then
                _G.ROCORDER_ACTOR_OK = true
                _G.ROCORDER_ACTOR = actor
                _G.ROCORDER_ACTOR_JOB = jobEvent
                _G.ROCORDER_ACTOR_RESULT = resultEvent
                _G.ROCORDER_ACTOR_DETACH = function()
                    pcall(function() actor:Destroy() end)
                    _G.ROCORDER_ACTOR_OK = false
                    _G.ROCORDER_ACTOR = nil
                end
                print("[ROCORDER] Actor scaffold installed and ping round-trip "
                    .. "succeeded — parallel extraction available")
            else
                _G.ROCORDER_ACTOR_ERR = pong and ("ping returned " .. tostring(pong.data))
                    or "ping timed out (worker not responding to BindableEvents)"
                pcall(function() actor:Destroy() end)
                print("[ROCORDER] Actor scaffold unavailable: "
                    .. _G.ROCORDER_ACTOR_ERR)
            end
        end
    end
end

-- ============================================================================
-- Parallel-Luau probe.
--
-- The remaining ~160 ms stalls during extraction come from Roblox's own
-- CreateEditableMeshAsync / CreateEditableImageAsync calls — C-bound API
-- that blocks the calling Luau thread while the engine loads + decodes the
-- asset. Pacing AROUND those calls helps recovery but can't make them
-- non-blocking.
--
-- Parallel Luau (`task.desynchronize()`) marks the current thread as
-- "desynchronized" — running on a worker VM thread separate from the main
-- game thread. While desynchronized, heavy calls don't stall the main
-- thread. To re-enter Roblox-state-mutating APIs (like writefile via
-- executor, or Instance property writes), the thread re-synchronizes.
--
-- Officially `task.desynchronize` requires the script to be parented to an
-- Actor. From an executor coroutine (no Actor parent), this errors. We
-- probe at module load to find out which world we're in:
--   - Probe succeeds: worker calls desync before heavy ops, sync after.
--   - Probe fails: worker stays serial as before. No code change.
--
-- Probe is non-blocking; runs in a spawned coroutine with a 1 s timeout.
-- ============================================================================
local PARALLEL_OK = false
local PARALLEL_PROBE_ERR = nil
do
    local result
    task.spawn(function()
        local ok, err = pcall(function()
            task.desynchronize()
            task.synchronize()
        end)
        result = { ok = ok, err = err }
    end)
    local waited = 0
    while result == nil and waited < 1.0 do
        task.wait(0.05); waited = waited + 0.05
    end
    if result and result.ok then
        PARALLEL_OK = true
        print("[ROCORDER] parallel Luau available — extractor will desync "
            .. "around heavy API calls to keep main thread smooth")
    else
        PARALLEL_PROBE_ERR = (result and tostring(result.err)) or "probe timed out"
        print("[ROCORDER] parallel Luau unavailable: "
            .. PARALLEL_PROBE_ERR .. " — extractor stays serial")
    end
end

-- Safe desync/sync wrappers. No-ops when PARALLEL_OK is false. When ok,
-- they're pcall'd so a runtime "not in actor" failure doesn't crash the
-- worker — it just means this particular call isn't going to be parallel.
local function _desyncSafe()
    if not PARALLEL_OK then return false end
    local ok = pcall(task.desynchronize)
    return ok
end
local function _syncSafe()
    if not PARALLEL_OK then return end
    pcall(task.synchronize)
end

-- ============================================================================
-- Actor job dispatch.
--
-- When the Actor scaffold passed its probe (_G.ROCORDER_ACTOR_OK = true),
-- mesh and image extraction can be offloaded into the actor's worker
-- script — which runs in parallel context and doesn't stall the main
-- thread on CreateEditable*Async or any of the editable read methods.
--
-- _runJobInActor sends a (kind, payload) job, waits for the matching
-- result event by job id, and returns the data — or nil + reason on
-- timeout / actor reporting failure. Caller decides whether to retry
-- in serial.
-- ============================================================================
local _actorJobCounter = 0
local function _runJobInActor(kind, payload, timeoutSec)
    if not _G.ROCORDER_ACTOR_OK then return nil, "actor unavailable" end
    if not _G.ROCORDER_ACTOR_JOB or not _G.ROCORDER_ACTOR_RESULT then
        return nil, "actor events missing"
    end
    _actorJobCounter = _actorJobCounter + 1
    local jobId = "j" .. _actorJobCounter

    local result
    local conn
    conn = _G.ROCORDER_ACTOR_RESULT.Event:Connect(function(rJobId, ok, data)
        if rJobId == jobId then result = { ok = ok, data = data } end
    end)

    pcall(function() _G.ROCORDER_ACTOR_JOB:Fire(jobId, kind, payload) end)

    local waited = 0
    local timeout = timeoutSec or 30
    while result == nil and waited < timeout do
        task.wait(0.05); waited = waited + 0.05
    end
    if conn then pcall(function() conn:Disconnect() end) end

    if not result then return nil, "actor job timed out after " .. timeout .. "s" end
    if not result.ok then return nil, tostring(result.data or "actor reported failure") end
    return result.data
end

local EXTRACT_SLICE_SEC = 0.003   -- ~3 ms of work per frame, max
local EXTRACT_BACKOFF_FPS = 45    -- below this, back off harder
local _lastPaceYieldAt = 0
local function paceExtractor()
    local now = os.clock()
    if _G.ROCORDER_FRAME_DELTA > (1 / EXTRACT_BACKOFF_FPS) then
        -- Game is below our floor — yield two frames so we don't pile on.
        task.wait()
        task.wait()
        _lastPaceYieldAt = os.clock()
        return
    end
    local elapsed = now - _lastPaceYieldAt
    if elapsed > 0.030 then
        -- A single chunk took >30 ms. We almost certainly just returned
        -- from a heavy Roblox API call we couldn't pace inside
        -- (CreateEditableMeshAsync / CreateEditableImageAsync / a big
        -- ReadPixelsBuffer / a synchronous JSONEncode of a remaining
        -- string-concat tail). Yield THREE frames so the game can render
        -- a clean frame before the next chunk starts — single-frame yields
        -- leave the game ~7 ms (at 144 fps) to recover, which is rarely
        -- enough after a 100-150 ms hitch.
        task.wait()
        task.wait()
        task.wait()
        _lastPaceYieldAt = os.clock()
    elseif elapsed > EXTRACT_SLICE_SEC then
        task.wait()
        _lastPaceYieldAt = os.clock()
    end
end

-- Roblox sometimes hands us a Content userdata and sometimes a URL string.
-- Normalize to whatever CreateEditable*Async accepts on this client version.
local function _toContent(ref)
    if typeof(ref) == "Content" then return ref end
    if type(ref) == "string" then
        if typeof(Content) == "table" or typeof(Content) == "userdata" then
            -- Modern API: Content.fromUri(<rbxassetid://N>)
            local ok, c = pcall(function() return Content.fromUri(ref) end)
            if ok and c then return c end
        end
        return ref  -- last resort: pass the URL through
    end
    return ref
end

-- Extract mesh geometry from a BasePart that has a mesh (MeshPart or a Part
-- with a SpecialMesh child). Returns a JSON-encodable table:
--   { verts = {x,y,z, ...}, uvs = {u,v, ...},
--     normals = {x,y,z, ...}, faces = {a,b,c, ...} }   (0-indexed)
-- Or returns nil + reason string.
local function extractMeshFromPart(part)
    if not EXTRACT_OK then return nil, "EditableMesh API unavailable" end
    local content
    if part:IsA("MeshPart") then
        local ok, mc = pcall(function() return part.MeshContent end)
        if ok and mc then content = mc end
        if not content then
            local ok2, mid = pcall(function() return part.MeshId end)
            if ok2 and mid and mid ~= "" then content = _toContent(mid) end
        end
    else
        local sm = part:FindFirstChildOfClass("SpecialMesh")
        if sm then
            local ok, mid = pcall(function() return sm.MeshId end)
            if ok and mid and mid ~= "" then content = _toContent(mid) end
        end
    end
    if not content then return nil, "no mesh content on part" end

    -- FAST PATH: dispatch to the Actor's worker if available. Everything
    -- inside the actor runs in parallel context, so CreateEditableMeshAsync
    -- + GetVertices + per-vert loop + GetFaces loop don't stall the main
    -- thread. We get back a ready-built geom table. Falls through to
    -- serial path on any actor error so a single bad job doesn't break the
    -- recorder.
    if _G.ROCORDER_ACTOR_OK then
        local geom, err = _runJobInActor("mesh", content)
        if geom then return geom end
        -- err is informational; fall through to serial below.
    end

    local okem, em = pcall(function()
        return AssetService:CreateEditableMeshAsync(content)
    end)
    if not okem or not em then return nil, "CreateEditableMeshAsync: " .. tostring(em) end

    local vids = em:GetVertices()
    if not vids or #vids == 0 then return nil, "no vertices" end
    local verts, uvs, normals = {}, {}, {}
    local idMap = {}    -- engine-vertex-id -> 0-indexed slot
    -- Pace EVERY iteration. paceExtractor short-circuits cheaply (~1us) when
    -- the per-frame budget isn't spent yet, so the overhead is negligible
    -- compared to the per-vertex EditableMesh API cost. The previous
    -- "every 100 verts" gate let a chunk accumulate ~200ms of work on big
    -- meshes before the pace check could fire — visible as recurring
    -- ~200ms heartbeat stalls during long mesh extractions.
    for i, vid in ipairs(vids) do
        idMap[vid] = i - 1
        local p = em:GetPosition(vid)
        verts[#verts+1] = p.X; verts[#verts+1] = p.Y; verts[#verts+1] = p.Z
        local okUV, uv = pcall(em.GetUV, em, vid)
        if okUV and uv then
            uvs[#uvs+1] = uv.X; uvs[#uvs+1] = uv.Y
        else
            uvs[#uvs+1] = 0; uvs[#uvs+1] = 0
        end
        local okN, n = pcall(em.GetNormal, em, vid)
        if okN and n then
            normals[#normals+1] = n.X; normals[#normals+1] = n.Y; normals[#normals+1] = n.Z
        end
        paceExtractor()
    end

    -- Faces (triangles). Method name varies by Roblox API revision.
    local faces = {}
    local fids
    local okF = pcall(function() fids = em:GetFaces() end)
    if not okF or not fids then
        okF = pcall(function() fids = em:GetTriangles() end)
    end
    if okF and fids then
        for i, fid in ipairs(fids) do
            local fv
            local okFV = pcall(function() fv = em:GetFaceVertices(fid) end)
            if not okFV or not fv then
                okFV = pcall(function() fv = { em:GetTriangleVertices(fid) } end)
            end
            if okFV and fv and #fv >= 3 then
                local a, b, c = idMap[fv[1]], idMap[fv[2]], idMap[fv[3]]
                if a and b and c then
                    faces[#faces+1] = a; faces[#faces+1] = b; faces[#faces+1] = c
                end
            end
            paceExtractor()
        end
    end
    if #faces == 0 then return nil, "no faces produced" end

    return {
        format = "ROCORDER-GEOM/1",
        verts = verts, uvs = uvs, normals = normals, faces = faces,
    }
end

-- Encode a geom table to JSON with pacing between sections. A single
-- HttpService:JSONEncode on a 500 KB mesh blob blocks for 50-100 ms — long
-- enough to be a frame-killer on its own. We split the encode into four
-- subarray encodes (verts / uvs / normals / faces) with paceExtractor()
-- between them, so the work spreads across 2-4 frames instead of all
-- happening in one.
-- Manually encode a flat numeric array as JSON, pacing every CHUNK
-- elements. HttpService:JSONEncode of a 30000-number array (10k-vert mesh's
-- verts) blocks for ~20-40 ms in unbreakable C code; pacing AROUND that
-- can't help. Doing tostring() in a Lua loop with periodic paceExtractor()
-- yields lets us cut each encode into ~1 ms slices. tostring on a float
-- produces JSON-valid number literals (digits, optional ., optional e exp).
local _NUMARRAY_PACE_EVERY = 1000
local function _encodeNumberArrayPaced(arr)
    if not arr then return "null" end
    local n = #arr
    if n == 0 then return "[]" end
    local pieces = table.create(n)
    for i = 1, n do
        pieces[i] = tostring(arr[i])
        if i % _NUMARRAY_PACE_EVERY == 0 then paceExtractor() end
    end
    return "[" .. table.concat(pieces, ",") .. "]"
end

local function _encodeGeomChunked(geom)
    if type(geom) ~= "table" then return nil, "geom not a table" end
    -- format string is a literal; arrays go through the paced encoder.
    paceExtractor()
    local body = table.concat({
        '{"format":"', geom.format or "ROCORDER-GEOM/1", '"',
        ',"verts":',   _encodeNumberArrayPaced(geom.verts),
        ',"uvs":',     _encodeNumberArrayPaced(geom.uvs),
        ',"normals":', _encodeNumberArrayPaced(geom.normals),
        ',"faces":',   _encodeNumberArrayPaced(geom.faces),
        '}',
    })
    paceExtractor()
    return body
end

-- Extract an image to raw RGBA8 bytes + a small text header. Filename ends in
-- .rgba so the importer reads it without sniffing. Returns header+bytes string
-- or nil + reason.
local function extractImageFromContent(ref)
    if not EXTRACT_OK then return nil, "EditableImage API unavailable" end
    local content = _toContent(ref)
    if not content then return nil, "no content" end

    -- FAST PATH: actor runs the whole image extraction in parallel and
    -- returns the final ROCORDER-RGBA8 string body. Skip the local strip
    -- loop entirely when this succeeds.
    if _G.ROCORDER_ACTOR_OK then
        local body, err = _runJobInActor("image", content)
        if type(body) == "string" then return body end
        -- fall through to serial extraction on actor failure
    end

    local ok, ei = pcall(function()
        return AssetService:CreateEditableImageAsync(content)
    end)
    if not ok or not ei then return nil, "CreateEditableImageAsync: " .. tostring(ei) end
    local sz = ei.Size
    local w, h = math.floor(sz.X), math.floor(sz.Y)
    if w <= 0 or h <= 0 then return nil, "zero-size image" end

    -- Read pixels in row-strips instead of one big atomic call. A 1024x1024
    -- RGBA8 image is 4 MB; pulling that in one ReadPixelsBuffer call stalls
    -- the frame for ~30-50 ms. Strips of ~64 KB pace the work across frames.
    local STRIP_BYTES = 65536
    local stripRows = math.max(4, math.min(h, math.floor(STRIP_BYTES / (w * 4))))
    local rowStride = w * 4
    local totalBytes = w * h * 4

    local accumulated
    local okBuf, errBuf = pcall(function()
        accumulated = buffer.create(totalBytes)
    end)
    if not okBuf or not accumulated then
        return nil, "buffer.create failed: " .. tostring(errBuf)
    end

    for y = 0, h - 1, stripRows do
        local sh = math.min(stripRows, h - y)
        local strip
        local okR, errR = pcall(function()
            strip = ei:ReadPixelsBuffer(Vector2.new(0, y), Vector2.new(w, sh))
        end)
        if not okR or not strip then
            return nil, fmt("ReadPixelsBuffer strip y=%d: %s", y, tostring(errR))
        end
        local okC = pcall(function()
            buffer.copy(accumulated, y * rowStride, strip, 0, sh * rowStride)
        end)
        if not okC then
            return nil, fmt("buffer.copy strip y=%d failed", y)
        end
        paceExtractor()
    end

    local data
    local okStr = pcall(function() data = buffer.tostring(accumulated) end)
    if not okStr or not data then return nil, "buffer.tostring failed" end
    if #data ~= totalBytes then
        return nil, fmt("size mismatch: got %d expected %d (w=%d h=%d)",
            #data, totalBytes, w, h)
    end
    -- Header: "ROCORDER-RGBA8\n<w>\n<h>\n" then raw RGBA bytes
    return fmt("ROCORDER-RGBA8\n%d\n%d\n", w, h) .. data
end

-- ===========================================================================
-- Experimental "Decal route" for clothing templates and other assets whose
-- content-type causes CreateEditableImageAsync to reject the URL directly.
--
-- Theory: the engine already has the bytes in memory (the player is wearing
-- the shirt — it's rendered every frame). Roblox rejects clothing IDs
-- because the asset content-type is "ShirtTemplate", not "Image". But if we
-- wrap the URL in a live Decal Instance and call Content.fromObject(decal),
-- the resulting Content references the loaded bytes via the Instance, not
-- the URL — possibly bypassing the type check on subsequent calls.
--
-- Procedure:
--   1. Create a hidden anchored Part deep underground and parent a Decal
--      to it with Texture = ref.
--   2. ContentProvider:PreloadAsync the decal so the engine actually loads
--      the texture (rather than waiting for first render).
--   3. Content.fromObject(decal) -> Content userdata.
--   4. CreateEditableImageAsync(content) -> EditableImage on success.
--   5. Strip-extract pixels exactly like extractImageFromContent.
--   6. Destroy the host Part (cleans up Decal too).
--
-- Falls back silently on any error — the caller will then try the standard
-- HTTP route. Used as step 3 of _processOne's cascade for image entries
-- (after direct extract, before HTTP).
-- ===========================================================================
local _decalHostFolder
local function _ensureDecalHost()
    if _decalHostFolder and _decalHostFolder.Parent then return _decalHostFolder end
    -- Reuse a host from a prior script load (Xeno re-execute case) so we
    -- don't accumulate orphan folders in workspace across reloads.
    local existing = workspace:FindFirstChild("_ROCORDER_DecalHost")
    if existing then _decalHostFolder = existing; return existing end
    local folder = Instance.new("Folder")
    folder.Name = "_ROCORDER_DecalHost"
    folder.Parent = workspace
    _decalHostFolder = folder
    return folder
end

local function _extractImageViaDecal(ref, dbg)
    if not EXTRACT_OK then return nil, "EditableImage API unavailable" end
    if typeof(Content) ~= "table" and typeof(Content) ~= "userdata" then
        return nil, "Content global unavailable"
    end
    -- Content.fromObject may not exist on older Roblox versions.
    if not (Content.fromObject) then
        return nil, "Content.fromObject not available"
    end
    if type(ref) ~= "string" or ref == "" then
        return nil, "Decal route needs a URL ref"
    end

    local host = Instance.new("Part")
    host.Anchored = true
    host.CanCollide = false
    host.CanTouch = false
    host.CanQuery = false
    host.Transparency = 1
    host.Size = Vector3.new(1, 1, 1)
    host.Position = Vector3.new(0, -1e5, 0)
    host.Parent = _ensureDecalHost()

    local decal = Instance.new("Decal")
    local okSet = pcall(function() decal.Texture = ref end)
    if not okSet then
        host:Destroy()
        return nil, "could not set Decal.Texture"
    end
    decal.Parent = host

    -- Block until the engine has actually downloaded/decoded the texture.
    -- Time-bound this: PreloadAsync can yield indefinitely on a hung CDN.
    local preloadDone = false
    task.spawn(function()
        local ok = pcall(function()
            ContentProvider:PreloadAsync({decal})
        end)
        preloadDone = ok or "errored"
    end)
    local waited = 0
    while not preloadDone and waited < 5.0 do
        task.wait(0.1); waited = waited + 0.1
    end
    if not preloadDone then
        host:Destroy()
        return nil, "PreloadAsync timed out (>5 s)"
    end

    local okC, content = pcall(function() return Content.fromObject(decal) end)
    if not okC or not content then
        host:Destroy()
        return nil, "Content.fromObject failed: " .. tostring(content)
    end

    local okEI, ei = pcall(function()
        return AssetService:CreateEditableImageAsync(content)
    end)
    if not okEI or not ei then
        host:Destroy()
        return nil, "CreateEditableImageAsync via Decal: " .. tostring(ei)
    end

    local sz = ei.Size
    local w, h = math.floor(sz.X), math.floor(sz.Y)
    if w <= 0 or h <= 0 then host:Destroy(); return nil, "zero-size image" end

    -- Strip-extract loop (same shape as extractImageFromContent). Kept
    -- inline rather than refactored so this experimental path can be
    -- removed cleanly if it turns out not to work.
    local STRIP_BYTES = 65536
    local stripRows = math.max(4, math.min(h, math.floor(STRIP_BYTES / (w * 4))))
    local rowStride = w * 4
    local totalBytes = w * h * 4
    local accumulated
    local okBuf = pcall(function() accumulated = buffer.create(totalBytes) end)
    if not okBuf or not accumulated then
        host:Destroy(); return nil, "buffer.create failed"
    end
    for y = 0, h - 1, stripRows do
        local sh = math.min(stripRows, h - y)
        local strip
        local okR = pcall(function()
            strip = ei:ReadPixelsBuffer(Vector2.new(0, y), Vector2.new(w, sh))
        end)
        if not okR or not strip then
            host:Destroy()
            return nil, fmt("ReadPixelsBuffer strip y=%d failed", y)
        end
        pcall(function()
            buffer.copy(accumulated, y * rowStride, strip, 0, sh * rowStride)
        end)
        paceExtractor()
    end
    local data
    local okStr = pcall(function() data = buffer.tostring(accumulated) end)
    host:Destroy()
    if not okStr or not data then return nil, "buffer.tostring failed" end
    return fmt("ROCORDER-RGBA8\n%d\n%d\n", w, h) .. data
end

-- One-shot helper: try to extract `ref` as an image (texture / decal / clothing
-- template) and write to ROCORDER/assets/<id>.rgba. Returns true if the file
-- ended up on disk by any means.
local function _extractImageRef(ref, dbg)
    local id = _assetIdFromRef(ref)
    if not id then return false end
    if EXTRACTED[id] or _isCached(id) then return true end
    EXTRACTED[id] = true
    local body, err = extractImageFromContent(ref)
    if body then
        local ok = pcall(writefile, _imgPath(id), body)
        if ok and dbg then
            dbg(fmt("  EXTRACT image %s OK (%d bytes)", id, #body))
        end
        return ok
    elseif dbg then
        dbg(fmt("  EXTRACT image %s FAILED: %s", id, tostring(err)))
    end
    return false
end

-- Walk every asset reference on a part and try to extract via the in-engine
-- API. Each successful extraction writes a single file in ROCORDER/assets/.
-- Skips assets already cached (any format). dbg() is optional and gets called
-- on success/failure for diagnostic logging.
local function extractPartAssets(part, partInfo, dbg)
    if not isfolder(ASSETS_FOLDER) then pcall(makefolder, ASSETS_FOLDER) end

    -- Mesh
    local meshId = _assetIdFromRef(partInfo.meshId)
    if meshId and not EXTRACTED[meshId] and not _isCached(meshId) then
        EXTRACTED[meshId] = true
        local geom, err = extractMeshFromPart(part)
        if geom then
            local ok = pcall(writefile, _geomPath(meshId), HttpService:JSONEncode(geom))
            if ok and dbg then
                dbg(fmt("  EXTRACT mesh %s OK (%d verts, %d faces)",
                    meshId, #geom.verts / 3, #geom.faces / 3))
            end
        elseif dbg then
            dbg(fmt("  EXTRACT mesh %s FAILED: %s", meshId, tostring(err)))
        end
        task.wait()
    end

    -- Image references on the part: textureId, colorMap, decals[]
    if partInfo.textureId then
        _extractImageRef(partInfo.textureId, dbg); task.wait()
    end
    if partInfo.colorMap then
        _extractImageRef(partInfo.colorMap, dbg); task.wait()
    end
    if partInfo.decals then
        for _, d in ipairs(partInfo.decals) do
            if d.texture then _extractImageRef(d.texture, dbg); task.wait() end
        end
    end
end

-- Extract a player's clothing textures (Shirt.ShirtTemplate, Pants.PantsTemplate,
-- ShirtGraphic.Graphic). These live on the Shirt/Pants instances, not on parts.
-- (Used as a fallback when not using the queue.)
local function extractClothingAssets(clothing, dbg)
    if not clothing then return end
    if clothing.shirt  then _extractImageRef(clothing.shirt, dbg);  task.wait() end
    if clothing.pants  then _extractImageRef(clothing.pants, dbg);  task.wait() end
    if clothing.tshirt then _extractImageRef(clothing.tshirt, dbg); task.wait() end
end

----------------------------------------------------------------
-- Asset extraction queue — one global worker, frame-health-aware. Designed
-- so an hour-long Instant Replay session with players coming and going doesn't
-- produce constant lag spikes. Processes one asset at a time, only when the
-- last heartbeat dt was healthy. Persistent across reloads.
----------------------------------------------------------------
_G.ROCORDER_EXTRACT_QUEUE = _G.ROCORDER_EXTRACT_QUEUE or {
    queue         = {},      -- ordered list of pending entries
    byId          = {},      -- id -> entry (dedup; entries removed on pop)
    perPlayer     = {},      -- uid -> { name, total, done, failed, missed, ... }
    workerVersion = 0,       -- bumped per reload so stale workers self-exit
    stats         = { totalSeen = 0, done = 0, failed = 0, missed = 0 },
    -- last item we picked up — surfaced in the UI so the user sees activity
    activeKind    = nil,     -- "mesh" / "image"
    activeId      = nil,
    activePlayer  = nil,
}
local Q = _G.ROCORDER_EXTRACT_QUEUE

local function _ensurePlayerStats(uid, displayName)
    if not uid then return nil end
    local ps = Q.perPlayer[uid]
    if not ps then
        ps = {
            uid = uid, name = displayName or ("uid " .. tostring(uid)),
            total = 0, done = 0, failed = 0, missed = 0,
            seenAt = os.clock(), leftAt = nil, lastActivityAt = os.clock(),
        }
        Q.perPlayer[uid] = ps
    elseif displayName then
        ps.name = displayName
    end
    return ps
end

local function _markPlayerLeft(uid)
    local ps = Q.perPlayer[uid]
    if ps and not ps.leftAt then ps.leftAt = os.clock() end
end

-- Persistent ownership map: which players have this asset id in their kit.
-- Survives extraction (Q.byId[id] is removed when the entry finishes; this
-- table sticks around so the IR eviction scanner can decide whether anyone
-- in the live buffer still needs the asset). Lives in _G so script reloads
-- inherit the existing-on-disk attribution.
_G.ROCORDER_ASSET_OWNERS = _G.ROCORDER_ASSET_OWNERS or {}
local ASSET_OWNERS = _G.ROCORDER_ASSET_OWNERS

local function _registerOwner(id, uid)
    if not (id and uid) then return end
    local owners = ASSET_OWNERS[id]
    if not owners then owners = {}; ASSET_OWNERS[id] = owners end
    owners[uid] = true
end

local function _enqueueAsset(id, kind, partInst, partInfo, uid, displayName)
    if not id then return end
    local ps = uid and _ensurePlayerStats(uid, displayName) or nil
    _registerOwner(id, uid)

    -- already on disk (any format) → count as done and skip
    if EXTRACTED[id] or _isCached(id) then
        if ps then ps.total = ps.total + 1; ps.done = ps.done + 1
            ps.lastActivityAt = os.clock() end
        return
    end

    local entry = Q.byId[id]
    if entry then
        -- already queued — register this part as a backup ref and this player
        -- as an owner. If the first player leaves, we'll use the backup.
        entry.partRefs[#entry.partRefs + 1] = { inst = partInst, info = partInfo }
        if uid then
            if not entry.owners[uid] then
                entry.owners[uid] = true
                if ps then ps.total = ps.total + 1
                    ps.lastActivityAt = os.clock() end
            end
        end
        return
    end

    entry = {
        id = id, kind = kind,
        partRefs = { { inst = partInst, info = partInfo } },
        owners = uid and { [uid] = true } or {},
        enqueuedAt = os.clock(),
    }
    Q.byId[id] = entry
    Q.queue[#Q.queue + 1] = entry
    Q.stats.totalSeen = Q.stats.totalSeen + 1
    if ps then ps.total = ps.total + 1; ps.lastActivityAt = os.clock() end
end

local function _findLivePartRef(entry)
    for _, r in ipairs(entry.partRefs) do
        if r.inst and r.inst.Parent then return r end
    end
    return nil
end

local function _imgRefFromPartInfo(info, entryId)
    if not info then return "rbxassetid://" .. entryId end
    if info.textureId then return info.textureId end
    if info.colorMap  then return info.colorMap  end
    if info.decals then
        for _, d in ipairs(info.decals) do
            if d.texture then
                -- match the decal whose texture corresponds to this id
                if d.texture:find(entryId, 1, true) then return d.texture end
            end
        end
    end
    return "rbxassetid://" .. entryId
end

local function _markEntryDone(entry, dbg)
    EXTRACTED[entry.id] = true
    Q.stats.done = Q.stats.done + 1
    for uid in pairs(entry.owners) do
        local ps = Q.perPlayer[uid]
        if ps then ps.done = ps.done + 1; ps.lastActivityAt = os.clock() end
    end
end

local function _markEntryFailed(entry, wasMissed, dbg)
    Q.stats.failed = Q.stats.failed + 1
    if wasMissed then Q.stats.missed = Q.stats.missed + 1 end
    for uid in pairs(entry.owners) do
        local ps = Q.perPlayer[uid]
        if ps then
            ps.failed = ps.failed + 1
            if wasMissed then ps.missed = ps.missed + 1 end
            ps.lastActivityAt = os.clock()
        end
    end
end

-- Heuristic: does this byte-string look like a real Roblox asset, or like an
-- error page (401/403/HTML/JSON) the CDN handed back with a 2xx status? Used
-- by both the queue worker's HTTP fallback and the legacy downloadAssets
-- pass. Hoisted to module scope: the queue worker (line ~764 below) needs
-- it before the legacy pass defines it locally — without this hoist that
-- call was nil and the fallback threw "attempt to call a nil value", which
-- the worker counted as a failure even when the legacy pass later rescued
-- the same asset over HTTP.
local function looksLikeAsset(b)
    if type(b) ~= "string" or #b < 64 then return false end
    local head = b:sub(1, 8)
    if head:sub(1, 8) == "version " then return true end          -- Roblox mesh
    local b0 = string.byte(b, 1, 1)
    if b0 == 0x89 or b0 == 0xFF or b0 == 0x47 or b0 == 0x42 then    -- PNG/JPG/GIF/BMP/DDS
        return true
    end
    local first = b:sub(1, 1)
    if first == "{" or first == "<" then return false end          -- JSON / XML / HTML
    local probe = b:sub(1, 256)
    if probe:find("Unauthorized") or probe:find("Forbidden")
        or probe:find("InsufficientPermission") or probe:find("\"errors\"") then
        return false
    end
    return true  -- unknown-but-binary; let the importer decide
end

-- Try the extractor path; if it fails or no live part remains, try a one-shot
-- authenticated HTTP fetch. Returns true if the asset ended up on disk.
--
-- Parallel-Luau experiment (1.9.15 / 1.9.16) FAILED. Roblox's client-side
-- editable APIs are too restrictive: not just CreateEditable*Async but also
-- EditableMesh:GetVertices and (likely) every other editable read method
-- refuses parallel context. The probe at module load still runs and prints
-- the result (useful diagnostic), but _processOne no longer attempts to
-- desync — every API call below would just throw and the entry would fall
-- through to HTTP fallback, losing the .geom.json output we want. Better
-- to keep the existing 160 ms stalls than to lose every mesh extraction.
local function _processOne(entry, dbg)
    local ref = _findLivePartRef(entry)
    local body, err

    if ref then
        if entry.kind == "mesh" then
            local geom
            geom, err = extractMeshFromPart(ref.inst)
            if geom then
                -- Chunked encode with pacing between subarrays; a single
                -- HttpService:JSONEncode of a 500 KB mesh was a ~100 ms stall.
                body, err = _encodeGeomChunked(geom)
            end
        else
            local imgRef = _imgRefFromPartInfo(ref.info, entry.id)
            body, err = extractImageFromContent(imgRef)
        end
    end

    -- Step 2 (REMOVED in 1.9.14, kept as comment for record): we briefly
    -- tried wrapping the URL in a live Decal and using Content.fromObject
    -- to bypass the content-type check. The Decal trick failed because
    -- Content.fromObject only accepts EditableImage / EditableMesh, not
    -- arbitrary Instances ("Object expected, got table" in the log).
    -- The helper `_extractImageViaDecal` is kept above as documentation
    -- of what we tried, but it's no longer called. Falls straight through
    -- to HTTP fallback as in 1.9.12 and earlier.

    if body then
        local path = (entry.kind == "mesh") and _geomPath(entry.id) or _imgPath(entry.id)
        if pcall(writefile, path, body) then
            _markEntryDone(entry, dbg)
            if dbg then
                dbg(fmt("  EXTRACT %s %s OK (%d bytes, queued for %.1fs)",
                    entry.kind, entry.id,
                    #body, os.clock() - entry.enqueuedAt))
            end
            return true
        end
    end

    -- HTTP fallback. Used for two cases:
    --
    --   1. The player left mid-session and their parts were destroyed before
    --      we got to them in the queue. EditableMesh/Image require a live
    --      instance, HTTP doesn't.
    --   2. Clothing templates (Shirt.ShirtTemplate, Pants.PantsTemplate).
    --      AssetService:CreateEditableImageAsync does NOT accept clothing
    --      asset IDs — the engine has no public read-back API for them. So
    --      they always come through this path even when the player is live.
    --      The Roblox CDN does serve them publicly though, so the auth'd
    --      request reliably gets the same bytes the engine loaded.
    --
    -- Two attempts × two endpoints with a brief backoff between rounds.
    -- Most clothing fetches succeed on attempt 1; the retry handles 429
    -- rate-limits and transient CDN hiccups that are otherwise visible as
    -- "couldn't be fetched" failures.
    if httpRequest then
        local urls = {
            "https://assetdelivery.roblox.com/v1/asset/?id=" .. entry.id,
            "https://assetdelivery.roblox.com/v2/asset/?id=" .. entry.id,
        }
        local lastErr
        local sawAuthFailure = false  -- 401/403 means "we don't have access";
                                      -- retrying with the same credentials won't
                                      -- change that. Fail fast to avoid blowing
                                      -- 1-2 s per off-sale/private asset.
        for attempt = 1, 2 do
            for _, url in ipairs(urls) do
                local okr, resp = pcall(httpRequest, {
                    Url = url, Method = "GET",
                    Headers = {
                        ["User-Agent"]      = "Roblox/WinInet",
                        ["Roblox-Place-Id"] = tostring(game.PlaceId),
                    },
                })
                if okr and type(resp) == "table" then
                    local code = resp.StatusCode or resp.Status or 200
                    if resp.Body and code >= 200 and code < 300 and looksLikeAsset(resp.Body) then
                        if pcall(writefile, _binPath(entry.id), resp.Body) then
                            _markEntryDone(entry, dbg)
                            if dbg then
                                dbg(fmt("  EXTRACT %s %s OK via HTTP fallback "
                                    .. "(%d bytes, attempt %d)",
                                    entry.kind, entry.id, #resp.Body, attempt))
                            end
                            return true
                        end
                    else
                        lastErr = fmt("HTTP %s", tostring(code))
                        if code == 401 or code == 403 then
                            sawAuthFailure = true
                            break  -- both endpoints check the same auth;
                                   -- a 401 from v1 means v2 will 401 too.
                        end
                    end
                else
                    lastErr = tostring(resp)
                end
            end
            if sawAuthFailure then break end  -- don't waste the retry round
            if attempt < 2 then task.wait(0.8) end
        end
        err = err or lastErr
    end

    -- Was an in-engine extraction even possible? Clothing templates
    -- (Shirt/Pants) enqueue with partInst = nil because they aren't
    -- BaseParts — AssetService:CreateEditableImageAsync rejects their IDs,
    -- so they ALWAYS go through HTTP. If THAT also failed it's an
    -- inaccessible asset (off-sale UGC, private), not "player left".
    --
    -- For entries that DID have a partInst but lost it, distinguish two
    -- sub-cases: was the player still in the game (so the part itself was
    -- destroyed — tool unequipped, accessory removed, script-deleted), or
    -- has the player actually disconnected? The previous "player left"
    -- label fired for both, which read as confusing in logs where nobody
    -- had left.
    local hadAnyPart = false
    for _, r in ipairs(entry.partRefs) do
        if r.inst then hadAnyPart = true; break end
    end
    local ownerStillHere = false
    if hadAnyPart and ref == nil then
        for uid in pairs(entry.owners) do
            if Players:GetPlayerByUserId(uid) then
                ownerStillHere = true; break
            end
        end
    end
    -- "missed" stat counts only true disconnects.
    local missed = hadAnyPart and (ref == nil) and not ownerStillHere
    _markEntryFailed(entry, missed, dbg)
    if dbg then
        local tag
        if not hadAnyPart then
            tag = " (asset not publicly accessible — likely off-sale / "
                .. "private clothing / restricted UGC)"
        elseif ref == nil and ownerStillHere then
            tag = " (part instance destroyed — tool unequipped / "
                .. "accessory removed / script deleted)"
        elseif ref == nil then
            tag = " (player left before extraction)"
        else
            tag = ""
        end
        dbg(fmt("  EXTRACT %s %s FAILED%s — %s",
            entry.kind, entry.id, tag,
            tostring(err or "no path produced bytes")))
    end
    return false
end

-- Worker — single global coroutine. Drains the queue continuously, yielding
-- one frame between entries so the game gets to breathe. Politeness comes
-- from `extractMeshFromPart` yielding every 500 verts/faces.
--
-- Robustness: the worker writes its heartbeat timestamp to Q.lastIterationAt
-- every iteration. A separate watchdog respawns the worker if it dies. The
-- iteration body is double-pcall'd so nothing inside can silently kill it.
local function _startExtractorWorker()
    if not EXTRACT_OK then return end
    Q.workerVersion = (Q.workerVersion or 0) + 1
    local myVersion = Q.workerVersion
    Q.lastIterationAt = os.clock()
    Q.iterations = 0
    print("[ROCORDER] asset extractor worker v"
        .. tostring(myVersion) .. " starting")
    task.spawn(function()
        while Q.workerVersion == myVersion do
            -- Outer pcall: catches ANYTHING inside one iteration (including
            -- errors in queue manipulation, perPlayer updates, etc.) so a
            -- single bad extract or a typo can't take the whole loop down.
            pcall(function()
                Q.lastIterationAt = os.clock()
                Q.iterations = (Q.iterations or 0) + 1

                if #Q.queue == 0 then
                    Q.activeId = nil; Q.activeKind = nil; Q.activePlayer = nil
                    task.wait(0.1)
                    return
                end

                local entry = table.remove(Q.queue, 1)
                if not entry then return end
                Q.byId[entry.id] = nil
                Q.activeId = entry.id
                Q.activeKind = entry.kind
                for uid in pairs(entry.owners) do
                    local ps = Q.perPlayer[uid]
                    if ps then Q.activePlayer = ps.name; break end
                end

                local ok, err = pcall(_processOne, entry,
                    _G.ROCORDER_CURRENT_DBG)
                if not ok then
                    pcall(_markEntryFailed, entry, false)
                    if _G.ROCORDER_CURRENT_DBG then
                        pcall(_G.ROCORDER_CURRENT_DBG, fmt(
                            "  EXTRACT %s %s threw: %s",
                            entry.kind, entry.id, tostring(err)))
                    end
                end
                task.wait()
            end)
        end
        print("[ROCORDER] asset extractor worker v"
            .. tostring(myVersion) .. " exiting (replaced by v"
            .. tostring(Q.workerVersion) .. ")")
    end)
end

-- Watchdog — checks every 3 seconds whether the worker has updated its
-- heartbeat. If the queue is non-empty but the worker has been silent for
-- more than 5 seconds, respawn it. Also self-exits when its workerVersion
-- gets bumped (so reloads don't accumulate watchdogs).
local function _startWatchdog()
    if not EXTRACT_OK then return end
    local myWatchdogVersion = (Q.watchdogVersion or 0) + 1
    Q.watchdogVersion = myWatchdogVersion
    task.spawn(function()
        while Q.watchdogVersion == myWatchdogVersion do
            task.wait(3.0)
            local age = os.clock() - (Q.lastIterationAt or 0)
            if #Q.queue > 0 and age > 5.0 then
                warn(fmt("[ROCORDER] extractor worker silent for %.1fs with "
                    .. "%d items queued — respawning", age, #Q.queue))
                _startExtractorWorker()
            end
        end
    end)
end

-- ============================================================================
-- Instant-Replay cache pruning.
--
-- IR keeps an N-second rolling buffer. Players who join and leave outside
-- that window are deadweight — their assets will never be referenced by any
-- saveable replay. Without pruning, an hour-long IR session in a busy game
-- can accumulate hundreds of MB of meshes/textures from people who came,
-- left, and never made it into a saved clip.
--
-- The scanner:
--   1. Runs only when IR is on AND no normal recording session is active.
--   2. For each known asset id, checks whether any owning player was seen
--      within (IR_BUFFER_SEC + 5s) on the recording side. If not, deletes
--      every form of the file (.geom.json / .rgba / bare) and forgets it.
--   3. Skips if a save is in progress (set by SaveReplay).
-- ============================================================================
_G.ROCORDER_SAVE_IN_PROGRESS = _G.ROCORDER_SAVE_IN_PROGRESS or false

-- Collect every asset id that any saved .rig.json on disk references. The
-- ROCORDER/assets/ folder is SHARED across all recordings on disk — evicting
-- an id that an older saved .rec still references would silently corrupt
-- that recording on import. Cheap regex scrape (avoids full JSONDecode) of
-- the long numeric ids out of each .rig.json body.
local function _collectKeptAssetIds()
    local kept = {}
    if not (listfiles and readfile) then return kept end
    local ok, files = pcall(listfiles, FOLDER)
    if not ok or type(files) ~= "table" then return kept end
    for _, path in ipairs(files) do
        local norm = path:gsub("\\", "/")
        if norm:sub(-9) == ".rig.json" then
            local okR, body = pcall(readfile, path)
            if okR and type(body) == "string" then
                for id in body:gmatch("(%d%d%d%d%d+)") do
                    kept[id] = true
                end
            end
        end
    end
    return kept
end

local function _evictStaleIRAssets(rec)
    if not rec or not rec.cfg then return end
    if not rec.cfg.IR_ENABLED then return end
    if rec.session then return end  -- normal recording running -> never evict
    if _G.ROCORDER_SAVE_IN_PROGRESS then return end
    if not (delfile and isfile) then return end

    local tracker = rec.tracker
    if not tracker then return end
    local now = os.clock()
    local windowSec = (rec.cfg.IR_BUFFER_SEC or 30) + 5.0
    local kept = _collectKeptAssetIds()
    local evicted = 0
    local keptCount = 0
    local filesDeleted = 0

    for id, owners in pairs(ASSET_OWNERS) do
        if kept[id] then
            -- Referenced by a saved recording — must not delete.
            keptCount = keptCount + 1
        else
            local anyLive = false
            for uid in pairs(owners) do
                local e = tracker.tracked[uid]
                if e and e.lastSeenClock and (now - e.lastSeenClock) <= windowSec then
                    anyLive = true
                    break
                end
            end
            if not anyLive then
                for _, path in ipairs({_geomPath(id), _imgPath(id), _binPath(id)}) do
                    if isfile(path) then
                        if pcall(delfile, path) then
                            filesDeleted = filesDeleted + 1
                        end
                    end
                end
                ASSET_OWNERS[id] = nil
                EXTRACTED[id] = nil
                evicted = evicted + 1
            end
        end
    end

    if evicted > 0 and _G.ROCORDER_CURRENT_DBG then
        pcall(_G.ROCORDER_CURRENT_DBG, fmt(
            "IR cache: evicted %d stale asset(s) (%d files deleted, %d kept "
            .. "by saved recordings) — buffer window %.0fs",
            evicted, filesDeleted, keptCount, windowSec))
    end
end

local function _startEvictionScanner(rec)
    if not EXTRACT_OK then return end
    Q.evictVersion = (Q.evictVersion or 0) + 1
    local myV = Q.evictVersion
    task.spawn(function()
        -- Initial 10s grace so a freshly-joined player isn't evicted before
        -- their first tick lands in the buffer.
        task.wait(10.0)
        while Q.evictVersion == myV do
            pcall(_evictStaleIRAssets, rec)
            task.wait(3.0)
        end
    end)
end

-- Public enqueue helper used by Tracker:ensure. Walks a single (part, partInfo)
-- and enqueues every asset reference. Cheap — no yields, just bookkeeping.
local function enqueuePartAssets(part, partInfo, uid, displayName)
    if not isfolder(ASSETS_FOLDER) then pcall(makefolder, ASSETS_FOLDER) end
    local meshId = _assetIdFromRef(partInfo.meshId)
    if meshId then
        _enqueueAsset(meshId, "mesh", part, partInfo, uid, displayName)
    end
    local imgs = {}
    if partInfo.textureId then imgs[#imgs+1] = partInfo.textureId end
    if partInfo.colorMap  then imgs[#imgs+1] = partInfo.colorMap  end
    if partInfo.decals then
        for _, d in ipairs(partInfo.decals) do
            if d.texture then imgs[#imgs+1] = d.texture end
        end
    end
    for _, ref in ipairs(imgs) do
        local id = _assetIdFromRef(ref)
        if id then
            _enqueueAsset(id, "image", part, partInfo, uid, displayName)
        end
    end
end

local function enqueueClothing(clothing, uid, displayName)
    if not clothing then return end
    for _, key in ipairs({ "shirt", "pants", "tshirt" }) do
        local ref = clothing[key]
        local id = _assetIdFromRef(ref)
        if id then
            -- partInst is nil — these aren't a single Instance. The extractor
            -- will use the rbxassetid URL directly via Content.fromUri.
            _enqueueAsset(id, "image", nil, { textureId = ref }, uid, displayName)
        end
    end
end

-- Public read-only snapshot for the UI status loop.
local function queueSnapshot()
    return {
        queued       = #Q.queue,
        done         = Q.stats.done,
        failed       = Q.stats.failed,
        missed       = Q.stats.missed,
        totalSeen    = Q.stats.totalSeen,
        activeId     = Q.activeId,
        activeKind   = Q.activeKind,
        activePlayer = Q.activePlayer,
        perPlayer    = Q.perPlayer,
        workerAgeSec = os.clock() - (Q.lastIterationAt or 0),
        iterations   = Q.iterations or 0,
    }
end

----------------------------------------------------------------
-- Rig capture
----------------------------------------------------------------
-- Pull every asset reference + geometry hint off a single BasePart so the
-- Blender importer can fetch the real mesh + texture instead of a box.
-- Handles MeshPart (MeshId/TextureID/SurfaceAppearance) AND legacy
-- Part+SpecialMesh hats/gear (SpecialMesh.MeshId/TextureId/Scale).
local function partInfo(part, boneName)
    local cf = part.CFrame
    local info = {
        name         = boneName,
        className    = part.ClassName,
        size         = { part.Size.X, part.Size.Y, part.Size.Z },
        color        = { part.Color.R, part.Color.G, part.Color.B },
        transparency = part.Transparency,
        restCFrame   = { cf:GetComponents() },
    }
    if part:IsA("Part") then
        info.shape = part.Shape.Name
    elseif part:IsA("MeshPart") then
        info.shape = "MeshPart"
        local ok, m = pcall(function() return part.MeshId end)
        if ok and m and m ~= "" then info.meshId = m end
        local ok2, tx = pcall(function() return part.TextureID end)
        if ok2 and tx and tx ~= "" then info.textureId = tx end
    elseif part:IsA("WedgePart") then
        info.shape = "Wedge"
    else
        info.shape = "Block"
    end

    -- legacy SpecialMesh (Part-based hats/tools/classic heads):
    -- MeshId/TextureId/Scale + MeshType (Head/Sphere/Cylinder/FileMesh/...)
    local sm = part:FindFirstChildOfClass("SpecialMesh")
        or part:FindFirstChildOfClass("FileMesh")
    if sm then
        local ok, mid = pcall(function() return sm.MeshId end)
        if ok and mid and mid ~= "" then info.meshId = mid; info.shape = "FileMesh" end
        local ok2, tid = pcall(function() return sm.TextureId end)
        if ok2 and tid and tid ~= "" then info.textureId = tid end
        local oksc, sc = pcall(function() return sm.Scale end)
        if oksc and sc then info.meshScale = { sc.X, sc.Y, sc.Z } end
        local okmt, mt = pcall(function() return sm.MeshType end)
        if okmt and mt then info.meshType = mt.Name end
    end

    -- SurfaceAppearance PBR color map (modern layered clothing / accessories)
    local sa = part:FindFirstChildOfClass("SurfaceAppearance")
    if sa then
        local ok, cm = pcall(function() return sa.ColorMap end)
        if ok and cm and cm ~= "" then info.colorMap = cm end
    end

    -- Decals / Textures on the part (the classic FACE lives here, plus logos
    -- and surface images). Texture is a subclass of Decal, so this catches both.
    local decals = {}
    for _, d in ipairs(part:GetChildren()) do
        if d:IsA("Decal") then
            local okt, tx = pcall(function() return d.Texture end)
            if okt and tx and tx ~= "" then
                local face = "Front"
                local okf, fc = pcall(function() return d.Face end)
                if okf and fc then face = fc.Name end
                decals[#decals + 1] = {
                    name = d.Name, texture = tx, face = face,
                    isTexture = d:IsA("Texture"),
                }
            end
        end
    end
    if #decals > 0 then info.decals = decals end

    return info
end

-- Deep-scan a character into an ordered part list. Direct-child BaseParts
-- come first (these are the rig body parts that Motor6D joints reference by
-- name); everything else attached (accessory Handles, tool parts, nested
-- meshes) follows. Returns rig (parts + joints) and refs = { {name, inst}, ... }
-- where `inst` is a live Instance reference (so duplicate-named accessory
-- parts each resolve to the right object every tick).
local function captureRig(player)
    local char = player.Character
    if not char then return nil end
    local rig = {
        userId      = player.UserId,
        name        = player.Name,
        displayName = player.DisplayName,
        rigType     = "Custom",
        parts       = {},
        joints      = {},
    }
    local humanoid = char:FindFirstChildOfClass("Humanoid")
    if humanoid then
        if humanoid.RigType == Enum.HumanoidRigType.R15 then rig.rigType = "R15"
        elseif humanoid.RigType == Enum.HumanoidRigType.R6 then rig.rigType = "R6" end
    end

    -- classic clothing (wraps the blocky body via Roblox's UV template)
    local clothing = {}
    local shirt = char:FindFirstChildOfClass("Shirt")
    if shirt then
        local ok, t = pcall(function() return shirt.ShirtTemplate end)
        if ok and t and t ~= "" then clothing.shirt = t end
    end
    local pants = char:FindFirstChildOfClass("Pants")
    if pants then
        local ok, t = pcall(function() return pants.PantsTemplate end)
        if ok and t and t ~= "" then clothing.pants = t end
    end
    local tshirt = char:FindFirstChildOfClass("ShirtGraphic")
    if tshirt then
        local ok, t = pcall(function() return tshirt.Graphic end)
        if ok and t and t ~= "" then clothing.tshirt = t end
    end
    if next(clothing) then rig.clothing = clothing end

    local refs = {}
    local used = {}
    local function uniqueName(raw)
        local nm = sanitizeName(raw)
        local cand, i = nm, 1
        while used[cand] do i = i + 1; cand = nm .. "_" .. i end
        used[cand] = true
        return cand
    end

    local directSet = {}
    for _, child in ipairs(char:GetChildren()) do
        if child:IsA("BasePart") then
            directSet[child] = true
            local nm = uniqueName(child.Name)
            rig.parts[#rig.parts + 1] = partInfo(child, nm)
            refs[#refs + 1] = { name = nm, inst = child }
        end
    end
    for _, desc in ipairs(char:GetDescendants()) do
        if desc:IsA("BasePart") and not directSet[desc] then
            local nm = uniqueName(desc.Name)
            rig.parts[#rig.parts + 1] = partInfo(desc, nm)
            refs[#refs + 1] = { name = nm, inst = desc }
        end
    end

    for _, dsc in ipairs(char:GetDescendants()) do
        if dsc:IsA("Motor6D") then
            local p0, p1 = dsc.Part0, dsc.Part1
            if p0 and p1 then
                rig.joints[#rig.joints + 1] = {
                    name  = dsc.Name,
                    part0 = sanitizeName(p0.Name),
                    part1 = sanitizeName(p1.Name),
                    c0    = { dsc.C0:GetComponents() },
                    c1    = { dsc.C1:GetComponents() },
                }
            end
        end
    end
    return rig, refs
end

----------------------------------------------------------------
-- Tracker — persistent per-player rig + last-pose cache. Lives across
-- sessions and across the instant-replay buffer.
----------------------------------------------------------------
local Tracker = {}; Tracker.__index = Tracker

function Tracker.new()
    return setmetatable({ tracked = {}, pending = {} }, Tracker)
end

function Tracker:reset() self.tracked = {} end

function Tracker:_makeEncoder(posPrec, rotPrec)
    local posFmt = "%." .. posPrec .. "f"
    local rotFmt = "%." .. rotPrec .. "f"
    local lineFmt = posFmt .. "," .. posFmt .. "," .. posFmt .. ","
                 .. rotFmt .. "," .. rotFmt .. "," .. rotFmt .. "," .. rotFmt
    return function(cf)
        local px,py,pz, r00,r01,r02, r10,r11,r12, r20,r21,r22 = cf:GetComponents()
        local qx,qy,qz,qw = matrixToQuat(r00,r01,r02,r10,r11,r12,r20,r21,r22)
        return fmt(lineFmt, px,py,pz, qx,qy,qz,qw)
    end
end

-- Re-scan for newly-attached parts (a tool/accessory equipped mid-recording)
-- and append them. Only APPENDS — never reorders/removes — so the positional
-- frame indices stay aligned (new parts simply have no keyframes before they
-- appeared, exactly like a late-joining player). Also enqueues assets for
-- the new part — the previous "append only, don't enqueue" behavior meant
-- a tool equipped mid-recording (Handle ATTACHED) showed up in the .rec
-- file but its mesh/texture had to be rescued by the end-of-recording legacy
-- HTTP pass, never appearing in the live extractor UI.
function Tracker:_appendNewParts(entry, char, encode, debugLog)
    local used = {}
    for _, r in ipairs(entry.refs) do used[r.name] = true end
    local function uniqueName(raw)
        local nm = sanitizeName(raw)
        local cand, i = nm, 1
        while used[cand] do i = i + 1; cand = nm .. "_" .. i end
        used[cand] = true
        return cand
    end
    local added = 0
    for _, desc in ipairs(char:GetDescendants()) do
        if desc:IsA("BasePart") and not entry.known[desc] then
            entry.known[desc] = true
            local nm = uniqueName(desc.Name)
            local idx = #entry.refs + 1
            local info = partInfo(desc, nm)
            entry.refs[idx] = { name = nm, inst = desc }
            entry.rig.parts[idx] = info
            entry.last[idx] = encode(desc.CFrame)
            added += 1
            if debugLog then
                debugLog(fmt("uid=%d part[%d] %s ATTACHED mid-recording (%s)",
                    entry.uid, idx - 1, nm, info.shape or "?"))
            end
            if EXTRACT_OK then
                enqueuePartAssets(desc, info, entry.uid, entry.displayName)
            end
        end
    end
    return added
end

-- Re-run partInfo on existing refs and enqueue any assets that materialized
-- since the initial ENSURE. Avatars stream in stages on Roblox: skeleton +
-- Motor6Ds first (which the readiness gate waits for), THEN mesh content
-- attaches as SpecialMesh children or MeshPart MeshId/TextureID populate.
-- A player joining mid-recording often captures with shape=Block parts and
-- no meshId; ~1 s later the engine fills in the FileMesh. This sweep catches
-- that and enqueues the now-available asset ids. Cheap: _enqueueAsset
-- dedupes via EXTRACTED + Q.byId so re-calling for already-seen ids is a
-- no-op.
--
-- Settled flag: after 3 consecutive empty rescans (no new content arrived
-- on any of this player's existing refs) we declare content "settled" and
-- stop rescanning that player. This eliminates the ~1 Hz frame stutter the
-- user reported in 1.9.9 — partInfo() does ~15 pcall'd property reads per
-- part, so 4 players × 20 parts × 15 pcalls = ~1200 pcalls/sec spent
-- looking for content that arrived seconds ago and isn't going to change.
-- The flag is reset on respawn so a swapped Character is re-scanned.
function Tracker:_rescanExistingAssets(entry, debugLog)
    if not EXTRACT_OK then return end
    if entry.rescanSettled then return end
    local foundNew = false
    for i, r in ipairs(entry.refs) do
        local inst = r.inst
        if inst and inst.Parent and inst:IsA("BasePart") then
            local old = entry.rig.parts[i]
            local oldName = (old and old.name) or r.name
            local new = partInfo(inst, oldName)
            local upgraded = false
            if new.meshId and not (old and old.meshId) then upgraded = true end
            if new.textureId and not (old and old.textureId) then upgraded = true end
            if new.colorMap and not (old and old.colorMap) then upgraded = true end
            local oldDecals = (old and old.decals) or {}
            if new.decals and #new.decals > #oldDecals then upgraded = true end
            if upgraded then
                foundNew = true
                if debugLog then
                    debugLog(fmt("uid=%d part[%d] %s mesh content arrived (mesh=%s tex=%s)",
                        entry.uid, i - 1, new.name,
                        tostring(new.meshId), tostring(new.textureId)))
                end
                entry.rig.parts[i] = new
                enqueuePartAssets(inst, new, entry.uid, entry.displayName)
            end
        end
    end
    if foundNew then
        entry.consecutiveEmptyRescans = 0
    else
        local n = (entry.consecutiveEmptyRescans or 0) + 1
        entry.consecutiveEmptyRescans = n
        if n >= 3 then
            entry.rescanSettled = true
            if debugLog then
                debugLog(fmt("uid=%d asset rescan settled (no new content "
                    .. "for 3 consecutive scans)", entry.uid))
            end
        end
    end
end

-- Rebuild the part references after a respawn (new Character = new Instances).
-- Re-captures the new character, then re-points each existing index's `inst`
-- by name so frame indices stay aligned; genuinely new parts are appended.
function Tracker:_rebuildRefs(entry, player, encode, debugLog)
    local ok, _rig, refs = pcall(captureRig, player)
    if not ok or not refs then return end
    local byName = {}
    for _, r in ipairs(refs) do byName[r.name] = r.inst end
    -- re-point existing
    for _, r in ipairs(entry.refs) do
        r.inst = byName[r.name]  -- nil if that part no longer exists
    end
    -- rebuild known-set + append any new parts
    entry.known = {}
    for _, r in ipairs(entry.refs) do
        if r.inst then entry.known[r.inst] = true end
    end
    entry.char = player.Character
    -- New Character means content streaming starts over. Reopen the rescan
    -- gate so newly-attached parts whose mesh content arrives late get
    -- caught by _rescanExistingAssets again.
    entry.rescanSettled = false
    entry.consecutiveEmptyRescans = 0
    self:_appendNewParts(entry, player.Character, encode, debugLog)
    if debugLog then
        debugLog(fmt("uid=%d refs rebuilt after respawn (%d parts)",
            entry.uid, #entry.refs))
    end
end

function Tracker:ensure(player, encode, debugLog)
    local uid = player.UserId
    if self.tracked[uid] then return self.tracked[uid] end
    local char = player.Character
    if not char then return nil end

    -- Readiness gate: don't capture a half-loaded character. If we capture
    -- before the Motor6Ds exist we get rigType=Custom / joints=0 and every
    -- part piles at the origin (the broken-player bug). Wait until the rig
    -- exists, with a short grace fallback for genuinely jointless models.
    local firstSeen = self.pending[uid]
    if not firstSeen then firstSeen = os.clock(); self.pending[uid] = firstSeen end
    local hrp = char:FindFirstChild("HumanoidRootPart")
    local hasMotor = char:FindFirstChildWhichIsA("Motor6D", true) ~= nil
    if not (hrp and (hasMotor or (os.clock() - firstSeen) > 2.0)) then
        return nil  -- try again next tick
    end

    local ok, rig, refs = pcall(captureRig, player)
    if not ok or not rig then
        if debugLog then
            debugLog(fmt("captureRig FAILED for uid=%d (%s) err=%s",
                uid, player.Name, tostring(rig)))
        end
        return nil
    end
    self.pending[uid] = nil
    local entry = {
        uid = uid,
        displayName = player.DisplayName or player.Name,
        rig = rig, refs = refs, last = {},
        char = player.Character, known = {},
        ticks = 0, culledTicks = 0,
        hadChar = true, partLost = {}, rangeIn = true,
        lastScanClock = os.clock(),
    }
    for i, r in ipairs(refs) do
        if r.inst then entry.known[r.inst] = true end
        entry.last[i] = (r.inst and r.inst:IsA("BasePart"))
            and encode(r.inst.CFrame) or "0,0,0,0,0,0,1"
        if not r.inst then entry.partLost[i] = true end
    end
    self.tracked[uid] = entry

    -- Enqueue every asset for the global extractor worker. The worker pops
    -- one at a time when frame health is good, so there are no spikes from
    -- a flood of player joins (e.g. during a long Instant Replay session).
    if EXTRACT_OK then
        local displayName = player.DisplayName or player.Name
        for i, r in ipairs(refs) do
            if r.inst then
                enqueuePartAssets(r.inst, rig.parts[i], uid, displayName)
            end
        end
        enqueueClothing(rig.clothing, uid, displayName)
    end

    if debugLog then
        local meshes = 0
        for _, p in ipairs(rig.parts) do if p.meshId then meshes += 1 end end
        debugLog(fmt("ENSURE uid=%d name=%s display=%s rigType=%s parts=%d joints=%d meshes=%d",
            uid, player.Name, player.DisplayName, rig.rigType,
            #rig.parts, #rig.joints, meshes))
        local lines = {}
        for i, p in ipairs(rig.parts) do
            lines[#lines+1] = fmt(
                "  [%d] %s class=%s shape=%s%s transparency=%.3f%s%s%s%s",
                i-1, p.name, p.className or "?", p.shape or "?",
                p.meshType and ("(" .. p.meshType .. ")") or "",
                p.transparency or 0,
                p.meshId and (" mesh=" .. p.meshId) or "",
                p.textureId and (" tex=" .. p.textureId) or "",
                p.colorMap and (" colorMap=" .. p.colorMap) or "",
                p.decals and (" decals=" .. #p.decals) or "")
        end
        debugLog("  parts:\n" .. table.concat(lines, "\n"))
        if rig.clothing then
            debugLog(fmt("  clothing: shirt=%s pants=%s tshirt=%s",
                tostring(rig.clothing.shirt), tostring(rig.clothing.pants),
                tostring(rig.clothing.tshirt)))
        end
    end
    return entry
end

function Tracker:snapshot(cfg, encode, debugLog)
    local entries = {}
    local cameraPos
    local cam = workspace.CurrentCamera
    if cam then cameraPos = cam.CFrame.Position end
    local maxDistSq = cfg.MAX_DISTANCE > 0 and (cfg.MAX_DISTANCE * cfg.MAX_DISTANCE) or nil
    local localPlayer = Players.LocalPlayer
    local now = os.clock()

    for _, p in ipairs(Players:GetPlayers()) do
        if cfg.INCLUDE_LOCAL or p ~= localPlayer then
            local entry = self:ensure(p, encode, debugLog)
            if entry then
                local char = p.Character
                local hasChar = char ~= nil
                if hasChar ~= entry.hadChar then
                    if debugLog then
                        debugLog(fmt("uid=%d character %s", p.UserId,
                            hasChar and "REGAINED" or "LOST"))
                    end
                    entry.hadChar = hasChar
                end

                -- respawn / avatar swap: Character identity changed -> rebuild refs
                if hasChar and char ~= entry.char then
                    self:_rebuildRefs(entry, p, encode, debugLog)
                end
                -- mid-recording equip & late mesh streaming: throttled
                -- append-scan + asset rescan (~1/s). _rescanExistingAssets
                -- catches Heads / accessories that captured as Block on
                -- ENSURE because mesh content hadn't streamed in yet.
                if hasChar and now - entry.lastScanClock >= 1.0 then
                    entry.lastScanClock = now
                    self:_appendNewParts(entry, char, encode, debugLog)
                    self:_rescanExistingAssets(entry, debugLog)
                end

                local inRange = true
                if maxDistSq and cameraPos and char then
                    local root = char:FindFirstChild("HumanoidRootPart")
                    if root then
                        local d = root.Position - cameraPos
                        if (d.X*d.X + d.Y*d.Y + d.Z*d.Z) > maxDistSq then inRange = false end
                    end
                end
                if inRange ~= entry.rangeIn and debugLog then
                    debugLog(fmt("uid=%d %s distance range", p.UserId,
                        inRange and "ENTERED" or "EXITED"))
                    entry.rangeIn = inRange
                end

                if inRange then
                    local parts = {}
                    for i, r in ipairs(entry.refs) do
                        local s
                        local pt = r.inst
                        local lost = not (pt and pt.Parent and pt:IsA("BasePart"))
                        if not lost then
                            s = encode(pt.CFrame); entry.last[i] = s
                        else
                            s = entry.last[i] or "0,0,0,0,0,0,1"
                        end
                        if lost ~= entry.partLost[i] and debugLog then
                            debugLog(fmt("uid=%d part[%d] %s %s", p.UserId, i-1,
                                (entry.rig.parts[i] and entry.rig.parts[i].name) or "?",
                                lost and "LOST (using cache)" or "REGAINED"))
                            entry.partLost[i] = lost
                        end
                        parts[i] = s
                    end
                    entries[#entries+1] = tostring(p.UserId) .. ":" .. table.concat(parts, "|")
                    entry.ticks += 1
                    entry.lastSeenClock = now  -- for per-session rig filtering
                else
                    entry.culledTicks += 1
                end
            end
        end
    end
    return entries
end

-- Build the rig payload. `sinceClock` (optional) excludes players not seen
-- since that clock time — the tracker persists across recordings (for Instant
-- Replay), so without this filter a recording's rig would include stale players
-- from earlier sessions who have NO frames in this .rec (they import as a
-- frozen pile of boxes at the origin).
function Tracker:rigData(filenameForRef, sinceClock)
    local data = {
        format = "ROCORDER-RIG/2",
        recFile = filenameForRef,
        capturedAt = os.time(),
        players = {},
    }
    local n = 0
    for uid, e in pairs(self.tracked) do
        if (not sinceClock) or (e.lastSeenClock and e.lastSeenClock >= sinceClock) then
            data.players[tostring(uid)] = e.rig
            n += 1
        end
    end
    return data, n
end

----------------------------------------------------------------
-- Session — one on-disk recording (.rec + .rig.json + .debug.log)
----------------------------------------------------------------
local Session = {}; Session.__index = Session

function Session.new(folder, cfg, debugEnabled)
    local base = fmt("replay_%d_%d", game.PlaceId, os.time())
    local self = setmetatable({
        folder        = folder,
        filename      = base .. ".rec",
        rigFilename   = base .. ".rig.json",
        debugFilename = base .. ".debug.log",
        metaFilename  = base .. ".meta.json",
        debugEnabled  = debugEnabled,
        buffer        = {},
        debugBuffer   = {},
        tickCount     = 0,
        gapCount      = 0,
        stallCount    = 0,
        startedAt     = os.time(),
        startClock    = os.clock(),
        cfg           = cfg,
        bytesWritten  = 0,
        placeId       = game.PlaceId,
        placeName     = lookupGameName(game.PlaceId),
    }, Session)
    return self
end

function Session:_path(name) return self.folder .. "/" .. name end

function Session:writeHeader(roster)
    local header = {
        format       = "ROCORDER/3",
        recorder     = ROCORDER_VERSION,
        placeId      = self.placeId,
        placeName    = self.placeName,
        jobId        = game.JobId,
        startedAt    = self.startedAt,
        tickRate     = self.cfg.TICK_RATE,
        posPrecision = self.cfg.POS_PRECISION,
        rotPrecision = self.cfg.ROT_PRECISION,
        captureMode  = "parts-posquat",
        rigFile      = self.rigFilename,
        metaFile     = self.metaFilename,
        debugFile    = self.debugEnabled and self.debugFilename or nil,
        sources      = {
            playerParts = self.cfg.SRC_PLAYER_PARTS and true or false,
            camera      = self.cfg.SRC_CAMERA      and true or false,
        },
        columns       = { "px","py","pz","qx","qy","qz","qw" },
        cameraColumns = { "px","py","pz","qx","qy","qz","qw","fov" },
        roster        = roster,
    }
    local s = HttpService:JSONEncode(header) .. "\n"
    self.bytesWritten = #s
    pcall(writefile, self:_path(self.filename), s)
    if self.debugEnabled then
        pcall(writefile, self:_path(self.debugFilename),
            fmt("ROCORDER %s debug log for %s\n", ROCORDER_VERSION, self.filename))
        self:debugLog(fmt(
            "START tickRate=%d posPrec=%d rotPrec=%d maxCatchup=%.1fs maxDistance=%d "
         .. "IR=%s IRbuf=%ds",
            self.cfg.TICK_RATE, self.cfg.POS_PRECISION, self.cfg.ROT_PRECISION,
            self.cfg.MAX_CATCHUP_SEC, self.cfg.MAX_DISTANCE,
            tostring(self.cfg.IR_ENABLED), self.cfg.IR_BUFFER_SEC))
        -- Extractor backend status: did the Actor scaffold (1.9.18+) load,
        -- or are we on the serial fallback? Persisted to the debug log so
        -- we don't need the Roblox dev console to know which path is live.
        self:debugLog(fmt("EXTRACTOR backend=%s%s",
            _G.ROCORDER_ACTOR_OK and "actor-parallel" or "serial",
            (_G.ROCORDER_ACTOR_OK == false and _G.ROCORDER_ACTOR_ERR)
                and (" (actor probe failed: " .. _G.ROCORDER_ACTOR_ERR .. ")")
                or ""))
    end
end

function Session:writeFrame(t, snapshot)
    local n = #snapshot
    local line = table.create and table.create(n + 1) or {}
    line[1] = "t=" .. fmt("%." .. self.cfg.POS_PRECISION .. "f", t)
    for i = 1, n do line[i + 1] = snapshot[i] end
    self.buffer[#self.buffer + 1] = table.concat(line, ";")
    self.tickCount += 1
end

function Session:flush()
    if #self.buffer == 0 then return end
    local chunk = table.concat(self.buffer, "\n") .. "\n"
    local ok, err = pcall(appendfile, self:_path(self.filename), chunk)
    if ok then
        self.bytesWritten = self.bytesWritten + #chunk
        table.clear(self.buffer)
    else
        self:debugLog(fmt("*** FLUSH FAILED: %s — kept %d lines for retry",
            tostring(err), #self.buffer))
    end
end

function Session:debugLog(msg)
    if not self.debugEnabled then return end
    local t = self.startClock > 0 and (os.clock() - self.startClock) or 0
    self.debugBuffer[#self.debugBuffer + 1] = fmt("[t=%7.3f] %s", t, msg)
end

function Session:flushDebug()
    if not self.debugEnabled or #self.debugBuffer == 0 then return end
    local chunk = table.concat(self.debugBuffer, "\n") .. "\n"
    local ok = pcall(appendfile, self:_path(self.debugFilename), chunk)
    if ok then table.clear(self.debugBuffer) end
end

function Session:writeRig(rigData)
    local ok = pcall(writefile, self:_path(self.rigFilename),
        HttpService:JSONEncode(rigData))
    return ok
end

-- Sidecar meta file written at Stop. Holds the things the Files-tab UI wants
-- without needing to scan the (possibly huge) .rec for them.
function Session:writeMeta()
    local meta = {
        format      = "ROCORDER-META/1",
        recFile     = self.filename,
        recorder    = ROCORDER_VERSION,
        placeId     = self.placeId,
        placeName   = self.placeName,
        jobId       = game.JobId,
        startedAt   = self.startedAt,
        endedAt     = os.time(),
        durationSec = self:elapsed(),
        frameCount  = self.tickCount,
        byteCount   = self.bytesWritten,
        tickRate    = self.cfg.TICK_RATE,
        sources     = {
            playerParts = self.cfg.SRC_PLAYER_PARTS and true or false,
            camera      = self.cfg.SRC_CAMERA      and true or false,
        },
    }
    pcall(writefile, self:_path(self.metaFilename),
        HttpService:JSONEncode(meta))
end

function Session:elapsed() return os.clock() - self.startClock end

----------------------------------------------------------------
-- Replay — circular buffer of the last N seconds of frames.
----------------------------------------------------------------
local Replay = {}; Replay.__index = Replay

function Replay.new(maxSec, tickRate)
    return setmetatable({
        maxFrames = math.max(1, math.ceil(maxSec * tickRate)),
        tickRate  = tickRate,
        buf       = {},
        head      = 1,
        count     = 0,
    }, Replay)
end

function Replay:reconfigure(maxSec, tickRate)
    local newMax = math.max(1, math.ceil(maxSec * tickRate))
    if newMax == self.maxFrames and tickRate == self.tickRate then return end
    self.maxFrames = newMax
    self.tickRate = tickRate
    self.buf = {}
    self.head = 1
    self.count = 0
end

function Replay:push(t, snapshot)
    local i = ((self.head - 1) % self.maxFrames) + 1
    self.buf[i] = { t = t, snapshot = snapshot }
    self.head = i + 1
    if self.count < self.maxFrames then self.count = self.count + 1 end
end

function Replay:clear() self.buf = {}; self.head = 1; self.count = 0 end

function Replay:seconds()
    if self.count < 2 then return 0 end
    -- compute oldest..newest spread
    local newestIdx = self.head - 1; if newestIdx < 1 then newestIdx = self.maxFrames end
    local oldestIdx
    if self.count < self.maxFrames then oldestIdx = 1
    else oldestIdx = self.head end
    if oldestIdx > self.maxFrames then oldestIdx = oldestIdx - self.maxFrames end
    local newest = self.buf[newestIdx]
    local oldest = self.buf[oldestIdx]
    if not (newest and oldest) then return 0 end
    return math.max(0, newest.t - oldest.t)
end

-- Save the last `seconds` seconds (or all available if seconds == nil) to disk.
-- `rigData` is built from the Tracker by the caller. Returns the saved filename
-- or nil + error string.
function Replay:save(folder, cfg, rigData, lastSeconds)
    if self.count == 0 then return nil, "buffer is empty" end

    -- ordered list of frames, oldest first
    local frames = {}
    if self.count < self.maxFrames then
        for i = 1, self.count do frames[#frames + 1] = self.buf[i] end
    else
        for i = self.head, self.maxFrames do frames[#frames + 1] = self.buf[i] end
        for i = 1, self.head - 1 do frames[#frames + 1] = self.buf[i] end
    end
    if #frames == 0 then return nil, "buffer empty after order" end

    local newestT = frames[#frames].t
    local cutoffT = lastSeconds and (newestT - lastSeconds) or -math.huge
    -- drop frames older than cutoff
    local kept = {}
    for _, f in ipairs(frames) do
        if f.t >= cutoffT then kept[#kept + 1] = f end
    end
    if #kept == 0 then return nil, "no frames in window" end

    local t0 = kept[1].t
    local base = fmt("replay_%d_%d_clip", game.PlaceId, os.time())
    local recName = base .. ".rec"
    local rigName = base .. ".rig.json"
    rigData.recFile = recName

    -- roster taken from the rigData we received
    local roster = {}
    for uidStr, rig in pairs(rigData.players) do
        roster[#roster + 1] = {
            userId      = tonumber(uidStr) or rig.userId,
            name        = rig.name,
            displayName = rig.displayName,
        }
    end

    local placeName = lookupGameName(game.PlaceId)
    local metaName  = base .. ".meta.json"
    local duration  = newestT - t0
    local header = {
        format        = "ROCORDER/3",
        recorder      = ROCORDER_VERSION,
        placeId       = game.PlaceId,
        placeName     = placeName,
        jobId         = game.JobId,
        startedAt     = os.time(),
        tickRate      = cfg.TICK_RATE,
        posPrecision  = cfg.POS_PRECISION,
        rotPrecision  = cfg.ROT_PRECISION,
        captureMode   = "parts-posquat",
        rigFile       = rigName,
        metaFile      = metaName,
        source        = "instant-replay",
        clipSeconds   = duration,
        columns       = { "px","py","pz","qx","qy","qz","qw" },
        cameraColumns = { "px","py","pz","qx","qy","qz","qw","fov" },
        roster        = roster,
    }

    local lines = { HttpService:JSONEncode(header) }
    local tFmt = "t=%." .. cfg.POS_PRECISION .. "f"
    for _, f in ipairs(kept) do
        local row = { fmt(tFmt, f.t - t0) }
        for i = 1, #f.snapshot do row[#row + 1] = f.snapshot[i] end
        lines[#lines + 1] = table.concat(row, ";")
    end

    local body = table.concat(lines, "\n") .. "\n"
    local ok1, err1 = pcall(writefile, folder .. "/" .. recName, body)
    if not ok1 then return nil, "writefile .rec failed: " .. tostring(err1) end
    local ok2, err2 = pcall(writefile, folder .. "/" .. rigName,
        HttpService:JSONEncode(rigData))
    if not ok2 then return nil, "writefile .rig failed: " .. tostring(err2) end

    -- meta sidecar (so the clip appears with full info in the Files tab)
    local meta = {
        format      = "ROCORDER-META/1",
        recFile     = recName,
        recorder    = ROCORDER_VERSION,
        placeId     = game.PlaceId,
        placeName   = placeName,
        jobId       = game.JobId,
        startedAt   = header.startedAt,
        endedAt     = header.startedAt,
        durationSec = duration,
        frameCount  = #kept,
        byteCount   = #body,
        tickRate    = cfg.TICK_RATE,
        source      = "instant-replay",
        sources     = {
            playerParts = cfg.SRC_PLAYER_PARTS and true or false,
            camera      = cfg.SRC_CAMERA      and true or false,
        },
    }
    pcall(writefile, folder .. "/" .. metaName, HttpService:JSONEncode(meta))

    return recName, #kept, duration
end

----------------------------------------------------------------
-- Coordinator
----------------------------------------------------------------
local function notify(title, text, duration)
    pcall(function()
        StarterGui:SetCore("SendNotification",
            { Title = title, Text = text, Duration = duration or 3 })
    end)
end

local rec = {
    version       = ROCORDER_VERSION,
    cfg           = loadSettings(),
    tracker       = Tracker.new(),
    session       = nil,
    replay        = nil,         -- always present; created lazily
    encode        = nil,
    scriptStart   = os.clock(),
    nextTickAt    = 0,
    lastFlushAt   = 0,
    lastHeartbeat = 0,
    tickInterval  = 0,
    conns         = {},
    ui            = nil,
    onStateChange = nil,         -- UI hook
}
_G.ROCORDER = rec

function rec:_rebuildEncoder()
    self.encode = self.tracker:_makeEncoder(self.cfg.POS_PRECISION, self.cfg.ROT_PRECISION)
end

function rec:_ensureReplay()
    if not self.replay then
        self.replay = Replay.new(self.cfg.IR_BUFFER_SEC, self.cfg.TICK_RATE)
    end
    self.replay:reconfigure(self.cfg.IR_BUFFER_SEC, self.cfg.TICK_RATE)
end

function rec:_signalUI()
    if self.onStateChange then pcall(self.onStateChange) end
    if Indicator then Indicator:refresh() end
end

function rec:_setActive()
    self.tickInterval = 1 / self.cfg.TICK_RATE
    self.nextTickAt = os.clock()
end

function rec:Start()
    if self.session then return end
    self:_rebuildEncoder()
    self.session = Session.new(FOLDER, self.cfg, self.cfg.DEBUG)
    local roster = {}
    local localPlayer = Players.LocalPlayer
    for _, p in ipairs(Players:GetPlayers()) do
        if self.cfg.INCLUDE_LOCAL or p ~= localPlayer then
            roster[#roster + 1] = {
                userId = p.UserId, name = p.Name, displayName = p.DisplayName,
            }
        end
    end
    self.session:writeHeader(roster)
    self.lastFlushAt = os.clock()
    if self.session.debugEnabled then
        local rs = {}
        for _, p in ipairs(Players:GetPlayers()) do
            if self.cfg.INCLUDE_LOCAL or p ~= localPlayer then
                rs[#rs+1] = fmt("uid=%d %s%s", p.UserId, p.Name,
                    p.Character and "" or " (no character yet)")
            end
        end
        self.session:debugLog("roster: " .. table.concat(rs, ", "))
    end
    notify("ROCORDER", "Recording -> " .. self.session.filename, 3)
    -- Surface the session's debug log to the global extractor worker so
    -- per-asset OK/FAILED lines land in this recording's .debug.log.
    _G.ROCORDER_CURRENT_DBG = self.session.debugEnabled
        and function(m) self.session:debugLog(m); self.session:flushDebug() end
        or nil
    self:_signalUI()
end

-- Gather every unique Roblox asset id referenced by a rig (meshes, textures,
-- color maps, decals, clothing) into `set`.
local function collectAssetIds(rig, set)
    local function add(ref)
        if type(ref) == "string" then
            local id = ref:match("(%d%d%d%d+)")
            if id then set[id] = true end
        end
    end
    for _, p in ipairs(rig.parts) do
        add(p.meshId); add(p.textureId); add(p.colorMap)
        if p.decals then for _, d in ipairs(p.decals) do add(d.texture) end end
    end
    if rig.clothing then
        add(rig.clothing.shirt); add(rig.clothing.pants); add(rig.clothing.tshirt)
    end
end

-- Download every asset the tracked characters use, into ROCORDER/assets/<id>,
-- using the executor's authenticated session. Runs in a coroutine so it never
-- blocks. Blender then reads these local files instead of hitting the (401'd)
-- anonymous CDN. `logSink` is the just-finished session (for its debug log).
function rec:_downloadAssets(logSink)
    if not self.cfg.DOWNLOAD_ASSETS then return end
    -- Concurrency guard: prevent the dual-download bug where a re-loaded
    -- recorder's reload-guard Stop() races the user's F8 Stop() and both
    -- kick off this function in parallel (you'd see every asset downloaded
    -- twice in the log and "ASSET DOWNLOAD start/done" appearing twice).
    if _G.ROCORDER_ASSETS_RUNNING then
        if logSink and logSink.debugEnabled then
            logSink:debugLog("ASSET DOWNLOAD already running — skipping duplicate call")
            logSink:flushDebug()
        end
        return
    end
    _G.ROCORDER_ASSETS_RUNNING = true

    local ids = {}
    for _, e in pairs(self.tracker.tracked) do collectAssetIds(e.rig, ids) end
    local list = {}
    for id in pairs(ids) do list[#list + 1] = id end
    if #list == 0 then return end

    local function dbg(msg)
        if logSink and logSink.debugEnabled then
            logSink:debugLog(msg); logSink:flushDebug()
        end
    end

    -- Reject error-page bodies. game:HttpGet (and some request impls) return
    -- the 401/403 error TEXT as a normal string for non-2xx responses; saving
    -- that as the asset file produced "meshes" that silently fell back to a box
    -- on import. We reject JSON/HTML/known error-page bodies via the
    -- module-scope `looksLikeAsset` helper hoisted near _processOne.

    task.spawn(function()
        if not isfolder(ASSETS_FOLDER) then pcall(makefolder, ASSETS_FOLDER) end
        local okc, failc, skipc = 0, 0, 0
        local missing = {}  -- ids that couldn't be fetched anywhere
        dbg(fmt("ASSET DOWNLOAD start: %d unique assets — readers: "
            .. "EditableMesh/Image=%s httpRequest=%s getcustomasset=%s "
            .. "getsynasset=%s readcustomasset=%s readasset=%s getAssetBytes=%s",
            #list,
            EXTRACT_OK and "yes" or "no",
            httpRequest and "yes" or "no",
            getCustomAsset and "yes" or "no",
            getSynAsset and "yes" or "no",
            readCustomAsset and "yes" or "no",
            readAsset and "yes" or "no",
            getAssetBytes and "yes" or "no"))
        notify("ROCORDER", fmt("Downloading %d assets…", #list), 3)

        for _, id in ipairs(list) do
            local path = ASSETS_FOLDER .. "/" .. id

            -- Treat as cached if the queue worker already extracted/saved it
            -- this session (EXTRACTED flag — survives the brief writefile→
            -- isfile lag) OR if any form is on disk. For bare bin files we
            -- still validate via looksLikeAsset to weed out 401 error-page
            -- bodies left by pre-1.7.2 versions.
            local cached = false
            if EXTRACTED[id] then
                cached = true
            elseif isfile and (isfile(_geomPath(id)) or isfile(_imgPath(id))) then
                cached = true
            elseif isfile and isfile(path) then
                if readfile then
                    local okr, existing = pcall(readfile, path)
                    if okr and looksLikeAsset(existing) then
                        cached = true
                    elseif delfile then
                        pcall(delfile, path)  -- stale 401 body; replace it
                    end
                else
                    cached = true  -- can't validate; trust it
                end
            end

            if cached then
                skipc += 1
            else
                local body, source

                -- 1) ContentProvider/getcustomasset: the client already has this
                --    asset loaded — pull bytes straight from the in-memory copy.
                --    Works for any asset the player can currently see in-game,
                --    including UGC the CDN refuses to serve us anonymously.
                local cp, cpSrc = readFromContentProvider(id, dbg)
                if cp and #cp > 0 then body, source = cp, cpSrc end

                -- 2) Authenticated HTTP with proper game-client headers.
                if not body and httpRequest then
                    local urls = {
                        "https://assetdelivery.roblox.com/v1/asset/?id=" .. id,
                        "https://assetdelivery.roblox.com/v2/asset/?id=" .. id,
                        "https://c0.rbxcdn.com/" .. id,
                        "https://t0.rbxcdn.com/" .. id,
                    }
                    for _, url in ipairs(urls) do
                        local okr, resp = pcall(httpRequest, {
                            Url = url, Method = "GET",
                            Headers = {
                                ["User-Agent"]      = "Roblox/WinInet",
                                ["Roblox-Place-Id"] = tostring(game.PlaceId),
                                ["Accept"]          = "*/*",
                            },
                        })
                        if okr and type(resp) == "table" then
                            local code = resp.StatusCode or resp.Status or 200
                            if resp.Body and #resp.Body > 0 and code >= 200 and code < 300 then
                                body, source = resp.Body, url:match("//([^/]+)")
                                break
                            else
                                dbg(fmt("  asset %s %s -> %s",
                                    id, url:match("//([^/]+)"), tostring(code)))
                            end
                        end
                    end
                end

                -- 3) Last resort: plain game:HttpGet on the v1 endpoint.
                if not body then
                    local url = "https://assetdelivery.roblox.com/v1/asset/?id=" .. id
                    local okg, b = pcall(function() return game:HttpGet(url, true) end)
                    if okg and type(b) == "string" and #b > 0 and b:sub(1, 3) ~= "404" then
                        body, source = b, "HttpGet"
                    end
                end

                if body and looksLikeAsset(body) then
                    local okw = pcall(writefile, path, body)
                    if okw then
                        okc += 1
                        dbg(fmt("  asset %s via %s (%d bytes) OK",
                            id, tostring(source), #body))
                    else
                        failc += 1
                    end
                else
                    -- don't save an error page as if it were the asset
                    if body then
                        dbg(fmt("  asset %s rejected via %s (not a mesh/image, "
                            .. "%d bytes — likely 401/403 body)",
                            id, tostring(source), #body))
                    else
                        dbg(fmt("  asset %s — all download paths failed", id))
                    end
                    failc += 1
                    missing[#missing + 1] = id
                end
            end
            task.wait()  -- yield between downloads so we never hitch the client
        end

        dbg(fmt("ASSET DOWNLOAD done: %d saved, %d already cached, %d failed -> %s",
            okc, skipc, failc, ASSETS_FOLDER))
        -- Write a human-readable list of unfetchable assets next to the cache
        -- so the user can hand-drop them (drag any equivalent .mesh / .png /
        -- .jpg into ROCORDER/assets named '<id>' and the importer will pick
        -- it up automatically).
        if #missing > 0 then
            local lines = {
                "# Assets the executor couldn't fetch (CDN refused, asset has",
                "# restricted permissions, or the in-memory copy wasn't reachable).",
                "# Drop a file named EXACTLY '<id>' into ROCORDER/assets/ — the",
                "# importer's local-first lookup will use it.",
                "",
            }
            for _, id in ipairs(missing) do lines[#lines + 1] = id end
            pcall(writefile, ASSETS_FOLDER .. "/_missing.txt",
                table.concat(lines, "\n") .. "\n")
        end
        notify("ROCORDER",
            fmt("Assets: %d saved, %d cached, %d failed%s", okc, skipc, failc,
                #missing > 0 and "\nSee ROCORDER/assets/_missing.txt" or ""), 6)
        _G.ROCORDER_ASSETS_RUNNING = nil
    end)
end

function rec:Stop()
    if not self.session then return end
    local s = self.session
    self.session = nil
    s:flush()
    -- only include players actually seen during THIS recording (the tracker
    -- persists across sessions for Instant Replay)
    local data, nPlayers = self.tracker:rigData(s.filename, s.startClock)
    s:writeRig(data)
    s:writeMeta()
    self:_invalidateRecordingsCache()
    if s.debugEnabled then
        s:debugLog(fmt("STOP after %.2fs: ticks=%d stalls=%d gaps=%d rigPlayers=%d",
            s:elapsed(), s.tickCount, s.stallCount, s.gapCount, nPlayers))
        for uid, e in pairs(self.tracker.tracked) do
            if e.lastSeenClock and e.lastSeenClock >= s.startClock then
                s:debugLog(fmt("  uid=%d (%s) ticks=%d culled=%d parts=%d joints=%d",
                    uid, e.rig.name, e.ticks, e.culledTicks, #e.rig.parts, #e.rig.joints))
            end
        end
        s:flushDebug()
    end
    notify("ROCORDER", fmt("Saved %d ticks -> %s", s.tickCount, s.filename), 4)
    _G.ROCORDER_CURRENT_DBG = nil
    self:_downloadAssets(s)
    self:_signalUI()
end

function rec:Toggle() if self.session then self:Stop() else self:Start() end end
function rec:IsRecording() return self.session ~= nil end

function rec:SaveReplay(seconds)
    if not self.cfg.IR_ENABLED or not self.replay then
        notify("ROCORDER", "Instant Replay is off — enable it in Settings.", 4)
        return nil, "IR off"
    end
    if self.replay.count == 0 then
        notify("ROCORDER", "Replay buffer is empty.", 3)
        return nil, "empty"
    end
    -- Take the save lock: blocks the IR eviction scanner from deleting any
    -- asset for the duration of this save. Cleared in finally-style at exit.
    _G.ROCORDER_SAVE_IN_PROGRESS = true
    local ok, saved, frames, secsOut = pcall(function()
        seconds = seconds or self.cfg.IR_BUFFER_SEC
        -- only players seen within the buffered window
        local rigData = self.tracker:rigData("",
            os.clock() - (self.cfg.IR_BUFFER_SEC + 2))
        return self.replay:save(FOLDER, self.cfg, rigData, seconds)
    end)
    _G.ROCORDER_SAVE_IN_PROGRESS = false
    if not ok then
        notify("ROCORDER", "Replay save threw: " .. tostring(saved), 5)
        return nil, saved
    end
    if not saved then
        notify("ROCORDER", "Replay save failed: " .. tostring(frames), 5)
        return nil, frames
    end
    notify("ROCORDER",
        fmt("Saved %.1fs clip (%d frames) -> %s", secsOut, frames, saved), 5)
    self:_invalidateRecordingsCache()
    self:_downloadAssets(nil)
    self:_signalUI()
    return saved
end

-- name -> { size, meta?, header?, mtime? }
rec._recordingsCache = {}

function rec:_invalidateRecordingsCache(name)
    if name then self._recordingsCache[name] = nil
    else self._recordingsCache = {} end
end

local function _readJSONFile(path)
    if not (readfile and isfile and isfile(path)) then return nil end
    local ok, body = pcall(readfile, path)
    if not ok or type(body) ~= "string" then return nil end
    local ok2, data = pcall(function() return HttpService:JSONDecode(body) end)
    if ok2 then return data end
    return nil
end

local function _readHeaderFromRec(path)
    if not readfile then return nil end
    local ok, body = pcall(readfile, path)
    if not ok or type(body) ~= "string" then return nil, 0 end
    local firstLine = body:match("^([^\n]+)")
    if not firstLine then return nil, #body end
    local ok2, data = pcall(function() return HttpService:JSONDecode(firstLine) end)
    return ok2 and data or nil, #body
end

function rec:GetRecordings()
    if not listfiles then return {} end
    local out = {}
    local ok, files = pcall(listfiles, FOLDER)
    if not ok or type(files) ~= "table" then return out end

    for _, path in ipairs(files) do
        local norm = path:gsub("\\", "/")
        local name = norm:match("([^/]+)$") or norm
        if name:sub(-4) == ".rec" then
            local base = name:gsub("%.rec$", "")
            local metaPath = FOLDER .. "/" .. base .. ".meta.json"

            -- prefer the small meta sidecar for almost everything; only fall
            -- back to scanning the (potentially huge) .rec header when the
            -- meta file is missing (in-progress recording or pre-1.2 file).
            local meta = _readJSONFile(metaPath)
            local size = meta and meta.byteCount or 0
            local header = nil
            if not meta then
                local h, sz = _readHeaderFromRec(path)
                header = h
                size = sz
            else
                -- size from meta is the bytes-at-stop; check live file size if
                -- a session is still appending. We don't trust meta size for
                -- in-progress recordings, but we don't have a cheap stat call;
                -- the cache below picks up changes between refreshes.
            end

            -- info row used by the Files tab
            local info = {
                name        = name,
                path        = path,
                size        = size,
                startedAt   = (meta and meta.startedAt) or (header and header.startedAt),
                durationSec = meta and meta.durationSec or nil,
                frameCount  = meta and meta.frameCount  or nil,
                placeId     = (meta and meta.placeId)   or (header and header.placeId),
                placeName   = (meta and meta.placeName) or (header and header.placeName),
                sources     = (meta and meta.sources)   or (header and header.sources),
                isClip      = header and header.source == "instant-replay"
                            or (meta and meta.source == "instant-replay") or false,
                hasMeta     = meta ~= nil,
            }
            -- if we don't know the place name but have the id, try to look it
            -- up (cheap if cached, async-ish if not — pcall keeps yields safe)
            if not info.placeName and info.placeId then
                info.placeName = lookupGameName(info.placeId)
            end
            out[#out + 1] = info
        end
    end
    table.sort(out, function(a, b) return a.name > b.name end)
    return out
end

function rec:DeleteRecording(name)
    if not delfile then return false, "executor lacks delfile" end
    local sister = name:gsub("%.rec$", "")
    pcall(delfile, FOLDER .. "/" .. name)
    pcall(delfile, FOLDER .. "/" .. sister .. ".rig.json")
    pcall(delfile, FOLDER .. "/" .. sister .. ".debug.log")
    pcall(delfile, FOLDER .. "/" .. sister .. ".meta.json")
    self:_invalidateRecordingsCache(name)
    return true
end

function rec:SetSetting(key, value)
    local d = defByKey(key)
    if not d then return false, "no such setting" end
    if d.type == "choice" then
        if type(value) ~= "string" then return false, "wrong type" end
        local valid = false
        for _, c in ipairs(d.choices or {}) do
            if c == value then valid = true; break end
        end
        if not valid then return false, "not in choices" end
    elseif type(value) ~= type(d.default) then
        return false, "wrong type"
    end
    self.cfg[key] = value
    saveSettings(self.cfg)
    -- live-apply effects
    if key == "POS_PRECISION" or key == "ROT_PRECISION" then self:_rebuildEncoder() end
    if key == "TICK_RATE" then self:_setActive() end
    if key == "TICK_RATE" or key == "IR_BUFFER_SEC" then self:_ensureReplay() end
    if key == "IR_ENABLED" then
        if value then self:_ensureReplay()
        elseif self.replay then self.replay:clear() end
    end
    if key == "IR_ENABLED" or key == "INDICATOR_ENABLED" or key == "INDICATOR_CORNER" then
        if Indicator then Indicator:refresh() end
    end
    self:_signalUI()
    return true
end

----------------------------------------------------------------
-- Heartbeat loop
----------------------------------------------------------------
rec:_rebuildEncoder()
rec:_ensureReplay()
rec:_setActive()

rec.conns.heartbeat = RunService.Heartbeat:Connect(function()
    local now = os.clock()
    local cfg = rec.cfg
    local s   = rec.session

    -- stall detection (only meaningful while a session writes a debug log)
    if s and s.debugEnabled then
        local gap = now - rec.lastHeartbeat
        if rec.lastHeartbeat > 0 and gap > 0.15 then
            s.stallCount += 1
            s:debugLog(fmt("heartbeat stall %.2fs", gap))
        end
    end
    rec.lastHeartbeat = now

    local active = s or cfg.IR_ENABLED
    if not active then
        rec.nextTickAt = now  -- don't accumulate catchup while idle
        return
    end

    if now >= rec.nextTickAt then
        local debugLog = s and s.debugEnabled and function(m) s:debugLog(m) end or nil
        -- assemble snapshot from each enabled source
        local snapshot = {}
        if cfg.SRC_PLAYER_PARTS then
            local players = rec.tracker:snapshot(cfg, rec.encode, debugLog)
            for i = 1, #players do snapshot[#snapshot + 1] = players[i] end
        end
        if cfg.SRC_CAMERA then
            local camStr = captureCameraStr(rec.encode, cfg.POS_PRECISION)
            if camStr then snapshot[#snapshot + 1] = camStr end
        end
        local maxCatchup = math.ceil(cfg.MAX_CATCHUP_SEC / rec.tickInterval)
        local filled = 0
        while now >= rec.nextTickAt and filled < maxCatchup do
            if s then s:writeFrame(rec.nextTickAt - s.startClock, snapshot) end
            if cfg.IR_ENABLED and rec.replay then
                rec.replay:push(rec.nextTickAt - rec.scriptStart, snapshot)
            end
            rec.nextTickAt = rec.nextTickAt + rec.tickInterval
            filled += 1
        end
        if now >= rec.nextTickAt then
            local lost = now + rec.tickInterval - rec.nextTickAt
            if s then
                s.gapCount += 1
                s:debugLog(fmt(
                    "*** GAP CREATED: catchup cap hit (filled %d frames), " ..
                    "jumped %.2fs forward (≈%d frames lost)",
                    filled, lost, math.floor(lost * cfg.TICK_RATE + 0.5)))
            end
            rec.nextTickAt = now + rec.tickInterval
        end
    end

    if s and now - rec.lastFlushAt >= cfg.FLUSH_INTERVAL then
        s:flush(); s:flushDebug()
        rec.lastFlushAt = now
    end
end)

----------------------------------------------------------------
-- UI
----------------------------------------------------------------
local THEME = {
    bg        = Color3.fromRGB(20, 22, 28),
    panel     = Color3.fromRGB(30, 33, 40),
    panelHi   = Color3.fromRGB(42, 46, 56),
    border    = Color3.fromRGB(55, 60, 72),
    text      = Color3.fromRGB(228, 231, 238),
    subtext   = Color3.fromRGB(150, 156, 168),
    accent    = Color3.fromRGB(88, 142, 252),
    accentHi  = Color3.fromRGB(118, 162, 255),
    danger    = Color3.fromRGB(236, 90, 90),
    dangerHi  = Color3.fromRGB(246, 110, 110),
    success   = Color3.fromRGB(96, 220, 132),
    recording = Color3.fromRGB(236, 90, 90),
    standby   = Color3.fromRGB(150, 156, 168),
    titleBar  = Color3.fromRGB(24, 27, 33),
    tabActive = Color3.fromRGB(42, 46, 56),
    tabInact  = Color3.fromRGB(30, 33, 40),
    fontReg   = Enum.Font.Gotham,
    fontBold  = Enum.Font.GothamBold,
    fontMono  = Enum.Font.Code,
}

local function applyProps(o, props)
    if props then for k, v in pairs(props) do o[k] = v end end
    return o
end
local function mk(class, props, parent)
    local o = Instance.new(class)
    applyProps(o, props)
    if parent then o.Parent = parent end
    return o
end
local function corner(o, r)
    mk("UICorner", { CornerRadius = UDim.new(0, r or 6) }, o); return o
end
local function stroke(o, color, thick)
    mk("UIStroke", { Color = color or THEME.border, Thickness = thick or 1,
        ApplyStrokeMode = Enum.ApplyStrokeMode.Border }, o); return o
end
local function pad(o, l, t, r, b)
    mk("UIPadding", {
        PaddingLeft   = UDim.new(0, l or 0),
        PaddingTop    = UDim.new(0, t or l or 0),
        PaddingRight  = UDim.new(0, r or l or 0),
        PaddingBottom = UDim.new(0, b or t or l or 0),
    }, o); return o
end
local function vlist(o, gap)
    mk("UIListLayout", {
        FillDirection = Enum.FillDirection.Vertical,
        SortOrder = Enum.SortOrder.LayoutOrder,
        Padding = UDim.new(0, gap or 6),
    }, o); return o
end
local function hlist(o, gap)
    mk("UIListLayout", {
        FillDirection = Enum.FillDirection.Horizontal,
        SortOrder = Enum.SortOrder.LayoutOrder,
        Padding = UDim.new(0, gap or 6),
    }, o); return o
end

local function getUIParent()
    if typeof and typeof(gethui) == "function" then
        local ok, h = pcall(gethui)
        if ok and h then return h end
    end
    local ok, cg = pcall(function() return game:GetService("CoreGui") end)
    if ok and cg then return cg end
    return Players.LocalPlayer and Players.LocalPlayer:WaitForChild("PlayerGui")
end

local function keyFromName(name)
    local ok, k = pcall(function() return Enum.KeyCode[name] end)
    if ok and k then return k end
    return Enum.KeyCode.F8
end

local function humanBytes(n)
    if n < 1024 then return fmt("%d B", n) end
    if n < 1024*1024 then return fmt("%.1f KB", n/1024) end
    return fmt("%.2f MB", n/1024/1024)
end

local function humanDuration(seconds)
    seconds = math.max(0, math.floor(seconds))
    local h = math.floor(seconds / 3600)
    local m = math.floor((seconds % 3600) / 60)
    local s = seconds % 60
    if h > 0 then return fmt("%d:%02d:%02d", h, m, s) end
    return fmt("%02d:%02d", m, s)
end

----------------------------------------------------------------
-- Indicator overlay (small dot in a screen corner while capturing)
----------------------------------------------------------------
Indicator = {
    gui        = nil,
    dot        = nil,
    ringStroke = nil,
    innerDot   = nil,
    DOT_SIZE   = 22,
    MARGIN     = 14,
    -- BackgroundTransparency 0.75 = 25% opaque per the user's request
    OPACITY    = 0.75,
}

function Indicator:_ensureGui()
    if self.gui and self.gui.Parent then return end
    local parent = getUIParent()
    if not parent then return end
    self.gui = mk("ScreenGui", {
        Name            = "ROCORDER_Indicator",
        IgnoreGuiInset  = true,
        ResetOnSpawn    = false,
        DisplayOrder    = 999990,   -- below the main UI window
        ZIndexBehavior  = Enum.ZIndexBehavior.Sibling,
    })
    self.gui.Parent = parent

    -- Container = the outer ring. Its background is transparent, the colored
    -- ring comes from a thick UIStroke. UICorner with radius=size/2 makes
    -- both the (invisible) background and the stroke render as a circle.
    local container = mk("Frame", {
        Name                   = "Indicator",
        Size                   = UDim2.fromOffset(self.DOT_SIZE, self.DOT_SIZE),
        BackgroundTransparency = 1,
        BorderSizePixel        = 0,
        Visible                = false,
    }, self.gui)
    corner(container, math.floor(self.DOT_SIZE / 2))

    local ringStroke = mk("UIStroke", {
        Color           = THEME.recording,
        Thickness       = math.max(2, math.floor(self.DOT_SIZE / 7)),
        Transparency    = self.OPACITY,
        ApplyStrokeMode = Enum.ApplyStrokeMode.Border,
    }, container)

    -- Inner solid dot — together with the ring this gives the classic
    -- record-button look (the user's reference image).
    local innerSize = math.max(4, math.floor(self.DOT_SIZE * 0.32))
    local inner = mk("Frame", {
        Name                   = "Inner",
        Size                   = UDim2.fromOffset(innerSize, innerSize),
        Position               = UDim2.fromScale(0.5, 0.5),
        AnchorPoint            = Vector2.new(0.5, 0.5),
        BackgroundColor3       = THEME.recording,
        BackgroundTransparency = self.OPACITY,
        BorderSizePixel        = 0,
    }, container)
    corner(inner, math.floor(innerSize / 2))

    self.dot        = container
    self.ringStroke = ringStroke
    self.innerDot   = inner
end

function Indicator:_setCorner(name)
    if not self.dot then return end
    local m = self.MARGIN
    if name == "TopLeft" then
        self.dot.AnchorPoint = Vector2.new(0, 0)
        self.dot.Position    = UDim2.new(0,  m, 0,  m)
    elseif name == "BottomLeft" then
        self.dot.AnchorPoint = Vector2.new(0, 1)
        self.dot.Position    = UDim2.new(0,  m, 1, -m)
    elseif name == "BottomRight" then
        self.dot.AnchorPoint = Vector2.new(1, 1)
        self.dot.Position    = UDim2.new(1, -m, 1, -m)
    else  -- TopRight (default)
        self.dot.AnchorPoint = Vector2.new(1, 0)
        self.dot.Position    = UDim2.new(1, -m, 0,  m)
    end
end

function Indicator:refresh()
    self:_ensureGui()
    if not self.dot then return end
    if not rec.cfg.INDICATOR_ENABLED then
        self.dot.Visible = false
        return
    end
    self:_setCorner(rec.cfg.INDICATOR_CORNER)
    local color
    if rec:IsRecording() then
        color = THEME.recording                      -- red while recording
    elseif rec.cfg.IR_ENABLED then
        color = Color3.fromRGB(245, 245, 245)        -- white while buffering
    else
        self.dot.Visible = false
        return
    end
    self.dot.Visible = true
    if self.ringStroke then self.ringStroke.Color = color end
    if self.innerDot   then self.innerDot.BackgroundColor3 = color end
end

function Indicator:destroy()
    if self.gui then pcall(function() self.gui:Destroy() end) end
    self.gui = nil
    self.dot = nil
end

----------------------------------------------------------------
-- UI: build
----------------------------------------------------------------
local UI = {
    gui = nil, window = nil, tabs = {}, views = {},
    activeTab = "Record", visible = true,
    statusLoopRunning = false,
    keyBindBtn = nil,
    refreshFiles = nil,
}

local HOVER_TWEEN = TweenInfo.new(0.12, Enum.EasingStyle.Quad)

local function buildButton(parent, text, kind, layoutOrder)
    local fill, hover, textColor = THEME.panel, THEME.panelHi, THEME.text
    local addStroke = false
    if kind == "primary" then
        fill, hover, textColor = THEME.accent, THEME.accentHi, Color3.new(1,1,1)
    elseif kind == "danger" then
        fill, hover, textColor = THEME.danger, THEME.dangerHi, Color3.new(1,1,1)
    elseif kind == "secondary" then
        -- Visible-but-muted; outlined so it reads as a button against the
        -- content background (the old "ghost" was invisible because its fill
        -- matched the surrounding bg).
        fill, hover, textColor = THEME.panel, THEME.panelHi, THEME.accent
        addStroke = true
    elseif kind == "ghost" then
        fill, hover = THEME.bg, THEME.panel
    end
    local b = mk("TextButton", {
        Text = text, Font = THEME.fontBold, TextSize = 14,
        BackgroundColor3 = fill, TextColor3 = textColor,
        AutoButtonColor = false, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, 32), LayoutOrder = layoutOrder or 1,
    }, parent)
    corner(b, 6)
    if addStroke then stroke(b, THEME.accent, 1) end
    -- Attribute-based colors so external code (the status loop) can change
    -- the button's role mid-life without the hover handlers snapping it
    -- back to its original fill on MouseLeave.
    b:SetAttribute("FillColor",  fill)
    b:SetAttribute("HoverColor", hover)
    b.MouseEnter:Connect(function()
        TweenService:Create(b, HOVER_TWEEN,
            { BackgroundColor3 = b:GetAttribute("HoverColor") or hover }):Play()
    end)
    b.MouseLeave:Connect(function()
        TweenService:Create(b, HOVER_TWEEN,
            { BackgroundColor3 = b:GetAttribute("FillColor") or fill }):Play()
    end)
    return b
end

-- Helper for the status loop: swap a buildButton's role-colors atomically.
local function setButtonColors(b, fill, hover)
    b:SetAttribute("FillColor",  fill)
    b:SetAttribute("HoverColor", hover)
    b.BackgroundColor3 = fill
end

local function buildStatusPanel(parent)
    -- Auto-sizing: panel grows to fit its content so adding fields later
    -- doesn't truncate them at the bottom.
    local panel = mk("Frame", {
        BackgroundColor3 = THEME.panel, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, 0),
        AutomaticSize = Enum.AutomaticSize.Y, LayoutOrder = 1,
    }, parent)
    corner(panel, 8); pad(panel, 14, 12, 14, 14); vlist(panel, 10)

    -- header row: live dot + status label
    local row1 = mk("Frame", {
        BackgroundTransparency = 1, Size = UDim2.new(1, 0, 0, 22),
        LayoutOrder = 1 }, panel)
    local dot = mk("Frame", {
        Size = UDim2.fromOffset(10, 10),
        Position = UDim2.fromOffset(0, 6),
        BackgroundColor3 = THEME.standby, BorderSizePixel = 0,
    }, row1); corner(dot, 5)
    local statusLabel = mk("TextLabel", {
        Text = "Idle", Font = THEME.fontBold, TextSize = 16,
        TextColor3 = THEME.text, BackgroundTransparency = 1,
        Position = UDim2.fromOffset(18, 0),
        Size = UDim2.new(1, -18, 1, 0),
        TextXAlignment = Enum.TextXAlignment.Left,
    }, row1)

    -- thin divider
    mk("Frame", {
        BackgroundColor3 = THEME.border, BorderSizePixel = 0,
        BackgroundTransparency = 0.5,
        Size = UDim2.new(1, 0, 0, 1), LayoutOrder = 2,
    }, panel)

    -- detail rows
    local detailRows = mk("Frame", {
        BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 0, 0),
        AutomaticSize = Enum.AutomaticSize.Y, LayoutOrder = 3,
    }, panel)
    vlist(detailRows, 4)

    local function detailRow(label, order)
        local f = mk("Frame", { BackgroundTransparency = 1,
            Size = UDim2.new(1, 0, 0, 18), LayoutOrder = order }, detailRows)
        mk("TextLabel", { Text = label, Font = THEME.fontReg, TextSize = 13,
            TextColor3 = THEME.subtext, BackgroundTransparency = 1,
            Size = UDim2.new(0, 130, 1, 0),
            TextXAlignment = Enum.TextXAlignment.Left }, f)
        local val = mk("TextLabel", { Text = "—", Font = THEME.fontMono, TextSize = 13,
            TextColor3 = THEME.text, BackgroundTransparency = 1,
            Position = UDim2.fromOffset(130, 0),
            Size = UDim2.new(1, -130, 1, 0),
            TextXAlignment = Enum.TextXAlignment.Left }, f)
        return val
    end
    local v = {
        time    = detailRow("Elapsed",         1),
        ticks   = detailRow("Ticks / size",    2),
        buffer  = detailRow("Replay buffer",   3),
        tracked = detailRow("Players tracked", 4),
    }
    return { panel = panel, statusLabel = statusLabel, dot = dot, fields = v }
end

local function buildRecordView(parent)
    -- ScrollingFrame so content (especially the Assets panel's per-player
    -- list) never collides with the bottom footer. Same pattern as Settings.
    local view = mk("ScrollingFrame", {
        BackgroundTransparency = 1,
        Size = UDim2.fromScale(1, 1),
        CanvasSize = UDim2.new(0, 0, 0, 0),
        AutomaticCanvasSize = Enum.AutomaticSize.Y,
        ScrollBarThickness = 6,
        ScrollBarImageColor3 = THEME.border,
        BorderSizePixel = 0,
    }, parent)
    pad(view, 16); vlist(view, 12)

    local status = buildStatusPanel(view)

    local recordBtn = buildButton(view, "Start Recording", "primary", 2)
    recordBtn.Size = UDim2.new(1, 0, 0, 44)
    recordBtn.TextSize = 16

    local replayBtn = buildButton(view, "Save Instant Replay", "secondary", 3)
    replayBtn.Size = UDim2.new(1, 0, 0, 38)
    -- track the stroke ref so _refreshStatus can dim it when IR is off
    for _, c in ipairs(replayBtn:GetChildren()) do
        if c:IsA("UIStroke") then replayBtn:SetAttribute("StrokeRef", c.Name) end
    end

    local irRow = mk("Frame", { BackgroundColor3 = THEME.panel, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, 40), LayoutOrder = 4 }, view)
    corner(irRow, 6); pad(irRow, 12, 0)
    mk("TextLabel", { Text = "Instant Replay (continuous buffer)",
        Font = THEME.fontReg, TextSize = 14, TextColor3 = THEME.text,
        BackgroundTransparency = 1, Size = UDim2.new(1, -60, 1, 0),
        TextXAlignment = Enum.TextXAlignment.Left }, irRow)
    local irToggle = mk("TextButton", { Text = "OFF", Font = THEME.fontBold,
        TextSize = 13, TextColor3 = Color3.new(1,1,1),
        BackgroundColor3 = THEME.standby, BorderSizePixel = 0, AutoButtonColor = false,
        Position = UDim2.new(1, -56, 0.5, -12), Size = UDim2.fromOffset(48, 24) }, irRow)
    corner(irToggle, 4)

    local function refreshIrToggle()
        if rec.cfg.IR_ENABLED then
            irToggle.Text = "ON"; irToggle.BackgroundColor3 = THEME.accent
        else
            irToggle.Text = "OFF"; irToggle.BackgroundColor3 = THEME.standby
        end
    end
    irToggle.MouseButton1Click:Connect(function()
        rec:SetSetting("IR_ENABLED", not rec.cfg.IR_ENABLED)
        refreshIrToggle()
    end)
    refreshIrToggle()

    recordBtn.MouseButton1Click:Connect(function() rec:Toggle() end)
    replayBtn.MouseButton1Click:Connect(function() rec:SaveReplay() end)

    ---- Assets panel: aggregate progress + per-player breakdown ----
    local assetPanel = mk("Frame", {
        BackgroundColor3 = THEME.panel, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, 0),
        AutomaticSize = Enum.AutomaticSize.Y,
        LayoutOrder = 5,
    }, view)
    corner(assetPanel, 8); pad(assetPanel, 14, 12, 14, 12); vlist(assetPanel, 8)

    mk("TextLabel", { Text = "Assets",
        Font = THEME.fontBold, TextSize = 13, TextColor3 = THEME.accent,
        BackgroundTransparency = 1, Size = UDim2.new(1, 0, 0, 16),
        TextXAlignment = Enum.TextXAlignment.Left, LayoutOrder = 1 }, assetPanel)

    local assetHeadline = mk("TextLabel", {
        Text = "extractor idle — no players tracked yet",
        Font = THEME.fontMono, TextSize = 12, TextColor3 = THEME.text,
        BackgroundTransparency = 1, Size = UDim2.new(1, 0, 0, 16),
        TextXAlignment = Enum.TextXAlignment.Left, LayoutOrder = 2 }, assetPanel)

    -- progress bar
    local barBg = mk("Frame", { BackgroundColor3 = THEME.bg, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, 6), LayoutOrder = 3 }, assetPanel)
    corner(barBg, 3)
    local barFill = mk("Frame", { BackgroundColor3 = THEME.accent,
        BorderSizePixel = 0, Size = UDim2.new(0, 0, 1, 0) }, barBg)
    corner(barFill, 3)

    local assetStats = mk("TextLabel", {
        Text = "", Font = THEME.fontMono, TextSize = 11,
        TextColor3 = THEME.subtext, BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 0, 14),
        TextXAlignment = Enum.TextXAlignment.Left, LayoutOrder = 4 }, assetPanel)

    -- separator
    mk("Frame", { BackgroundColor3 = THEME.border, BorderSizePixel = 0,
        BackgroundTransparency = 0.5,
        Size = UDim2.new(1, 0, 0, 1), LayoutOrder = 5 }, assetPanel)

    -- per-player rows (rebuilt each refresh)
    local playerList = mk("Frame", {
        BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 0, 0),
        AutomaticSize = Enum.AutomaticSize.Y, LayoutOrder = 6 }, assetPanel)
    vlist(playerList, 2)

    return {
        view = view, status = status,
        recordBtn = recordBtn, replayBtn = replayBtn,
        irToggle = irToggle, refreshIrToggle = refreshIrToggle,
        assetPanel = assetPanel,
        assetHeadline = assetHeadline,
        assetStats = assetStats,
        assetBarFill = barFill,
        assetPlayerList = playerList,
    }
end

local function buildSettingsView(parent)
    local view = mk("ScrollingFrame", { BackgroundTransparency = 1,
        Size = UDim2.fromScale(1, 1), CanvasSize = UDim2.new(0,0,0,0),
        AutomaticCanvasSize = Enum.AutomaticSize.Y, ScrollBarThickness = 6,
        ScrollBarImageColor3 = THEME.border, BorderSizePixel = 0 }, parent)
    pad(view, 16); vlist(view, 14)

    local order, groups = settingsByGroup(SETTINGS_TAB_OMIT)
    local controls = {}
    -- Track per-group bookkeeping so we can hide a whole group when all of
    -- its items are advanced and advanced is currently hidden.
    local groupInfo = {}        -- gname -> { box, hasBasic, advancedRows[] }
    local advancedRows = {}     -- flat list of all advanced item rows
    local advancedTextBoxes = {} -- TextBox refs we need to NOT overwrite while focused

    for gi, gname in ipairs(order) do
        local groupBox = mk("Frame", { BackgroundColor3 = THEME.panel,
            BorderSizePixel = 0, Size = UDim2.new(1, -6, 0, 36),
            AutomaticSize = Enum.AutomaticSize.Y, LayoutOrder = gi }, view)
        corner(groupBox, 8); pad(groupBox, 14, 12, 14, 14); vlist(groupBox, 10)

        mk("TextLabel", { Text = gname, Font = THEME.fontBold, TextSize = 14,
            TextColor3 = THEME.accent, BackgroundTransparency = 1,
            Size = UDim2.new(1, 0, 0, 18),
            TextXAlignment = Enum.TextXAlignment.Left }, groupBox)

        local hasBasic = false
        for _, d in ipairs(groups[gname]) do
            local row = mk("Frame", { BackgroundTransparency = 1,
                Size = UDim2.new(1, 0, 0, 44) }, groupBox)

            mk("TextLabel", { Text = d.label, Font = THEME.fontBold, TextSize = 13,
                TextColor3 = THEME.text, BackgroundTransparency = 1,
                Size = UDim2.new(0.5, 0, 0, 18),
                TextXAlignment = Enum.TextXAlignment.Left }, row)
            mk("TextLabel", { Text = d.desc, Font = THEME.fontReg, TextSize = 11,
                TextColor3 = THEME.subtext, BackgroundTransparency = 1,
                Position = UDim2.fromOffset(0, 20),
                Size = UDim2.new(0.6, 0, 0, 22),
                TextWrapped = true, TextYAlignment = Enum.TextYAlignment.Top,
                TextXAlignment = Enum.TextXAlignment.Left }, row)

            if d.type == "number" then
                local box = mk("TextBox", {
                    Font = THEME.fontMono, TextSize = 14, TextColor3 = THEME.text,
                    BackgroundColor3 = THEME.bg, BorderSizePixel = 0,
                    Text = tostring(rec.cfg[d.key]), ClearTextOnFocus = false,
                    Position = UDim2.new(1, -118, 0, 0),
                    Size = UDim2.fromOffset(112, 30),
                }, row); corner(box, 4); pad(box, 8, 0)
                stroke(box, THEME.border, 1)
                box.FocusLost:Connect(function()
                    local n = tonumber(box.Text)
                    if not n then box.Text = tostring(rec.cfg[d.key]); return end
                    if d.int then n = math.floor(n + 0.5) end
                    if d.min and n < d.min then n = d.min end
                    if d.max and n > d.max then n = d.max end
                    rec:SetSetting(d.key, n)
                    box.Text = tostring(rec.cfg[d.key])
                end)
                controls[d.key] = box

            elseif d.type == "bool" then
                local btn = mk("TextButton", { Text = "OFF",
                    Font = THEME.fontBold, TextSize = 13, TextColor3 = Color3.new(1,1,1),
                    BackgroundColor3 = THEME.standby, BorderSizePixel = 0,
                    AutoButtonColor = false,
                    Position = UDim2.new(1, -66, 0, 4),
                    Size = UDim2.fromOffset(58, 26),
                }, row); corner(btn, 4)
                local function refresh()
                    if rec.cfg[d.key] then
                        btn.Text = "ON"; btn.BackgroundColor3 = THEME.accent
                    else
                        btn.Text = "OFF"; btn.BackgroundColor3 = THEME.standby
                    end
                end
                btn.MouseButton1Click:Connect(function()
                    rec:SetSetting(d.key, not rec.cfg[d.key]); refresh()
                end)
                refresh()
                controls[d.key] = { btn = btn, refresh = refresh }

            elseif d.type == "key" then
                local btn = mk("TextButton", {
                    Text = rec.cfg[d.key], Font = THEME.fontMono, TextSize = 13,
                    TextColor3 = THEME.text, BackgroundColor3 = THEME.bg,
                    BorderSizePixel = 0, AutoButtonColor = false,
                    Position = UDim2.new(1, -118, 0, 4),
                    Size = UDim2.fromOffset(112, 26),
                }, row); corner(btn, 4); stroke(btn, THEME.border, 1)
                btn.MouseButton1Click:Connect(function()
                    btn.Text = "Press a key…"
                    UI.keyBindBtn = { btn = btn, key = d.key }
                end)
                controls[d.key] = btn

            elseif d.type == "choice" then
                -- click cycles through d.choices
                local btn = mk("TextButton", {
                    Text = tostring(rec.cfg[d.key]),
                    Font = THEME.fontMono, TextSize = 13,
                    TextColor3 = THEME.text, BackgroundColor3 = THEME.bg,
                    BorderSizePixel = 0, AutoButtonColor = false,
                    Position = UDim2.new(1, -118, 0, 4),
                    Size = UDim2.fromOffset(112, 26),
                }, row); corner(btn, 4); stroke(btn, THEME.border, 1)
                local function refresh()
                    btn.Text = tostring(rec.cfg[d.key])
                end
                btn.MouseButton1Click:Connect(function()
                    local cur = rec.cfg[d.key]
                    local nextIdx = 1
                    for i, c in ipairs(d.choices or {}) do
                        if c == cur then nextIdx = (i % #d.choices) + 1; break end
                    end
                    rec:SetSetting(d.key, d.choices[nextIdx])
                    refresh()
                end)
                controls[d.key] = { btn = btn, refresh = refresh }
            end

            if d.advanced then
                advancedRows[#advancedRows + 1] = row
                row.Visible = false  -- hidden by default
            else
                hasBasic = true
            end
        end

        groupInfo[gname] = { box = groupBox, hasBasic = hasBasic }
        groupBox.Visible = hasBasic  -- if a group has ONLY advanced items, hide it
    end

    -- "Show advanced settings" toggle button (always last in the list)
    local advBtn = mk("TextButton", {
        Text = "\xE2\x96\xBC  Show advanced settings",   -- ▼
        Font = THEME.fontBold, TextSize = 13,
        TextColor3 = THEME.subtext, BackgroundColor3 = THEME.panel,
        BorderSizePixel = 0, AutoButtonColor = false,
        Size = UDim2.new(1, -6, 0, 36),
        LayoutOrder = 9999,
    }, view); corner(advBtn, 6); stroke(advBtn, THEME.border, 1)
    advBtn.MouseEnter:Connect(function()
        TweenService:Create(advBtn, HOVER_TWEEN,
            { TextColor3 = THEME.text }):Play()
    end)
    advBtn.MouseLeave:Connect(function()
        TweenService:Create(advBtn, HOVER_TWEEN,
            { TextColor3 = THEME.subtext }):Play()
    end)

    local showAdvanced = false
    local function refreshAdvanced()
        for _, row in ipairs(advancedRows) do row.Visible = showAdvanced end
        for _, info in pairs(groupInfo) do
            info.box.Visible = info.hasBasic or showAdvanced
        end
        advBtn.Text = showAdvanced
            and "\xE2\x96\xB2  Hide advanced settings"   -- ▲
            or  "\xE2\x96\xBC  Show advanced settings"   -- ▼
    end
    advBtn.MouseButton1Click:Connect(function()
        showAdvanced = not showAdvanced
        refreshAdvanced()
    end)
    refreshAdvanced()

    return { view = view, controls = controls }
end

local function buildFilesView(parent)
    local view = mk("Frame", { BackgroundTransparency = 1,
        Size = UDim2.fromScale(1, 1) }, parent)
    pad(view, 16); vlist(view, 10)

    local header = mk("Frame", { BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 0, 32), LayoutOrder = 1 }, view)
    mk("TextLabel", { Text = "Recordings", Font = THEME.fontBold, TextSize = 16,
        TextColor3 = THEME.text, BackgroundTransparency = 1,
        Size = UDim2.new(0.5, 0, 1, 0),
        TextXAlignment = Enum.TextXAlignment.Left }, header)
    local refresh = mk("TextButton", { Text = "Refresh", Font = THEME.fontBold,
        TextSize = 13, TextColor3 = Color3.new(1,1,1),
        BackgroundColor3 = THEME.accent, BorderSizePixel = 0, AutoButtonColor = false,
        Position = UDim2.new(1, -88, 0.5, -14),
        Size = UDim2.fromOffset(80, 28) }, header); corner(refresh, 4)

    local scroll = mk("ScrollingFrame", { BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 1, -42), LayoutOrder = 2,
        CanvasSize = UDim2.new(0,0,0,0),
        AutomaticCanvasSize = Enum.AutomaticSize.Y,
        ScrollBarThickness = 6, ScrollBarImageColor3 = THEME.border,
        BorderSizePixel = 0 }, view)
    vlist(scroll, 6); pad(scroll, 0, 0, 8, 0)

    local function populate()
        for _, c in ipairs(scroll:GetChildren()) do
            if c:IsA("Frame") then c:Destroy() end
        end
        local files = rec:GetRecordings()
        if #files == 0 then
            local empty = mk("Frame", { BackgroundColor3 = THEME.panel,
                BorderSizePixel = 0, Size = UDim2.new(1, 0, 0, 60) }, scroll)
            corner(empty, 6); pad(empty, 12)
            mk("TextLabel", { Text = "No recordings yet.",
                Font = THEME.fontReg, TextSize = 13,
                TextColor3 = THEME.subtext, BackgroundTransparency = 1,
                Size = UDim2.fromScale(1, 1),
                TextXAlignment = Enum.TextXAlignment.Left,
                TextYAlignment = Enum.TextYAlignment.Top }, empty)
            return
        end
        for _, f in ipairs(files) do
            local row = mk("Frame", { BackgroundColor3 = THEME.panel,
                BorderSizePixel = 0, Size = UDim2.new(1, 0, 0, 78) }, scroll)
            corner(row, 6); pad(row, 14, 10, 14, 10)

            -- top row: filename + delete button
            mk("TextLabel", { Text = f.name, Font = THEME.fontBold, TextSize = 13,
                TextColor3 = THEME.text, BackgroundTransparency = 1,
                Size = UDim2.new(1, -88, 0, 18),
                TextXAlignment = Enum.TextXAlignment.Left,
                TextTruncate = Enum.TextTruncate.AtEnd }, row)
            -- a subtle "CLIP" pill on instant-replay clips so they're identifiable
            if f.isClip then
                local clipPill = mk("TextLabel", { Text = "CLIP",
                    Font = THEME.fontBold, TextSize = 10,
                    TextColor3 = THEME.accent, BackgroundColor3 = THEME.bg,
                    BorderSizePixel = 0,
                    TextXAlignment = Enum.TextXAlignment.Center,
                    TextYAlignment = Enum.TextYAlignment.Center,
                    Position = UDim2.new(1, -134, 0, 0),
                    Size = UDim2.fromOffset(46, 18) }, row)
                corner(clipPill, 4); stroke(clipPill, THEME.accent, 1)
            end

            -- middle row: duration · date
            local duration = f.durationSec and humanDuration(f.durationSec)
                          or (f.hasMeta and "0s" or "?")
            local dateStr  = f.startedAt and os.date("%Y-%m-%d %H:%M", f.startedAt)
                          or "unknown date"
            mk("TextLabel", {
                Text = duration .. "  ·  " .. dateStr,
                Font = THEME.fontMono, TextSize = 12,
                TextColor3 = THEME.subtext, BackgroundTransparency = 1,
                Position = UDim2.fromOffset(0, 22),
                Size = UDim2.new(1, -88, 0, 16),
                TextXAlignment = Enum.TextXAlignment.Left }, row)

            -- bottom row: game name · size
            local gameStr = f.placeName
                         or (f.placeId and fmt("placeId %d", f.placeId))
                         or "unknown place"
            mk("TextLabel", {
                Text = gameStr .. "  ·  " .. humanBytes(f.size or 0),
                Font = THEME.fontReg, TextSize = 12,
                TextColor3 = THEME.subtext, BackgroundTransparency = 1,
                Position = UDim2.fromOffset(0, 42),
                Size = UDim2.new(1, -88, 0, 16),
                TextXAlignment = Enum.TextXAlignment.Left,
                TextTruncate = Enum.TextTruncate.AtEnd }, row)

            -- delete button (anchored right, centered vertically)
            local del = mk("TextButton", { Text = "Delete",
                Font = THEME.fontBold, TextSize = 12, TextColor3 = Color3.new(1,1,1),
                BackgroundColor3 = THEME.danger, BorderSizePixel = 0,
                AutoButtonColor = false,
                Position = UDim2.new(1, -82, 1, -34),
                Size = UDim2.fromOffset(76, 26) }, row); corner(del, 4)
            del.MouseEnter:Connect(function()
                TweenService:Create(del, HOVER_TWEEN,
                    { BackgroundColor3 = THEME.dangerHi }):Play()
            end)
            del.MouseLeave:Connect(function()
                TweenService:Create(del, HOVER_TWEEN,
                    { BackgroundColor3 = THEME.danger }):Play()
            end)
            del.MouseButton1Click:Connect(function()
                rec:DeleteRecording(f.name)
                -- Optimistic UI update: drop just this row instead of
                -- re-populating the whole list (which made every other
                -- recording flicker out + back in).
                row:Destroy()
                local any = false
                for _, c in ipairs(scroll:GetChildren()) do
                    if c:IsA("Frame") then any = true; break end
                end
                if not any then populate() end  -- show empty state
            end)
        end
    end
    refresh.MouseButton1Click:Connect(populate)
    UI.refreshFiles = populate
    return { view = view, populate = populate }
end

local function buildSourcesView(parent)
    local view = mk("Frame", { BackgroundTransparency = 1,
        Size = UDim2.fromScale(1, 1) }, parent)
    pad(view, 16); vlist(view, 10)

    mk("TextLabel", { Text = "Capture Sources", Font = THEME.fontBold,
        TextSize = 16, TextColor3 = THEME.text, BackgroundTransparency = 1,
        Size = UDim2.new(1, 0, 0, 22), LayoutOrder = 1,
        TextXAlignment = Enum.TextXAlignment.Left }, view)
    mk("TextLabel", {
        Text = "Toggle what each tick captures. Changes save instantly and "
            .. "apply to both full recordings and the Instant Replay buffer.",
        Font = THEME.fontReg, TextSize = 12, TextColor3 = THEME.subtext,
        BackgroundTransparency = 1, Size = UDim2.new(1, 0, 0, 32),
        TextWrapped = true,
        LayoutOrder = 2,
        TextYAlignment = Enum.TextYAlignment.Top,
        TextXAlignment = Enum.TextXAlignment.Left }, view)

    local controls = {}  -- key -> { btn, refresh }   (for cross-tab sync)

    local function plannedRow(name, desc, order)
        local row = mk("Frame", { BackgroundColor3 = THEME.panel,
            BorderSizePixel = 0, Size = UDim2.new(1, 0, 0, 60),
            LayoutOrder = order }, view)
        corner(row, 6); pad(row, 14, 12, 14, 12)
        mk("TextLabel", { Text = name, Font = THEME.fontBold, TextSize = 14,
            TextColor3 = THEME.text, BackgroundTransparency = 1,
            Size = UDim2.new(1, -110, 0, 18),
            TextXAlignment = Enum.TextXAlignment.Left }, row)
        mk("TextLabel", { Text = desc, Font = THEME.fontReg, TextSize = 12,
            TextColor3 = THEME.subtext, BackgroundTransparency = 1,
            Position = UDim2.fromOffset(0, 20),
            Size = UDim2.new(1, -110, 0, 18),
            TextXAlignment = Enum.TextXAlignment.Left,
            TextTruncate = Enum.TextTruncate.AtEnd }, row)
        local pill = mk("TextLabel", { Text = "PLANNED",
            Font = THEME.fontBold, TextSize = 11,
            TextColor3 = Color3.new(1, 1, 1),
            BackgroundColor3 = THEME.standby, BorderSizePixel = 0,
            Position = UDim2.new(1, -98, 0.5, -11),
            Size = UDim2.fromOffset(90, 22),
        }, row); corner(pill, 4)
    end

    local function sourceRow(d, order)
        local row = mk("Frame", { BackgroundColor3 = THEME.panel,
            BorderSizePixel = 0, Size = UDim2.new(1, 0, 0, 60),
            LayoutOrder = order }, view)
        corner(row, 6); pad(row, 14, 12, 14, 12)
        mk("TextLabel", { Text = d.label, Font = THEME.fontBold, TextSize = 14,
            TextColor3 = THEME.text, BackgroundTransparency = 1,
            Size = UDim2.new(1, -110, 0, 18),
            TextXAlignment = Enum.TextXAlignment.Left }, row)
        mk("TextLabel", { Text = d.desc, Font = THEME.fontReg, TextSize = 12,
            TextColor3 = THEME.subtext, BackgroundTransparency = 1,
            Position = UDim2.fromOffset(0, 20),
            Size = UDim2.new(1, -110, 0, 18),
            TextWrapped = true, TextYAlignment = Enum.TextYAlignment.Top,
            TextXAlignment = Enum.TextXAlignment.Left }, row)

        local btn = mk("TextButton", { Text = "OFF",
            Font = THEME.fontBold, TextSize = 12, TextColor3 = Color3.new(1,1,1),
            BackgroundColor3 = THEME.standby, BorderSizePixel = 0,
            AutoButtonColor = false,
            Position = UDim2.new(1, -98, 0.5, -13),
            Size = UDim2.fromOffset(90, 26) }, row); corner(btn, 4)
        local function refresh()
            if rec.cfg[d.key] then
                btn.Text = "ON"
                btn.BackgroundColor3 = THEME.accent
            else
                btn.Text = "OFF"
                btn.BackgroundColor3 = THEME.standby
            end
        end
        btn.MouseButton1Click:Connect(function()
            rec:SetSetting(d.key, not rec.cfg[d.key])
            refresh()
        end)
        refresh()
        controls[d.key] = { btn = btn, refresh = refresh }
    end

    local order = 3
    for _, d in ipairs(settingsForGroup("Sources")) do
        sourceRow(d, order); order = order + 1
    end
    plannedRow("Audio events",
        "SoundService cues with timestamps for post-sync.", order)

    return { view = view, controls = controls }
end

----------------------------------------------------------------
-- UI: window assembly
----------------------------------------------------------------
local function buildUI()
    local parent = getUIParent()
    if not parent then return end
    local gui = mk("ScreenGui", {
        Name = "ROCORDER_UI",
        IgnoreGuiInset = true, ResetOnSpawn = false,
        ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
        DisplayOrder = 1000000,
    })
    gui.Parent = parent

    local WINDOW_W, WINDOW_H = 640, 560
    local TITLE_H, TAB_H, FOOTER_H = 38, 36, 28

    local window = mk("Frame", {
        Name = "Window", BackgroundColor3 = THEME.bg, BorderSizePixel = 0,
        Position = UDim2.fromOffset(60, 60),
        Size = UDim2.fromOffset(WINDOW_W, WINDOW_H),
        AnchorPoint = Vector2.new(0, 0), ClipsDescendants = true,
    }, gui); corner(window, 10); stroke(window, THEME.border, 1)
    UI.windowFullHeight = WINDOW_H

    -- ---- title bar ----
    local title = mk("Frame", { Name = "TitleBar",
        BackgroundColor3 = THEME.titleBar, BorderSizePixel = 0,
        Size = UDim2.new(1, 0, 0, TITLE_H),
    }, window); corner(title, 10)
    -- mask the bottom of the title bar's rounded corner so it merges into the content
    mk("Frame", { BackgroundColor3 = THEME.titleBar, BorderSizePixel = 0,
        Position = UDim2.new(0, 0, 0.5, 0), Size = UDim2.new(1, 0, 0.5, 0) }, title)

    -- accent stripe along the title bar's left edge — gives the window a recognizable mark
    mk("Frame", { BackgroundColor3 = THEME.accent, BorderSizePixel = 0,
        Position = UDim2.fromOffset(0, 8),
        Size = UDim2.fromOffset(3, TITLE_H - 16) }, title)

    mk("TextLabel", { Text = "ROCORDER",
        Font = THEME.fontBold, TextSize = 14, TextColor3 = THEME.text,
        BackgroundTransparency = 1,
        Position = UDim2.fromOffset(14, 0),
        Size = UDim2.fromOffset(96, TITLE_H),
        TextXAlignment = Enum.TextXAlignment.Left }, title)
    mk("TextLabel", { Text = "v" .. ROCORDER_VERSION,
        Font = THEME.fontMono, TextSize = 12, TextColor3 = THEME.subtext,
        BackgroundTransparency = 1,
        Position = UDim2.fromOffset(94, 0),
        Size = UDim2.new(1, -200, 1, 0),
        TextXAlignment = Enum.TextXAlignment.Left }, title)

    local function titleIconBtn(text, x)
        local b = mk("TextButton", { Text = text, Font = THEME.fontBold,
            TextSize = 16, TextColor3 = THEME.subtext,
            BackgroundTransparency = 1, AutoButtonColor = false,
            Position = UDim2.new(1, x, 0, 0),
            Size = UDim2.fromOffset(TITLE_H, TITLE_H) }, title)
        b.MouseEnter:Connect(function()
            TweenService:Create(b, HOVER_TWEEN, { TextColor3 = THEME.text }):Play()
        end)
        b.MouseLeave:Connect(function()
            TweenService:Create(b, HOVER_TWEEN, { TextColor3 = THEME.subtext }):Play()
        end)
        return b
    end
    local minBtn   = titleIconBtn("\xE2\x80\x93", -TITLE_H * 2)  -- en dash
    local closeBtn = titleIconBtn("\xE2\x9C\x95", -TITLE_H)       -- ✕
    minBtn.MouseButton1Click:Connect(function()
        UI:setMinimized(not UI.minimized)
    end)
    closeBtn.MouseButton1Click:Connect(function() UI:setVisible(false) end)
    UI.minBtn = minBtn

    -- drag
    do
        local dragging, startPos, startMouse
        title.InputBegan:Connect(function(i)
            if i.UserInputType == Enum.UserInputType.MouseButton1
                or i.UserInputType == Enum.UserInputType.Touch then
                dragging = true
                startPos = window.Position
                startMouse = Vector2.new(i.Position.X, i.Position.Y)
            end
        end)
        rec.conns.uiDragChanged = UserInputService.InputChanged:Connect(function(i)
            if dragging and (i.UserInputType == Enum.UserInputType.MouseMovement
                or i.UserInputType == Enum.UserInputType.Touch) then
                local dx = i.Position.X - startMouse.X
                local dy = i.Position.Y - startMouse.Y
                window.Position = UDim2.new(
                    startPos.X.Scale, startPos.X.Offset + dx,
                    startPos.Y.Scale, startPos.Y.Offset + dy)
            end
        end)
        rec.conns.uiDragEnded = UserInputService.InputEnded:Connect(function(i)
            if i.UserInputType == Enum.UserInputType.MouseButton1
                or i.UserInputType == Enum.UserInputType.Touch then
                dragging = false
            end
        end)
    end

    -- ---- tab bar with accent indicators ----
    local tabBar = mk("Frame", { BackgroundColor3 = THEME.titleBar,
        BorderSizePixel = 0, Position = UDim2.new(0, 0, 0, TITLE_H),
        Size = UDim2.new(1, 0, 0, TAB_H) }, window)
    hlist(tabBar, 4); pad(tabBar, 10, 4, 10, 0)
    -- 1px divider line UNDER the tab bar — parented to window so the tab bar's
    -- UIListLayout doesn't try to lay it out (and consume the whole row width).
    mk("Frame", { BackgroundColor3 = THEME.border, BorderSizePixel = 0,
        BackgroundTransparency = 0.5,
        Position = UDim2.new(0, 0, 0, TITLE_H + TAB_H - 1),
        Size = UDim2.new(1, 0, 0, 1) }, window)

    local content = mk("Frame", { BackgroundColor3 = THEME.bg,
        BorderSizePixel = 0,
        Position = UDim2.new(0, 0, 0, TITLE_H + TAB_H),
        Size = UDim2.new(1, 0, 1, -(TITLE_H + TAB_H + FOOTER_H)) }, window)

    -- ---- footer / hotkey hint bar ----
    local footer = mk("Frame", { BackgroundColor3 = THEME.titleBar,
        BorderSizePixel = 0,
        Position = UDim2.new(0, 0, 1, -FOOTER_H),
        Size = UDim2.new(1, 0, 0, FOOTER_H) }, window)
    mk("Frame", { BackgroundColor3 = THEME.border, BorderSizePixel = 0,
        BackgroundTransparency = 0.5,
        Size = UDim2.new(1, 0, 0, 1) }, footer)
    local footerLabel = mk("TextLabel", {
        Font = THEME.fontMono, TextSize = 12, TextColor3 = THEME.subtext,
        BackgroundTransparency = 1, Text = "",
        Position = UDim2.fromOffset(14, 0),
        Size = UDim2.new(1, -28, 1, 0),
        TextXAlignment = Enum.TextXAlignment.Left }, footer)
    UI.footerLabel = footerLabel

    local tabs = { "Record", "Settings", "Files", "Sources" }
    UI.tabBtns = {}
    UI.tabIndicators = {}
    UI.tabViews = {}
    for i, name in ipairs(tabs) do
        local b = mk("TextButton", { Text = name, Font = THEME.fontBold,
            TextSize = 13, TextColor3 = THEME.subtext,
            BackgroundColor3 = THEME.tabInact,
            BorderSizePixel = 0, AutoButtonColor = false,
            Size = UDim2.fromOffset(94, 30), LayoutOrder = i }, tabBar)
        corner(b, 6)
        -- accent underline shown when active
        local indicator = mk("Frame", {
            BackgroundColor3 = THEME.accent, BorderSizePixel = 0,
            Position = UDim2.new(0.5, -16, 1, -2),
            Size = UDim2.fromOffset(32, 2), Visible = false,
        }, b)
        corner(indicator, 1)
        b.MouseEnter:Connect(function()
            if UI.activeTab ~= name then
                TweenService:Create(b, HOVER_TWEEN,
                    { TextColor3 = THEME.text }):Play()
            end
        end)
        b.MouseLeave:Connect(function()
            if UI.activeTab ~= name then
                TweenService:Create(b, HOVER_TWEEN,
                    { TextColor3 = THEME.subtext }):Play()
            end
        end)
        b.MouseButton1Click:Connect(function() UI:selectTab(name) end)
        UI.tabBtns[name] = b
        UI.tabIndicators[name] = indicator
    end

    UI.recordCtl   = buildRecordView(content)
    UI.settingsCtl = buildSettingsView(content)
    UI.filesCtl    = buildFilesView(content)
    UI.sourcesCtl  = buildSourcesView(content)

    UI.tabViews.Record   = UI.recordCtl.view
    UI.tabViews.Settings = UI.settingsCtl.view
    UI.tabViews.Files    = UI.filesCtl.view
    UI.tabViews.Sources  = UI.sourcesCtl.view

    UI.gui = gui; UI.window = window
    UI.tabBar = tabBar; UI.content = content; UI.footer = footer
    UI:selectTab("Record")
    UI:_startStatusLoop()
end

function UI:selectTab(name)
    self.activeTab = name
    for tabName, view in pairs(self.tabViews) do
        view.Visible = (tabName == name)
    end
    for tabName, btn in pairs(self.tabBtns) do
        local active = (tabName == name)
        TweenService:Create(btn, HOVER_TWEEN, {
            BackgroundColor3 = active and THEME.tabActive or THEME.tabInact,
            TextColor3 = active and THEME.text or THEME.subtext,
        }):Play()
        local ind = self.tabIndicators and self.tabIndicators[tabName]
        if ind then ind.Visible = active end
    end
    if name == "Files" and self.refreshFiles then self.refreshFiles() end
end

function UI:setVisible(v)
    self.visible = v
    if self.window then self.window.Visible = v end
end

function UI:toggle() self:setVisible(not self.visible) end

function UI:setMinimized(min)
    if not self.window then return end
    self.minimized = min
    if self.tabBar then  self.tabBar.Visible  = not min end
    if self.content then self.content.Visible = not min end
    if self.footer then  self.footer.Visible  = not min end
    -- collapse window to just the title bar height when minimized
    local h = min and 38 or (self.windowFullHeight or 560)
    self.window.Size = UDim2.new(0, self.window.AbsoluteSize.X, 0, h)
    if self.minBtn then
        self.minBtn.Text = min and "\xE2\x96\xA2" or "\xE2\x80\x93"  -- ▢ vs –
    end
end

function UI:_refreshStatus()
    local ctl = self.recordCtl; if not ctl then return end
    local s = ctl.status
    local active = rec:IsRecording()
    if active then
        s.dot.BackgroundColor3 = THEME.recording
        s.statusLabel.Text = "Recording"
        ctl.recordBtn.Text = "Stop Recording"
        setButtonColors(ctl.recordBtn, THEME.danger, THEME.dangerHi)
        s.fields.time.Text = humanDuration(rec.session:elapsed())
        s.fields.ticks.Text = fmt("%d / %s", rec.session.tickCount,
            humanBytes(rec.session.bytesWritten + 0))
    elseif rec.cfg.IR_ENABLED then
        s.dot.BackgroundColor3 = THEME.accent
        s.statusLabel.Text = "Instant Replay (buffering)"
        ctl.recordBtn.Text = "Start Recording"
        setButtonColors(ctl.recordBtn, THEME.accent, THEME.accentHi)
        s.fields.time.Text = "—"
        s.fields.ticks.Text = "—"
    else
        s.dot.BackgroundColor3 = THEME.standby
        s.statusLabel.Text = "Idle"
        ctl.recordBtn.Text = "Start Recording"
        setButtonColors(ctl.recordBtn, THEME.accent, THEME.accentHi)
        s.fields.time.Text = "—"
        s.fields.ticks.Text = "—"
    end
    if rec.cfg.IR_ENABLED and rec.replay then
        s.fields.buffer.Text = fmt("%.1fs / %ds (%d frames)",
            rec.replay:seconds(), rec.cfg.IR_BUFFER_SEC, rec.replay.count)
    else
        s.fields.buffer.Text = "off"
    end
    local nt = 0; for _ in pairs(rec.tracker.tracked) do nt = nt + 1 end
    s.fields.tracked.Text = tostring(nt)
    -- replay button: grayed out when Instant Replay is off so it's obvious
    -- the button won't do anything (clicks still notify).
    if rec.cfg.IR_ENABLED then
        setButtonColors(ctl.replayBtn, THEME.panel, THEME.panelHi)
        ctl.replayBtn.TextColor3 = THEME.accent
        for _, c in ipairs(ctl.replayBtn:GetChildren()) do
            if c:IsA("UIStroke") then c.Color = THEME.accent; c.Transparency = 0 end
        end
    else
        -- equal Fill/Hover -> no hover swap when disabled
        setButtonColors(ctl.replayBtn, THEME.panel, THEME.panel)
        ctl.replayBtn.TextColor3 = THEME.subtext
        for _, c in ipairs(ctl.replayBtn:GetChildren()) do
            if c:IsA("UIStroke") then c.Color = THEME.border; c.Transparency = 0.4 end
        end
    end
    ctl.replayBtn.Text = "Save Instant Replay"
    ctl.refreshIrToggle()

    -- cross-tab: keep settings boolean toggles + key-binding labels in sync
    if self.settingsCtl then
        for key, c in pairs(self.settingsCtl.controls) do
            local d = defByKey(key)
            if d and d.type == "bool" and type(c) == "table" and c.refresh then
                c.refresh()
            elseif d and d.type == "choice" and type(c) == "table" and c.refresh then
                c.refresh()
            elseif d and d.type == "key" and typeof(c) == "Instance"
                and c:IsA("TextButton") and (not UI.keyBindBtn or UI.keyBindBtn.key ~= key) then
                c.Text = tostring(rec.cfg[key])
            end
        end
    end
    if self.sourcesCtl and self.sourcesCtl.controls then
        for _, c in pairs(self.sourcesCtl.controls) do
            if c.refresh then c.refresh() end
        end
    end

    -- footer hotkey hints
    if self.footerLabel then
        self.footerLabel.Text = fmt(
            "%s record  ·  %s save replay  ·  %s toggle window",
            rec.cfg.HOTKEY_RECORD, rec.cfg.HOTKEY_SAVE_REPLAY, rec.cfg.HOTKEY_UI)
    end

    -- Assets panel: aggregate progress + per-player rows
    if ctl.assetHeadline then
        local snap = queueSnapshot()
        local total = snap.totalSeen
        -- Progress bar = (done + failed) / total. Both done and failed are
        -- "finished" states — we're not waiting on them. Splitting them
        -- prevents the "stuck at 31/39" misread when 8 actually failed.
        local finished = snap.done + snap.failed
        local pct = total > 0 and (finished / total) or 0
        ctl.assetBarFill.Size = UDim2.new(pct, 0, 1, 0)

        local workerHealth = ""
        if snap.workerAgeSec > 5 and snap.queued > 0 then
            workerHealth = fmt(" — WORKER SILENT %.0fs", snap.workerAgeSec)
        end

        if total == 0 then
            ctl.assetHeadline.Text = "extractor ready · waiting for players"
        elseif snap.activeId then
            ctl.assetHeadline.Text = fmt(
                "extracting %s %s%s   (%d done · %d failed · %d queued)",
                snap.activeKind or "?", snap.activeId,
                snap.activePlayer and ("  (" .. snap.activePlayer .. ")") or "",
                snap.done, snap.failed, snap.queued)
        elseif snap.queued > 0 then
            ctl.assetHeadline.Text = fmt(
                "%d in queue (%d done · %d failed)%s",
                snap.queued, snap.done, snap.failed, workerHealth)
        elseif snap.failed > 0 then
            ctl.assetHeadline.Text = fmt(
                "complete: %d extracted · %d couldn't be fetched (player left "
                .. "or asset permission-locked)", snap.done, snap.failed)
        else
            ctl.assetHeadline.Text = fmt("complete: all %d extracted", snap.done)
        end
        ctl.assetStats.Text = fmt(
            "done %d · failed %d (of which %d missed: player left) · queued %d · "
            .. "worker tick %d (%.1fs ago)",
            snap.done, snap.failed, snap.missed, snap.queued,
            snap.iterations, snap.workerAgeSec)

        -- per-player rows: rebuild from sorted snapshot
        local list = ctl.assetPlayerList
        list:ClearAllChildren()
        vlist(list, 2)
        local rows = {}
        for _, ps in pairs(snap.perPlayer) do rows[#rows + 1] = ps end
        table.sort(rows, function(a, b)
            return (a.lastActivityAt or 0) > (b.lastActivityAt or 0)
        end)
        for i = 1, math.min(8, #rows) do
            local ps = rows[i]
            local pending = math.max(0, ps.total - ps.done - ps.failed)
            local icon, color
            if ps.leftAt and ps.missed > 0 then
                icon = "\xE2\x9A\xA0"; color = THEME.danger   -- ⚠
            elseif ps.leftAt then
                icon = "\xE2\x86\x90"; color = THEME.subtext  -- ←
            elseif pending == 0 and ps.failed == 0 then
                icon = "\xE2\x9C\x93"; color = THEME.success  -- ✓
            elseif pending > 0 then
                icon = "\xE2\x80\xA6"; color = THEME.accent   -- …
            else
                icon = "\xE2\x97\x8B"; color = THEME.subtext  -- ○
            end
            local row = mk("Frame", { BackgroundTransparency = 1,
                Size = UDim2.new(1, 0, 0, 14) }, list)
            mk("TextLabel", { Text = icon, Font = THEME.fontBold, TextSize = 11,
                TextColor3 = color, BackgroundTransparency = 1,
                Size = UDim2.fromOffset(14, 14),
                TextXAlignment = Enum.TextXAlignment.Left }, row)
            mk("TextLabel", { Text = ps.name,
                Font = THEME.fontReg, TextSize = 11, TextColor3 = THEME.text,
                BackgroundTransparency = 1,
                Position = UDim2.fromOffset(18, 0),
                Size = UDim2.new(0.55, -18, 1, 0),
                TextXAlignment = Enum.TextXAlignment.Left,
                TextTruncate = Enum.TextTruncate.AtEnd }, row)
            local detail
            if ps.leftAt and ps.missed > 0 then
                detail = fmt("left — %d missed", ps.missed)
            elseif ps.leftAt then
                detail = fmt("left — %d/%d", ps.done, ps.total)
            elseif pending > 0 then
                detail = fmt("%d/%d  (%d pending)", ps.done, ps.total, pending)
            else
                detail = fmt("%d/%d", ps.done, ps.total)
            end
            mk("TextLabel", { Text = detail,
                Font = THEME.fontMono, TextSize = 11, TextColor3 = THEME.subtext,
                BackgroundTransparency = 1,
                Position = UDim2.new(0.55, 0, 0, 0),
                Size = UDim2.new(0.45, 0, 1, 0),
                TextXAlignment = Enum.TextXAlignment.Right }, row)
        end
    end
end

function UI:_startStatusLoop()
    if self.statusLoopRunning then return end
    self.statusLoopRunning = true
    task.spawn(function()
        while self.gui and self.gui.Parent do
            if self.visible then self:_refreshStatus() end
            task.wait(0.2)
        end
        self.statusLoopRunning = false
    end)
end

rec.onStateChange = function() if UI.gui then UI:_refreshStatus() end end

----------------------------------------------------------------
-- Hotkeys + boot
----------------------------------------------------------------
buildUI()
Indicator:refresh()  -- pick up persisted state (e.g. IR_ENABLED) on script load

-- Asset extractor worker + watchdog. The worker drains the queue; the
-- watchdog respawns it if it goes silent (defensive — should never actually
-- need to trigger, but if it does we recover automatically).
_startExtractorWorker()
_startWatchdog()
_startEvictionScanner(rec)

-- Mark a player as "left" so the UI can show "X assets missed" for them. The
-- extractor's own fallback already tries HTTP when no live partRef remains.
rec.conns.playerLeaving = Players.PlayerRemoving:Connect(function(p)
    _markPlayerLeft(p.UserId)
end)

rec.conns.input = UserInputService.InputBegan:Connect(function(input, processed)
    -- key-bind picker captures the next key regardless of processed flag
    if UI.keyBindBtn and input.UserInputType == Enum.UserInputType.Keyboard then
        local k = UI.keyBindBtn
        local newName = input.KeyCode.Name
        rec:SetSetting(k.key, newName)
        k.btn.Text = newName
        UI.keyBindBtn = nil
        return
    end
    if input.UserInputType ~= Enum.UserInputType.Keyboard then return end
    -- Don't fire hotkeys while the user is editing a setting's TextBox.
    if UserInputService:GetFocusedTextBox() then return end
    -- We intentionally ignore the `processed` flag here: Roblox itself
    -- consumes some keys (e.g. F7 toggles built-in performance stats) and
    -- marks them processed before our handler runs, which used to silently
    -- swallow Save-Replay. The focused-TextBox check above is enough to
    -- prevent shortcuts firing while typing.
    local kc = input.KeyCode
    if kc == keyFromName(rec.cfg.HOTKEY_RECORD) then rec:Toggle()
    elseif kc == keyFromName(rec.cfg.HOTKEY_UI)     then UI:toggle()
    elseif kc == keyFromName(rec.cfg.HOTKEY_SAVE_REPLAY) then rec:SaveReplay() end
end)

game:BindToClose(function()
    if rec.session then rec:Stop() end
end)

function rec:OpenUI()   UI:setVisible(true)  end
function rec:CloseUI()  UI:setVisible(false) end
function rec:ToggleUI() UI:toggle()          end

function rec:_destroy()
    for _, c in pairs(self.conns) do pcall(function() c:Disconnect() end) end
    self.conns = {}
    if UI.gui then pcall(function() UI.gui:Destroy() end); UI.gui = nil end
    if Indicator then Indicator:destroy() end
end

notify("ROCORDER", fmt("v%s loaded. %s = UI, %s = record.", ROCORDER_VERSION,
    rec.cfg.HOTKEY_UI, rec.cfg.HOTKEY_RECORD), 5)
print(fmt("[ROCORDER] v%s ready. UI=%s record=%s saveReplay=%s",
    ROCORDER_VERSION, rec.cfg.HOTKEY_UI, rec.cfg.HOTKEY_RECORD, rec.cfg.HOTKEY_SAVE_REPLAY))
