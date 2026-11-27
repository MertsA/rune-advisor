-- Values the game shows on its own interface and nowhere the API can read otherwise. Read only: nothing on the
-- interface is changed, and only the numbers below are kept. Jagex's API documentation describes this use: plugins
-- may "scan the built-in UI for data that might not yet be exposed in an easier-to-access manner".
--
-- What is read:
--   porter charges   the number on the sign of the porter's buff icon (by the buff bar), shown while a porter is
--                    active: a worn sign's charges, or all the charges a Grace of the Elves holds. The charges live
--                    on the item itself, which the API can't read (checked 2026-11-26). The icon is the same for
--                    every porter tier (item 51490, sign VII), so it marks the porter's buff entry wherever it sits.
--                    With both a sign and the Grace worn the number is their total (the sign's charges are used
--                    first): what the planner wants, every charge held. With no porter active the entry isn't there:
--                    the value is then unknown (-1) and the server keeps the last one it accepted.
--   the Slayer helmet on the Anachronia base camp's stand   "Check" on the stand opens a dialog: "The following
--                    helmet is stored on the stand:<br>Reinforced slayer helmet". No variable says which helmet it
--                    is (DINO_BASE_CAMP_SLAYER_STAND_STATE only says 1 = built and empty, 2 = a helmet on it), and the
--                    stand's inventory never reaches the client (both checked 2026-11-27). The dialog's helmet name
--                    is kept as a small code, tier * 8 + suffix (HELMET_TIERS, HELMET_SUFFIXES; the colour is left
--                    out: it changes nothing): -1 until the dialog has been seen this session (the server keeps the
--                    last value), 0 once the stand's state changes (a helmet put on or taken off: unknown until the
--                    next check, so the planner can ask for one).
-- Each value is posted as a hand-listed field of kind "ui" (lib/exported_vars.lua), read before every post; the
-- helmet is looked for on every server tick, since the dialog is shown only briefly.

local M = {}

M.FIELDS = { UI_PORTER_CHARGES = true, UI_SLAYER_STAND_HELMET = true }

local BUFF_INTERFACE = 284     -- the buff bar's interface
local PORTER_ICON = 51490      -- the item shown on the porter buff's icon, whatever the porter's tier
local MAX_STATIC = 300         -- static components tried on the buff bar
local STATIC_GAP = 60          -- stop after this many missing static ids in a row

local function call(fn)
    local ok, value = pcall(fn)
    if ok then return value end
    return nil
end

-- The porter buff entry's number, or nil when it isn't shown.
local function porterCharges()
    local gap = 0
    for staticID = 0, MAX_STATIC do
        local entry = call(function() return ui.Interfaces:GetComponent(BUFF_INTERFACE, staticID) end)
        if entry then
            gap = 0
            -- The number is the first text after the porter icon, in child order: if several buffs share this
            -- component, another buff's number is never taken.
            local afterIcon, number = false, nil
            call(function()
                entry:IterateDynamicComponents(function(child)
                    local object = call(function() return child.associatedObjectID end)
                    if object == PORTER_ICON then
                        afterIcon = true
                        return false
                    end
                    local content = call(function() return child.content end)
                    if afterIcon and type(content) == "string" and content:match("^%d+$") then
                        number = tonumber(content)
                        return true
                    end
                    return false
                end)
            end)
            if number then return math.tointeger(number) end
        else
            gap = gap + 1
            if gap >= STATIC_GAP then break end
        end
    end
    return nil
end

-- The Slayer helmet stand's dialog.
local STAND_DIALOG = 1189      -- the dialog interface "Check" opens
local STAND_TEXT = 3           -- its text component
local STAND_STATE_VAR = "DINO_BASE_CAMP_SLAYER_STAND_STATE"
M.HELMET_TIERS = { "Slayer helmet", "Full slayer helmet", "Reinforced slayer helmet", "Strong slayer helmet",
                   "Mighty slayer helmet", "Corrupted slayer helmet" }
M.HELMET_SUFFIXES = { "", "(c)", "(e)", "(f)", "(cf)", "(ef)" }

--- A helmet's code from its name (tier * 8 + suffix index, both counted from 1 and 0), or nil for a name that
--- isn't a slayer helmet this list knows.
function M.helmetCode(name)
    name = name:gsub("<[^>]*>", ""):gsub(" %((%a+)%)", function(word)
        if word == "red" or word == "blue" or word == "green" or word == "yellow" then return "" end
    end):gsub("^%s+", ""):gsub("%s+$", "")
    for tier, base in ipairs(M.HELMET_TIERS) do
        if name:sub(1, #base):lower() == base:lower() then
            local rest = name:sub(#base + 1):gsub("^%s+", "")
            for i, suffix in ipairs(M.HELMET_SUFFIXES) do
                if rest == suffix then return tier * 8 + (i - 1) end
            end
        end
    end
    return nil
end

local helmet = nil      -- the stand's helmet code this session: nil = not seen, 0 = changed since
local standState = nil  -- the stand's state last tick

--- Once a server tick: forget the helmet when the stand's state changes, and read the dialog if it is shown.
function M.tick()
    local okState, state = pcall(function() return var.VarPlayer[STAND_STATE_VAR].value end)
    if okState and type(state) == "number" then
        if standState ~= nil and state ~= standState then helmet = 0 end
        standState = state
    end
    local text = call(function() return ui.Interfaces:GetComponent(STAND_DIALOG, STAND_TEXT).content end)
    if type(text) == "string" then
        local name = text:match("[Ss]tored on the stand:%s*<br>%s*(.-)%s*$")
        local code = name and M.helmetCode(name)
        if code then helmet = code end
    end
end

local READERS = {
    UI_PORTER_CHARGES = porterCharges,
    UI_SLAYER_STAND_HELMET = function() return helmet end,
}

--- The current value of a ui field, or -1 when the game isn't showing it.
function M.value(name)
    local reader = READERS[name]
    local value = reader and reader()
    if value == nil then return -1 end
    return value
end

function M.start()
    pcall(function() Event.ServerLogic.Subscribe("runeadvisor-uiread", M.tick) end)
end

return M
