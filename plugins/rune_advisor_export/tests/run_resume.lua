-- Resuming from a saved state checks it with the server first. From the repo root:
--   lua5.4 plugins/rune_advisor_export/tests/run_resume.lua
-- A relogin resumes from the last accepted state (PersistentDB) and posts changes only. The server may not hold
-- that state any more: the API address changed (2026-11-27: pointed back at the live API after a local test
-- server, the exporter resumed and posted nothing, so the live server kept stale data), or the server lost its
-- copy. So the first post after resuming goes out even with nothing changed: an empty delta on the saved hash.
package.path = "plugins/rune_advisor_export/?.lua;" .. package.path
local mock = dofile("plugins/tests/mock_api.lua")
local main = dofile("plugins/rune_advisor_export/main.lua")
local W = main.wire
local settings = require("lib/settings")

local function ticks(n)
    for _ = 1, n do
        for _ = 1, 30 do mock.fire("Logic") end
        mock.fire("ServerLogic")
    end
end
-- Every request the plugin sends, as the fake server answered it: { kind, accepted }.
local seen = {}
local function server()
    local fake = mock.fakeServer()
    return function(request, body)
        local code, reply = fake(request, body)
        if body then seen[#seen + 1] = { body:match('"kind":"(%a+)"'), code == 200 } end
        return code, reply
    end
end
local function since(n)
    local out = {}
    for i = n + 1, #seen do out[#out + 1] = seen[i][1] .. (seen[i][2] and "+" or "-") end
    return table.concat(out, " ")
end

mock.http_handler = server()
mock.fire("SettingsLayerReady", { component = {} })
settings.field.content = "tok-test"
settings.field.hooks[ui.Hook.ONCONTENTCHANGED](settings.field)
ticks(40)
assert(W.state.acked and since(0) == "full+", "the first login posts the full state: " .. since(0))

-- Relogin with the same server and nothing changed: one empty check delta, accepted; then quiet.
local mark = #seen
mock.fire("GameStateChanged", { newState = GameState.GAME })
ticks(40)
assert(since(mark) == "delta+", "a resume checks once with the server: " .. since(mark))
assert(not W.state.check, "checked")

-- Relogin against a server without this state (another address, or a server that lost it): nothing changed in
-- the game, yet the check is refused (409) and the full state follows at once.
mark = #seen
mock.http_handler = server()
mock.fire("GameStateChanged", { newState = GameState.GAME })
ticks(40)
assert(since(mark) == "delta- full+", "a server without the state gets the full state: " .. since(mark))
print("ok")
