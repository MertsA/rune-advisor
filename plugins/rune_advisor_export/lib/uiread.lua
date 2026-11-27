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
-- Each value is posted as a hand-listed field of kind "ui" (lib/exported_vars.lua), read fresh before every post.

local M = {}

M.FIELDS = { UI_PORTER_CHARGES = true }

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

local READERS = { UI_PORTER_CHARGES = porterCharges }

--- The current value of a ui field, or -1 when the game isn't showing it.
function M.value(name)
    local reader = READERS[name]
    local value = reader and reader()
    if value == nil then return -1 end
    return value
end

return M
