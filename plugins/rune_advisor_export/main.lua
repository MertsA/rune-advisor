-- Rune Advisor Export
--
-- Posts your own character's state to the Rune Advisor API (https://api.runeadvisor.com), where the Rune
-- Advisor planner reads it to recommend what to do next. What is read from the game, all of it your own
-- character's:
--   - skill experience, whether the world is a members' world, and the account mode (Ironman, Hardcore,
--     Group Ironman: four flags, lib/exported_vars.lua)
--   - the player variables that quests and achievements are made of (which are done, how far along)
--   - a hand-listed set of other player variables: currencies, storages kept in variables, the collection
--     log, banked bonus xp and the like (lib/exported_vars.lua lists every one)
--   - your own item storage: bank, backpack, worn equipment and the like (lib/inventories.lua)
-- It posts the full state once per login and after that only what changed (lib/wire.lua). Nothing is sent
-- until you paste your API token into the plugin's settings (the settings icon in the Plugin Hub's Manage
-- tab); the token is checked with the API before it is saved. The same settings hold one choice per
-- character, off until you turn it on: whether that character's data may also be analysed to improve the
-- planner for everyone. It changes nothing about what is sent. httpAccess is the plugin's one permission
-- and is required (the plugin does nothing without it); chatAccess is optional (see below).
--
-- Nothing in the game is touched: the plugin clicks nothing, draws nothing over the game and changes no
-- setting. Other players' data is never read or sent: no player chat, no other characters, no trade screens
-- or shops (the inventories are an allow-list of your own). With the optional chatAccess permission it reads
-- the game's own messages for the few values the game gives only there (your rested experience; lib/gamechat.lua):
-- a message with a sender is dropped before anything else is looked at, and only the numbers are kept. It also
-- reads the number on the porter's buff icon (lib/uiread.lua), the charges a porter or Grace of the Elves holds.
--
-- The only things kept on this computer (PersistentDB) are the token, the posting pace the server last
-- asked for, and the last state the API accepted, so a new session can post just the changes.
--
-- Where to look:
--   main.lua             this file: how variables and inventories are read, and the wiring
--   lib/exported_vars.lua  the player variables posted by name, written out
--   lib/http.lua         every network call (one destination, four calls)
--   lib/wire.lua         what is posted and when: the state, full posts and deltas
--   lib/manifest.lua     which bits of which player variables go into the state
--   lib/settings.lua     the settings panel: the token field and the consent button
--   lib/inventories.lua  the allow-list of own inventories
--   lib/gamechat.lua     the game's own chat messages (optional chatAccess): rested experience
--   lib/uiread.lua       numbers the game shows on its interface only: the active porter's charges, the
--                        helmet on the Anachronia Slayer helmet stand
--   lib/configdump.lua, lib/canon.lua, lib/configsync.lua
--                        the game's quest and achievement definitions: read, hashed, and sent to the
--                        server when a game update changed them (game data, nothing about the player)
--   lib/bits.lua, lib/json.lua, lib/jobs.lua, lib/configs.lua
--                        helpers: bit packing and hashes, JSON, work split across ticks, listing configs
--
-- Client API functions are only ever called from Lua code (PersistentDB:GetString(...), inside a closure
-- where an error must be caught), never handed to pcall as a function value: doing that with a client
-- function (`pcall(PersistentDB.GetString, PersistentDB, key)`) shut the whole game down when tried.

local json = require("lib/json")
local configs = require("lib/configs")
local bits = require("lib/bits")
local jobs = require("lib/jobs")
local manifest = require("lib/manifest")
local wire = require("lib/wire")
local settings = require("lib/settings")
local http = require("lib/http")
local canon = require("lib/canon")
local configdump = require("lib/configdump")
local configsync = require("lib/configsync")
local gamechat = require("lib/gamechat")
local uiread = require("lib/uiread")


-- Sent with every full post, so the server knows which protocol it is talking to (the API exposes no
-- game or plugin version, so this is kept by hand; bump it with plugin.json: a test checks they match).
local PLUGIN_VERSION = "0.5.0"

-- PersistentDB rejects large strings, so big values (the acknowledged wire state) are split into numbered
-- chunks: "<key>#1", "<key>#2", ... plus a header stored at "<key>" itself: {"chunks": N}.
local CHUNK_BYTES = 60000

--- Store a JSON string, chunking it if needed. Returns true if every write succeeded.
local function saveJson(key, encoded)
    -- Remember how many chunks the previous save used so leftovers can be deleted.
    local previous = 0
    local header = PersistentDB:GetString(key)
    if header then
        previous = tonumber(header:match('^{"chunks":(%d+)}$')) or 0
    end

    local ok = true
    local count = 0
    if #encoded <= CHUNK_BYTES then
        ok = PersistentDB:SetString(key, encoded)
    else
        local start = 1
        while start <= #encoded do
            local stop = math.min(start + CHUNK_BYTES - 1, #encoded)
            -- Don't split a multi-byte UTF-8 character: back up while the next byte is a continuation byte.
            while stop < #encoded and stop > start and (encoded:byte(stop + 1) & 0xC0) == 0x80 do
                stop = stop - 1
            end
            count = count + 1
            ok = PersistentDB:SetString(key .. "#" .. count, encoded:sub(start, stop)) and ok
            start = stop + 1
        end
        -- Write the header last so a reader never sees a header pointing at missing chunks.
        -- Plain concatenation: formatStr treats any "{" as a placeholder.
        ok = PersistentDB:SetString(key, '{"chunks":' .. count .. '}') and ok
    end
    for stale = count + 1, previous do
        PersistentDB:Delete(key .. "#" .. stale)
    end
    return ok, count
end

--- Read back what saveJson stored: the string itself, or its chunks joined. nil when nothing is stored or a
--- chunk is missing.
local function loadJson(key)
    local stored = PersistentDB:GetString(key)
    if not stored then return nil end
    local chunks = tonumber(stored:match('^{"chunks":(%d+)}$'))
    if not chunks then return stored end
    local parts = {}
    for n = 1, chunks do
        local part = PersistentDB:GetString(key .. "#" .. n)
        if not part then return nil end
        parts[n] = part
    end
    return table.concat(parts)
end

local function playerName()
    local entity = Player.GetEntity()
    return entity and entity.displayName or nil
end

-- SkillType can't be iterated with pairs() in the client, so list the skills explicitly
-- (names from the Player docs, in SkillType order 0..28).
local SKILL_NAMES = {
    "ATTACK", "DEFENCE", "STRENGTH", "HITPOINTS", "RANGED", "PRAYER", "MAGIC", "COOKING",
    "WOODCUTTING", "FLETCHING", "FISHING", "FIREMAKING", "CRAFTING", "SMITHING", "MINING",
    "HERBLORE", "AGILITY", "THIEVING", "SLAYER", "FARMING", "RUNECRAFTING", "HUNTER",
    "CONSTRUCTION", "SUMMONING", "DUNGEONEERING", "DIVINATION", "INVENTION", "ARCHAEOLOGY",
    "NECROMANCY",
}

-- The player variables posted by name: lib/exported_vars.lua, every one written out with its width.
local exported = require("lib/exported_vars")

-- A player var, else a client var of that name: the client keeps copies of some item vars there
-- (STATUE_BAG_CLIENT holds the statue collection bag's obj var), and the API can't read obj vars directly.
-- A "chat" field is the value lib/gamechat.lua last read from the game's own messages (-1: not seen yet).
local function readVar(name)
    if gamechat.FIELDS[name] then return gamechat.value(name) end
    if uiread.FIELDS[name] then return uiread.value(name) end
    for _, domain in ipairs({ "VarPlayer", "VarClient" }) do
        local ok, value = pcall(function() return var[domain][name].value end)
        if ok and (type(value) == "number" or type(value) == "string" or type(value) == "boolean") then
            return value
        end
    end
    return nil
end

-- The player's own item storage (lib/inventories.lua): the only inventories ever sent.
local inventories = require("lib/inventories")
local EXPORTED_INVENTORIES = inventories.EXPORTED

-- The named-var section of the wire layout: lib/exported_vars.lua's entries, in its order, checked against
-- this client's variables at login. An entry this client doesn't have as listed (the name is gone, or it is no
-- longer a VarBit of that width after a game update) stays in the layout, so the layout never depends on the
-- client, but is sent as 0 and reported absent. A VarBit's place in its base var comes from its mask: a change
-- event names the base var, and the field is cut from its new value. Client variables announce no changes,
-- so the wire re-reads them before every post.
-- onDone(fields): { name, width, kind, start (VarBits), base (the variable a change event names), absent }
-- per entry, in the manifest's order. As a job, a slice per tick: there are ~1,500 entries.
local FIELDS_PER_SLICE = 500
local function resolveFields(onDone)
    local out = {}
    jobs.add({
        name = "exported vars", items = exported.FIELDS, perTick = FIELDS_PER_SLICE,
        each = function(entry)
            local name, width, kind = entry[1], entry[2], entry[3]
            local f = { name = name, width = width, kind = kind, base = name, absent = true }
            if kind == "client" then
                local ok, present = pcall(function() return var.VarClient[name] ~= nil end)
                f.absent = not (ok and present)
            elseif kind == "chat" then
                f.absent = not gamechat.FIELDS[name]  -- filled from the game's messages, never a variable
            elseif kind == "ui" then
                f.absent = not uiread.FIELDS[name]    -- read off the game's interface, never a variable
            else
                local ok, isBit, base, mask = pcall(function()
                    local v = var.VarPlayer[name]
                    if v == nil then return nil end
                    if v.isVarBit then return true, v.baseVar and v.baseVar.configName, v.varBitMask end
                    return false
                end)
                if ok and isBit == false and kind == "var" then
                    f.absent = false
                elseif ok and isBit == true and kind == "bit" and type(base) == "string"
                        and type(mask) == "number" and mask > 0 then
                    local start, w = 0, 0
                    while (mask >> start) & 1 == 0 do start = start + 1 end
                    local rest = mask >> start
                    while rest > 0 do rest, w = rest >> 1, w + 1 end
                    if w == width then f.absent, f.start, f.base = false, start, base end
                end
            end
            out[#out + 1] = f
        end,
        done = function() onDone(out) end,
    })
end

-- An inventory as a set: item id -> count, placeholders (count 0) and empty slots left out.
local function readInventorySet(configName)
    local cfg = config.Inv[configName]
    local inv = cfg and Inventory.Find(cfg)
    if not inv or not inv.isValid then return nil end
    local set = {}
    for _, slot in ipairs(inv:GetSlots() or {}) do
        -- Each field is read once: every read of a slot goes through the client.
        local obj = slot and slot.obj
        local itemId, count = obj and obj.id, slot and slot.count
        if itemId and itemId >= 0 and count > 0 then
            set[itemId] = (set[itemId] or 0) + count
        end
    end
    return set
end

local WIRE_INVENTORIES = {}
for name in pairs(EXPORTED_INVENTORIES) do WIRE_INVENTORIES[#WIRE_INVENTORIES + 1] = name end
table.sort(WIRE_INVENTORIES)

-- Posts go to the Rune Advisor API (lib/http.lua holds every network call), and only once the user has set
-- a token in the settings (checked at every post, so pasting a token takes effect at once). httpAccess is a
-- required permission, so the HasPermission check below is a guard, not a prompt the user has to answer.
-- Without a token nothing is sent anywhere. The API's address comes from plugin.json (`urls.api`).
local function apiUrl()
    local ok, url = pcall(function() return Plugin.urls.api end)
    return ok and url or nil
end

local function httpGranted()
    local ok, granted = pcall(function() return Plugin.HasPermission(PluginPermissions.httpAccess) end)
    return ok and granted == true
end

local function apiReady()
    return settings.token() ~= nil and apiUrl() ~= nil and httpGranted()
end

-- The line the settings panel shows when nothing is wrong.
local function apiStatus()
    if settings.token() == nil then return "No token: nothing is sent" end
    if not httpGranted() then return "Token set; grant httpAccess to post" end
    if apiUrl() == nil then return "Token set; no API URL in the manifest" end
    return "Posting to the Rune Advisor API"
end

local api = http.new({ json = json, bits = bits, url = apiUrl, token = settings.token,
                       forgetToken = settings.forget, noteConsent = settings.noteConsent })

-- A pasted token is checked with the API before it is saved (lib/settings.lua).
local function checkToken(token, callback)
    if not httpGranted() then return callback("no-permission") end
    if apiUrl() == nil then return callback("offline", "no API URL in the manifest") end
    api.checkToken(token, callback)
end

-- The settings' consent button (lib/settings.lua): the server holds the answer, the plugin asks it to change.
local function setConsent(player, wanted, callback)
    if not httpGranted() or apiUrl() == nil then return callback(nil, "no httpAccess") end
    api.setConsent(player, wanted, callback)
end

local settingsHandle = settings.build({ status = apiStatus, check = checkToken, setConsent = setConsent, player = playerName })
Event.ServerLogic.Subscribe("runeadvisor-export-settings", settings.tick)

-- The hash of one quest or achievement definition, taken on the login walk that builds the layout. The
-- hash of the whole set (its "fingerprint") goes with a full post, and a server that doesn't know that set
-- (a game update it has not seen) is sent the definitions it lacks (lib/configsync.lua).
local function configHash(kind, cfg)
    local entry = configdump.KINDS[kind].build(cfg)
    if not entry then return nil end
    return entry.id, canon.configHash(entry)
end

local function syncConfigs(layout, reply, done)
    configsync.run({
        configs = configs, jobs = jobs, configdump = configdump, configHashes = layout.configHashes,
        fingerprint = layout.fingerprint, upload = api.uploadConfigs,
    }, reply, done)
end

-- The login walk (lib/manifest.lua, then the baseline in lib/wire.lua) runs a slice per client logic tick
-- (50 a second), not per server tick (one every 0.6 s): it is about half a second of work in all, and at
-- one slice per server tick it took some 25 seconds before the first post could go out. The slices are
-- sized to a few milliseconds each (measured on the client).
Event.Logic.Subscribe("runeadvisor-jobs", jobs.tick)
gamechat.start()
uiread.start()
local wireHandle = wire.start({
    json = json, saveJson = saveJson, loadJson = loadJson, bits = bits, jobs = jobs, configs = configs, manifest = manifest,
    playerName = playerName, skillNames = SKILL_NAMES, namedVars = resolveFields,
    inventoryNames = WIRE_INVENTORIES, readInventory = readInventorySet, readVar = readVar,
    transport = { post = api.postState }, ready = apiReady, loadInterval = settings.interval, saveInterval = settings.setInterval,
    configHash = configHash, fingerprint = canon.fingerprint, syncConfigs = syncConfigs,
    pluginVersion = PLUGIN_VERSION,
})


-- A token the API just accepted: post the state right away instead of waiting for the next interval.
settings.onSaved = function() wireHandle.nudge() end

return { wire = wireHandle, settings = settingsHandle }
