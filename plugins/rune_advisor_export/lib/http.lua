-- Everything this plugin sends over the network, and everything it asks for, is in this file.
--
-- There is one destination: the Rune Advisor API, whose address is `urls.api` in plugin.json
-- (https://api.runeadvisor.com). Requests go out only with the API token the user pasted into the plugin's
-- settings, as an `Authorization: Bearer` header, and only with the httpAccess permission. The four calls:
--
--   POST /v1/state     the character's state: in full once per login, then only what changed (lib/wire.lua
--                      builds the post; this file adds the character's name and sends it)
--   POST /v1/configs   after a game update, the quest and achievement definitions the server doesn't have
--                      yet (lib/configsync.lua builds them; they are game data, nothing about the player)
--   GET  /v1/players   asks whether a token the user just pasted is one the server knows, before it is saved
--   POST /v1/consent   the settings button: allow, or stop allowing, analysis of this character's data
--
-- What comes back is a small JSON object each time: whether the post was accepted, how often the server
-- would like posts, which definitions it lacks, whether the token is still valid, and the character's
-- consent setting. Nothing the server sends is run as code or changes what the plugin reads from the game.
--
-- The token is never written to the log, and the character's name never goes in an address: it travels
-- in the body of a state post, or in the `X-Player` header.

local M = {}

local wire = require("lib/wire")  -- for the hold times a failed or refused post leads to

local STATE_PATH = "/v1/state"
local CONFIGS_PATH = "/v1/configs"
local CHECK_PATH = "/v1/players"
local CONSENT_PATH = "/v1/consent"

--- A character's name as a header value: anything but letters, digits and - _ . ~ percent-encoded.
local function headerSafe(name)
    return (name:gsub("[^%w%-_.~]", function(c) return string.format("%%%02X", c:byte()) end))
end

--- One request, the only place the client's HTTPRequest is used. A POST of `body` (a JSON string) when
--- there is one, else a GET, to `path` under the API's address, with `token` as the bearer and, when
--- `player` is given, the character's name in the X-Player header. A new HTTPRequest every time: the client
--- forbids reusing an active one.
--- callback(code, reply, why): the status code (0 when there was no response at all), the reply decoded
--- from JSON ({} when it isn't a JSON object), and for a request that got no usable answer the client's own
--- words for what went wrong (never the token).
local function request(deps, path, token, body, callback, player)
    local function finished(req)
        -- Each field is read inside its own closure: a missing one must not stop the others.
        local function read(name)
            local ok, value = pcall(function() return req[name] end)
            if ok then return value end
            return nil
        end
        local code = read("responseCode")
        if type(code) ~= "number" then code = 0 end
        local okBody, reply = pcall(function() return deps.json.decode(req.data) end)
        if not okBody or type(reply) ~= "table" then reply = {} end
        local why = {}
        if read("timedOut") == true then why[#why + 1] = "timed out" end
        if read("failed") == true then why[#why + 1] = "failed" end
        local errorCode = read("errorCode")
        if errorCode ~= nil and errorCode ~= 0 then why[#why + 1] = "error code " .. tostring(errorCode) end
        local errorInfo = read("errorInfo")
        if type(errorInfo) == "string" and errorInfo ~= "" then why[#why + 1] = errorInfo end
        callback(code, reply, #why > 0 and table.concat(why, ", ") or nil)
    end
    local ok, err = pcall(function()
        local req = HTTPRequest.new(deps.url(), path)
        req:AddHeader("Authorization", "Bearer " .. token)
        if player then req:AddHeader("X-Player", headerSafe(player)) end
        if body then
            req:AddHeader("Content-Type", "application/json")
            req:Post(body, finished)
        else
            req:Get(finished)
        end
    end)
    if not ok then callback(0, {}, tostring(err)) end
end

--- The API client. deps:
---   json, bits                 the libraries of those names under lib/
---   url() -> PluginURL         the API's address from plugin.json
---   token() -> string or nil   the saved API token (lib/settings.lua)
---   forgetToken(reason)        called when the server says the token was revoked
---   noteConsent(player, bool)  called with the character's consent setting as the server has it
function M.new(deps)
    local client = {}

    --- Send one state post (lib/wire.lua: `header` is the post's JSON header, `body` its bytes). The
    --- character's name and the body in base64 are added to the header and the whole goes as JSON.
    --- callback(accepted, holdTicks, reply), as lib/wire.lua's `answered` expects:
    ---   200          accepted. The reply also tells the character's consent setting, so that a change made
    ---                on the website shows in the plugin's settings.
    ---   409          refused, the server's copy of the state differs: the full state is sent next.
    ---   428          the server lacks this game version's definitions: lib/configsync.lua sends them.
    ---   401 / 403    the token was refused. "revoked": the server deleted it, so the plugin forgets it too
    ---                and posts nothing until a new one is pasted. Otherwise: wait as long as the reply's
    ---                `retry_after` says (about 5 minutes when it doesn't) and tell the user.
    ---   anything else, or no response: try again in a minute.
    function client.postState(player, header, body, callback)
        header.player = player
        header.body = deps.bits.base64(body)
        header.bytes = #body
        request(deps, STATE_PATH, deps.token(), deps.json.encode(header), function(code, reply, why)
            local reason = reply.reason
            if code == 200 then
                if type(reply.consent) == "boolean" and deps.noteConsent then deps.noteConsent(player, reply.consent) end
                callback(true, nil, reply)
            elseif code == 409 then
                log("Rune Advisor: the API is out of step (" .. tostring(reason) .. "): resending the full state")
                callback(false, nil, reply)
            elseif code == 428 then
                callback(nil, wire.RETRY_HOLD_TICKS, reply)  -- reply.need == "configs"
            elseif code == 401 or code == 403 then
                if reply.token == "revoked" then
                    log("Rune Advisor: the API token was revoked (" .. tostring(reason) .. "): forgetting it; nothing is posted until a new one is pasted")
                    if deps.forgetToken then deps.forgetToken("Token revoked by the server: paste a new one") end
                    callback(nil, 1)
                else
                    local hold = wire.secondsToTicks(reply.retry_after, 1, wire.MAX_INTERVAL_TICKS) or wire.AUTH_HOLD_TICKS
                    log(string.format("Rune Advisor: the API refused the token (%s): check it in the plugin's settings; next try in %.0f s",
                        tostring(reason), hold * wire.TICK_SECONDS))
                    callback(nil, hold)
                end
            else
                log(string.format("Rune Advisor: API post failed (status %d%s): retrying in a minute", code,
                    why and (", " .. why) or ""))
                callback(nil)
            end
        end)
    end

    --- Send one part of the game definitions the server lacks (lib/configsync.lua builds `body`).
    --- callback(code, reply, why) as for `request`.
    function client.uploadConfigs(body, callback)
        request(deps, CONFIGS_PATH, deps.token(), deps.json.encode(body), callback)
    end

    --- Ask whether a token the user just pasted is known to the server, before it is saved. Nothing is
    --- sent but the token itself, as the bearer. callback(result, reason): "ok", "unknown" (mistyped or cut
    --- short), "revoked", or "offline" (no answer, or any other status: nothing is saved for now).
    function client.checkToken(token, callback)
        request(deps, CHECK_PATH, token, nil, function(code, reply, why)
            if code == 200 then
                callback("ok")
            elseif code == 401 or code == 403 then
                callback(reply.token == "revoked" and "revoked" or "unknown", reply.reason)
            else
                callback("offline", "status " .. tostring(code) .. (why and (", " .. why) or ""))
            end
        end)
    end

    --- Allow (wanted = true) or stop allowing analysis of `player`'s data: the settings button.
    --- callback(consent, why): what the server now has (true/false), or nil with "unposted" (the server
    --- holds no state from this token for that character yet) or a description of the failure.
    function client.setConsent(player, wanted, callback)
        request(deps, CONSENT_PATH, deps.token(), deps.json.encode({ consent = wanted }), function(code, reply, why)
            if code == 200 and type(reply.consent) == "boolean" then
                callback(reply.consent)
            elseif code == 404 then
                callback(nil, "unposted")
            else
                callback(nil, "status " .. tostring(code) .. (why and (", " .. why) or ""))
            end
        end, player)
    end

    return client
end

return M
