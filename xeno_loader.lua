-- ROCORDER loader for Xeno (and any executor with HttpGet + loadstring).
--
-- Paste this into your executor ONCE. Each time you re-execute it, it pulls
-- the latest rocorder.lua from GitHub and runs it — no need to copy-paste
-- the recorder again after every change.
--
-- Workflow:
--   1. Edit rocorder.lua locally
--   2. git commit && git push
--   3. Re-execute this loader in Xeno
--
-- Requires the repo to be PUBLIC (raw.githubusercontent.com refuses anonymous
-- requests for private repos).

local ROCORDER_LOADER_VERSION = "1.9.0-alpha"

local REPO_USER   = "ArtemChig"
local REPO_NAME   = "Rocorder"
local BRANCH      = "main"
local SCRIPT_PATH = "rocorder.lua"

-- ?t=<unix> defeats the CDN cache so we don't get a stale copy after a push.
local url = string.format(
    "https://raw.githubusercontent.com/%s/%s/%s/%s?t=%d",
    REPO_USER, REPO_NAME, BRANCH, SCRIPT_PATH, os.time()
)

local ok, body = pcall(game.HttpGet, game, url, true)
if not ok then
    warn("[ROCORDER loader] HttpGet failed:", body)
    return
end
if type(body) ~= "string" or #body == 0 then
    warn("[ROCORDER loader] Empty response from GitHub. URL:", url)
    return
end
-- GitHub returns a plain-text "404: Not Found" body for missing paths.
if body:sub(1, 3) == "404" then
    warn("[ROCORDER loader] 404 from GitHub. Is the repo public and the path right?")
    warn("                  URL:", url)
    return
end

local fn, err = loadstring(body, "=rocorder.lua")
if not fn then
    warn("[ROCORDER loader] loadstring failed:", err)
    return
end

print(string.format("[ROCORDER loader v%s] Fetched %d bytes from %s@%s, running...",
    ROCORDER_LOADER_VERSION, #body, REPO_NAME, BRANCH))

local runOk, runErr = pcall(fn)
if not runOk then
    warn("[ROCORDER loader] Recorder errored on load:", runErr)
end
