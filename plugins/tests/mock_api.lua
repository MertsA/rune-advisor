-- A tiny stand-in for the RuneScape plugin API, just enough to run the exporter and the dev plugin
-- outside the client. Shapes follow https://developer.runescape.com/rs/ ; values are made up.
-- Shared by plugins/rune_advisor_export/tests and plugins/rune_advisor_dev/tests: load with dofile() before
-- a plugin's main.lua, then drive events through mock.fire().

local mock = { handlers = {}, store = {} }
-- The plugins' own JSON (a copy lives in each plugin folder; the test harness borrows the exporter's).
local json_lib = dofile("plugins/rune_advisor_export/lib/json.lua")
local function json_decode_for_mock(s) return json_lib.decode(s) end
local function json_encode_for_mock(t) return json_lib.encode(t) end

-- The real plugin sandbox has no `os` library (confirmed on the beta world).
os = nil

local function enum(values)
    local t = {}
    for name, v in pairs(values) do t[name] = v end
    t.FromID = function(id)
        for name, v in pairs(values) do
            if v == id then return name end
        end
        return nil
    end
    return t
end

-- In the client, pairs(SkillType) yields nothing; mimic that with an __index-only table.
SkillType = setmetatable({}, { __index = { ATTACK = 0, WOODCUTTING = 8, HUNTER = 21,
    FromID = function(id) return ({ [0] = "ATTACK", [8] = "WOODCUTTING", [21] = "HUNTER" })[id] end } })
GameState = enum({ LOGINSCREEN = 10, LOBBY = 20, GAME = 30 })
QuestType = enum({ NORMAL = 0, SEASONAL = 1, TUTORIAL = 2 })
QuestDifficulty = enum({ NOVICE = 0, MASTER = 3 })
AchievementProgressType = enum({ READY = 0, LOCKED = -1, COMPLETE = -2, GRACE = -3, DEPRECATED = -4 })
AchievementHiddenType = enum({ NEVER = 0, IF_LOCKED = 1, UNTIL_COMPLETE = 2 })
AchievementDescriptionType = enum({ DEFAULT = 0, IRONMAN = 1, GROUP_IRONMAN_SIZE_2 = 2 })

function log(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts + 1] = tostring(select(i, ...)) end
    io.stderr:write("[log] ", table.concat(parts, " "), "\n")
end

-- Like the real formatStr, reject any "{" that isn't part of a "{}" placeholder
-- (the client errors with "invalid format string" / "missing '}'").
function formatStr(pattern, ...)
    assert(not pattern:gsub("{}", ""):find("[{}]"), "invalid format string: " .. pattern)
    local args, i = { ... }, 0
    return (pattern:gsub("{}", function()
        i = i + 1
        return tostring(args[i])
    end))
end

local levels = { [0] = { 90, 5346332 }, [8] = { 85, 3258594 }, [21] = { 93, 7195629 } }
-- The own PlayerEntity's tile and route (CoordGrid: level, x, z); a test moves the player by setting these.
mock.pos = { level = 0, x = 3200, z = 3200 }
mock.route = {}
Player = {
    IsAvailable = function() return true end,
    GetEntity = function() return { displayName = "PlayerOne", coordGrid = mock.pos, route = mock.route,
                                    routeEnd = mock.route[#mock.route] or mock.pos } end,
    GetLevel = function(s) return math.min(levels[s][1], 99) end,
    GetLevelUncapped = function(s) return levels[s][1] end,
    GetXPUncapped = function(s) return levels[s][2] end,
    GetCombatLevel = function() return 134 end,
}

-- `filler` adds that many completed achievements so the snapshot is big enough to need chunking.
local function configTable(list, filler)
    for i = 1, filler or 0 do
        list[#list + 1] = { id = 1000 + i, configName = "CHEEVO_FILLER_" .. i,
            -- Accented names make sure chunk boundaries never split a UTF-8 character.
            name = "Filler achievement número " .. i .. " café", runeScore = 5, progress = -2, tallyProgress = -1, tallyTarget = -1 }
    end
    local t = {}
    for _, c in ipairs(list) do t[c.configName] = c end
    t.FromID = function(id)
        for _, c in ipairs(list) do
            if c.id == id then return c end
        end
        return nil
    end
    return t
end

-- VarType proxies (VarPlayer): a quest's progress var and the vars achievement tests read.
local questVar = { id = 30, configName = "HERALDS_PROGRESS", value = 40, isVarBit = false, typeName = "VarPlayer" }
local tallyVar = { id = 31, configName = "HERALDS_POST_KILLS", value = 7, isVarBit = false, typeName = "VarPlayer" }
local baseVar = { id = 35, configName = "HERALDS_BASE", value = 10, isVarBit = false, typeName = "VarPlayer" }
local flagVar = { id = -32, configName = "HERALDS_FLAGS", value = 2, isVarBit = true, varBitMask = 7,
                  baseVar = baseVar, typeName = "VarPlayer" }
-- Reads nil through the API although the game has a value (a reported edge case); its base var reads fine.
local nullVar = { id = -34, configName = "HERALDS_BEACON_NULL", value = nil, isVarBit = true, varBitMask = 8,
                  baseVar = baseVar, typeName = "VarPlayer", valueType = 0 }
-- ParamType configs: QuestType:GetParamValue(param) returns the quest's value, else param.defaultValue.
local releaseYear = { id = 700, configName = "QUEST_RELEASE_YEAR", defaultValue = -1, valueType = 0 }
local questSeries = { id = 701, configName = "QUEST_SERIES_NAME", defaultValue = "", valueType = 36 }
local function withParams(quest, values)
    quest.params = {}
    for param in pairs(values) do quest.params[#quest.params + 1] = param end
    function quest:GetParamValue(param)
        local v = values[param]
        if v == nil then return param.defaultValue end
        return v
    end
    function quest:HasParam(param) return values[param] ~= nil end
    return quest
end
local heralds = withParams({ id = 501, configName = "HERALDS_OF_CRIMSON", name = "Heralds of Crimson", questPoints = 3,
    isMembers = true, type = 0, difficulty = 3, hasStartedQuest = true, hasFinishedQuest = false,
    canStartQuest = true, deprecated = false,
    masterQuestVars = { { varPlayer = questVar, startState = 1, endState = 100 } },
    preReqQuestPoints = 50, preReqQuests = {}, preReqSkills = { { skillType = 8, minimumLevel = 70 } },
    completedPreReqQuestPoints = true, completedPreReqQuests = {}, completedPreReqSkills = { true },
    startCoordGrids = { { level = 0, x = 3210, z = 3220 } }, viaCoordGrid = { level = 0, x = 3200, z = 3200 } },
    { [releaseYear] = 2024, [questSeries] = "Heralds" })
local coldFront = withParams({ id = 502, configName = "COLD_FRONT", name = "Cold Front", questPoints = 0,
    isMembers = false, type = 1, difficulty = 0, hasStartedQuest = false, hasFinishedQuest = false,
    canStartQuest = true, deprecated = false,
    masterQuestVars = { { varPlayer = { id = 33, configName = "COLD_FRONT_PROGRESS", value = 0, isVarBit = false },
                         startState = 1, endState = 10 } },
    preReqQuestPoints = 0, preReqQuests = { heralds }, preReqSkills = {}, startCoordGrids = {} }, {})
-- Prerequisite quests are listed on the quest configs they gate (a 1-based Lua list), as in the API.
coldFront.completedPreReqQuests = { false }
config = {
    Quest = configTable({ heralds, coldFront }),
    Param = configTable({ releaseYear, questSeries }),
    Achievement = configTable({
        { id = 1, configName = "CHEEVO_DONE", name = "Done Already", runeScore = 10, progress = -2,
          tallyProgress = -1, tallyTarget = -1, isMembers = true, category = 3, subCategory = 12, reward = "",
          hiddenType = 0, graceExpiryRunedate = -1, descriptions = { { type = 0, text = "Already done." } } },
        { id = 2, configName = "CHEEVO_WC90", name = "Lumberjack Ninety", runeScore = 25, progress = 0,
          tallyProgress = -1, tallyTarget = -1, completionCountForRequirementGroups = 1, prereqsComplete = true,
          isMembers = true, category = 3, subCategory = 12, reward = "A hat<br>Some logs", hiddenType = 0,
          graceExpiryRunedate = -1,
          descriptions = { { type = 0, text = "Reach 90 Woodcutting." }, { type = 1, text = "Reach 90 Woodcutting (ironman)." } },
          requirementGroupCount = 1, prereqGroupCount = 0, completionCountForPrereqGroups = 0,
          GetSubItemCompletionCountForRequirementGroup = function(self, group) return 2 end,
          requirements = {
              skills = { { group = 0, skillTypes = { 8 }, totalLevels = 90, isComplete = false } },
              quests = { { group = 0, quest = heralds, isComplete = false } },
          } },
        -- Post-quest achievement: a tally over one var and a flag bit in another, "any 1 of the 2" sub-items.
        { id = 4, configName = "CHEEVO_HERALDS_AFTER", name = "After the Heralds", runeScore = 15, progress = 3500,
          tallyProgress = 7, tallyTarget = 20, completionCountForRequirementGroups = 1, prereqsComplete = true,
          isMembers = true, category = 5, subCategory = -1, reward = "", hiddenType = 1, graceExpiryRunedate = -1,
          descriptions = { { type = 0, text = "Kill 20 heralds and light the beacon." } },
          requirementGroupCount = 2, prereqGroupCount = 1, completionCountForPrereqGroups = 1,
          GetSubItemCompletionCountForRequirementGroup = function(self, group) return 1 end,
          GetSubItemCompletionCountForPrereqGroup = function(self, group) return 1 end,
          prereqs = { quests = { { group = 0, quest = heralds, isComplete = false } } },
          requirements = {
              varTotals = { { group = 0, vars = { tallyVar }, totalValue = 20, description = "Heralds killed", isComplete = false } },
              varTestBits = { { group = 0, var = flagVar, bitIndex = 1, bitValue = 1, description = "Beacon lit", isComplete = true },
                              { group = 1, var = nullVar, bitIndex = 0, bitValue = 1, description = "Beacon seen", isComplete = true } },
          } },
        { id = 3, configName = "CHEEVO_OLD", name = "Gone", runeScore = 5, progress = -4 },
    }, 800),
    Obj = configTable({ { id = 1511, configName = "LOGS", name = "Logs" },
                        { id = 23716, configName = "LAMP_HUGE", name = "Huge XP lamp" },
                        { id = 4151, configName = "ABYSSAL_WHIP", name = "Abyssal whip" } }),
    Inv = { BANK = { id = 95, configName = "BANK" }, INV = { id = 93, configName = "INV" },
            WORN = { id = 94, configName = "WORN" }, TRADEOFFER = { id = 90, configName = "TRADEOFFER" },
            METAL_BANK = { id = 96, configName = "METAL_BANK" } },
}

local contents = {
    BANK = { { obj = { id = 1511, name = "Logs", configName = "LOGS" }, count = 120 },
             { obj = { id = 23716, name = "Huge XP lamp" }, count = 12 },
             { obj = { id = 9075, name = "Astral rune" }, count = 0 },  -- placeholder
             { obj = { id = -1, name = "" }, count = 0 } },              -- empty slot
    INV = { { obj = { id = 23716, name = "Huge XP lamp" }, count = 1 } },
    WORN = { { obj = { id = 9075, name = "Astral rune" }, count = 50 } },
    TRADEOFFER = { { obj = { id = 995, name = "Coins" }, count = 1000000 } },  -- must never be exported
}
-- Hand-listed VarBits are fields of bigger player variables, sent at their own width. Their bases and masks are
-- the game's (the 2026-11-17 var dump), since the exporter checks each against lib/exported_vars.lua; a VarBit's
-- `value` is its field's value. SMKI_2 holds the Slayer points (bits 0-16) and master (17-22) and, at bit 30, a
-- field nobody listed.
local smki2 = { id = 40, configName = "SMKI_2", value = 610 | (6 << 17) | (1 << 30) }
local function varBit(id, name, base, mask, value)
    return { id = id, configName = name, value = value, isVarBit = true, varBitMask = mask,
             baseVar = type(base) == "table" and base or { id = id + 1000, configName = base } }
end
var = { VarPlayer = configTable({
    { id = 1, configName = "QP", value = 473 },
    varBit(-2, "MINING_OREBOX_COPPER", "MINING_PARENT_10", 0x3FFF, 120),
    varBit(-3, "MINING_OREBOX_COPPER_UPGRADED", "MINING_PARENT_10", 0x1FC000, 100),
    { id = 7, configName = "MINING_OREBOX_GOLD_STONE_SPIRIT_VAR", value = 936 },
    { id = 4, configName = "FRIEND_SORT_MODE", value = 2 },  -- not exported
    { id = 5, configName = "DNDTRACKER_PENGUIN_HUNTING_FAV", value = 1 },  -- interface state: not exported
    varBit(-6, "TOKEN_DD_PENGUIN_HIDE_SEEK", "WOF23_TOKENS_DD", 0x80, 0),  -- a reset token used this week: no
    varBit(-8, "TOOLBELT_HAMMER", "TOOLBELT_FREE_TOOLS", 0x10000, 1),  -- a tool on the belt: listed by name
    { id = 21, configName = "TOOLBELT_LAST_SLOT", value = 5 },  -- interface state: not exported
    { id = 22, configName = "CHEEVO_TOOLBELT", value = 1 },  -- has the word in it, but is no tool: not exported
    varBit(-9, "ITEMLOG_ITEM_ABYSSAL_WHIP", "ITEMLOG_TRACKING207", 0xFFFF, 2),  -- collection log: 2 whips obtained
    { id = 10, configName = "ITEMLOG_ITEM_DRAGON_AXE", value = 0 },  -- made-up name
    { id = 11, configName = "INVENT_MATERIAL_BASE_PARTS", value = 3100 },  -- Invention materials storage
    { id = 12, configName = "INVENT_MATERIAL_JUNK", value = 900 },
    varBit(-13, "CRAMULET_HAS_CAMULET", "APMEKEN_VAR1", 0x1, 1),  -- cramulet: one flag per absorbed amulet
    varBit(-14, "CRAMULET_HAS_CATSPEAK", "APMEKEN_VAR1", 0x2, 0),
    varBit(-15, "STATUE_LAST_SKILL", "STATUE_MAIN", 0xFE0, 9),  -- last strange rock: Herblore (code 9, observed 2026-10-31)
    varBit(-16, "ELFCITY_STATUE_LAST_SKILL", "ELFCITY_PERM4", 0x3E0, 14),  -- last golden rock: Smithing (8 + index 6)
    varBit(-17, "SMKI_SLAYER_POINTS", smki2, 0x1FFFF, 610),  -- Slayer points balance
    { id = 24, configName = "RAND_REWARD_TOKENS", value = 316448 },  -- Dungeoneering tokens balance
    varBit(-25, "BSLAY_POINTS", "BSLAY", 0x1FFF8000, 264),  -- Reaper points
    varBit(-18, "PVM_HUB_CAMPFIRE_STATE", "PVM_HUB_1", 0x1E00000, 6),  -- War's Retreat campfire: elder (assumed encoding)
    { id = 19, configName = "BONUSXP_ATTACK", value = 25000 },  -- banked bonus xp
    { id = 21, configName = "SLAYER_TARGET", value = 42 },  -- Slayer assignment: abyssal demons, 37 left, from Kuradal
    { id = 22, configName = "SLAYER_COUNT", value = 37 },
    varBit(-23, "SLAYER_MASTER", smki2, 0x7E0000, 6),
    { id = 20, configName = "BONUSXP_ATTACK_STORED", value = 0 },
    questVar, tallyVar, flagVar, nullVar, baseVar, smki2,
}), VarClient = configTable({
    { id = 1, configName = "STATUE_BAG_CLIENT", value = 1022623217 },  -- client copy of an item's obj var
}) }

Inventory = {
    Find = function(inv)
        local slots = contents[inv.configName]
        if not slots then return nil end
        return { isValid = true, GetSlots = function() return slots end }
    end,
}

-- Active locs in the world (LocEntity shapes); a test adds to mock.locs. ByDistance with range 0 matches
-- the exact tile on the same level, as the docs say.
mock.locs = {}
LocList = {
    ByDistance = function(coord, range, sameLevel)
        local out = {}
        for _, l in ipairs(mock.locs) do
            local c = l.coordGrid
            if math.abs(c.x - coord.x) <= range and math.abs(c.z - coord.z) <= range
                and (not sameLevel or c.level == coord.level) then
                out[#out + 1] = l
            end
        end
        return out
    end,
}
-- CoordGrid.new(packed): the one-argument constructor, packed as (level << 28) | (x << 14) | z.
CoordGrid = { new = function(packed)
    return { level = packed >> 28, x = (packed >> 14) & 0x3FFF, z = packed & 0x3FFF }
end }

-- Mirrors the real store's size limit closely enough to exercise chunking.
mock.MAX_VALUE_BYTES = 100000
World = { IsMembers = function() return mock.membersWorld end }
mock.membersWorld = true

PersistentDB = {}
function PersistentDB:SetString(key, value)
    if #value > mock.MAX_VALUE_BYTES then return false end
    mock.store[key] = value
    return true
end
function PersistentDB:GetString(key)
    return mock.store[key]
end
function PersistentDB:Delete(key)
    mock.store[key] = nil
    return true
end
-- Typed numbers: the store keeps the Lua value; a typed Get returns nil on a type mismatch, as documented.
function PersistentDB:SetFloat(key, value) mock.store[key] = value + 0.0 return true end
function PersistentDB:SetInt(key, value) mock.store[key] = math.tointeger(value) return true end
function PersistentDB:GetFloat(key)
    local v = mock.store[key]
    return (type(v) == "number" and math.type(v) == "float") and v or nil
end
function PersistentDB:GetInt(key)
    local v = mock.store[key]
    return (type(v) == "number" and math.type(v) == "integer") and v or nil
end

Event = setmetatable({}, {
    __index = function(t, name)
        local ev = {
            Subscribe = function(ref, fn)
                mock.handlers[name] = mock.handlers[name] or {}
                mock.handlers[name][ref] = fn
            end,
            Unsubscribe = function(ref)
                if mock.handlers[name] then mock.handlers[name][ref] = nil end
            end,
        }
        rawset(t, name, ev)
        return ev
    end,
})

-- Chat (Event.ChatMessage) and the optional permission it needs; mock.chat_granted turns it off.
ChatType = { GENERAL = 0, PUBLIC = 2, PRIVATE_INCOMING = 3, ENGINE = 4, FRIEND_CHAT = 9, SPAM = 109,
             BROADCAST = 125, GENERAL_LINKABLE = 145, NPC_CHAT = 116 }
PluginPermissions = { httpAccess = 0, chatAccess = 1, trackingAccess = 2 }
mock.chat_granted = true
mock.http_granted = true
Plugin = {
    HasPermission = function(p)
        if p == PluginPermissions.chatAccess then return mock.chat_granted end
        if p == PluginPermissions.httpAccess then return mock.http_granted end
        return true
    end,
    -- The manifest's `urls`, as the client exposes them (PluginURL values; here plain strings).
    urls = { api = "https://api.runeadvisor.com" },
}

-- HTTP (core/http.html): a request posts to mock.http_handler(request, body) -> statusCode, responseBody
-- (nil: no response, as for a timeout) and runs the completion callback at once. Every request is logged
-- in mock.http_log with its url, headers and body.
mock.http_log = {}
mock.http_handler = nil
HTTPRequest = {}
function HTTPRequest.new(base, path)
    local r = { url = tostring(base) .. (path or ""), headers = {}, successful = false, failed = false,
                finished = false, cancelled = false, timedOut = false, responseCode = 0, data = "", header = "",
                errorCode = 0, errorInfo = "", active = false }
    function r:AddHeader(key, value)
        assert(not self.active, "AddHeader on an active request")
        self.headers[key] = value
    end
    local function run(self, body, done)
        assert(not self.active, "Post/Get on an active request")
        self.active = true
        mock.http_log[#mock.http_log + 1] = { url = self.url, headers = self.headers, body = body }
        local code, data = nil, nil
        if mock.http_handler then code, data = mock.http_handler(self, body) end
        self.finished, self.active = true, false
        if code then
            self.responseCode, self.data, self.successful = code, data or "", code < 400
            self.failed = code >= 400
        else
            self.failed, self.timedOut, self.errorInfo = true, true, "timed out"
        end
        if done then done(self) end
    end
    function r:Post(body, done) run(self, body, done) end
    function r:Get(done) run(self, nil, done) end
    function r:Cancel() self.cancelled = true end
    return r
end

-- The console (core ExecuteConsoleCommand): commands are recorded in mock.console; mock.console_handler(command),
-- when set, plays the game's side (a cheat changing a var or an inventory).
mock.console = {}
mock.console_handler = nil
function ExecuteConsoleCommand(command)
    mock.console[#mock.console + 1] = command
    if mock.console_handler then mock.console_handler(command) end
end

-- Just enough ui.* for the session recorder's panel: components remember their hooks so a test
-- can "press" a button with mock.press(button).
local function component()
    local c = { hooks = {} }
    function c:SetSize() end
    function c:SetPos() end
    function c:SetHeaderLayout() end
    function c:Subscribe(hook, name, fn) self.hooks[hook] = fn or name end
    return c
end
ui = {
    -- One argument: a combined component id (the plugin layer); two or more: interface id, static id (and, for a
    -- dynamic component, category and dynamic ids), looked up in mock.interfaces.
    Interfaces = { GetComponent = function(self, interfaceID, staticID, categoryID, dynamicID)
                       if staticID == nil then return component() end
                       local iface = ui_interfaces_get(self, interfaceID)
                       local c = iface and iface:GetComponent(staticID)
                       if c and categoryID ~= nil and dynamicID ~= nil then
                           local found
                           c:IterateDynamicComponents(function(child)
                               if child.categoryID == categoryID and child.dynamicID == dynamicID then found = child return true end
                               return false
                           end)
                           return found
                       end
                       return c
                   end,
                   GetInterface = function(self, interfaceID) return ui_interfaces_get(self, interfaceID) end },
    GroupBox = { new = component }, Text = { new = component }, Layer = { new = component },
    InputField = { new = function() local c = component() c.content = "" return c end },
    Button = { new = function() local c = component() c.isToggled = false return c end },
    Hook = { ONBUTTONPRESSED = 1, ONCONTENTCHANGED = 2 },
}
-- One fake game interface for UI scans (ui.Interfaces:GetInterface): mock.interfaces[id] = { [staticID] = component },
-- a component being { type, content, dynamic = { child components } }.
mock.interfaces = {}
local function fakeComponent(spec, interfaceID, staticID, categoryID, dynamicID)
    local c = { type = spec.type or 4, content = spec.content, interfaceID = interfaceID, staticID = staticID,
                categoryID = categoryID or -1, dynamicID = dynamicID or -1, associatedObjectID = spec.object or -1 }
    function c:IterateDynamicComponents(fn)
        for i, child in ipairs(spec.dynamic or {}) do
            if fn(fakeComponent(child, interfaceID, staticID, 0, i - 1), true) then return end
        end
    end
    return c
end
ui_interfaces_get = function(_, interfaceID)
    local spec = mock.interfaces[interfaceID]
    if not spec then return nil end
    return { GetComponent = function(_, staticID)
        local c = spec[staticID]
        return c and fakeComponent(c, interfaceID, staticID) or nil
    end }
end
id = { Component = { TOPLEVEL_V2__PLUGIN_BUILD_LAYER_TOP = 1 }, Font = { MUSEO_SANS_15PT_REGULAR = 1 },
       Interface = { BUFF_BAR = 284, CHATBOX = 137 },
       StyleSheet = { BUTTON_DEFAULT = 1, INPUT_DEFAULT = 2, GROUPBOX_DEFAULT = 3 } }

-- On the real client the callback's toggle flag read "on" for the Stop press too (session 2026-10-29),
-- so the mock always passes true: handlers must track their own state.
function mock.press(button)
    button.hooks[ui.Hook.ONBUTTONPRESSED](button, true)
end

mock.contents = contents
mock.levels = levels

-- A fake Rune Advisor API for mock.http_handler: accepts any token check, records every state post in
-- mock.store as wire:<player>:post:<n> (header JSON with the body in base64 and an "accepted" flag, the
-- keys the desktop replay reads) and plays the server's hash checks: a full post is always accepted, a delta
-- only when its base hash is the server's current hash.
function mock.fakeServer()
    local servers = {}
    return function(request, body)
        if not body then return 200, '{"players":[]}' end
        local header = json_decode_for_mock(body)
        local player = header.player or "?"
        local server = servers[player] or { hash = nil, posts = 0 }
        servers[player] = server
        server.posts = server.posts + 1
        local accepted = header.kind == "full" or (server.hash ~= nil and header.base == server.hash)
        header.accepted = accepted
        if accepted then server.hash = header.new end
        mock.store[string.format("wire:%s:post:%d", player, server.posts)] = json_encode_for_mock(header)
        mock.store["wire:" .. player .. ":server"] = json_encode_for_mock({ hash = server.hash, posts = server.posts })
        if accepted then return 200, '{"accepted":true}' end
        return 409, '{"accepted":false,"reason":"base hash is not the server\'s state"}'
    end
end

function mock.fire(name, payload)
    for _, fn in pairs(mock.handlers[name] or {}) do fn(payload or {}) end
end

return mock
