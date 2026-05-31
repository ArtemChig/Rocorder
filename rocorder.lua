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

local ROCORDER_VERSION = "1.6.1-alpha"

if _G.ROCORDER then
    if _G.ROCORDER.Stop then pcall(function() _G.ROCORDER:Stop() end) end
    if _G.ROCORDER._destroy then pcall(function() _G.ROCORDER:_destroy() end) end
end

local Players            = game:GetService("Players")
local RunService         = game:GetService("RunService")
local UserInputService   = game:GetService("UserInputService")
local StarterGui         = game:GetService("StarterGui")
local HttpService        = game:GetService("HttpService")
local TweenService       = game:GetService("TweenService")
local MarketplaceService = game:GetService("MarketplaceService")

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
-- appeared, exactly like a late-joining player).
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
            entry.refs[idx] = { name = nm, inst = desc }
            entry.rig.parts[idx] = partInfo(desc, nm)
            entry.last[idx] = encode(desc.CFrame)
            added += 1
            if debugLog then
                debugLog(fmt("uid=%d part[%d] %s ATTACHED mid-recording (%s)",
                    entry.uid, idx - 1, nm, entry.rig.parts[idx].shape or "?"))
            end
        end
    end
    return added
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
        uid = uid, rig = rig, refs = refs, last = {},
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
                -- mid-recording equip: throttled append-scan (~1/s)
                if hasChar and now - entry.lastScanClock >= 1.0 then
                    entry.lastScanClock = now
                    self:_appendNewParts(entry, char, encode, debugLog)
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
                else
                    entry.culledTicks += 1
                end
            end
        end
    end
    return entries
end

function Tracker:rigData(filenameForRef)
    local data = {
        format = "ROCORDER-RIG/2",
        recFile = filenameForRef,
        capturedAt = os.time(),
        players = {},
    }
    local n = 0
    for uid, e in pairs(self.tracked) do
        data.players[tostring(uid)] = e.rig
        n += 1
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

    task.spawn(function()
        if not isfolder(ASSETS_FOLDER) then pcall(makefolder, ASSETS_FOLDER) end
        local okc, failc, skipc = 0, 0, 0
        dbg(fmt("ASSET DOWNLOAD start: %d unique assets (httpRequest=%s)",
            #list, httpRequest and "yes" or "no (game:HttpGet only)"))
        notify("ROCORDER", fmt("Downloading %d assets…", #list), 3)

        for _, id in ipairs(list) do
            local path = ASSETS_FOLDER .. "/" .. id
            if isfile and isfile(path) then
                skipc += 1
            else
                local url = "https://assetdelivery.roblox.com/v1/asset/?id=" .. id
                local body

                if httpRequest then
                    local okr, resp = pcall(httpRequest, { Url = url, Method = "GET" })
                    if okr and type(resp) == "table" then
                        local code = resp.StatusCode or resp.Status or 200
                        if resp.Body and #resp.Body > 0 and code >= 200 and code < 300 then
                            body = resp.Body
                        else
                            dbg(fmt("  asset %s http status=%s", id, tostring(code)))
                        end
                    end
                end
                if not body then
                    local okg, b = pcall(function() return game:HttpGet(url, true) end)
                    if okg and type(b) == "string" and #b > 0 and b:sub(1, 3) ~= "404" then
                        body = b
                    end
                end

                if body then
                    local okw = pcall(writefile, path, body)
                    if okw then okc += 1 else failc += 1 end
                else
                    failc += 1
                end
            end
            task.wait()  -- yield between downloads so we never hitch the client
        end

        dbg(fmt("ASSET DOWNLOAD done: %d saved, %d already cached, %d failed -> %s",
            okc, skipc, failc, ASSETS_FOLDER))
        notify("ROCORDER",
            fmt("Assets: %d saved, %d cached, %d failed", okc, skipc, failc), 5)
    end)
end

function rec:Stop()
    if not self.session then return end
    local s = self.session
    self.session = nil
    s:flush()
    local data = self.tracker:rigData(s.filename)
    s:writeRig(data)
    s:writeMeta()
    self:_invalidateRecordingsCache()
    if s.debugEnabled then
        s:debugLog(fmt("STOP after %.2fs: ticks=%d stalls=%d gaps=%d",
            s:elapsed(), s.tickCount, s.stallCount, s.gapCount))
        for uid, e in pairs(self.tracker.tracked) do
            s:debugLog(fmt("  uid=%d (%s) ticks=%d culled=%d parts=%d joints=%d",
                uid, e.rig.name, e.ticks, e.culledTicks, #e.rig.parts, #e.rig.joints))
        end
        s:flushDebug()
    end
    notify("ROCORDER", fmt("Saved %d ticks -> %s", s.tickCount, s.filename), 4)
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
    seconds = seconds or self.cfg.IR_BUFFER_SEC
    local rigData = self.tracker:rigData("")
    local saved, frames, secsOut = self.replay:save(FOLDER, self.cfg, rigData, seconds)
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
    local view = mk("Frame", { BackgroundTransparency = 1,
        Size = UDim2.fromScale(1, 1) }, parent)
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

    return {
        view = view, status = status,
        recordBtn = recordBtn, replayBtn = replayBtn,
        irToggle = irToggle, refreshIrToggle = refreshIrToggle,
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
