-- Drive the exporter's HTTP transport against the mock API: posts go to PersistentDB until a token is
-- pasted into the panel and accepted by the API, then to the API, with the server's answers steering what
-- is sent next and how often.
-- From the repo root:  lua5.4 plugins/rune_advisor_export/tests/run_http.lua
package.path = "plugins/rune_advisor_export/?.lua;" .. package.path
local mock = dofile("plugins/tests/mock_api.lua")
local main = dofile("plugins/rune_advisor_export/main.lua")
local W = main.wire
local settings = require("lib/settings")
-- The client fires SettingsLayerReady when the user opens the plugin's settings in the Plugin Hub.
mock.fire("SettingsLayerReady", { component = {} })
assert(settings.field, "the settings layer was populated")

-- One server tick: the 30 client logic ticks that pass in 0.6 s (the jobs run on those), then ServerLogic.
local function ticks(n)
    for _ = 1, n do
        for _ = 1, 30 do mock.fire("Logic") end
        mock.fire("ServerLogic")
    end
end
local function localPosts()
    local n = 0
    for k in pairs(mock.store) do if k:match("^wire:PlayerOne:post:%d+$") then n = n + 1 end end
    return n
end
-- The API log split by method: POSTs carry a body, the token checks are GETs.
local function apiPosts()
    local out = {}
    for _, r in ipairs(mock.http_log) do if r.body then out[#out + 1] = r end end
    return out
end
local function apiChecks()
    local out = {}
    for _, r in ipairs(mock.http_log) do if not r.body then out[#out + 1] = r end end
    return out
end
local json = require("lib/json")
local function kinds()
    local ks = {}
    for _, r in ipairs(apiPosts()) do
        if r.url:find("/v1/state$") then ks[#ks + 1] = r.body:match('"kind":"(%a+)"') end
    end
    return table.concat(ks, " ")
end
local function ticksUntilPost(limit)
    local before = #apiPosts()
    for i = 1, limit do
        ticks(1)
        if #apiPosts() > before then return i end
    end
    return nil
end
local function typeToken(value)
    settings.field.content = value
    settings.field.hooks[ui.Hook.ONCONTENTCHANGED](settings.field)
end
local answers = {}
mock.http_handler = function(request, body)
    local a = table.remove(answers, 1) or { 200, '{"accepted":true}' }
    return a[1], a[2]
end

-- No token: nothing is posted anywhere.
ticks(100)
assert(W.state.ready, "wire ready")
assert(localPosts() == 0 and #mock.http_log == 0, "without a token nothing is sent or saved")
assert(mock.store["settings:apiToken"] == nil, "no token saved")

-- A pasted token is checked with the API ~2 s after the last edit and only saved when the API knows it.
-- Mistyped: the panel says so and nothing is saved.
answers = { { 401, '{"accepted":false,"reason":"unknown token","token":"unknown","retry_after":300}' } }
typeToken("tok-abc12")
assert(settings.problem == nil, "no verdict yet")
ticks(3)
assert(#apiChecks() == 1 and apiChecks()[1].url == "https://api.runeadvisor.com/v1/players", "one check request")
assert(apiChecks()[1].headers["Authorization"] == "Bearer tok-abc12", "checked with the pasted token")
assert(mock.store["settings:apiToken"] == nil, "a token the API doesn't know is not saved")
assert(settings.problem:find("not recognised"), "the panel explains: " .. tostring(settings.problem))
-- API offline: not saved either, and told so.
answers = { { nil } }
typeToken("tok-abc123")
ticks(3)
assert(#apiChecks() == 2 and mock.store["settings:apiToken"] == nil, "nothing saved while the API is offline")
assert(settings.problem:find("Can't reach"), tostring(settings.problem))
-- Edits within the delay are coalesced into one check: only the final text is checked. Once the API
-- accepts the token, the full state goes out on the same tick (no waiting for the interval). This server
-- runs an older game version: it answers the full post with 428 and the config hashes it knows. The
-- plugin sends only the difference (here: achievement 4 changed, achievement 2 unknown to the server, id
-- 99999 gone), then posts the full state again.
local layout = W.state.layout
assert(layout.fingerprint and layout.configHashes.achievements[4], "the login walk hashed the configs")
local known = { quests = {}, achievements = {} }
for id, h in pairs(layout.configHashes.quests) do known.quests[tostring(id)] = h end
for id, h in pairs(layout.configHashes.achievements) do known.achievements[tostring(id)] = h end
known.achievements["4"] = known.achievements["4"] ~ 1
known.achievements["2"] = nil
known.achievements["99999"] = 12345
local serverFingerprint = layout.fingerprint ~ 0xFFFF
answers = {
    { 200, '{"players":[]}' },  -- the token check
    { 428, json.encode({ accepted = false, reason = "configs unknown", need = "configs", base = serverFingerprint, known = known }) },
    { 200, '{"accepted":true,"fingerprint":1,"learned":true}' },
}
typeToken("tok-ab")
ticks(2)
typeToken("tok-abc123  ")
ticks(2)
assert(#apiChecks() == 2, "no check until the user has paused")
ticks(1)
assert(#apiChecks() == 3 and apiChecks()[3].headers["Authorization"] == "Bearer tok-abc123", "the final text is checked")
assert(mock.store["settings:apiToken"] == "tok-abc123" and settings.problem == nil, "accepted: saved trimmed")
-- (In the mock the tick handlers run in no fixed order, so the post lands on this tick or the next.)
if #apiPosts() == 0 then ticks(1) end
assert(#apiPosts() == 1 and kinds() == "full", "the full state goes out as soon as the token is accepted: " .. kinds())
assert(apiPosts()[1].body:find('"configs":' .. layout.fingerprint, 1, true) and apiPosts()[1].body:find('"plugin":"0.4.3"', 1, true),
    "the full post names the config fingerprint and the plugin version")
ticks(40)  -- the config walk runs as jobs, then the upload goes out
local upload = apiPosts()[2]
assert(upload and upload.url:find("/v1/configs$"), "the difference is uploaded: " .. tostring(upload and upload.url))
local u = json.decode(upload.body)
assert(u.fingerprint == layout.fingerprint and u.base == serverFingerprint and u.part == 1 and u.parts == 1, "part header")
local addedIds = {}
for _, e in ipairs(u.added.achievements) do addedIds[#addedIds + 1] = e.id end
table.sort(addedIds)
assert(#addedIds == 2 and addedIds[1] == 2 and addedIds[2] == 4, "only the changed and the new achievement go up: " .. table.concat(addedIds, ","))
assert(next(u.added.quests) == nil and #u.removed.achievements == 1 and u.removed.achievements[1] == 99999, "the server's stale id is reported removed")
assert(u.added.achievements[1].requirements and not upload.body:find('"progress"', 1, true)
       and not upload.body:find('"complete"', 1, true), "entries are config shapes with no player state")
ticksUntilPost(10)  -- the full post usually already went out during the ticks above
assert(kinds() == "full full", "the full state is posted again once the configs are accepted: " .. kinds())
local first = apiPosts()[1]
assert(first.url == "https://api.runeadvisor.com/v1/state", first.url)
assert(first.headers["Authorization"] == "Bearer tok-abc123", "bearer token header")
assert(first.headers["Content-Type"] == "application/json", "json content type")
assert(first.body:find('"player":"PlayerOne"') and first.body:find('"body":"'), "the post names the player and carries the body")
assert(localPosts() == 0, "nothing is ever recorded locally")

-- No response (timeout): the same delta is retried after the hold, not a full post.
answers = { { nil } }
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 9, newValue = 12 })
assert(ticksUntilPost(17) and kinds() == "full full delta", "the delta went out: " .. kinds())
assert(not ticksUntilPost(90), "held off after the failure")
assert(ticksUntilPost(40) and kinds() == "full full delta delta", "the same delta is retried after ~60 s: " .. kinds())
local p = apiPosts()
assert(p[#p].body:match('"base":(%d+)') == p[#p - 1].body:match('"base":(%d+)'), "same base state")

-- A saved token the server no longer knows (but didn't revoke): hold off for retry_after, keep the token.
answers = { { 401, '{"accepted":false,"reason":"unknown token","token":"unknown","retry_after":120}' } }
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 12, newValue = 13 })
assert(ticksUntilPost(17) and #apiPosts() == 6, "posted once")
assert(not ticksUntilPost(190), "no retries for 120 s after an unknown token")
assert(ticksUntilPost(20) and kinds():match("delta$"), "retried as a delta after the hold: " .. kinds())
assert(mock.store["settings:apiToken"] == "tok-abc123", "a saved token is kept on 'unknown'")

-- The server sets the cadence: `interval` in a reply changes how often the plugin posts (the next post comes
-- one new interval after the reply), and the interval is remembered.
answers = { { 200, '{"accepted":true,"interval":5}' } }
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 13, newValue = 14 })
assert(ticksUntilPost(17) and #apiPosts() == 8, "posted on the old cadence")
assert(W.state.intervalTicks == 8 and tonumber(mock.store["settings:postInterval"]) == 5, "5 s = 8 ticks, saved: " .. tostring(W.state.intervalTicks))
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 14, newValue = 15 })
assert(ticksUntilPost(20) == 8, "the next post came 8 ticks after the reply")
answers = { { 200, '{"accepted":true,"interval":300}' } }
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 15, newValue = 16 })
assert(ticksUntilPost(8) == 8 and W.state.intervalTicks == 500, "300 s = 500 ticks")
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 16, newValue = 17 })
assert(ticksUntilPost(600) == 500, "quiet for 5 minutes, then a post")
answers = { { 200, '{"accepted":true,"interval":0.001}' } }  -- clamped, never a tick-by-tick spam
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 17, newValue = 18 })
assert(ticksUntilPost(500) == 500 and W.state.intervalTicks == 5, "interval clamped to 3 s: " .. tostring(W.state.intervalTicks))

-- The token was revoked: the plugin forgets it, the panel says so, and posts go local again.
answers = { { 401, '{"accepted":false,"reason":"token revoked","token":"revoked"}' } }
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 18, newValue = 19 })
assert(ticksUntilPost(5) == 5 and #apiPosts() == 13, "the post that was refused: " .. #apiPosts())
assert(mock.store["settings:apiToken"] == nil and settings.field.content == "", "the revoked token is forgotten")
assert(settings.problem:find("revoked"), tostring(settings.problem))
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 19, newValue = 20 })
ticks(10)
assert(#apiPosts() == 13 and localPosts() == 0, "after a revoke nothing is posted: " .. #apiPosts())
-- Pasting a fresh token: checked, saved, message cleared.
answers = { { 200, '{"players":[]}' } }
typeToken("tok-new")
ticks(3)
assert(mock.store["settings:apiToken"] == "tok-new" and settings.problem == nil, "a new token is checked and saved")

-- Without httpAccess (a required permission, so only a guard now) a pasted token can't be checked,
-- so it isn't saved and nothing is posted.
mock.http_granted = false
local checks = #apiChecks()
typeToken("tok-other")
ticks(3)
assert(#apiChecks() == checks and mock.store["settings:apiToken"] == "tok-new", "no check, nothing saved without httpAccess")
assert(settings.problem:find("httpAccess"), tostring(settings.problem))
local posts = #apiPosts()
mock.fire("VarTransmit", { var = { configName = "HERALDS_POST_KILLS" }, oldValue = 20, newValue = 21 })
ticks(10)
assert(#apiPosts() == posts and localPosts() == 0, "without httpAccess nothing is posted")

-- An empty field never deletes the saved token: the client fires ONCONTENTCHANGED with "" by itself when
-- the panel opens (2026-11-09), and a half-typed token backspaced away is only a cancelled check.
mock.http_granted = true
mock.fire("SettingsLayerReady", { component = {} })  -- the panel opened again, field empty
typeToken("")
assert(mock.store["settings:apiToken"] == "tok-new" and settings.problem == nil, "opening the panel keeps the token")
checks = #apiChecks()
typeToken("tok-half")
typeToken("")
ticks(5)
assert(#apiChecks() == checks and mock.store["settings:apiToken"] == "tok-new", "emptying cancels the pending check only")

-- "Show the saved token": the button puts the saved token into the field so it can be copied out, starts no
-- check, and takes it away again on a second press or after half a minute. The saved token never changes.
checks = #apiChecks()
mock.press(settings.revealButton)
assert(settings.field.content == "tok-new" and settings.revealButton.label == "Hide the token", "the token is shown")
settings.field.hooks[ui.Hook.ONCONTENTCHANGED](settings.field)  -- the client may report the change we made
ticks(5)
assert(#apiChecks() == checks, "showing the saved token is not a token to check")
mock.press(settings.revealButton)
assert(settings.field.content == "" and settings.revealButton.label == "Show the saved token", "pressed again: hidden")
mock.press(settings.revealButton)
ticks(60)
assert(settings.field.content == "" and mock.store["settings:apiToken"] == "tok-new", "hidden by itself, still saved")
mock.press(settings.revealButton)
typeToken("tok-new-typed-over")  -- the user pastes another token over the shown one: that one is checked
assert(settings.revealButton.label == "Show the saved token", "typing over it ends the showing")
typeToken("")
ticks(5)
assert(mock.store["settings:apiToken"] == "tok-new", "and the saved token is still the saved token")

-- Consent to analysis: off until the user turns it on; the server holds the character's answer and the
-- panel shows only what the server said.
local function consentPosts()
    local out = {}
    for _, r in ipairs(apiPosts()) do if r.url:find("/v1/consent$") then out[#out + 1] = r end end
    return out
end
assert(settings.consent() ~= true and settings.consentButton.label == "Allow analysis", "consent starts off")
answers = {}  -- the API is unreachable: the button changes nothing
mock.http_handler = function() return nil end
mock.press(settings.consentButton)
assert(#consentPosts() == 1 and consentPosts()[1].body == '{"consent":true}', "the button asks the server: " .. tostring(consentPosts()[1].body))
assert(consentPosts()[1].headers["Authorization"] == "Bearer tok-new", "with the saved token")
assert(consentPosts()[1].headers["X-Player"] == "PlayerOne" and not consentPosts()[1].url:find("PlayerOne"), "the name in the header, not the address")
assert(settings.consent() ~= true and settings.consentLine.content:find("nothing was changed"), settings.consentLine.content)
mock.http_handler = function() return 404, '{"accepted":false,"reason":"this token has not posted for that character"}' end
mock.press(settings.consentButton)
assert(settings.consent() ~= true and settings.consentLine.content:find("hasn't been sent yet"), settings.consentLine.content)
mock.http_handler = function(_, body)
    if body == '{"consent":true}' then return 200, '{"consent":true,"consent_since":"2026-11-14"}' end
    if body == '{"consent":false}' then return 200, '{"consent":false,"consent_since":null}' end
    return 200, '{"accepted":true,"consent":false}'  -- a state post: the server's current answer rides along
end
mock.press(settings.consentButton)
assert(settings.consent() == true and mock.store["settings:consent"] == nil, "the server confirmed: allowed; nothing kept on disk")
assert(settings.consentLine.content == "Analysis: allowed for PlayerOne" and settings.consentButton.label == "Stop allowing analysis", settings.consentLine.content)
-- Withdrawn on the website meanwhile: the next accepted post's reply says so and the panel follows.
W.state.acked = nil  -- make sure there is something to post
W.nudge()
ticks(3)
assert(settings.consent() == false and settings.consentLine.content == "Analysis: not allowed for PlayerOne", "the site's change reached the panel: " .. settings.consentLine.content)
mock.press(settings.consentButton)
mock.press(settings.consentButton)
assert(settings.consent() == false and consentPosts()[#consentPosts()].body == '{"consent":false}', "on, then off again")
-- A client-only variable (the statue bag's copy) reads -1 until the client has loaded it. -1 is "not known
-- yet": it never replaces the value already held, in this session or after logging in again.
local bag = nil
for i, slot in ipairs(W.state.layout.slots) do if slot.name == "STATUE_BAG_CLIENT" then bag = i end end
assert(bag and W.state.slots[bag] == 1022623217, "the bag's copy was read at login")
var.VarClient.STATUE_BAG_CLIENT.value = -1
W.state.acked = nil
W.nudge()
ticks(3)
assert(W.state.slots[bag] == 1022623217 and W.state.acked, "-1 before a post leaves the known value alone")
mock.fire("GameStateChanged", { newState = GameState.GAME })
ticks(40)
assert(W.state.ready and W.state.slots[bag] == 1022623217, "a new login with the bag unopened starts from the accepted value")
var.VarClient.STATUE_BAG_CLIENT.value = 13
W.nudge()
ticks(3)
assert(W.state.slots[bag] == 13 and W.state.acked.slots[bag] == 13, "opening the bag: the real value is read and posted")

-- A token the server revokes: what was known is forgotten; without a token the button only explains.
mock.press(settings.consentButton)
assert(settings.consent() == true)
settings.forget("Token revoked by the server: paste a new one")
assert(settings.consent() == nil and settings.consentButton.label == "Allow analysis", "forgotten with the token")
local before = #consentPosts()
mock.press(settings.consentButton)
assert(#consentPosts() == before and settings.consentLine.content:find("Paste a token first"), settings.consentLine.content)

print("run_http: ok (" .. #apiPosts() .. " API posts, " .. #apiChecks() .. " token checks)")
