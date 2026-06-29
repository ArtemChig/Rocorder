--[[
  ROCORDER render-readback feasibility spike   (standalone, read-only*)
  --------------------------------------------------------------------
  GO/NO-GO gate for the render-bake redesign. The whole "capture rendered
  pixels instead of re-deriving appearance" plan depends on ONE thing:
  can we get the pixels of a render back into a buffer we can write to disk?

  This probe does NOT bake anything yet. It only answers: which render-
  readback mechanism (if any) exists on this build/executor, and can we
  round-trip a render -> EditableImage -> RGBA bytes.

  *It briefly creates a ViewportFrame in your PlayerGui and (if you allow it)
  may invoke a screenshot capture, then cleans up. Nothing is recorded.

  RUN: paste into the executor while in any game. Read the console output
  and the report at workspace/ROCORDER/diag/render_<unixtime>.txt.

  Probes, in order of usefulness for baking:
    1. CaptureService / ScreenshotService : capture -> content id -> can we
       CreateEditableImageAsync it and read pixels?  (best: real readback)
    2. ViewportFrame                       : can we create one + render an
       avatar clone into it?  (needed as the thing we'd screenshot)
    3. Executor framebuffer globals        : any getscreenshot/captureframe?
    4. EditableImage write API surface     : DrawImage/WritePixels present?
       (decides the FALLBACK plan: record-time self-compositing.)
]]

local PROBE_VERSION = "render/1"
local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")

local writefile  = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder   = isfolder  or (syn and syn.isfolder)
local lp = Players.LocalPlayer

local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[RENDER] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local report = { version = PROBE_VERSION, placeId = game.PlaceId }

local function svc(name)
    local ok, s = pcall(function() return game:GetService(name) end)
    return ok and s or nil
end
local function has(obj, m)
    if not obj then return false end
    local ok, v = pcall(function() return obj[m] end)
    return ok and typeof(v) == "function"
end
local function gexists(name) return getfenv()[name] ~= nil end

-- read pixels from an EditableImage -> {w,h,variance}
local function readEI(ei)
    local sz = ei and ei.Size
    local w = sz and math.floor(sz.X) or 0
    local h = sz and math.floor(sz.Y) or 0
    if w <= 0 or h <= 0 then return { ok = false, err = "zero-size" } end
    if not has(ei, "ReadPixelsBuffer") then return { ok = true, w = w, h = h, variance = -1, note = "no ReadPixelsBuffer" } end
    local okR, buf = pcall(function() return ei:ReadPixelsBuffer(Vector2.new(0,0), sz) end)
    if not okR or not buf then return { ok = true, w = w, h = h, variance = -1, note = "read failed" } end
    local sr,sg,sb,n = 0,0,0,0; local samples = {}
    pcall(function()
        local total = w*h*4; local step = math.max(4, math.floor(total/(64*4))*4)
        for off = 0, total-4, step do
            local r,g,b = buffer.readu8(buf,off), buffer.readu8(buf,off+1), buffer.readu8(buf,off+2)
            samples[#samples+1] = {r,g,b}; sr=sr+r; sg=sg+g; sb=sb+b; n=n+1
        end
    end)
    local variance = -1
    if n > 0 then
        local mr,mg,mb = sr/n,sg/n,sb/n; local vs=0
        for _,p in ipairs(samples) do vs=vs+(p[1]-mr)^2+(p[2]-mg)^2+(p[3]-mb)^2 end
        variance = math.floor(vs/n)
    end
    return { ok = true, w = w, h = h, variance = variance }
end

----------------------------------------------------------------------
-- 4 first (cheap, no side effects): EditableImage WRITE surface.
-- This decides the realistic FALLBACK if render-readback is impossible:
-- record-time self-compositing (build per-part textures with DrawImage).
section("4. EditableImage write/compositing surface")
do
    local writeOK = false
    if has(AssetService, "CreateEditableImage") or has(AssetService, "CreateEditableImageAsync") then
        local ei
        pcall(function()
            if has(AssetService, "CreateEditableImage") then
                ei = AssetService:CreateEditableImage({ Size = Vector2.new(64,64) })
            end
        end)
        if not ei then pcall(function() ei = Instance.new("EditableImage") end) end
        if ei then
            log("  EditableImage instance: created")
            log("    DrawImage         : " .. (has(ei,"DrawImage") and "yes" or "no"))
            log("    DrawImageTransformed: " .. (has(ei,"DrawImageTransformed") and "yes" or "no"))
            log("    DrawRectangle     : " .. (has(ei,"DrawRectangle") and "yes" or "no"))
            log("    WritePixelsBuffer : " .. (has(ei,"WritePixelsBuffer") and "yes" or "no"))
            log("    WritePixels       : " .. (has(ei,"WritePixels") and "yes" or "no"))
            writeOK = has(ei,"WritePixelsBuffer") or has(ei,"DrawImage") or has(ei,"WritePixels")
            pcall(function() ei:Destroy() end)
        else
            log("  could not instantiate a blank EditableImage")
        end
    else
        log("  no CreateEditableImage API")
    end
    report.editableWrite = writeOK
    log("  => self-compositing fallback viable: " .. tostring(writeOK))
end

----------------------------------------------------------------------
-- 1. Screenshot / capture services
section("1. CaptureService / ScreenshotService")
do
    local cs = svc("CaptureService") or svc("ScreenshotService")
    if not cs then
        log("  no CaptureService/ScreenshotService on this build")
        report.captureService = false
    else
        log("  service: " .. cs.ClassName)
        for _, m in ipairs({"CaptureScreenshot","Capture","PromptSaveScreenshotToCameraRoll",
                            "SaveScreenshotCapture","CaptureRegion"}) do
            log("    " .. m .. " : " .. (has(cs, m) and "yes" or "no"))
        end
        report.captureService = true
        -- Try CaptureScreenshot -> content id -> EditableImage round-trip.
        if has(cs, "CaptureScreenshot") then
            local capId
            local done = false
            pcall(function()
                cs:CaptureScreenshot(function(contentId)
                    capId = contentId; done = true
                end)
            end)
            local waited = 0
            while not done and waited < 3 do task.wait(0.1); waited = waited + 0.1 end
            log("  CaptureScreenshot -> " .. tostring(capId) .. (done and "" or "  (TIMED OUT)"))
            if capId then
                local content = capId
                if (typeof(Content)=="table" or typeof(Content)=="userdata") and Content.fromUri then
                    pcall(function() content = Content.fromUri(capId) end)
                end
                local ok, ei = pcall(function() return AssetService:CreateEditableImageAsync(content) end)
                if ok and ei then
                    local r = readEI(ei)
                    log("  *** screenshot -> EditableImage ROUND-TRIP OK: " .. HttpService:JSONEncode(r))
                    report.screenshotRoundTrip = r
                else
                    log("  screenshot id could NOT be opened as EditableImage: " .. tostring(ei))
                    report.screenshotRoundTrip = { ok = false, err = tostring(ei) }
                end
            end
        end
    end
end

----------------------------------------------------------------------
-- 2. ViewportFrame: can we make one and render an avatar clone into it?
section("2. ViewportFrame render isolation")
do
    local okVF, vf = pcall(function() return Instance.new("ViewportFrame") end)
    if not okVF or not vf then
        log("  ViewportFrame: cannot instantiate")
        report.viewportFrame = false
    else
        log("  ViewportFrame: created")
        log("    has WorldModel support: testing…")
        local pg = lp and lp:FindFirstChildOfClass("PlayerGui")
        if pg then
            local sg = Instance.new("ScreenGui"); sg.Name = "_ROCORDER_RenderProbe"; sg.Parent = pg
            vf.Size = UDim2.fromScale(1,1); vf.Parent = sg
            local cam = Instance.new("Camera"); cam.Parent = vf; vf.CurrentCamera = cam
            -- try to clone the local avatar into the viewport
            local cloned = false
            if lp and lp.Character then
                local okC, clone = pcall(function() return lp.Character:Clone() end)
                if okC and clone then
                    local wm = Instance.new("WorldModel"); wm.Parent = vf
                    clone.Parent = wm
                    cloned = true
                    log("    avatar clone parented into ViewportFrame+WorldModel: OK")
                end
            end
            report.viewportFrame = { created = true, clonedAvatar = cloned }
            task.wait(0.2)
            sg:Destroy()
        else
            log("    no PlayerGui to host the ViewportFrame")
            report.viewportFrame = { created = true, clonedAvatar = false, note = "no PlayerGui" }
        end
    end
end

----------------------------------------------------------------------
-- 3. Executor framebuffer / screenshot globals
section("3. Executor framebuffer globals")
do
    local found = {}
    for _, name in ipairs({"getscreenshot","captureframe","getframebuffer",
                            "screenshot","take_screenshot","getrenderframe"}) do
        if gexists(name) then found[#found+1] = name end
    end
    if #found > 0 then log("  found: " .. table.concat(found, ", "))
    else log("  none of the common executor screenshot globals exist") end
    report.executorGlobals = found
end

----------------------------------------------------------------------
section("VERDICT (read me)")
log("  render-readback viable if EITHER:")
log("   - section 1 shows 'ROUND-TRIP OK' (screenshot -> EditableImage), or")
log("   - section 3 lists a framebuffer global.")
log("  If neither, photograph-style bake is NOT possible on this build, and")
log("  the realistic core redesign is record-time self-compositing")
log("  (needs section 4 = true).")

----------------------------------------------------------------------
section("WRITE")
local stamp = os.time(); local base = "ROCORDER/diag"
if writefile then
    pcall(function()
        if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
        if isfolder and not isfolder(base) then makefolder(base) end
    end)
    local txt = base .. "/render_" .. stamp .. ".txt"
    local okT = pcall(writefile, txt, table.concat(lines, "\n"))
    pcall(function() writefile(base .. "/render_" .. stamp .. ".json", HttpService:JSONEncode(report)) end)
    log("wrote " .. txt .. " (" .. (okT and "ok" or "FAIL") .. ")")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER render probe", Text="DONE — see ROCORDER/diag/render_*.txt", Duration=8 })
end)
log("RENDER PROBE DONE")
