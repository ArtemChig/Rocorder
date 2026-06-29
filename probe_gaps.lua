--[[
  ROCORDER gap-closing probe   (standalone, read-only)          gaps/1
  -----------------------------------------------------------------
  Tests EVERY workaround for the uncovered types so we can write a complete
  plan on evidence:
    B. MeshContent universal extraction (does CreateEditableMeshAsync accept
       a live part's .MeshContent? -> also cracks runtime/procedural meshes)
    C. Skinned mesh: GetVertexBoneWeights return shape + Bone read per frame
    D. Layered clothing: WrapLayer/WrapTarget cage props + cage-mesh extract
    E. Union/CSG: hidden MeshData blob via gethiddenproperty, + raycast
       voxelization feasibility, + executor mesh-dump globals
    F. Runtime mesh: MeshId-empty MeshParts -> MeshContent extract

  Read-only. Writes workspace/ROCORDER/diag/gaps_<unixtime>.txt.
  RUN IN VARIED GAMES — one with modern R15 layered/3D clothing, one with
  skinned-mesh avatars, plus the usual. Each run appends evidence; I read
  them from the diag folder.
]]

local PROBE_VERSION = "gaps/1"
local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")
local Workspace    = workspace
local writefile  = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder   = isfolder  or (syn and syn.isfolder)
local lp = Players.LocalPlayer

local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[GAPS] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local report = { version = PROBE_VERSION, placeId = game.PlaceId }

local function has(o,m) if not o then return false end local ok,v=pcall(function() return o[m] end) return ok and typeof(v)=="function" end
local function sp(i,p) local ok,v=pcall(function() return i[p] end) if ok then return v end return nil end
local function ne(s) return s~=nil and tostring(s)~="" and tostring(s)~="0" and tostring(s)~="rbxassetid://0" end
local function gget(name) local ok,v=pcall(function() return getfenv()[name] end) if ok then return v end return nil end
local function fromUri(u)
    if (typeof(Content)=="table" or typeof(Content)=="userdata") and Content.fromUri then
        local ok,c=pcall(function() return Content.fromUri(u) end) if ok and c then return c end
    end
    return u
end

local function emFromContent(content)
    if not content or not has(AssetService,"CreateEditableMeshAsync") then return nil, "no api/content" end
    local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(content) end)
    if not ok or not em then return nil, tostring(em) end
    return em
end
local function emSummary(em)
    local vids; if not pcall(function() vids = em:GetVertices() end) or not vids then return { err = "GetVertices" } end
    return { verts = #vids }
end

----------------------------------------------------------------------
section("A. capability matrix")
do
    local em
    for _, pl in ipairs(Players:GetPlayers()) do
        if pl.Character then
            for _, d in ipairs(pl.Character:GetDescendants()) do
                if d:IsA("MeshPart") then
                    local mc = sp(d,"MeshContent"); local mid = sp(d,"MeshId")
                    local c = (mc and tostring(mc)~="" and mc) or (ne(mid) and fromUri(mid)) or nil
                    if c then em = emFromContent(c); if em then break end end
                end
            end
        end
        if em then break end
    end
    if em then
        local meths = {"GetVertices","GetPosition","GetUV","GetFaceUVs","GetFaces","GetTriangles",
            "GetVertexBoneWeights","GetBoneWeights","GetSkinWeights","GetBones","GetBoneNames",
            "GetFaceColors","GetVertexNormals","GetFaceVertices"}
        local present = {}
        for _,m in ipairs(meths) do if has(em,m) then present[#present+1]=m end end
        log("  EditableMesh methods: " .. table.concat(present, ", "))
        report.emMethods = present
    else log("  no sample EditableMesh") end

    -- executor power globals relevant to gap-closing
    local globals = {"gethiddenproperty","sethiddenproperty","getproperty","setscriptable",
        "getrendermesh","getmesh","dumpmesh","getcustomasset","getsynasset","getrawmetatable",
        "getnilinstances","getinstances"}
    local found = {}
    for _,g in ipairs(globals) do if gget(g) ~= nil then found[#found+1]=g end end
    log("  executor globals: " .. (table.concat(found, ", ")))
    report.executorGlobals = found
    log("  Raycast: " .. (has(Workspace,"Raycast") and "yes" or "no")
        .. "  GetPartBoundsInBox: " .. (has(Workspace,"GetPartBoundsInBox") and "yes" or "no"))
end

----------------------------------------------------------------------
section("B. MeshContent universal-extraction test")
do
    -- Does CreateEditableMeshAsync(part.MeshContent) work directly off a live
    -- part (vs needing the rbxassetid)? If yes, it's the preferred capture
    -- path AND it handles runtime/procedural meshes that set MeshContent.
    local tested = 0
    for _, pl in ipairs(Players:GetPlayers()) do
        if pl.Character and tested < 3 then
            for _, d in ipairs(pl.Character:GetDescendants()) do
                if d:IsA("MeshPart") and tested < 3 then
                    local mc = sp(d,"MeshContent")
                    local mid = sp(d,"MeshId")
                    if mc and tostring(mc) ~= "" then
                        local em, err = emFromContent(mc)
                        log(fmt("  %s.MeshContent (MeshId=%s): %s", d.Name, tostring(mid),
                            em and ("OK verts="..(emSummary(em).verts or "?")) or ("FAIL: "..tostring(err))))
                        tested = tested + 1
                    end
                end
            end
        end
    end
    if tested == 0 then log("  no MeshParts with a MeshContent to test") end
end

----------------------------------------------------------------------
section("C. skinned mesh")
do
    local skinned, bones = nil, {}
    for _, pl in ipairs(Players:GetPlayers()) do
        if pl.Character then
            for _, d in ipairs(pl.Character:GetDescendants()) do
                if d:IsA("MeshPart") and d:FindFirstChildWhichIsA("Bone", true) then skinned = d; break end
            end
        end
        if skinned then break end
    end
    -- also scan workspace for any skinned mesh (NPCs/props)
    if not skinned then
        local n = 0
        for _, d in ipairs(Workspace:GetDescendants()) do
            n = n + 1; if n > 6000 then break end
            if d:IsA("MeshPart") and d:FindFirstChildWhichIsA("Bone", true) then skinned = d; break end
        end
    end
    if not skinned then
        log("  no skinned MeshPart found here (need a skinned-avatar game)")
    else
        log("  skinned MeshPart: " .. skinned:GetFullName())
        local nb = 0
        for _, b in ipairs(skinned:GetDescendants()) do if b:IsA("Bone") then nb = nb + 1
            if nb <= 4 then
                log(fmt("    Bone %s: WorldCFrame readable=%s Transform readable=%s",
                    b.Name, tostring(sp(b,"WorldCFrame")~=nil), tostring(sp(b,"Transform")~=nil)))
            end
        end end
        log("    total bones: " .. nb)
        local mc = sp(skinned,"MeshContent"); local mid = sp(skinned,"MeshId")
        local em = emFromContent((mc and tostring(mc)~="" and mc) or fromUri(mid))
        if em then
            if has(em,"GetVertexBoneWeights") then
                local vids = em:GetVertices()
                local okW, w = pcall(function() return em:GetVertexBoneWeights(vids[1]) end)
                log("    GetVertexBoneWeights(v0): " .. (okW and HttpService:JSONEncode(w) or ("err: "..tostring(w))))
            else log("    GetVertexBoneWeights: NOT present") end
            if has(em,"GetBones") then
                local okB, bl = pcall(function() return em:GetBones() end)
                log("    GetBones(): " .. (okB and ("count="..tostring(bl and #bl)) or ("err: "..tostring(bl))))
            end
        else log("    could not build EditableMesh for the skinned part") end
    end
end

----------------------------------------------------------------------
section("D. layered clothing (WrapLayer / WrapTarget)")
do
    -- prefer a WrapLayer (an equipped 3D garment) over a WrapTarget (body cage)
    local wrap, wrapTargetFallback
    for _, pl in ipairs(Players:GetPlayers()) do
        if pl.Character then
            for _, d in ipairs(pl.Character:GetDescendants()) do
                if d:IsA("WrapLayer") then wrap = d; break end
                if d:IsA("WrapTarget") and not wrapTargetFallback then wrapTargetFallback = d end
            end
        end
        if wrap then break end
    end
    wrap = wrap or wrapTargetFallback
    if not wrap then
        log("  no WrapLayer/WrapTarget here (need a modern layered-clothing avatar)")
    else
        log("  found " .. wrap.ClassName .. " on " .. (wrap.Parent and wrap.Parent:GetFullName() or "?"))
        for _, p in ipairs({"CageMeshId","CageMeshContent","CageOrigin","CageWeight","ImportOrigin",
            "ReferenceMeshId","ReferenceOrigin","Order","ShrinkFactor","Stiffness","BindOffset","AutoSkin","Enabled"}) do
            local v = sp(wrap, p)
            if v ~= nil then log(fmt("    %s = %s", p, tostring(v))) end
        end
        -- try to extract the cage mesh (the authored, undeformed layer geometry)
        local cmc = sp(wrap,"CageMeshContent"); local cmid = sp(wrap,"CageMeshId")
        local c = (cmc and tostring(cmc)~="" and cmc) or (ne(cmid) and fromUri(cmid)) or nil
        if c then
            local em, err = emFromContent(c)
            log("    cage mesh extract: " .. (em and ("OK verts="..(emSummary(em).verts or "?")) or ("FAIL: "..tostring(err))))
        else log("    no CageMeshId/Content to extract") end
    end
end

----------------------------------------------------------------------
section("E. Union / CSG")
do
    local union
    local n = 0
    for _, d in ipairs(Workspace:GetDescendants()) do
        n = n + 1; if n > 9000 then break end
        if d:IsA("UnionOperation") then union = d; break end
    end
    if not union then log("  no UnionOperation found")
    else
        log("  union: " .. union:GetFullName())
        log("    MeshContent=" .. tostring(sp(union,"MeshContent")) .. "  AssetId=" .. tostring(sp(union,"AssetId")))
        -- THE real route: read the hidden cooked-mesh blob via gethiddenproperty
        local ghp = gget("gethiddenproperty")
        if ghp then
            for _, prop in ipairs({"MeshData","PhysicsData","InitialSize","ChildData"}) do
                local ok, val = pcall(ghp, union, prop)
                if ok and val ~= nil then
                    local len = (type(val)=="string") and #val or (typeof(val)=="buffer" and buffer.len(val)) or "?"
                    log(fmt("    gethiddenproperty(%s): type=%s len=%s  *** BLOB AVAILABLE", prop, typeof(val), tostring(len)))
                else
                    log(fmt("    gethiddenproperty(%s): %s", prop, ok and "nil" or ("err: "..tostring(val))))
                end
            end
        else
            log("    gethiddenproperty: NOT available (can't read MeshData blob)")
        end
        -- raycast voxelization feasibility (last-resort approximation)
        if has(Workspace,"Raycast") then
            local sz = sp(union,"Size"); local cf = sp(union,"CFrame")
            if sz and cf then
                local params = RaycastParams.new(); params.FilterType = Enum.RaycastFilterType.Include
                params.FilterDescendantsInstances = { union }
                local origin = cf.Position + Vector3.new(0, sz.Y, 0)
                local res = Workspace:Raycast(origin, Vector3.new(0, -sz.Y*2, 0), params)
                log("    raycast hit self: " .. (res and "yes (voxelization feasible)" or "no"))
            end
        end
    end
end

----------------------------------------------------------------------
section("F. runtime / procedural mesh (MeshId empty)")
do
    local found = 0
    local n = 0
    for _, d in ipairs(Workspace:GetDescendants()) do
        n = n + 1; if n > 9000 then break end
        if d:IsA("MeshPart") then
            local mid = sp(d,"MeshId"); local mc = sp(d,"MeshContent")
            if (not ne(mid)) then
                found = found + 1
                if found <= 3 then
                    local hasC = mc and tostring(mc) ~= ""
                    local result
                    if hasC then
                        local em, err = emFromContent(mc)
                        result = em and ("OK verts="..(emSummary(em).verts or "?")) or ("EXTRACT FAILED: "..tostring(err))
                    else
                        result = "no MeshContent (truly id-less)"
                    end
                    log(fmt("  runtime MeshPart %s: MeshContent=%s -> %s", d.Name, tostring(hasC), result))
                end
            end
        end
    end
    if found == 0 then log("  none found (no id-less MeshParts in scan)") end
    report.runtimeMeshCount = found
end

----------------------------------------------------------------------
section("WRITE")
local stamp = os.time(); local base = "ROCORDER/diag"
if writefile then
    pcall(function()
        if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
        if isfolder and not isfolder(base) then makefolder(base) end
    end)
    local txt = base .. "/gaps_" .. stamp .. ".txt"
    local okT = pcall(writefile, txt, table.concat(lines, "\n"))
    pcall(function() writefile(base .. "/gaps_" .. stamp .. ".json", HttpService:JSONEncode(report)) end)
    log("wrote " .. txt .. " (" .. (okT and "ok" or "FAIL") .. ")")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER gaps", Text="DONE — see ROCORDER/diag/gaps_*.txt", Duration=8 })
end)
log("GAPS PROBE DONE")
