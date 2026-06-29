--[[
  ROCORDER composite-write API probe   (standalone)            composite/1
  -------------------------------------------------------------------
  Locks the EXACT EditableImage WRITE API signatures the Stage 3 self-
  compositor needs, end-to-end, before committing the real implementation
  (which is untestable Lua here). It actually builds a composite and writes
  it to disk so we can confirm every call shape works on this build.

  Pipeline exercised (== the real compositor):
    1. create a blank EditableImage canvas (585x559)
    2. DrawRectangle a skin-tone fill over it
    3. CreateEditableImageAsync(shirt template) -> source image
    4. DrawImage the shirt onto the canvas (alpha-composited)
    5. ReadPixelsBuffer the canvas -> raw RGBA
    6. write ROCORDER/diag/composite_test.rgba (+ a log of what worked)

  Run in any game where your (or someone's) avatar wears a classic Shirt.
  Read workspace/ROCORDER/diag/composite_<unixtime>.txt — it reports the
  working call shape for each step (or the error), so the real compositor
  uses exactly what this build accepts.
]]

local PROBE_VERSION = "composite/1"
local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")
local writefile  = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder   = isfolder  or (syn and syn.isfolder)
local lp = Players.LocalPlayer

local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[COMPOSITE] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local function has(o,m) if not o then return false end local ok,v=pcall(function() return o[m] end) return ok and typeof(v)=="function" end
local function sp(i,p) local ok,v=pcall(function() return i[p] end) if ok then return v end return nil end

local W, H = 585, 559

----------------------------------------------------------------------
-- 1) create a blank EditableImage canvas. Try every known constructor.
section("1. create canvas")
local canvas, createForm
do
    -- a) AssetService:CreateEditableImage({ Size = Vector2 })  (modern)
    if not canvas and has(AssetService, "CreateEditableImage") then
        local ok, ei = pcall(function()
            return AssetService:CreateEditableImage({ Size = Vector2.new(W, H) })
        end)
        if ok and ei then canvas, createForm = ei, "AssetService:CreateEditableImage{Size}" end
        if not canvas then
            -- b) positional Vector2 arg
            local ok2, ei2 = pcall(function()
                return AssetService:CreateEditableImage(Vector2.new(W, H))
            end)
            if ok2 and ei2 then canvas, createForm = ei2, "AssetService:CreateEditableImage(Vector2)" end
        end
    end
    -- c) Instance.new("EditableImage") then set Size
    if not canvas then
        local ok, ei = pcall(function()
            local e = Instance.new("EditableImage")
            pcall(function() e.Size = Vector2.new(W, H) end)
            return e
        end)
        if ok and ei then canvas, createForm = ei, "Instance.new(EditableImage)+Size" end
    end
    if canvas then
        log("  OK via " .. createForm .. "  Size=" .. tostring(sp(canvas, "Size")))
    else
        log("  FAILED to create any EditableImage canvas — compositor not possible")
    end
end

----------------------------------------------------------------------
-- 2) DrawRectangle skin fill. Probe the argument shape.
section("2. DrawRectangle (skin fill)")
local drawRectForm
if canvas and has(canvas, "DrawRectangle") then
    local skin = Color3.fromRGB(234, 184, 146)
    -- a) (pos, size, color, transparency, combineType)
    local ct = (Enum and Enum.ImageCombineType) and Enum.ImageCombineType.Overwrite or nil
    local okA = pcall(function()
        canvas:DrawRectangle(Vector2.new(0,0), Vector2.new(W,H), skin, 0, ct)
    end)
    if okA then drawRectForm = "(pos,size,color,transparency,combineType)" end
    if not drawRectForm then
        local okB = pcall(function()
            canvas:DrawRectangle(Vector2.new(0,0), Vector2.new(W,H), skin, 0)
        end)
        if okB then drawRectForm = "(pos,size,color,transparency)" end
    end
    if not drawRectForm then
        local okC = pcall(function()
            canvas:DrawRectangle(Vector2.new(0,0), Vector2.new(W,H), skin)
        end)
        if okC then drawRectForm = "(pos,size,color)" end
    end
    log(drawRectForm and ("  OK shape: DrawRectangle" .. drawRectForm)
        or "  DrawRectangle FAILED on all probed shapes")
else
    log("  DrawRectangle not available — will WritePixelsBuffer a skin fill instead")
end

----------------------------------------------------------------------
-- find a shirt template to draw
local shirtRef
for _, plr in ipairs(Players:GetPlayers()) do
    local ch = plr.Character
    local sh = ch and ch:FindFirstChildOfClass("Shirt")
    local t = sh and sp(sh, "ShirtTemplate")
    if t and tostring(t) ~= "" then shirtRef = t; break end
end
log("  shirt template found: " .. tostring(shirtRef))

----------------------------------------------------------------------
-- 3) source image from the shirt template
section("3. source image (shirt)")
local shirtImg
if shirtRef then
    local content = shirtRef
    if (typeof(Content)=="table" or typeof(Content)=="userdata") and Content.fromUri then
        local okc, c = pcall(function() return Content.fromUri(shirtRef) end)
        if okc and c then content = c end
    end
    local ok, ei = pcall(function() return AssetService:CreateEditableImageAsync(content) end)
    if ok and ei then
        shirtImg = ei
        log("  OK shirt EditableImage Size=" .. tostring(sp(ei, "Size")))
    else
        log("  CreateEditableImageAsync(shirt) FAILED: " .. tostring(ei))
    end
else
    log("  no shirt to load (run where someone wears a classic Shirt)")
end

----------------------------------------------------------------------
-- 4) DrawImage shirt onto canvas. Probe the argument shape.
section("4. DrawImage (shirt over canvas)")
local drawImgForm
if canvas and shirtImg and has(canvas, "DrawImage") then
    local ct = (Enum and Enum.ImageCombineType) and Enum.ImageCombineType.AlphaBlend or nil
    -- a) (position, image, combineType)
    local okA = pcall(function()
        canvas:DrawImage(Vector2.new(0,0), shirtImg, ct)
    end)
    if okA then drawImgForm = "(position, image, combineType)" end
    if not drawImgForm then
        local okB = pcall(function()
            canvas:DrawImage(Vector2.new(0,0), shirtImg)
        end)
        if okB then drawImgForm = "(position, image)" end
    end
    log(drawImgForm and ("  OK shape: DrawImage" .. drawImgForm)
        or "  DrawImage FAILED on all probed shapes")
    -- also report whether DrawImageTransformed exists (needed for Stage 3b)
    log("  DrawImageTransformed present: " .. (has(canvas,"DrawImageTransformed") and "yes" or "no"))
else
    log("  skipped (no canvas/shirt/DrawImage)")
end

----------------------------------------------------------------------
-- 5) read the composited pixels back + 6) write to disk
section("5/6. ReadPixelsBuffer -> write composite_test.rgba")
local stamp = os.time()
local wrote = false
if canvas and has(canvas, "ReadPixelsBuffer") then
    local sz = sp(canvas, "Size") or Vector2.new(W, H)
    local ok, buf = pcall(function() return canvas:ReadPixelsBuffer(Vector2.new(0,0), sz) end)
    if ok and buf then
        local data
        pcall(function() data = buffer.tostring(buf) end)
        if data and writefile then
            local base = "ROCORDER/diag"
            pcall(function()
                if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
                if isfolder and not isfolder(base) then makefolder(base) end
            end)
            local body = fmt("ROCORDER-RGBA8\n%d\n%d\n", math.floor(sz.X), math.floor(sz.Y)) .. data
            wrote = pcall(writefile, base .. "/composite_test_" .. stamp .. ".rgba", body)
            log(fmt("  composite written: %s (%d bytes incl header) — open it / import to eyeball",
                tostring(wrote), #body))
        else
            log("  buffer.tostring or writefile unavailable")
        end
    else
        log("  ReadPixelsBuffer FAILED: " .. tostring(buf))
    end
else
    log("  ReadPixelsBuffer not available")
end

----------------------------------------------------------------------
section("SUMMARY (the real compositor will use these shapes)")
log("  canvas create : " .. tostring(createForm))
log("  DrawRectangle : " .. tostring(drawRectForm))
log("  DrawImage     : " .. tostring(drawImgForm))
log("  wrote .rgba   : " .. tostring(wrote))

if writefile then
    local base = "ROCORDER/diag"
    pcall(writefile, base .. "/composite_" .. stamp .. ".txt", table.concat(lines, "\n"))
    log("wrote ROCORDER/diag/composite_" .. stamp .. ".txt")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER composite", Text="DONE — see ROCORDER/diag/composite_*", Duration=8 })
end)
log("COMPOSITE PROBE DONE")
