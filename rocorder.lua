--!nocheck
-- ROCORDER — replay recorder for Xeno (and other executors with file IO).
--
-- Records every player's per-part world transform (position + quaternion) at a
-- fixed tick rate, plus a companion rig snapshot used by the Blender importer
-- to rebuild a proper armature. Toggle with F8.
--
-- ============================================================================

local ROCORDER_VERSION = "1.0.0-alpha"

-- ============================================================================
-- FILE FORMAT  (ROCORDER/3)
-- ----------------------------------------------------------------------------
-- .rec       line 1 : JSON header
--            line N : t=<sec>;<uid>:<part0>|<part1>|...;<uid>:...
--                     each <partK> = px,py,pz,qx,qy,qz,qw  (positional, the
--                     K-th part of a player maps to rig.players[uid].parts[K])
-- .rig.json  Per-player rig (parts ordered, Motor6D joints with C0/C1).
-- .debug.log Sibling file with diagnostic events (toggle via CONFIG.DEBUG).
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
    TICK_RATE       = 30,
    FLUSH_INTERVAL  = 0.5,
    MAX_CATCHUP_SEC = 5.0,
    MAX_DISTANCE    = 0,         -- 0 = unlimited
    POS_PRECISION   = 3,
    ROT_PRECISION   = 5,
    HOTKEY          = Enum.KeyCode.F8,
    FOLDER          = "ROCORDER",
    INCLUDE_LOCAL   = true,
    DEBUG           = true,      -- write <name>.debug.log with diagnostics
    STALL_LOG_SEC   = 0.15,      -- log heartbeat gaps longer than this
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
    return (s:gsub("[;:=,|]", "_"))
end

local function matrixToQuat(m00, m01, m02, m10, m11, m12, m20, m21, m22)
    local trace = m00 + m11 + m22
    local qw, qx, qy, qz
    if trace > 0 then
        local s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (m21 - m12) / s
        qy = (m02 - m20) / s
        qz = (m10 - m01) / s
    elseif m00 > m11 and m00 > m22 then
        local s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        qw = (m21 - m12) / s
        qx = 0.25 * s
        qy = (m01 + m10) / s
        qz = (m02 + m20) / s
    elseif m11 > m22 then
        local s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        qw = (m02 - m20) / s
        qx = (m01 + m10) / s
        qy = 0.25 * s
        qz = (m12 + m21) / s
    else
        local s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        qw = (m10 - m01) / s
        qx = (m02 + m20) / s
        qy = (m12 + m21) / s
        qz = 0.25 * s
    end
    return qx, qy, qz, qw
end

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
            order[#order + 1] = child.Name
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
        debugFilename = nil,
        startClock    = 0,
        nextTickAt    = 0,
        lastFlushAt   = 0,
        lastHeartbeat = 0,
        tickInterval  = 1 / CONFIG.TICK_RATE,
        buffer        = {},
        debugBuffer   = {},
        tickCount     = 0,
        gapCount      = 0,       -- times nextTickAt was force-jumped (real gaps)
        stallCount    = 0,
        connHeartbeat = nil,
        tracked       = {},   -- uid -> entry (rig, order, last, stats...)
    }, Recorder)
end

function Recorder:_shouldRecord(player)
    return CONFIG.INCLUDE_LOCAL or player ~= Players.LocalPlayer
end

-- ---- debug logging ----
function Recorder:_debugLog(msg)
    if not CONFIG.DEBUG then return end
    local t = self.startClock > 0 and (os.clock() - self.startClock) or 0
    self.debugBuffer[#self.debugBuffer + 1] = fmt("[t=%7.3f] %s", t, msg)
end

function Recorder:_flushDebug()
    if not CONFIG.DEBUG or #self.debugBuffer == 0 then return end
    local chunk = table.concat(self.debugBuffer, "\n") .. "\n"
    local ok = pcall(appendfile,
        CONFIG.FOLDER .. "/" .. self.debugFilename, chunk)
    if ok then table.clear(self.debugBuffer) end
    -- if it fails, retain — but don't recursively try to log the failure
end

function Recorder:_ensureTracked(player)
    local uid = player.UserId
    if self.tracked[uid] then return self.tracked[uid] end
    if not player.Character then return nil end

    local ok, rig, order = pcall(captureRig, player)
    if not ok or not rig then
        self:_debugLog(fmt("captureRig FAILED for uid=%d (%s) err=%s",
            uid, player.Name, tostring(rig)))
        if not ok then warn("[ROCORDER] captureRig failed for", player.Name, "->", rig) end
        return nil
    end

    local entry = {
        rig         = rig,
        order       = order,
        last        = {},
        ticks       = 0,
        culledTicks = 0,
        hadChar     = true,
        partLost    = {},   -- i -> bool
        rangeIn     = true,
    }
    local char = player.Character
    for i, rawName in ipairs(order) do
        local part = char:FindFirstChild(rawName)
        entry.last[i] = part and encodePart(part.CFrame) or "0,0,0,0,0,0,1"
        if not part then entry.partLost[i] = true end
    end
    self.tracked[uid] = entry

    self:_debugLog(fmt("ENSURE uid=%d name=%s display=%s rigType=%s parts=%d joints=%d",
        uid, player.Name, player.DisplayName, rig.rigType, #rig.parts, #rig.joints))
    -- list parts in order so the importer log can correlate index -> name
    local names = {}
    for i, p in ipairs(rig.parts) do
        names[#names + 1] = fmt("  [%d] %s shape=%s transparency=%.3f size=(%.2f,%.2f,%.2f)",
            i - 1, p.name, p.shape or "?", p.transparency or 0,
            p.size[1], p.size[2], p.size[3])
    end
    self:_debugLog("  parts:\n" .. table.concat(names, "\n"))
    -- joint hierarchy summary
    local jn = {}
    for _, j in ipairs(rig.joints) do
        jn[#jn + 1] = fmt("    %s: %s -> %s", j.name, j.part0, j.part1)
    end
    self:_debugLog("  joints:\n" .. table.concat(jn, "\n"))
    return entry
end

function Recorder:_writeHeader()
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
        recorder     = ROCORDER_VERSION,
        placeId      = game.PlaceId,
        jobId        = game.JobId,
        startedAt    = os.time(),
        tickRate     = CONFIG.TICK_RATE,
        posPrecision = CONFIG.POS_PRECISION,
        rotPrecision = CONFIG.ROT_PRECISION,
        captureMode  = "parts-posquat",
        rigFile      = self.rigFilename,
        debugFile    = CONFIG.DEBUG and self.debugFilename or nil,
        columns      = { "px", "py", "pz", "qx", "qy", "qz", "qw" },
        roster       = roster,
    }
    writefile(CONFIG.FOLDER .. "/" .. self.filename,
        HttpService:JSONEncode(header) .. "\n")
end

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

                -- char-lost transition log
                local hasChar = char ~= nil
                if hasChar ~= entry.hadChar then
                    self:_debugLog(fmt("uid=%d character %s",
                        p.UserId, hasChar and "REGAINED" or "LOST"))
                    entry.hadChar = hasChar
                end

                -- distance cull
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
                if inRange ~= entry.rangeIn then
                    self:_debugLog(fmt("uid=%d %s distance range",
                        p.UserId, inRange and "ENTERED" or "EXITED"))
                    entry.rangeIn = inRange
                end

                if inRange then
                    local parts = {}
                    for i, rawName in ipairs(entry.order) do
                        local str
                        local part = char and char:FindFirstChild(rawName)
                        local isLost = not (part and part:IsA("BasePart"))
                        if not isLost then
                            str = encodePart(part.CFrame)
                            entry.last[i] = str
                        else
                            str = entry.last[i]
                        end
                        if isLost ~= entry.partLost[i] then
                            self:_debugLog(fmt("uid=%d part[%d] %s %s",
                                p.UserId, i - 1, entry.rig.parts[i].name,
                                isLost and "LOST (using cache)" or "REGAINED"))
                            entry.partLost[i] = isLost
                        end
                        parts[i] = str
                    end
                    entries[#entries + 1] =
                        tostring(p.UserId) .. ":" .. table.concat(parts, "|")
                    entry.ticks += 1
                else
                    entry.culledTicks += 1
                end
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
    -- IMPORTANT: pcall the append and only clear the buffer on success. The
    -- previous version cleared first, so any silent appendfile failure (the
    -- executor occasionally drops a chunk under load) lost ~FLUSH_INTERVAL
    -- seconds of frames, producing the keyframe gaps we kept seeing in
    -- Blender's dopesheet despite the recorder reporting no internal gaps.
    local ok, err = pcall(appendfile,
        CONFIG.FOLDER .. "/" .. self.filename, chunk)
    if ok then
        table.clear(self.buffer)
    else
        self:_debugLog(fmt(
            "*** FLUSH FAILED: %s — kept %d lines for retry next flush",
            tostring(err), #self.buffer))
    end
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

    self.filename      = fmt("replay_%d_%d.rec", game.PlaceId, os.time())
    self.rigFilename   = (self.filename:gsub("%.rec$", ".rig.json"))
    self.debugFilename = (self.filename:gsub("%.rec$", ".debug.log"))
    self.startClock    = os.clock()
    self.nextTickAt    = self.startClock
    self.lastFlushAt   = self.startClock
    self.lastHeartbeat = self.startClock
    self.tickCount     = 0
    self.gapCount      = 0
    self.stallCount    = 0
    self.tracked       = {}
    table.clear(self.buffer)
    table.clear(self.debugBuffer)

    self:_writeHeader()

    -- prime the debug log
    if CONFIG.DEBUG then
        pcall(function()
            writefile(CONFIG.FOLDER .. "/" .. self.debugFilename,
                fmt("ROCORDER %s debug log for %s\n",
                    ROCORDER_VERSION, self.filename))
        end)
        self:_debugLog(fmt(
            "START tickRate=%d posPrec=%d rotPrec=%d maxCatchup=%.1fs maxDistance=%d posPrec=%d",
            CONFIG.TICK_RATE, CONFIG.POS_PRECISION, CONFIG.ROT_PRECISION,
            CONFIG.MAX_CATCHUP_SEC, CONFIG.MAX_DISTANCE, CONFIG.POS_PRECISION))
        local roster = {}
        for _, p in ipairs(Players:GetPlayers()) do
            if self:_shouldRecord(p) then
                roster[#roster + 1] = fmt("uid=%d %s%s",
                    p.UserId, p.Name,
                    p.Character and "" or " (no character yet)")
            end
        end
        self:_debugLog("roster: " .. table.concat(roster, ", "))
    end

    self.active = true

    self.connHeartbeat = RunService.Heartbeat:Connect(function()
        if not self.active then return end
        local now = os.clock()

        -- stall detection (purely diagnostic — recording continues)
        local hbGap = now - self.lastHeartbeat
        if hbGap > CONFIG.STALL_LOG_SEC then
            self.stallCount += 1
            self:_debugLog(fmt("heartbeat stall %.2fs", hbGap))
        end
        self.lastHeartbeat = now

        if now >= self.nextTickAt then
            local snapshot = self:_takeSnapshot()
            local maxCatchup = math.ceil(CONFIG.MAX_CATCHUP_SEC / self.tickInterval)
            local filled = 0
            while now >= self.nextTickAt and filled < maxCatchup do
                self:_writeFrame(self.nextTickAt - self.startClock, snapshot)
                self.nextTickAt += self.tickInterval
                filled += 1
            end
            -- If still behind, we hit the catchup cap — this CREATES A GAP in
            -- the keyframe timeline. Log loudly so we can correlate with the
            -- gaps you see in Blender's dopesheet.
            if now >= self.nextTickAt then
                local lostSec = now + self.tickInterval - self.nextTickAt
                self.gapCount += 1
                self:_debugLog(fmt(
                    "*** GAP CREATED: catchup cap hit (filled %d frames), " ..
                    "jumping nextTickAt forward %.2fs (≈%d frames lost)",
                    filled, lostSec, math.floor(lostSec * CONFIG.TICK_RATE + 0.5)))
                self.nextTickAt = now + self.tickInterval
            end
        end

        if now - self.lastFlushAt >= CONFIG.FLUSH_INTERVAL then
            self:_flush()
            self:_flushDebug()
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
    self:_writeRigFile()

    local seconds = os.clock() - self.startClock
    self:_debugLog(fmt("STOP after %.2fs: ticks=%d stalls=%d gaps=%d",
        seconds, self.tickCount, self.stallCount, self.gapCount))
    for uid, entry in pairs(self.tracked) do
        self:_debugLog(fmt(
            "  uid=%d (%s) ticks=%d culled=%d parts=%d joints=%d",
            uid, entry.rig.name, entry.ticks, entry.culledTicks,
            #entry.rig.parts, #entry.rig.joints))
    end
    self:_flushDebug()

    local msg = fmt("Saved %d ticks (%.1fs) gaps=%d -> %s",
        self.tickCount, seconds, self.gapCount, self.filename)
    notify("ROCORDER", msg, 5)
    print("[ROCORDER]", msg)
end

function Recorder:Toggle()
    if self.active then self:Stop() else self:Start() end
end

function Recorder:IsRecording()
    return self.active
end

function Recorder:SetDebug(flag)
    CONFIG.DEBUG = flag and true or false
    print("[ROCORDER] DEBUG =", CONFIG.DEBUG)
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

notify("ROCORDER", fmt("v%s loaded. Press F8 to start/stop recording.",
    ROCORDER_VERSION), 4)
print(fmt("[ROCORDER] v%s ready. Hotkey: %s",
    ROCORDER_VERSION, CONFIG.HOTKEY.Name))
