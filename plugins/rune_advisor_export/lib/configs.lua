-- Enumerate every config of one kind (e.g. config.Quest, config.Achievement, var.VarPlayer).
--
-- The API docs show lookup by name and by FromID but don't document iteration; on the client pairs()
-- over a config table works. Seen there: 382 quests, 5,025 achievements, 948 inventory names and 62,360
-- player var names. Only the configs are listed here; nothing about the player is read.

local configs = {}

--- Returns an array of the table's configs.
function configs.all(configTable)
    local found = {}
    for _, cfg in pairs(configTable) do
        -- Skip helper functions such as FromID; keep only config proxies.
        if type(cfg) ~= "function" and type(cfg) ~= "nil" then
            found[#found + 1] = cfg
        end
    end
    return found
end

return configs
