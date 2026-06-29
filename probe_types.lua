--[[
  ROCORDER asset-type census   (standalone, read-only)         types/1
  -----------------------------------------------------------------
  Answers: which mesh/texture types exist here, and for each UNKNOWN type
  (layered clothing / skinned mesh / Union / tiling Texture / runtime mesh /
  PBR SurfaceAppearance), what does the API actually give us? So the redesign
  can plan EVERY type on evidence, not assumption.

  Read-only. Writes a report to workspace/ROCORDER/diag/types_<unixtime>.txt.

  RUN IN VARIED GAMES — especially one with modern layered/3D clothing and one
  with skinned-mesh or Union-built avatars. Run once per game; send each report
  (or I read them from the diag folder).

  It does:
    A. API surface: EditableMesh method list (incl. skinning accessors),
       EditableImage, Content, CreateEditableMesh overloads.
    B. Avatar census: per player, classify every part; deep-probe the first
       WrapLayer (layered clothing), first skinned MeshPart, first
       SurfaceAppearance.
    C. Workspace sweep (capped): count + first-example deep-probe of
       UnionOperation, tiling Texture, runtime MeshPart (no id), non-mesh
       primitives (Cylinder/Wedge), SpecialMesh primitive types.
]]

local PROBE_VERSION = "types/1"
local Players      = game:GetService("Players")
local AssetService = game:GetService("AssetService")
local HttpService  = game:GetService("HttpService")
local writefile  = writefile or (syn and syn.writefile)
local makefolder = makefolder or (syn and syn.makefolder)
local isfolder   = isfolder  or (syn and syn.isfolder)
local lp = Players.LocalPlayer

local lines = {}
local function log(s) s = tostring(s); lines[#lines+1] = s; print("[TYPES] " .. s) end
local function fmt(...) return string.format(...) end
local function section(t) log(""); log("==== " .. t .. " ====") end
local report = { version = PROBE_VERSION, placeId = game.PlaceId }

local function has(o, m) if not o then return false end local ok,v = pcall(function() return o[m] end) return ok and typeof(v)=="function" end
local function sp(i, p) local ok,v = pcall(function() return i[p] end) if ok then return v end return nil end
local function ne(s) return s~=nil and tostring(s)~="" and tostring(s)~="rbxassetid://0" and tostring(s)~="0" end
local function fromUri(u)
    if (typeof(Content)=="table" or typeof(Content)=="userdata") and Content.fromUri then
        local ok,c = pcall(function() return Content.fromUri(u) end); if ok and c then return c end
    end
    return u
end
local function meshContentOf(part)
    if part:IsA("MeshPart") then
        local mc = sp(part,"MeshContent"); if mc and tostring(mc)~="" then return mc, "MeshContent" end
        local mid = sp(part,"MeshId"); if ne(mid) then return fromUri(mid), "MeshId" end
    end
    local sm = part:FindFirstChildOfClass("SpecialMesh")
    if sm then local mid = sp(sm,"MeshId"); if ne(mid) then return fromUri(mid), "SpecialMesh.MeshId" end end
    return nil
end
local function meshBBoxVerts(content)
    if not content or not has(AssetService,"CreateEditableMeshAsync") then return nil end
    local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(content) end)
    if not ok or not em then return { err = tostring(em) } end
    local vids; if not pcall(function() vids = em:GetVertices() end) or not vids then return { err = "GetVertices" } end
    local mn = Vector3.new(1e9,1e9,1e9); local mx = Vector3.new(-1e9,-1e9,-1e9)
    for i,vid in ipairs(vids) do
        local p = em:GetPosition(vid)
        mn = Vector3.new(math.min(mn.X,p.X),math.min(mn.Y,p.Y),math.min(mn.Z,p.Z))
        mx = Vector3.new(math.max(mx.X,p.X),math.max(mx.Y,p.Y),math.max(mx.Z,p.Z))
        if i > 5000 then break end
    end
    return { verts = #vids, size = fmt("%.2fx%.2fx%.2f", mx.X-mn.X, mx.Y-mn.Y, mx.Z-mn.Z) }
end

----------------------------------------------------------------------
section("A. API surface")
do
    log("  CreateEditableMeshAsync : " .. (has(AssetService,"CreateEditableMeshAsync") and "yes" or "NO"))
    log("  CreateEditableImageAsync: " .. (has(AssetService,"CreateEditableImageAsync") and "yes" or "NO"))
    log("  CreateEditableMesh (sync): " .. (has(AssetService,"CreateEditableMesh") and "yes" or "no"))
    -- EditableMesh method surface, incl. skinning accessors. Build one from
    -- any avatar mesh we can find.
    local sample
    for _, pl in ipairs(Players:GetPlayers()) do
        if pl.Character then
            for _, d in ipairs(pl.Character:GetDescendants()) do
                if d:IsA("MeshPart") or d:FindFirstChildOfClass("SpecialMesh") then
                    local c = meshContentOf(d); if c then sample = c; break end
                end
            end
        end
        if sample then break end
    end
    if sample then
        local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(sample) end)
        if ok and em then
            local methods = {"GetVertices","GetPosition","GetUV","GetFaceUVs","GetFaces",
                "GetTriangles","GetFaceNormals","GetVertexNormals","GetFaceColors",
                "GetVertexBoneWeights","GetBoneWeights","GetSkinWeights","GetVertexSkinningData",
                "GetFaceVertices","GetAdjacentFaces","FindClosestVertex"}
            local present = {}
            for _, m in ipairs(methods) do if has(em, m) then present[#present+1] = m end end
            log("  EditableMesh methods present: " .. table.concat(present, ", "))
            report.editableMeshMethods = present
        else log("  could not build a sample EditableMesh") end
    else log("  no avatar mesh to sample EditableMesh from") end
end

----------------------------------------------------------------------
section("B. Avatar census")
for _, pl in ipairs(Players:GetPlayers()) do
    local char = pl.Character
    if char then
        local hum = char:FindFirstChildOfClass("Humanoid")
        local rt = hum and sp(hum,"RigType")
        local counts = {}
        local firstWrap, firstSkinned, firstSA, firstAccessoryMesh
        for _, d in ipairs(char:GetDescendants()) do
            local k = d.ClassName
            counts[k] = (counts[k] or 0) + 1
            if (d:IsA("WrapLayer") or d:IsA("WrapTarget")) and not firstWrap then firstWrap = d end
            if d:IsA("MeshPart") and d:FindFirstChildWhichIsA("Bone", true) and not firstSkinned then firstSkinned = d end
            if d:IsA("SurfaceAppearance") and not firstSA then firstSA = d end
        end
        log(fmt("  %s [%s]: %s", pl.Name, rt and rt.Name or "?",
            HttpService:JSONEncode(counts)))
        if firstWrap then
            local host = firstWrap.Parent
            local c = host and meshContentOf(host)
            local bb = c and meshBBoxVerts(c) or nil
            log(fmt("    LAYERED CLOTHING: %s on %s | partSize=%s | authoredMesh=%s",
                firstWrap.ClassName, host and host.Name or "?",
                host and tostring(sp(host,"Size")) or "?",
                bb and HttpService:JSONEncode(bb) or "n/a"))
            log(fmt("      WrapLayer props: CageMeshId=%s CageOrigin=%s Order=%s",
                tostring(sp(firstWrap,"CageMeshId")), tostring(sp(firstWrap,"CageOrigin")),
                tostring(sp(firstWrap,"Order"))))
            log("      NOTE: authoredMesh size is the UNDEFORMED cage; no API returns the worn-deformed mesh")
        else log("    layered clothing: none") end
        if firstSkinned then
            local nb = 0
            for _, b in ipairs(firstSkinned:GetDescendants()) do if b:IsA("Bone") then nb = nb + 1 end end
            log(fmt("    SKINNED MESH: %s has %d Bone(s) | the .rec records ONE rigid CFrame/part, so per-bone skin deform is NOT representable",
                firstSkinned.Name, nb))
        else log("    skinned mesh: none") end
        if firstSA then
            log(fmt("    SurfaceAppearance: Color=%s Normal=%s Metal=%s Rough=%s Alpha=%s",
                tostring(ne(sp(firstSA,"ColorMap"))), tostring(ne(sp(firstSA,"NormalMap"))),
                tostring(ne(sp(firstSA,"MetalnessMap"))), tostring(ne(sp(firstSA,"RoughnessMap"))),
                tostring(sp(firstSA,"AlphaMode"))))
        else log("    SurfaceAppearance: none") end
    end
end

----------------------------------------------------------------------
section("C. Workspace sweep (capped)")
do
    local CAP = 9000
    local seen = 0
    local nUnion, nTexTile, nRuntimeMesh, nCyl, nWedge, nSpecial = 0,0,0,0,0,0
    local firstUnion, firstTexTile, firstRuntime
    local specialTypes = {}
    local stop = false
    local function visit(d)
        if stop then return end
        seen = seen + 1
        if seen > CAP then stop = true; return end
        if d:IsA("UnionOperation") or d:IsA("NegateOperation") then
            nUnion = nUnion + 1; if not firstUnion then firstUnion = d end
        elseif d:IsA("Texture") then
            local su = sp(d,"StudsPerTileU"); if su and su > 0 then nTexTile = nTexTile + 1; if not firstTexTile then firstTexTile = d end end
        elseif d:IsA("MeshPart") then
            local mc = sp(d,"MeshContent"); local mid = sp(d,"MeshId")
            if (not ne(mid)) and (not mc or tostring(mc)=="") then
                nRuntimeMesh = nRuntimeMesh + 1; if not firstRuntime then firstRuntime = d end
            end
        elseif d:IsA("Part") then
            local sh = sp(d,"Shape")
            if sh == Enum.PartType.Cylinder then nCyl = nCyl + 1
            elseif sh == Enum.PartType.Wedge then nWedge = nWedge + 1 end
        end
        local sm = d:FindFirstChildOfClass("SpecialMesh")
        if sm then
            local mt = sp(sm,"MeshType"); if mt then nSpecial = nSpecial + 1; specialTypes[mt.Name] = (specialTypes[mt.Name] or 0) + 1 end
        end
    end
    -- breadth-ish: iterate workspace descendants but bail at CAP
    for _, d in ipairs(workspace:GetDescendants()) do visit(d); if stop then break end end
    log(fmt("  scanned %d instances (cap %d%s)", math.min(seen,CAP), CAP, stop and ", HIT CAP" or ""))
    log(fmt("  Unions/Negate=%d  tilingTextures=%d  runtimeMeshParts(noId)=%d  Cylinders=%d  Wedges=%d  SpecialMesh=%d",
        nUnion, nTexTile, nRuntimeMesh, nCyl, nWedge, nSpecial))
    log("  SpecialMesh MeshTypes: " .. HttpService:JSONEncode(specialTypes))
    report.workspace = { unions=nUnion, tilingTextures=nTexTile, runtimeMeshParts=nRuntimeMesh,
        cylinders=nCyl, wedges=nWedge, specialMeshTypes=specialTypes }

    if firstUnion then
        log("  UNION example: " .. firstUnion:GetFullName() .. " class=" .. firstUnion.ClassName)
        log("    MeshContent=" .. tostring(sp(firstUnion,"MeshContent")) .. " (" .. typeof(sp(firstUnion,"MeshContent")) .. ")")
        -- can we get geometry out of a Union at all?
        local mc = sp(firstUnion,"MeshContent")
        if mc and tostring(mc) ~= "" then
            local bb = meshBBoxVerts(mc)
            log("    via MeshContent -> EditableMesh: " .. HttpService:JSONEncode(bb or {err="nil"}))
        else
            local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(firstUnion) end)
            log("    CreateEditableMeshAsync(union) directly: " .. (ok and "OK" or ("err: "..tostring(em))))
        end
    end
    if firstTexTile then
        log(fmt("  TILING TEXTURE example on %s: StudsPerTileU=%s V=%s OffsetU=%s V=%s Tex=%s",
            firstTexTile.Parent and firstTexTile.Parent.Name or "?",
            tostring(sp(firstTexTile,"StudsPerTileU")), tostring(sp(firstTexTile,"StudsPerTileV")),
            tostring(sp(firstTexTile,"OffsetStudsU")), tostring(sp(firstTexTile,"OffsetStudsV")),
            tostring(sp(firstTexTile,"Texture"))))
    end
    if firstRuntime then
        log("  RUNTIME MESHPART example: " .. firstRuntime:GetFullName())
        -- is its live geometry readable without an asset id?
        local ok, em = pcall(function() return AssetService:CreateEditableMeshAsync(firstRuntime) end)
        log("    CreateEditableMeshAsync(livePart) directly: " .. (ok and "OK (live geometry readable!)" or ("err: "..tostring(em))))
    end
end

----------------------------------------------------------------------
section("WRITE")
local stamp = os.time(); local base = "ROCORDER/diag"
if writefile then
    pcall(function()
        if isfolder and not isfolder("ROCORDER") then makefolder("ROCORDER") end
        if isfolder and not isfolder(base) then makefolder(base) end
    end)
    local txt = base .. "/types_" .. stamp .. ".txt"
    local okT = pcall(writefile, txt, table.concat(lines, "\n"))
    pcall(function() writefile(base .. "/types_" .. stamp .. ".json", HttpService:JSONEncode(report)) end)
    log("wrote " .. txt .. " (" .. (okT and "ok" or "FAIL") .. ")")
end
pcall(function()
    game.StarterGui:SetCore("SendNotification",
        { Title="ROCORDER types", Text="DONE — see ROCORDER/diag/types_*.txt", Duration=8 })
end)
log("TYPES PROBE DONE")
