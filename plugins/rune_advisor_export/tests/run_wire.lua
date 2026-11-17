-- Drive the exporter's wire protocol against the mock API and print what the mock transport recorded.
-- From the repo root:  lua5.4 plugins/rune_advisor_export/tests/run_wire.lua
package.path = "plugins/rune_advisor_export/?.lua;" .. package.path
local mock = dofile("plugins/tests/mock_api.lua")
local main = dofile("plugins/rune_advisor_export/main.lua")
local W = main.wire
local settings = require("lib/settings")

-- One server tick: the 30 client logic ticks that pass in 0.6 s (the jobs run on those), then ServerLogic.
local function ticks(n)
    for _ = 1, n do
        for _ = 1, 30 do mock.fire("Logic") end
        mock.fire("ServerLogic")
    end
end
-- The plugin posts nothing until a token is saved: paste one in the settings layer (the fake server accepts
-- any token check), then every state post is recorded in mock.store like the real server would see it.
mock.http_handler = mock.fakeServer()
mock.fire("SettingsLayerReady", { component = {} })
settings.field.content = "tok-test"
settings.field.hooks[ui.Hook.ONCONTENTCHANGED](settings.field)
ticks(3)
assert(mock.store["settings:apiToken"] == "tok-test", "token saved after the check")
local function posts()
    local out = {}
    for k, v in pairs(mock.store) do
        local n = k:match("^wire:PlayerOne:post:(%d+)$")
        if n then out[tonumber(n)] = v end
    end
    return out
end
local function kinds()
    local ks = {}
    for i, v in ipairs(posts()) do ks[i] = v:match('"kind":"(%a+)"') .. (v:find('"accepted":true') and "+" or "-") end
    return table.concat(ks, " ")
end

-- Login already happened at load: the layout builds and the baseline reads over a few ticks, then the
-- first post goes out on the next post interval.
ticks(100)  -- 60 ticks of start delay, then the baseline jobs
assert(W.state.ready, "wire ready after the baseline jobs")
assert(kinds() == "full+", "first post is the accepted full state, got: " .. kinds())
local layout = W.state.layout
local counts = {}
for _, slot in ipairs(layout.slots) do counts[slot.kind] = (counts[slot.kind] or 0) + 1 end
assert(counts.base == 4 and counts.skill == 29 and counts.members == 1 and counts.named >= 10,
    string.format("base %s named %s skill %s members %s", counts.base, counts.named, counts.skill, counts.members))
local hasBag = false
for _, n in ipairs(W.state.named) do if n == "STATUE_BAG_CLIENT" then hasBag = true end end
assert(hasBag, "the VarClient statue bag mirror is in the named section (client-only, re-read before each post)")

-- A base var transmitted with the same value: nothing changes, nothing is posted.
mock.fire("VarTransmit", { var = { configName = "HERALDS_BASE" }, oldValue = 10, newValue = 10 })
ticks(17)
assert(kinds() == "full+", "no delta without a change")
-- The kill tally moves 7 -> 9: one slot changes.
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 7, newValue = 9 })
ticks(17)
assert(kinds() == "full+ delta+", "a changed slot posts a delta: " .. kinds())
-- Past the ceiling (5 bits: 31): 40 saturates to 31 and posts; 45 repacks to 31 again and doesn't.
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 9, newValue = 40 })
ticks(17)
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 40, newValue = 45 })
ticks(17)
assert(kinds() == "full+ delta+ delta+", "a saturated counter incrementing is no change: " .. kinds())
-- Inventory as a set: picking up logs is one entry; reshuffling the backpack is nothing.
table.insert(mock.contents.INV, 1, { obj = { id = 1511, name = "Logs" }, count = 5 })
mock.fire("InventoryUpdated", { inv = config.Inv.INV, slotsUpdated = { 1, 2 } })
ticks(17)
local p4 = posts()[4]
assert(p4 and p4:find('"items":1') and p4:find('"slots":0'), "one inventory entry: " .. tostring(p4))
mock.contents.INV[1], mock.contents.INV[2] = mock.contents.INV[2], mock.contents.INV[1]
mock.fire("InventoryUpdated", { inv = config.Inv.INV, slotsUpdated = { 1, 2 } })
ticks(17)
assert(#posts() == 4, "a reorder posts nothing")
-- Xp lands in a skill slot.
mock.fire("SkillTransmit", { skill = 0, oldXP = 5346332, newXP = 5347000 })
ticks(17)
assert(#posts() == 5, "xp posts a delta")
-- Another computer posted in between: our base hash no longer matches, the delta is rejected and the
-- full state follows.
W.state.acked.hash = 12345
mock.fire("VarTransmit", { var = { configName = "HERALDS_PROGRESS" }, oldValue = 40, newValue = 41 })
ticks(17)
ticks(17)
assert(kinds() == "full+ delta+ delta+ delta+ delta+ delta- full+", "reject then resync: " .. kinds())
assert(mock.store["wire:PlayerOne:acked"]:find('"hash":'), "the acknowledged state is persisted")

-- An inventory loaded for the first time, and empty (only a placeholder): the delta announces it as known
-- (one entry, the 0xFFFFFFFF marker) so the server turns "unknown" into an empty set, as the hash expects.
mock.contents.METAL_BANK = { { obj = { id = 2349, name = "Bronze bar" }, count = 0 } }
mock.fire("InventoryAdded", { inv = config.Inv.METAL_BANK, slotsUpdated = { 1 } })
ticks(17)
local p8 = posts()[8]
assert(p8 and p8:find('"kind":"delta"') and p8:find('"items":1') and p8:find('"accepted":true'), "known-empty marker: " .. tostring(p8))

-- Logging in again with the bank not yet opened: the acknowledged state seeds it, so the first delta of the
-- session doesn't report the bank emptied (only the var that changed).
mock.contents.BANK = nil
-- The mock's vars and xp don't move with the events fired above (the real client's do), so bring them to the
-- values the events reported before the baseline re-reads them.
var.VarPlayer.HERALDS_POST_KILLS.value, var.VarPlayer.HERALDS_PROGRESS.value = 45, 41
mock.levels[0][2] = 5347000
mock.fire("GameStateChanged", { newState = GameState.GAME })
ticks(40)
assert(W.state.ready and W.state.acked, "resumed from the acknowledged state")
assert(W.state.invs.BANK and W.state.invs.BANK[1511] == 120, "the unloaded bank is seeded from the last post")
mock.fire("VarTransmit", { var = { configName = "HERALDS_PROGRESS" }, oldValue = 41, newValue = 42 })
ticks(17)
local p9 = posts()[9]
assert(p9 and p9:find('"kind":"delta"') and p9:find('"items":0') and p9:find('"slots":1') and p9:find('"accepted":true'),
    "after a relogin only the var moves: " .. tostring(p9))
mock.contents.BANK = { { obj = { id = 1511, name = "Logs", configName = "LOGS" }, count = 120 },
                       { obj = { id = 23716, name = "Huge XP lamp" }, count = 12 } }

local keys = {}
for k in pairs(mock.store) do keys[#keys + 1] = k end
table.sort(keys)
for _, k in ipairs(keys) do io.write(k, "\t", mock.store[k], "\n") end
