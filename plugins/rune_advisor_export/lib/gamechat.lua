-- The game's own messages, for values the game tells the player only in chat. Optional: only with the chatAccess
-- permission, which the plugin asks for as optional (it works fully without it; these values are then unknown).
--
-- Every message is checked in this order, and dropped at the first step it fails:
--   1. it has a sender: it was written by a player (public, private, clan, group, trade...). Dropped at once,
--      before anything else is looked at.
--   2. its type is not one of the game's own message types (GAME_TYPES).
--   3. its text doesn't match one of the patterns below.
-- Only the numbers a pattern finds are kept; no message text is stored, logged or sent.
--
-- What is read:
--   rested experience   "Working in the fort increases your rested XP (3/1500)": the balance and the cap of Fort
--                       Forinthry's rested experience. The game sends this as a message and in no player variable
--                       (checked 2026-11-26), so chat is the only place the client learns it. Cashing it in with
--                       Aster sends no message: the balance then stays at the last one seen until the next gain.
-- Each value is posted as a hand-listed field of kind "chat" (lib/exported_vars.lua): -1 until a message has been
-- seen this session, and the server keeps the value it last accepted meanwhile.

local M = {}

M.GAME_TYPES = { "GENERAL", "ENGINE", "SPAM", "GENERAL_LINKABLE" }

-- name -> value (nil = not seen this session)
local values = {}

-- The patterns: each returns the fields it sets, or nil. Matched on the game's own text only (step 3).
local PATTERNS = {
    function(text)
        local balance, cap = text:match("[Rr]ested XP %((%d[%d,]*)/(%d[%d,]*)%)")
        if not balance then return nil end
        return { CHAT_RESTED_XP = tonumber((balance:gsub(",", ""))), CHAT_RESTED_XP_CAP = tonumber((cap:gsub(",", ""))) }
    end,
}

--- The value of a chat field, or -1 when no message has told it yet.
function M.value(name)
    local v = values[name]
    if v == nil then return -1 end
    return v
end

--- True for the field names this module fills.
M.FIELDS = { CHAT_RESTED_XP = true, CHAT_RESTED_XP_CAP = true }

local gameTypes = {}  -- ChatType value -> true

function M.onMessage(e)
    -- 1. A player's message: dropped before its type or text is read.
    local okSender, sender, senderUnfiltered = pcall(function() return e.senderName, e.senderNameUnfiltered end)
    if not okSender then return end
    if (sender ~= nil and sender ~= "") or (senderUnfiltered ~= nil and senderUnfiltered ~= "") then return end
    -- 2. Only the game's own message types.
    local okType, kind = pcall(function() return e.chatType end)
    if not okType or not gameTypes[kind] then return end
    -- 3. Only text a pattern recognises; only its numbers are kept.
    local okText, text = pcall(function() return e.chatMessage end)
    if not okText or type(text) ~= "string" then return end
    for _, pattern in ipairs(PATTERNS) do
        local found = pattern(text)
        if found then
            for name, v in pairs(found) do
                if math.tointeger(v) then values[name] = math.tointeger(v) end
            end
            return
        end
    end
end

--- Subscribe while chatAccess is granted. It can be granted (or withdrawn) while the plugin runs:
--- Event.OptionalPermissionChanged switches the subscription.
function M.start()
    for _, name in ipairs(M.GAME_TYPES) do
        local ok, value = pcall(function() return ChatType[name] end)
        if ok and value ~= nil then gameTypes[value] = true end
    end
    local subscribed = false
    local function set(on)
        if on and not subscribed then
            Event.ChatMessage.Subscribe("runeadvisor-gamechat", M.onMessage)
        elseif not on and subscribed then
            Event.ChatMessage.Unsubscribe("runeadvisor-gamechat")
        end
        subscribed = on
    end
    local ok, granted = pcall(function() return Plugin.HasPermission(PluginPermissions.chatAccess) end)
    set(ok and granted == true)
    pcall(function()
        Event.OptionalPermissionChanged.Subscribe("runeadvisor-gamechat-perm", function(e)
            if tostring(e.permissionName):lower():find("chat") then set(e.enabled and true or false) end
        end)
    end)
end

return M
