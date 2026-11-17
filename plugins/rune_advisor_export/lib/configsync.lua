-- Config sync: when the Rune Advisor server doesn't know the config set this client's layout came from (a
-- game update added or changed quests or achievements), it answers a full post with 428 and the hashes of
-- every config it knows. This module sends back only the difference: the configs whose hash differs or
-- whose id the server lacks (as lib/configdump.lua entries, which hold nothing about the player), and the ids the server listed that this client
-- no longer has. The server composes the new set from what it knew plus that difference and checks it
-- hashes to the fingerprint the client claimed, so nothing else needs to travel.
--
-- Entries go up in parts of PART_CONFIGS at most (one request in flight, parts in order); a typical game
-- update is one part of a few KB.

local M = {}

local PART_CONFIGS = 150      -- config entries per request
local CONFIGS_PER_SLICE = 40  -- configs looked at per tick while collecting the wanted ones (lib/jobs.lua)

--- deps: configs, jobs, configdump, configHashes (this client's { quests = {[id] = hash}, ... }),
---       fingerprint (this client's), upload(body, callback(code, reply, why)) (lib/http.lua)
--- reply: the server's 428 body { base = <its fingerprint>, known = { quests = {["id"] = hash}, ... } }
--- done(ok, reason)
function M.run(deps, reply, done)
    local known = type(reply.known) == "table" and reply.known or {}
    local wanted, removed, counts = {}, { quests = {}, achievements = {} }, { added = 0, removed = 0 }
    for kind, mine in pairs(deps.configHashes) do
        wanted[kind] = {}
        local theirs = known[kind] or {}
        for id, h in pairs(mine) do
            if theirs[tostring(id)] ~= h then
                wanted[kind][id] = true
                counts.added = counts.added + 1
            end
        end
        for idText in pairs(theirs) do
            local id = tonumber(idText)
            if id and mine[id] == nil then
                table.insert(removed[kind], id)
                counts.removed = counts.removed + 1
            end
        end
    end
    log(string.format("Rune Advisor: the API lacks this game version's configs: sending %d changed and %d removed",
        counts.added, counts.removed))

    -- Walk the configs again, building entries only for the wanted ids (the login walk kept hashes only).
    local added = { quests = {}, achievements = {} }
    local kinds = { "quests", "achievements" }
    local pending = #kinds
    local function upload()
        -- Split into parts: every entry once, the removed ids with the first part.
        local items = {}
        for _, kind in ipairs(kinds) do
            for _, entry in ipairs(added[kind]) do items[#items + 1] = { kind = kind, entry = entry } end
        end
        local parts = math.max(1, math.ceil(#items / PART_CONFIGS))
        local function send(partIndex)
            local body = { fingerprint = deps.fingerprint, base = reply.base, part = partIndex, parts = parts,
                           added = { quests = {}, achievements = {} }, removed = partIndex == 1 and removed or nil }
            for i = (partIndex - 1) * PART_CONFIGS + 1, math.min(#items, partIndex * PART_CONFIGS) do
                table.insert(body.added[items[i].kind], items[i].entry)
            end
            deps.upload(body, function(code, answer, why)
                if code ~= 200 then
                    done(false, string.format("part %d/%d: status %s (%s)", partIndex, parts, tostring(code),
                        tostring(type(answer) == "table" and answer.reason or why)))
                elseif partIndex < parts then
                    send(partIndex + 1)
                else
                    done(true)
                end
            end)
        end
        send(1)
    end
    for _, kind in ipairs(kinds) do
        local spec = deps.configdump.KINDS[kind]
        deps.jobs.add({
            name = "configsync: " .. kind, items = deps.configs.all(spec.table()), perTick = CONFIGS_PER_SLICE,
            each = function(cfg)
                local ok, id = pcall(function() return cfg.id end)
                if ok and wanted[kind][id] then
                    local entry = spec.build(cfg)
                    if entry then table.insert(added[kind], entry) end
                end
            end,
            done = function()
                pending = pending - 1
                if pending == 0 then upload() end
            end,
        })
    end
end

return M
