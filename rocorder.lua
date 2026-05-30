--!nocheck
-- ROCORDER — lightweight replay recorder for Xeno (and other executors with file IO).
-- Captures position + YXZ rotation of every player's HumanoidRootPart at a fixed tick rate.
-- Toggle with F8, or call _G.ROCORDER:Start() / _G.ROCORDER:Stop() / _G.ROCORDER:Toggle().

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
    FLUSH_INTERVAL  = 0.5,       -- seconds between disk flushes (smaller = less stall per flush)
    MAX_CATCHUP_SEC = 5.0,       -- cap on how much missed time we'll backfill after a stall
    MAX_DISTANCE    = 0,         -- studs from workspace.CurrentCamera.CFrame.Position.
                                 -- Players whose HumanoidRootPart is beyond this radius
                                 -- are skipped entirely for the tick. 0 = unlimited.
    PRECISION       = 2,         -- decimal places for floats
    HOTKEY          = Enum.KeyCode.F8,
    FOLDER          = "ROCORDER",
    INCLUDE_LOCAL   = true,      -- record the local player too
}

----------------------------------------------------------------
-- Executor IO sanity check
----------------------------------------------------------------
local writefile   = writefile   or (syn and syn.writefile)
local appendfile  = appendfile
local isfolder    = isfolder
local makefolder  = makefolder

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
local function notify(title, text, duration)
    pcall(function()
        StarterGui:SetCore("SendNotification", {
            Title    = title,
            Text     = text,
            Duration = duration or 3,
        })
    end)
end

local fmt = string.format
local PREC_FMT = "%." .. CONFIG.PRECISION .. "f"

local function f(n)
    return fmt(PREC_FMT, n)
end

local function safeRoot(player)
    local char = player.Character
    if not char then return nil end
    return char:FindFirstChild("HumanoidRootPart")
end

local function sanitizeName(s)
    return (s:gsub("[;:=,]", "_"))
end

-- Snapshot the rig structure for a single player: parts (dimensions, shape,
-- color, mesh refs) and Motor6D joints. Used by the importer to build a
-- properly-proportioned model instead of placeholder spheres.
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

    for _, child in ipairs(char:GetChildren()) do
        if child:IsA("BasePart") then
            local cf = child.CFrame
            local info = {
                name         = sanitizeName(child.Name),
                className    = child.ClassName,
                size         = { child.Size.X, child.Size.Y, child.Size.Z },
                color        = { child.Color.R, child.Color.G, child.Color.B },
                transparency = child.Transparency,
                -- world CFrame at record start: 12 floats from CFrame:GetComponents()
                -- (x, y, z, R00, R01, R02, R10, R11, R12, R20, R21, R22)
                restCFrame   = { cf:GetComponents() },
            }
            if child:IsA("Part") then
                info.shape = child.Shape.Name
            elseif child:IsA("MeshPart") then
                info.shape = "MeshPart"
                -- pcall every asset-id read; some MeshPart subclasses can throw
                -- on access in security contexts where these properties are filtered.
                local ok, meshId = pcall(function() return child.MeshId end)
                if ok and meshId and meshId ~= "" then info.meshId = meshId end
                local ok2, texId = pcall(function() return child.TextureID end)
                if ok2 and texId and texId ~= "" then info.textureId = texId end
            else
                info.shape = "Block"
            end
            table.insert(rig.parts, info)
        end
    end

    for _, desc in ipairs(char:GetDescendants()) do
        if desc:IsA("Motor6D") then
            local p0, p1 = desc.Part0, desc.Part1
            if p0 and p1 then
                -- world position of the joint at record start (snapshot pose)
                local pivot = (p0.CFrame * desc.C0).Position
                -- C0/C1 are STRUCTURAL: offsets in Part0/Part1's local frames,
                -- unchanged by animation. The importer walks these to build
                -- the canonical T-pose, independent of whatever the character
                -- happened to be doing when recording started.
                local c0Comp = { desc.C0:GetComponents() }
                local c1Comp = { desc.C1:GetComponents() }
                table.insert(rig.joints, {
                    name  = desc.Name,
                    part0 = sanitizeName(p0.Name),
                    part1 = sanitizeName(p1.Name),
                    pivot = { pivot.X, pivot.Y, pivot.Z },
                    c0    = c0Comp,
                    c1    = c1Comp,
                })
            end
        end
    end

    return rig
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
        startClock    = 0,
        nextTickAt    = 0,
        lastFlushAt   = 0,
        tickInterval  = 1 / CONFIG.TICK_RATE,
        buffer        = {},     -- pending lines
        tickCount     = 0,
        connHeartbeat = nil,
    }, Recorder)
end

function Recorder:_writeHeader()
    local localPlayer = Players.LocalPlayer
    local roster = {}
    for _, p in ipairs(Players:GetPlayers()) do
        if CONFIG.INCLUDE_LOCAL or p ~= localPlayer then
            roster[#roster + 1] = {
                userId      = p.UserId,
                name        = p.Name,
                displayName = p.DisplayName,
            }
        end
    end

    local header = {
        format      = "ROCORDER/2",
        placeId     = game.PlaceId,
        jobId       = game.JobId,
        startedAt   = os.time(),
        tickRate    = CONFIG.TICK_RATE,
        precision   = CONFIG.PRECISION,
        captureMode = "bones",
        rigFile     = self.rigFilename,
        -- per-frame line format:
        --   t=<sec>;<userId>:<boneName>=<x>,<y>,<z>,<rx>,<ry>,<rz>;<userId>:<boneName>=...
        -- rx/ry/rz are YXZ Euler in radians
        columns     = { "userId", "bone", "x", "y", "z", "rx", "ry", "rz" },
        roster      = roster,
    }

    writefile(CONFIG.FOLDER .. "/" .. self.filename,
        HttpService:JSONEncode(header) .. "\n")
end

function Recorder:_writeRigFile()
    local rigData = {
        format     = "ROCORDER-RIG/1",
        recFile    = self.filename,
        capturedAt = os.time(),
        players    = {},
    }
    local captured = 0
    for _, p in ipairs(Players:GetPlayers()) do
        if CONFIG.INCLUDE_LOCAL or p ~= Players.LocalPlayer then
            local ok, rig = pcall(captureRig, p)
            if ok and rig then
                rigData.players[tostring(p.UserId)] = rig
                captured += 1
            elseif not ok then
                warn("[ROCORDER] captureRig failed for", p.Name, "->", rig)
            end
        end
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
        -- null out the rigFilename so the .rec header doesn't promise a file
        -- that isn't there and the importer doesn't go hunting for it.
        self.rigFilename = nil
    end
end

-- Build the per-player part entries for the current moment, *without* a
-- timestamp prefix. Returns an array of "uid:bone=x,y,z,rx,ry,rz" strings.
-- Decoupling the snapshot from the timestamp lets us reuse one snapshot
-- across multiple missed sample slots after a stall.
function Recorder:_takeSnapshot()
    local entries = {}

    local cameraPos
    local cam = workspace.CurrentCamera
    if cam then cameraPos = cam.CFrame.Position end
    local maxDistSq = CONFIG.MAX_DISTANCE > 0 and (CONFIG.MAX_DISTANCE * CONFIG.MAX_DISTANCE) or nil

    for _, p in ipairs(Players:GetPlayers()) do
        if CONFIG.INCLUDE_LOCAL or p ~= Players.LocalPlayer then
            local playerEntries  -- nil = emit nothing for this player this tick
            local char = p.Character

            if char then
                local inRange = true
                if maxDistSq and cameraPos then
                    local root = char:FindFirstChild("HumanoidRootPart")
                    if root then
                        local d = root.Position - cameraPos
                        if (d.X * d.X + d.Y * d.Y + d.Z * d.Z) > maxDistSq then
                            inRange = false
                        end
                    end
                end

                if inRange then
                    playerEntries = {}
                    for _, child in ipairs(char:GetChildren()) do
                        if child:IsA("BasePart") then
                            local boneName = sanitizeName(child.Name)
                            local cf = child.CFrame
                            local px, py, pz = cf.X, cf.Y, cf.Z
                            local rx, ry, rz = cf:ToEulerAnglesYXZ()
                            playerEntries[#playerEntries + 1] = fmt(
                                "%d:%s=%s,%s,%s,%s,%s,%s",
                                p.UserId, boneName,
                                f(px), f(py), f(pz), f(rx), f(ry), f(rz)
                            )
                        end
                    end
                    if #playerEntries > 0 then
                        -- cache for gap-holding when char briefly becomes nil (respawn etc.)
                        self.lastPoseByPlayer[p.UserId] = playerEntries
                    end
                end
                -- out-of-range players intentionally emit nothing — that's the
                -- whole point of distance filtering. If you want them held at
                -- last-seen pose instead, fall through to the last-pose branch.
            else
                -- Character missing (respawning, briefly streamed out): hold the
                -- last successful pose so Blender keyframes stay dense and we
                -- don't see "gap" stretches that line up with respawns.
                playerEntries = self.lastPoseByPlayer[p.UserId]
            end

            if playerEntries then
                for i = 1, #playerEntries do
                    entries[#entries + 1] = playerEntries[i]
                end
            end
        end
    end
    return entries
end

function Recorder:_writeFrame(t, snapshot)
    local n = #snapshot
    local line = table.create and table.create(n + 1) or {}
    line[1] = "t=" .. f(t)
    for i = 1, n do
        line[i + 1] = snapshot[i]
    end
    self.buffer[#self.buffer + 1] = table.concat(line, ";")
    self.tickCount += 1
end

function Recorder:_flush()
    if #self.buffer == 0 then return end
    local chunk = table.concat(self.buffer, "\n") .. "\n"
    table.clear(self.buffer)
    -- appendfile is safer than read/write/rewrite for ongoing recordings
    appendfile(CONFIG.FOLDER .. "/" .. self.filename, chunk)
end

function Recorder:Start()
    if self.active then return end

    self.filename    = fmt("replay_%d_%d.rec", game.PlaceId, os.time())
    self.rigFilename = (self.filename:gsub("%.rec$", ".rig.json"))
    self.startClock  = os.clock()
    self.nextTickAt  = self.startClock
    self.lastFlushAt = self.startClock
    self.tickCount   = 0
    self.lastPoseByPlayer = {}  -- uid -> { entry strings } for gap-holding
    table.clear(self.buffer)

    self:_writeRigFile()
    self:_writeHeader()
    self.active = true

    self.connHeartbeat = RunService.Heartbeat:Connect(function()
        if not self.active then return end
        local now = os.clock()

        if now >= self.nextTickAt then
            -- one snapshot per Heartbeat regardless of how many slots we owe.
            -- If we stalled (game lag, file IO, respawn), we backfill every
            -- missed slot with the same snapshot so the Blender timeline stays
            -- dense and Blender doesn't have to interpolate across a gap.
            local snapshot = self:_takeSnapshot()
            local maxCatchup = math.ceil(CONFIG.MAX_CATCHUP_SEC / self.tickInterval)
            local filled = 0
            while now >= self.nextTickAt and filled < maxCatchup do
                self:_writeFrame(self.nextTickAt - self.startClock, snapshot)
                self.nextTickAt += self.tickInterval
                filled += 1
            end
            -- if a really big stall left us still behind, jump nextTickAt
            -- forward so we don't get stuck in catch-up loops on every Heartbeat.
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

-- Auto-flush on teleport/leave so we don't lose the tail of the recording
game:BindToClose(function()
    if rec.active then rec:Stop() end
end)

notify("ROCORDER", "Loaded. Press F8 to start/stop recording.", 4)
print("[ROCORDER] Ready. Hotkey:", CONFIG.HOTKEY.Name)
