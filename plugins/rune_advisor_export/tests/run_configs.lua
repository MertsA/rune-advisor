-- Print the mock game's quest and achievement config entries (lib/configdump.lua), one "key<TAB>value" per
-- line, as the JSON arrays the desktop side's tests read as game configs.
-- From the repo root:  lua5.4 plugins/rune_advisor_export/tests/run_configs.lua
package.path = "plugins/rune_advisor_export/?.lua;" .. package.path
dofile("plugins/tests/mock_api.lua")
local json = require("lib/json")
local configs = require("lib/configs")
local configdump = require("lib/configdump")

for _, kind in ipairs({ "quests", "achievements" }) do
    local spec = configdump.KINDS[kind]
    local parts = {}
    for _, cfg in ipairs(configs.all(spec.table())) do
        local entry = spec.build(cfg)
        if entry then parts[#parts + 1] = json.encode(entry) end
    end
    print("config:" .. kind .. "\t[" .. table.concat(parts, ",") .. "]")
end
