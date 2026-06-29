--[[
  ROCORDER case-5 probe   (standalone, read-only)              case5/1
  ----------------------------------------------------------------
  ONE question: on an R15 avatar wearing classic Shirt/Pants, can we read the
  engine's COMPOSITED body texture (skin + clothing baked together) straight
  off a body MeshPart? If yes, R15 clothing is solved with the mesh's own UVs
  and NO projection.

  For each body MeshPart (UpperTorso, LowerTorso, arms, legs, hands) on a
  clothed R15 avatar, it reads every texture source the part exposes, turns
  each into an EditableImage, scores it, and WRITES it to disk so we can
  literally look at it:
      <executor>/workspace/ROCORDER/diag/case5_<uid>_<part>_<source>.rgba

  VERDICT it prints per source:
    - HIGH variance + not skin-colored  => that's the COMPOSITE (clothing in
      it) => case 5 SOLVED, use this texture + the mesh's authored UVs.
    - LOW variance + skin-toned         => just the base skin, composite not
      exposed here.

  RUN IN AN R15 GAME where you (or someone) wears a classic Shirt and/or Pants
  on a modern (MeshPart) body. Tell me the game; I'll read the .rgba files +
  report from workspace/ROCORDER/diag/.
]]

local PROBE_VERSION = "case5/1"
local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")
local writefile  = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder   = isfolder  or (syn and syn.isfolder)
local lp = Players.LocalPlayer

local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[CASE5] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local function has(o,m) if not o then return false end local ok,v=pcall(function() return o[m] end) return ok and typeof(v)=="function" end
local function sp(i,p) local ok,v=pcall(function() return i[p] end) if ok then return v end return nil end
local function ne(s) return s~=nil and tostring(s)~="" and tostring(s)~="rbxassetid://0" and tostring(s)~="0" end

local R15_BODY = {
    uppertorso=true, lowertorso=true,
    leftupperarm=true, leftlowerarm=true, lefthand=true,
    rightupperarm=true, rightlowerarm=true, righthand=true,
    leftupperleg=true, leftlowerleg=true, leftfoot=true,
    rightupperleg=true, rightlowerleg=true, rightfoot=true,
}

local base = "ROCORDER/diag"
pcall(function()
    if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
    if isfolder and not isfolder(base) then makefolder(base) end
end)

-- EditableImage from a Content/uri/string. Returns ei or nil,err.
local function makeEI(src)
    if not has(AssetService, "CreateEditableImageAsync") then return nil, "no API" end
    local content = src
    if type(src) == "string" then
        if (typeof(Content)=="table" or typeof(Content)=="userdata") and Content.fromUri then
            local ok,c = pcall(function() return Content.fromUri(src) end)
            if ok and c then content = c end
        end
    end
    local ok, ei = pcall(function() return AssetService:CreateEditableImageAsync(content) end)
    if not ok or not ei then return nil, tostring(ei) end
    return ei
end

-- score + write an EditableImage. Returns a small result table.
local function scoreAndWrite(ei, outName)
    local sz = sp(ei, "Size")
    local w = sz and math.floor(sz.X) or 0
    local h = sz and math.floor(sz.Y) or 0
    if w <= 0 or h <= 0 then return { ok=false, err="zero-size" } end
    local res = { ok=true, w=w, h=h, variance=-1 }
    if has(ei, "ReadPixelsBuffer") then
        local okR, buf = pcall(function() return ei:ReadPixelsBuffer(Vector2.new(0,0), sz) end)
        if okR and buf then
            -- variance over samples
            local sr,sg,sb,n = 0,0,0,0; local samples = {}
            pcall(function()
                local total = w*h*4; local step = math.max(4, math.floor(total/(80*4))*4)
                for off=0,total-4,step do
                    local r,g,b = buffer.readu8(buf,off),buffer.readu8(buf,off+1),buffer.readu8(buf,off+2)
                    samples[#samples+1]={r,g,b}; sr=sr+r; sg=sg+g; sb=sb+b; n=n+1
                end
            end)
            if n > 0 then
                local mr,mg,mb = sr/n,sg/n,sb/n; local vs=0
                for _,p in ipairs(samples) do vs=vs+(p[1]-mr)^2+(p[2]-mg)^2+(p[3]-mb)^2 end
                res.variance = math.floor(vs/n)
                res.mean = { math.floor(mr),math.floor(mg),math.floor(mb) }
            end
            -- write to disk for eyeballing
            if writefile then
                local data
                if pcall(function() data = buffer.tostring(buf) end) and data then
                    local body = fmt("ROCORDER-RGBA8\n%d\n%d\n", w, h) .. data
                    res.wrote = pcall(writefile, base .. "/" .. outName .. ".rgba", body)
                end
            end
        end
    end
    return res
end

----------------------------------------------------------------------
section("scan for clothed R15 avatars")
log("  CreateEditableImageAsync: " .. (has(AssetService,"CreateEditableImageAsync") and "yes" or "NO"))

local function probePlayer(plr)
    local char = plr.Character
    if not char then return false end
    local hum = char:FindFirstChildOfClass("Humanoid")
    local rt = hum and sp(hum, "RigType")
    local isR15 = rt and rt.Name == "R15"
    local shirt = char:FindFirstChildOfClass("Shirt")
    local pants = char:FindFirstChildOfClass("Pants")
    local shirtT = shirt and sp(shirt, "ShirtTemplate")
    local pantsT = pants and sp(pants, "PantsTemplate")
    -- need a MeshPart body part
    local bodyMP
    for _, d in ipairs(char:GetDescendants()) do
        if d:IsA("MeshPart") and R15_BODY[d.Name:lower()] then bodyMP = d; break end
    end
    if not bodyMP then return false end

    section(fmt("CLOTHED R15? %s  rig=%s  shirt=%s pants=%s",
        plr.Name, rt and rt.Name or "?", tostring(shirtT), tostring(pantsT)))
    if not isR15 then log("  (not R15 — skipping deep probe)"); return false end
    if not (ne(shirtT) or ne(pantsT)) then
        log("  (no classic Shirt/Pants on this avatar — composite N/A, body uses its own texture)")
    end

    -- write the raw shirt for comparison
    if ne(shirtT) then
        local ei = makeEI(shirtT)
        if ei then
            local r = scoreAndWrite(ei, "case5_" .. plr.UserId .. "_SHIRT_template")
            log("  [shirt template] " .. HttpService:JSONEncode(r))
        end
    end

    -- for up to 3 body MeshParts, read every texture source
    local count = 0
    for _, d in ipairs(char:GetDescendants()) do
        if d:IsA("MeshPart") and R15_BODY[d.Name:lower()] and count < 3 then
            count = count + 1
            log("  -- " .. d.Name .. " --")
            local texId   = sp(d, "TextureID")
            local texCont = sp(d, "TextureContent")
            log("    TextureID="..tostring(texId).."  TextureContent="..tostring(texCont).." ("..typeof(texCont)..")")

            -- source A: TextureContent (most likely to be the composite)
            if texCont and tostring(texCont) ~= "" then
                local ei, err = makeEI(texCont)
                if ei then
                    local r = scoreAndWrite(ei, "case5_"..plr.UserId.."_"..d.Name.."_TextureContent")
                    log("    [TextureContent->EI] " .. HttpService:JSONEncode(r))
                else
                    log("    [TextureContent->EI] FAILED: " .. tostring(err))
                end
            end
            -- source B: TextureID
            if ne(texId) then
                local ei, err = makeEI(texId)
                if ei then
                    local r = scoreAndWrite(ei, "case5_"..plr.UserId.."_"..d.Name.."_TextureID")
                    log("    [TextureID->EI] " .. HttpService:JSONEncode(r))
                else
                    log("    [TextureID->EI] FAILED: " .. tostring(err))
                end
            end
        end
    end
    log("  INTERPRETATION: a source with HIGH variance whose mean isn't skin-")
    log("  toned contains the CLOTHING (= the composite) -> case 5 SOLVED.")
    log("  If every source is low-variance / skin-toned, the composite isn't")
    log("  exposed and R15 classic clothing still needs reprojection.")
    return true
end

local any = false
if lp and probePlayer(lp) then any = true end
for _, p in ipairs(Players:GetPlayers()) do
    if p ~= lp then if probePlayer(p) then any = true end end
end
if not any then
    log("")
    log("NO clothed R15 MeshPart avatar found. Run this in an R15 game where")
    log("you (or someone) wears a classic Shirt/Pants on a modern body.")
end

----------------------------------------------------------------------
section("WRITE")
local stamp = os.time()
if writefile then
    pcall(writefile, base .. "/case5_" .. stamp .. ".txt", table.concat(lines, "\n"))
    log("wrote ROCORDER/diag/case5_" .. stamp .. ".txt  (+ any case5_*.rgba)")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER case5", Text="DONE — see ROCORDER/diag/case5_*", Duration=8 })
end)
log("CASE5 PROBE DONE")
