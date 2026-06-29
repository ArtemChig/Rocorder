--[[
  ROCORDER composite/texture probe  (standalone, read-only)   probe/2
  ------------------------------------------------------------------
  ONE job: find out, for BODY parts, whether the recorder can obtain a
  single texture that — applied with the part's own authored UVs —
  reproduces what the game shows. That single answer decides how big the
  clothing/UV redesign is.

  It does NOT record or modify anything. It inspects the avatars currently
  in the scene and writes a report to:
      <executor>/workspace/ROCORDER/diag/probe_<unixtime>.txt   (+ .json)

  RUN IT IN AS MANY AVATAR TYPES AS YOU CAN (run once per game, send each
  report): an R6 game, an R15 game, someone in a classic Shirt/Pants, a
  modern dynamic-head avatar, layered/3D clothing. It auto-samples your
  own avatar + up to 5 others.

  HOW TO RUN
    1. Join a game with avatars loaded.
    2. Paste this whole file into the executor (Potassium / Xeno / etc).
    3. Wait for the "PROBE DONE" notification.
    4. Send me workspace/ROCORDER/diag/probe_<time>.txt  (+ .json).

  For each BODY part it resolves, per avatar:
    - what CLASS it is (Block / Part+CharacterMesh / MeshPart)
    - the mesh's authored UV range  (does it follow the R6 template?)
    - every texture it could show (TextureID, CharacterMesh Base/Overlay,
      SurfaceAppearance ColorMap, classic Shirt/Pants template), each fed
      to CreateEditableImageAsync, with image size + a "clothing-ness"
      variance score (high = real clothing pixels, low = flat skin)
    - Content.fromObject(part) -> EditableImage  (does the engine hand back
      a COMPOSITED texture this way?)
  Plus a head probe and an API capability matrix. Everything pcall-guarded.
]]

local PROBE_VERSION = "probe/2"

local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")

local writefile = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder  = isfolder  or (syn and syn.isfolder)

local lp = Players.LocalPlayer

----------------------------------------------------------------------
local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[PROBE] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local report = { version = PROBE_VERSION, placeId = game.PlaceId, characters = {} }

local function has(obj, method)
    local ok, v = pcall(function() return obj[method] end)
    return ok and (typeof(v) == "function")
end
local function safeProp(inst, prop)
    local ok, v = pcall(function() return inst[prop] end)
    if ok then return v end
    return nil
end
local function nonEmpty(s) return s ~= nil and tostring(s) ~= "" and tostring(s) ~= "rbxassetid://0" end

-- Content from a uri (rbxassetid://… or http…/asset/?id=…), build-agnostic.
local function contentFromUri(uri)
    if (typeof(Content) == "table" or typeof(Content) == "userdata") and Content.fromUri then
        local ok, c = pcall(function() return Content.fromUri(uri) end)
        if ok and c then return c end
    end
    return uri   -- some builds accept the raw string
end
local function contentFromObject(inst)
    if not ((typeof(Content) == "table" or typeof(Content) == "userdata") and Content.fromObject) then
        return nil, "Content.fromObject n/a on build"
    end
    local ok, c = pcall(function() return Content.fromObject(inst) end)
    if ok and c then return c end
    return nil, "fromObject(" .. inst.ClassName .. ") -> " .. tostring(c)
end

-- EditableImage probe: size + "clothing-ness" variance over sampled pixels.
local function probeImage(content, tag)
    if not content then return { tag = tag, ok = false, err = "no content" } end
    if not has(AssetService, "CreateEditableImageAsync") then
        return { tag = tag, ok = false, err = "CreateEditableImageAsync n/a" }
    end
    local ok, ei = pcall(function() return AssetService:CreateEditableImageAsync(content) end)
    if not ok or not ei then return { tag = tag, ok = false, err = "create: " .. tostring(ei) } end
    local sz = safeProp(ei, "Size")
    local w = sz and math.floor(sz.X) or 0
    local h = sz and math.floor(sz.Y) or 0
    if w <= 0 or h <= 0 then return { tag = tag, ok = false, err = "zero-size" } end
    local variance, mean = -1, nil
    if has(ei, "ReadPixelsBuffer") then
        local okR, buf = pcall(function() return ei:ReadPixelsBuffer(Vector2.new(0,0), sz) end)
        if okR and buf then
            local sr,sg,sb,n = 0,0,0,0
            local samples = {}
            pcall(function()
                local total = w*h*4
                local step = math.max(4, math.floor(total/(80*4))*4)
                for off = 0, total-4, step do
                    local r,g,b = buffer.readu8(buf,off), buffer.readu8(buf,off+1), buffer.readu8(buf,off+2)
                    samples[#samples+1] = {r,g,b}; sr=sr+r; sg=sg+g; sb=sb+b; n=n+1
                end
            end)
            if n > 0 then
                local mr,mg,mb = sr/n, sg/n, sb/n
                local vs = 0
                for _,p in ipairs(samples) do vs = vs+(p[1]-mr)^2+(p[2]-mg)^2+(p[3]-mb)^2 end
                variance = math.floor(vs/n)
                mean = { math.floor(mr), math.floor(mg), math.floor(mb) }
            end
        end
    end
    return { tag = tag, ok = true, w = w, h = h, variance = variance, mean = mean }
end

-- EditableMesh probe: vert count + authored UV range (template check) + bbox.
local function probeMesh(content)
    if not content then return { ok = false, err = "no content" } end
    if not has(AssetService, "CreateEditableMeshAsync") then
        return { ok = false, err = "CreateEditableMeshAsync n/a" }
    end
    local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(content) end)
    if not ok or not em then return { ok = false, err = "create: " .. tostring(em) } end
    local vids
    if not pcall(function() vids = em:GetVertices() end) or not vids then
        return { ok = false, err = "GetVertices failed" }
    end
    local nv = #vids
    local mnp = Vector3.new(math.huge,math.huge,math.huge)
    local mxp = Vector3.new(-math.huge,-math.huge,-math.huge)
    for i, vid in ipairs(vids) do
        local p = em:GetPosition(vid)
        mnp = Vector3.new(math.min(mnp.X,p.X),math.min(mnp.Y,p.Y),math.min(mnp.Z,p.Z))
        mxp = Vector3.new(math.max(mxp.X,p.X),math.max(mxp.Y,p.Y),math.max(mxp.Z,p.Z))
        if i > 6000 then break end
    end
    -- authored UV range via per-face UVs (the layout the texture must match)
    local umn,umx,vmn,vmx = math.huge,-math.huge,math.huge,-math.huge
    local sampled = false
    local fids
    pcall(function() fids = em:GetFaces() end)
    if not fids then pcall(function() fids = em:GetTriangles() end) end
    if fids then
        for i, fid in ipairs(fids) do
            local fuv
            pcall(function() fuv = em:GetFaceUVs(fid) end)
            if fuv then
                for _, uvid in ipairs(fuv) do
                    local uv
                    if pcall(function() uv = em:GetUV(uvid) end) and uv then
                        sampled = true
                        umn=math.min(umn,uv.X); umx=math.max(umx,uv.X)
                        vmn=math.min(vmn,uv.Y); vmx=math.max(vmx,uv.Y)
                    end
                end
            end
            if i > 2000 then break end
        end
    end
    return {
        ok = true, verts = nv,
        bbox = fmt("(%.2f,%.2f,%.2f)-(%.2f,%.2f,%.2f)", mnp.X,mnp.Y,mnp.Z, mxp.X,mxp.Y,mxp.Z),
        uvRange = sampled and fmt("U[%.3f..%.3f] V[%.3f..%.3f]", umn,umx,vmn,vmx) or "no-uvs",
    }
end

----------------------------------------------------------------------
section("E. API capability matrix")
log("  CreateEditableImageAsync : " .. (has(AssetService,"CreateEditableImageAsync") and "yes" or "NO"))
log("  CreateEditableMeshAsync  : " .. (has(AssetService,"CreateEditableMeshAsync")  and "yes" or "NO"))
log("  Content                  : type=" .. tostring(typeof(Content))
    .. " fromUri=" .. ((typeof(Content)~="nil" and Content and Content.fromUri) and "yes" or "no")
    .. " fromObject=" .. ((typeof(Content)~="nil" and Content and Content.fromObject) and "yes" or "no"))
report.capabilities = {
    editableImage = has(AssetService,"CreateEditableImageAsync"),
    editableMesh  = has(AssetService,"CreateEditableMeshAsync"),
    contentType   = typeof(Content),
    fromObject    = (typeof(Content)~="nil" and Content and Content.fromObject) and true or false,
}

----------------------------------------------------------------------
-- canonical body-part name set (R6 + R15), lowercased
local BODY_NAMES = {
    ["head"]=true,
    ["torso"]=true, ["uppertorso"]=true, ["lowertorso"]=true,
    ["left arm"]=true, ["right arm"]=true, ["left leg"]=true, ["right leg"]=true,
    ["leftupperarm"]=true,["leftlowerarm"]=true,["lefthand"]=true,
    ["rightupperarm"]=true,["rightlowerarm"]=true,["righthand"]=true,
    ["leftupperleg"]=true,["leftlowerleg"]=true,["leftfoot"]=true,
    ["rightupperleg"]=true,["rightlowerleg"]=true,["rightfoot"]=true,
}
local CMESH_MAP = { Head="Head", Torso="Torso", ["Left Arm"]="LeftArm",
    ["Right Arm"]="RightArm", ["Left Leg"]="LeftLeg", ["Right Leg"]="RightLeg" }

local function collectCMesh(char)
    local out = {}
    for _, c in ipairs(char:GetChildren()) do
        if c:IsA("CharacterMesh") then
            local bp = safeProp(c, "BodyPart")
            if bp then
                out[bp.Name] = {
                    meshId = nonEmpty(safeProp(c,"MeshId")) and ("rbxassetid://"..safeProp(c,"MeshId")) or nil,
                    baseTex = nonEmpty(safeProp(c,"BaseTextureId")) and ("rbxassetid://"..safeProp(c,"BaseTextureId")) or nil,
                    overlay = nonEmpty(safeProp(c,"OverlayTextureId")) and ("rbxassetid://"..safeProp(c,"OverlayTextureId")) or nil,
                }
            end
        end
    end
    return out
end

local function probeCharacter(plr)
    local char = plr.Character
    if not char then return end
    section(fmt("CHARACTER: %s (uid=%d)", plr.Name, plr.UserId))
    local crep = { player = plr.Name, userId = plr.UserId, bodyParts = {} }

    local hum = char:FindFirstChildOfClass("Humanoid")
    local rt = hum and safeProp(hum, "RigType")
    crep.rigType = rt and rt.Name or "?"
    log("  rigType: " .. crep.rigType)

    -- classic clothing on the avatar
    local shirt = char:FindFirstChildOfClass("Shirt")
    local pants = char:FindFirstChildOfClass("Pants")
    local shirtTmpl = shirt and safeProp(shirt, "ShirtTemplate")
    local pantsTmpl = pants and safeProp(pants, "PantsTemplate")
    log(fmt("  classic clothing: Shirt=%s Pants=%s", tostring(shirtTmpl), tostring(pantsTmpl)))
    crep.shirt = tostring(shirtTmpl); crep.pants = tostring(pantsTmpl)

    -- extract the clothing template once + score it (proves template-vs-composite)
    if nonEmpty(shirtTmpl) then
        local r = probeImage(contentFromUri(shirtTmpl), "ShirtTemplate")
        log("    [shirt template] " .. HttpService:JSONEncode(r))
        crep.shirtProbe = r
    end

    local cmesh = collectCMesh(char)

    -- iterate body parts (any class), resolve every appearance source
    for _, d in ipairs(char:GetDescendants()) do
        if d:IsA("BasePart") and BODY_NAMES[d.Name:lower()] then
            local entry = { name = d.Name, class = d.ClassName }
            log("  -- body part: " .. d.Name .. " (class=" .. d.ClassName .. ") --")

            -- mesh source: MeshPart content/id, OR CharacterMesh override, OR none
            local meshContent
            local meshSrc = "none"
            if d:IsA("MeshPart") then
                local mc = safeProp(d, "MeshContent")
                local mid = safeProp(d, "MeshId")
                if mc and tostring(mc) ~= "" then meshContent = mc; meshSrc = "MeshPart.MeshContent"
                elseif nonEmpty(mid) then meshContent = contentFromUri(mid); meshSrc = "MeshPart.MeshId" end
            else
                local cm = cmesh[d.Name]
                if cm and cm.meshId then meshContent = contentFromUri(cm.meshId); meshSrc = "CharacterMesh.MeshId" end
            end
            entry.meshSource = meshSrc
            if meshContent then
                local m = probeMesh(meshContent)
                entry.mesh = m
                log("    mesh ["..meshSrc.."]: " .. HttpService:JSONEncode(m)
                    .. "  <- compare uvRange to the 585x559 R6 template layout")
            else
                log("    mesh: none (classic Block body part)")
            end

            -- texture sources to test for "what is actually displayed"
            local texProbes = {}
            local function tryTex(ref, tag)
                if not nonEmpty(ref) then return end
                texProbes[#texProbes+1] = probeImage(contentFromUri(ref), tag)
            end
            if d:IsA("MeshPart") then
                tryTex(safeProp(d, "TextureID"), "MeshPart.TextureID")
                local tc = safeProp(d, "TextureContent")
                if tc and tostring(tc) ~= "" then
                    texProbes[#texProbes+1] = probeImage(tc, "MeshPart.TextureContent")
                end
            end
            local cm = cmesh[d.Name]
            if cm then tryTex(cm.baseTex, "CharacterMesh.BaseTextureId"); tryTex(cm.overlay, "CharacterMesh.OverlayTextureId") end
            local sa = d:FindFirstChildOfClass("SurfaceAppearance")
            if sa then tryTex(safeProp(sa, "ColorMap"), "SurfaceAppearance.ColorMap") end

            -- THE KEY EXPERIMENT: does Content.fromObject(part) hand back a
            -- composited texture (skin+clothing baked) the refs above don't?
            local co, coErr = contentFromObject(d)
            if co then
                texProbes[#texProbes+1] = probeImage(co, "Content.fromObject(part)")
            else
                log("    Content.fromObject(part): " .. tostring(coErr))
            end

            for _, tp in ipairs(texProbes) do
                log(fmt("    tex [%-30s] %s", tp.tag, HttpService:JSONEncode(tp)))
            end
            entry.textures = texProbes
            crep.bodyParts[#crep.bodyParts + 1] = entry
        end
    end

    log("  INTERPRETATION: a texture whose variance is HIGH and whose mean isn't")
    log("    skin-toned is a real composite/clothing image. If only the Shirt")
    log("    TEMPLATE scores high but no per-part texture does, the engine isn't")
    log("    exposing the composite here -> importer-side reprojection needed.")
    report.characters[#report.characters + 1] = crep
end

section("SCAN")
if lp and lp.Character then probeCharacter(lp) end
local n = 0
for _, p in ipairs(Players:GetPlayers()) do
    if p ~= lp and p.Character and n < 5 then probeCharacter(p); n = n + 1 end
end
if #report.characters == 0 then
    log("NO characters found — make sure avatars are loaded, then re-run.")
end

----------------------------------------------------------------------
section("WRITE")
local stamp = os.time()
local base = "ROCORDER/diag"
if writefile then
    pcall(function()
        if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
        if isfolder and not isfolder(base) then makefolder(base) end
    end)
    local txt = base .. "/probe_" .. stamp .. ".txt"
    local json = base .. "/probe_" .. stamp .. ".json"
    local okT = pcall(writefile, txt, table.concat(lines, "\n"))
    local okJ = pcall(function() writefile(json, HttpService:JSONEncode(report)) end)
    log(fmt("wrote %s (%s) + %s (%s)", txt, okT and "ok" or "FAIL", json, okJ and "ok" or "FAIL"))
else
    log("writefile unavailable — copy the console output above instead.")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER probe", Text="PROBE DONE — see workspace/ROCORDER/diag/", Duration=8 })
end)
log("PROBE DONE")
