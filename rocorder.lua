--!nocheck
-- ROCORDER — replay recorder for Xeno (and other executors with file IO).
--
-- Records every player's per-part world transform (position + quaternion) at a
-- fixed tick rate, plus a companion rig snapshot used by the Blender importer
-- to rebuild a proper armature. Toggle with F8, or call the _G.ROCORDER API.
--
-- This is NOT a cheat: it only reads transforms the client already renders and
-- writes them to disk for offline post-processing.
--
-- ============================================================================
-- FILE FORMAT  (ROCORDER/3)
-- ----------------------------------------------------------------------------
-- .rec  line 1 : JSON header
--       line N : t=<sec>;<uid>:<part0>|<part1>|...;<uid>:...
--                where each <partK> = px,py,pz,qx,qy,qz,qw
--                (world position in studs + world rotation as a quaternion)
--
--       Parts are POSITIONAL: the K-th part in a player's frame entry maps to
--       the K-th entry of that player's `parts` array in the .rig.json. Every
--       frame always lists ALL of a player's parts in that fixed order, so the
--       importer never has to deal with holes in a bone's keyframe stream.
--
-- .rig.json : per-player rig (parts in canonical order + Motor6D joints with
--             C0/C1). Written at Stop so late joiners are included.
-- ============================================================================

if _G.ROCORDER then
    if _G.ROCORDER.Stop then
        pcall(function() _G.ROCORDER:Stop() end)
    end
    if _G.ROCORDER._inputConn then
        pcall(function() _G.ROCORDER._inputConn:Disconnect() end)
        _G.ROCORDER._inputConn = nil
    end
end

local Players          = game:GetService("Players")
local RunService       = game:GetService("RunService")
local UserInputService = game:GetService("UserInputService")
local StarterGui       = game:GetService("StarterGui")
local HttpService      = game:GetService("HttpService")

----------------------------------------------------------------
-- Config
----------------------------------------------------------------
local CONFIG = {
    TICK_RATE       = 30,        -- samples per second
    FLUSH_INTERVAL  = 0.5,       -- seconds between disk flushes
    MAX_CATCHUP_SEC = 5.0,       -- cap on backfilled frames after a stall
    MAX_DISTANCE    = 0,         -- studs from workspace.CurrentCamera. Players
                                 -- beyond this are skipped for the tick.
                                 -- 0 = unlimited (record everyone).
    POS_PRECISION   = 3,         -- decimal places for positions (studs)
    ROT_PRECISION   = 5,         -- decimal places for quaternion components
    HOTKEY          = Enum.KeyCode.F8,
    FOLDER          = "ROCORDER",
    INCLUDE_LOCAL   = true,      -- record the local player too
}

----------------------------------------------------------------
-- Executor IO sanity check
----------------------------------------------------------------
local writefile  = writefile or (syn and syn.writefile)
local appendfile = appendfile
local isfolder   = isfolder
local makefolder = makefolder

if not writefile or not appendfile or not makefolder or not isfolder then
    warn("[ROCORDER] Executor is missing required file IO functions.")
    return
end

if not isfolder(CONFIG.FOLDER) then
    makefolder(CONFIG.FOLDER)
end

----------------------------------------------------------------
-- Helpers
----------------------------------------------------------------
local fmt = string.format

local function notify(title, text, duration)
    pcall(function()
        StarterGui:SetCore("SendNotification", {
            Title = title, Text = text, Duration = duration or 3,
        })
    end)
end

local POS_FMT = "%." .. CONFIG.POS_PRECISION .. "f"
local ROT_FMT = "%." .. CONFIG.ROT_PRECISION .. "f"

local function sanitizeName(s)
    -- strip our delimiters so part names can't corrupt a frame line
    return (s:gsub("[;:=,|]", "_"))
end

-- Rotation matrix (row-major, from CFrame:GetComponents) -> unit quaternion.
-- Returns qx, qy, qz, qw. Standard Shepperd's method (numerically stable).
local function matrixToQuat(m00, m01, m02, m10, m11, m12, m20, m21, m22)
    local trace = m00 + m11 + m22
    local qw, qx, qy, qz
    if trace > 0 then
        local s = math.sqrt(trace + 1.0) * 2.0   -- s = 4*qw
        qw = 0.25 * s
        qx = (m21 - m12) / s
        qy = (m02 - m20) / s
        qz = (m10 - m01) / s
    elseif m00 > m11 and m00 > m22 then
        local s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0  -- s = 4*qx
        qw = (m21 - m12) / s
        qx = 0.25 * s
        qy = (m01 + m10) / s
        qz = (m02 + m20) / s
    elseif m11 > m22 then
        local s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0  -- s = 4*qy
        qw = (m02 - m20) / s
        qx = (m01 + m10) / s
        qy = 0.25 * s
        qz = (m12 + m21) / s
    else
        local s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0  -- s = 4*qz
        qw = (m10 - m01) / s
        qx = (m02 + m20) / s
        qy = (m12 + m21) / s
        qz = 0.25 * s
    end
    return qx, qy, qz, qw
end

-- Encode a part's world CFrame as "px,py,pz,qx,qy,qz,qw".
local function encodePart(cf)
    local px, py, pz,
          r00, r01, r02,
          r10, r11, r12,
          r20, r21, r22 = cf:GetComponents()
    local qx, qy, qz, qw = matrixToQuat(r00, r01, r02, r10, r11, r12, r20, r21, r22)
    return fmt(
        POS_FMT .. "," .. POS_FMT .. "," .. POS_FMT .. "," ..
        ROT_FMT .. "," .. ROT_FMT .. "," .. ROT_FMT .. "," .. ROT_FMT,
        px, py, pz, qx, qy, qz, qw
    )
end

-- Snapshot one player's rig: ordered parts (dimensions, color, shape, mesh
-- refs, rest CFrame) and Motor6D joints (with C0/C1 so the importer can derive
-- the canonical T-pose). The parts array order IS the positional index used in
-- the .rec frames.
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
        if humanoid.RigType == Enum.HumanoidRigType.R15 then
            rig.rigType = "R15"
        elseif humanoid.RigType == Enum.HumanoidRigType.R6 then
            rig.rigType = "R6"
        end
    end

    -- ordered list of part names (this defines the positional index)
    local order = {}

    for _, child in ipairs(char:GetChildren()) do
        if child:IsA("BasePart") then
            local cf = child.CFrame
            local info = {
                name         = sanitizeName(child.Name),
                className    = child.ClassName,
                size         = { child.Size.X, child.Size.Y, child.Size.Z },
                color        = { child.Color.R, child.Color.G, child.Color.B },
                transparency = child.Transparency,
                -- world CFrame right now: 12 floats from GetComponents()
                restCFrame   = { cf:GetComponents() },
            }
            if child:IsA("Part") then
                info.shape = child.Shape.Name
            elseif child:IsA("MeshPart") then
                info.shape = "MeshPart"
                local ok, meshId = pcall(function() return child.MeshId end)
                if ok and meshId and meshId ~= "" then info.meshId = meshId end
                local ok2, texId = pcall(function() return child.TextureID end)
                if ok2 and texId and texId ~= "" then info.textureId = texId end
            else
                info.shape = "Block"
            end
            rig.parts[#rig.parts + 1] = info
            order[#order + 1] = child.Name  -- raw name for FindFirstChild lookups
        end
    end

    for _, desc in ipairs(char:GetDescendants()) do
        if desc:IsA("Motor6D") then
            local p0, p1 = desc.Part0, desc.Part1
            if p0 and p1 then
                rig.joints[#rig.joints + 1] = {
                    name  = desc.Name,
                    part0 = sanitizeName(p0.Name),
                    part1 = sanitizeName(p1.Name),
                    -- C0/C1 are STRUCTURAL (constant under animation); the
                    -- importer walks them to build the canonical rest pose.
                    c0    = { desc.C0:GetComponents() },
                    c1    = { desc.C1:GetComponents() },
                }
            end
        end
    end

    return rig, order
end

----------------------------------------------------------------
-- Recorder
----------------------------------------------------------------
local Recorder = {}
Recorder.__index = Recorder

function Recorder.new()
    return setmetatable({
        active        = false,
        filename      = nil,
        rigFilename   = nil,
        startClock    = 0,
        nextTickAt    = 0,
        lastFlushAt   = 0,
        tickInterval  = 1 / CONFIG.TICK_RATE,
        buffer        = {},
        tickCount     = 0,
        connHeartbeat = nil,
        tracked       = {},   -- uid -> { rig, order={rawName...}, last={partStr...} }
    }, Recorder)
end

function Recorder:_shouldRecord(player)
    return CONFIG.INCLUDE_LOCAL or player ~= Players.LocalPlayer
end

-- Register a player on first sighting: capture its rig + fixed part order and
-- seed the last-pose cache from the current frame.
function Recorder:_ensureTracked(player)
    local uid = player.UserId
    if self.tracked[uid] then return self.tracked[uid] end
    if not player.Character then return nil end

    local ok, rig, order = pcall(captureRig, player)
    if not ok or not rig then
        if not ok then warn("[ROCORDER] captureRig failed for", player.Name, "->", rig) end
        return nil
    end

    local entry = { rig = rig, order = order, last = {} }
    -- seed last-pose cache so any briefly-missing part has something to hold
    local char = player.Character
    for i, rawName in ipairs(order) do
        local part = char:FindFirstChild(rawName)
        entry.last[i] = part and encodePart(part.CFrame) or "0,0,0,0,0,0,1"
    end
    self.tracked[uid] = entry
    return entry
end

function Recorder:_writeHeader()
    local localPlayer = Players.LocalPlayer
    local roster = {}
    for _, p in ipairs(Players:GetPlayers()) do
        if self:_shouldRecord(p) then
            roster[#roster + 1] = {
                userId = p.UserId, name = p.Name, displayName = p.DisplayName,
            }
        end
    end

    local header = {
        format       = "ROCORDER/3",
        placeId      = game.PlaceId,
        jobId        = game.JobId,
        startedAt    = os.time(),
        tickRate     = CONFIG.TICK_RATE,
        posPrecision = CONFIG.POS_PRECISION,
        rotPrecision = CONFIG.ROT_PRECISION,
        captureMode  = "parts-posquat",
        rigFile      = self.rigFilename,
        -- per-part columns inside a frame entry (positional, '|'-separated)
        columns      = { "px", "py", "pz", "qx", "qy", "qz", "qw" },
        roster       = roster,
    }
    writefile(CONFIG.FOLDER .. "/" .. self.filename,
        HttpService:JSONEncode(header) .. "\n")
end

-- Build the per-player entries for the current moment WITHOUT a timestamp.
-- Always emits every tracked part of every in-range player (holding the last
-- known value for parts that briefly vanished), so keyframe streams stay dense.
function Recorder:_takeSnapshot()
    local entries = {}

    local cameraPos
    local cam = workspace.CurrentCamera
    if cam then cameraPos = cam.CFrame.Position end
    local maxDistSq = CONFIG.MAX_DISTANCE > 0
        and (CONFIG.MAX_DISTANCE * CONFIG.MAX_DISTANCE) or nil

    for _, p in ipairs(Players:GetPlayers()) do
        if self:_shouldRecord(p) then
            local entry = self:_ensureTracked(p)
            if entry then
                local char = p.Character

                -- distance cull (uses HumanoidRootPart if present)
                local inRange = true
                if maxDistSq and cameraPos and char then
                    local root = char:FindFirstChild("HumanoidRootPart")
                    if root then
                        local d = root.Position - cameraPos
                        if (d.X * d.X + d.Y * d.Y + d.Z * d.Z) > maxDistSq then
                            inRange = false
                        end
                    end
                end

                if inRange then
                    local parts = {}
                    for i, rawName in ipairs(entry.order) do
                        local str
                        local part = char and char:FindFirstChild(rawName)
                        if part and part:IsA("BasePart") then
                            str = encodePart(part.CFrame)
                            entry.last[i] = str         -- refresh cache
                        else
                            str = entry.last[i]         -- hold last known pose
                        end
                        parts[i] = str
                    end
                    entries[#entries + 1] =
                        tostring(p.UserId) .. ":" .. table.concat(parts, "|")
                end
                -- out-of-range players intentionally emit nothing this tick
            end
        end
    end
    return entries
end

function Recorder:_writeFrame(t, snapshot)
    local n = #snapshot
    local line = table.create and table.create(n + 1) or {}
    line[1] = "t=" .. fmt(POS_FMT, t)
    for i = 1, n do line[i + 1] = snapshot[i] end
    self.buffer[#self.buffer + 1] = table.concat(line, ";")
    self.tickCount += 1
end

function Recorder:_flush()
    if #self.buffer == 0 then return end
    local chunk = table.concat(self.buffer, "\n") .. "\n"
    table.clear(self.buffer)
    appendfile(CONFIG.FOLDER .. "/" .. self.filename, chunk)
end

function Recorder:_writeRigFile()
    local rigData = {
        format     = "ROCORDER-RIG/2",
        recFile    = self.filename,
        capturedAt = os.time(),
        players    = {},
    }
    local captured = 0
    for uid, entry in pairs(self.tracked) do
        rigData.players[tostring(uid)] = entry.rig
        captured += 1
    end

    local ok, err = pcall(function()
        writefile(CONFIG.FOLDER .. "/" .. self.rigFilename,
            HttpService:JSONEncode(rigData))
    end)
    if ok then
        print(fmt("[ROCORDER] Wrote rig file %s (%d players)",
            self.rigFilename, captured))
    else
        warn("[ROCORDER] Failed to write rig file:", err)
    end
end

function Recorder:Start()
    if self.active then return end

    self.filename    = fmt("replay_%d_%d.rec", game.PlaceId, os.time())
    self.rigFilename = (self.filename:gsub("%.rec$", ".rig.json"))
    self.startClock  = os.clock()
    self.nextTickAt  = self.startClock
    self.lastFlushAt = self.startClock
    self.tickCount   = 0
    self.tracked     = {}
    table.clear(self.buffer)

    self:_writeHeader()
    self.active = true

    self.connHeartbeat = RunService.Heartbeat:Connect(function()
        if not self.active then return end
        local now = os.clock()

        if now >= self.nextTickAt then
            -- one snapshot per Heartbeat; backfill any missed slots with the
            -- same snapshot so the Blender timeline stays dense.
            local snapshot = self:_takeSnapshot()
            local maxCatchup = math.ceil(CONFIG.MAX_CATCHUP_SEC / self.tickInterval)
            local filled = 0
            while now >= self.nextTickAt and filled < maxCatchup do
                self:_writeFrame(self.nextTickAt - self.startClock, snapshot)
                self.nextTickAt += self.tickInterval
                filled += 1
            end
            if now >= self.nextTickAt then
                self.nextTickAt = now + self.tickInterval
            end
        end

        if now - self.lastFlushAt >= CONFIG.FLUSH_INTERVAL then
            self:_flush()
            self.lastFlushAt = now
        end
    end)

    notify("ROCORDER", "Recording started -> " .. self.filename, 4)
    print("[ROCORDER] Recording started:", self.filename)
end

function Recorder:Stop()
    if not self.active then return end
    self.active = false

    if self.connHeartbeat then
        self.connHeartbeat:Disconnect()
        self.connHeartbeat = nil
    end

    self:_flush()
    self:_writeRigFile()  -- written here so late joiners are included

    local seconds = os.clock() - self.startClock
    local msg = fmt("Saved %d ticks (%.1fs) -> %s",
        self.tickCount, seconds, self.filename)
    notify("ROCORDER", msg, 5)
    print("[ROCORDER]", msg)
end

function Recorder:Toggle()
    if self.active then self:Stop() else self:Start() end
end

function Recorder:IsRecording()
    return self.active
end

----------------------------------------------------------------
-- Wire up hotkey + global
----------------------------------------------------------------
local rec = Recorder.new()
_G.ROCORDER = rec

rec._inputConn = UserInputService.InputBegan:Connect(function(input, processed)
    if processed then return end
    if input.KeyCode == CONFIG.HOTKEY then
        rec:Toggle()
    end
end)

game:BindToClose(function()
    if rec.active then rec:Stop() end
end)

notify("ROCORDER", "Loaded. Press F8 to start/stop recording.", 4)
print("[ROCORDER] Ready. Hotkey:", CONFIG.HOTKEY.Name)
