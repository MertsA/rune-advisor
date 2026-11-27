-- Wire protocol: what the plugin holds of the player's state, how that is packed into bytes, and when a
-- post goes out. The first post of a login is the whole state ("full"); after that only what changed since
-- the last post the server accepted ("delta"). This file decides WHAT is posted and WHEN. It does not talk
-- to the network: it hands each post to deps.transport, which is lib/http.lua. Nothing is handed over while
-- deps.ready() is false (no token saved, or the httpAccess permission not granted).
--
-- State:
--   slots[i]      the packed value of layout slot i (lib/manifest.lua: the tested bits of a player variable,
--                 a hand-listed variable at its own width, a skill's xp, or the membership bit)
--   invs[name]    { [itemId] = count } for each of the player's own inventories (lib/inventories.lua). A set:
--                 slot order never matters, placeholders and empty slots are left out.
-- Canonical bytes (hashed, and the body of a full post): every slot at its width, least significant bit
-- first, then for each inventory name in the fixed order: u32 item count, then items by id: u32 id, u32
-- count. An inventory the client hasn't loaded this session (the bank until it is opened, storages until
-- visited) is written with the count 0xFFFFFFFF ("unknown"): the server keeps whatever it last knew for it.
-- State hash: FNV-1a 32 over the canonical bytes, seeded with the layout hash.
-- Delta body: u16 number of changed slots, each as u16 slot index + the value at the slot's width; u16
-- number of inventory entries, each as u8 inventory index, u32 item id, u32 count (0 = removed). An
-- inventory loaded for the first time since the last post is announced with the entry {index, 0xFFFFFFFF,
-- 0} ("now known") before its items, so an empty one still turns from unknown into an empty set on the
-- server (the hashes would differ otherwise).
-- Every post carries a JSON header beside the body: kind ("full"/"delta"), layout (the layout's hash), base
-- (the state hash the delta applies to), new (the hash after), seq, and on a full post `manifest` (the hash
-- of lib/exported_vars.lua's list, which the server holds too), `absent` (the entries of that list this client
-- lacks, sent as 0; left out when there are none), the skill and inventory names, `configs` (the fingerprint
-- of the quest/achievement configs the layout was derived from) and `plugin` (this plugin's version). The two
-- hashes let the server refuse a delta that does not fit the state it holds; the plugin then sends the full
-- state again.
-- A server that doesn't know the fingerprint (a game update it has not seen) asks for the configs it lacks:
-- deps.syncConfigs sends them (lib/configsync.lua), then the full state is posted again.
--
-- Changes are picked up from the client's own events, not by polling (the one exception: client-only
-- variables, which announce nothing, are re-read just before each post): VarTransmit names the variable that
-- changed, and the slots cut from it are repacked from the event's new value (a hand-listed VarBit is cut
-- from it too: its bits of the base var's value); SkillTransmit updates the
-- skill's slot; InventoryAdded/Updated re-reads that one inventory. The baseline at login reads everything
-- once, a slice per tick (lib/jobs.lua). A delta is the current state minus the last accepted state, worked
-- out when the post is made, never a list of events. One post is in flight at a time.

local M = {}

local TICK_SECONDS = 0.6        -- one server tick (Event.ServerLogic)
local POST_INTERVAL_TICKS = 17  -- ~10 s of server ticks: the buffering period until the server says otherwise
-- The server sets the post interval (`interval` seconds in every reply) to spread its load: faster with a few
-- users, slower with many. Clamped here so a bad reply can't make the plugin spam or go silent.
local MIN_INTERVAL_TICKS, MAX_INTERVAL_TICKS = 5, 6000  -- 3 s .. 1 h
local RETRY_HOLD_TICKS = 100    -- ~60 s before posting again after a request failed (server down, timeout)
local AUTH_HOLD_TICKS = 500     -- ~5 min after the server refused the token without saying how long to wait

--- Seconds as server ticks, kept within lo..hi; nil when `seconds` is not a usable number.
local function secondsToTicks(seconds, lo, hi)
    if type(seconds) ~= "number" or seconds ~= seconds then return nil end
    return math.max(lo, math.min(hi, math.floor(seconds / TICK_SECONDS + 0.5)))
end
-- For lib/http.lua, which turns the server's answers into the hold times above.
M.secondsToTicks = secondsToTicks
M.TICK_SECONDS, M.MAX_INTERVAL_TICKS = TICK_SECONDS, MAX_INTERVAL_TICKS
M.RETRY_HOLD_TICKS, M.AUTH_HOLD_TICKS = RETRY_HOLD_TICKS, AUTH_HOLD_TICKS

-- Small helpers ----------------------------------------------------------------------------------------

local function copySlots(slots)
    local out = {}
    for i, v in ipairs(slots) do out[i] = v end
    return out
end

local function copyInvs(invs)
    local out = {}
    for name, set in pairs(invs) do
        local s = {}
        for id, count in pairs(set) do s[id] = count end
        out[name] = s
    end
    return out
end

-- lib/bits.lua writes at most 32 bits at a time; a slot can be wider (several fields of one variable).
local function writeWide(bits, w, value, width)
    while width > 32 do
        bits.write(w, value & 0xFFFFFFFF, 32)
        value = value >> 32
        width = width - 32
    end
    bits.write(w, value, width)
end

local function readWide(bits, r, width)
    local v, shift = 0, 0
    while width > 32 do
        v = v | (bits.read(r, 32) << shift)
        shift, width = shift + 32, width - 32
    end
    return v | (bits.read(r, width) << shift)
end

-- The keys of a set in ascending order: both sides must list an inventory's items in the same order.
local function sortedIds(set)
    local ids = {}
    for id in pairs(set) do ids[#ids + 1] = id end
    table.sort(ids)
    return ids
end

-- Packing ---------------------------------------------------------------------------------------------
-- Pure functions: bytes in, bytes out, nothing read from the game and nothing sent. `bits` is lib/bits.lua,
-- `layout` the wire layout (lib/manifest.lua), `inventoryNames` the sorted list of own inventories.

--- The canonical bytes of a state (see the header): what is hashed, and the body of a full post.
local function packState(bits, layout, inventoryNames, slots, invs)
    local w = bits.writer()
    for i, slot in ipairs(layout.slots) do writeWide(bits, w, slots[i] or 0, slot.width) end
    for _, name in ipairs(inventoryNames) do
        local set = invs[name]
        if set == nil then
            bits.write(w, 0xFFFFFFFF, 32)  -- never loaded: unknown, not empty
        else
            local ids = sortedIds(set)
            bits.write(w, #ids, 32)
            for _, id in ipairs(ids) do
                bits.write(w, id, 32)
                bits.write(w, set[id], 32)
            end
        end
    end
    return bits.finish(w)
end

--- The reverse of packState: slots and inventory sets from canonical bytes (the saved copy of the last
--- state the server accepted). An inventory written as "unknown" is left out of the result.
local function unpackState(bits, layout, inventoryNames, bytes)
    local r = bits.reader(bytes)
    local slots, invs = {}, {}
    for i, slot in ipairs(layout.slots) do slots[i] = readWide(bits, r, slot.width) end
    for _, name in ipairs(inventoryNames) do
        local count = bits.read(r, 32)
        if count ~= 0xFFFFFFFF then
            local set = {}
            for _ = 1, count do
                local id = bits.read(r, 32)
                set[id] = bits.read(r, 32)
            end
            invs[name] = set
        end
    end
    return slots, invs
end

--- What differs between the current state and the last accepted one (`acked`): the indexes of the changed
--- slots, and the changed items as { inventory index, item id, count now } (count 0 = no longer there).
local function changesSince(layout, inventoryNames, slots, invs, acked)
    local changedSlots, changedItems = {}, {}
    for i = 1, #layout.slots do
        if (slots[i] or 0) ~= (acked.slots[i] or 0) then changedSlots[#changedSlots + 1] = i end
    end
    for index, name in ipairs(inventoryNames) do
        -- An inventory still unknown on both sides has nothing to say; one seen for the first time
        -- since the last post announces itself (the server had "unknown", now it has a set, possibly
        -- empty) and then sends all its items. One the client hasn't loaded this session never gets
        -- unknown again: login seeds it from the accepted state (see baseline).
        local now, before = invs[name], acked.invs[name]
        if now == nil then goto continue end
        if before == nil then
            before = {}
            changedItems[#changedItems + 1] = { index, 0xFFFFFFFF, 0 }
        end
        local ids = {}
        for id in pairs(now) do ids[id] = true end
        for id in pairs(before) do ids[id] = true end
        for _, id in ipairs(sortedIds(ids)) do
            if (now[id] or 0) ~= (before[id] or 0) then
                changedItems[#changedItems + 1] = { index, id, now[id] or 0 }
            end
        end
        ::continue::
    end
    return changedSlots, changedItems
end

--- The body of a delta post (see the header) for the changes changesSince found.
local function packDelta(bits, layout, slots, changedSlots, changedItems)
    local w = bits.writer()
    bits.write(w, #changedSlots, 16)
    for _, i in ipairs(changedSlots) do
        bits.write(w, i - 1, 16)
        writeWide(bits, w, slots[i] or 0, layout.slots[i].width)
    end
    bits.write(w, #changedItems, 16)
    for _, entry in ipairs(changedItems) do
        bits.write(w, entry[1] - 1, 8)
        bits.write(w, entry[2], 32)
        bits.write(w, entry[3], 32)
    end
    return bits.finish(w)
end

-- The exporter ----------------------------------------------------------------------------------------

--- Start keeping and posting the state. Everything the wire reads from the game or does to the outside
--- comes in through `deps`, so this file can be read (and tested) without the rest:
---   json, bits, jobs, configs, manifest      the libraries of those names under lib/
---   saveJson(key, text)                      store a string on this computer (PersistentDB), in chunks
---   playerName() -> string or nil            the logged-in character's display name
---   skillNames                               the skills, in SkillType order
---   namedVars(onDone(fields))              lib/exported_vars.lua's entries resolved against this client (main.lua)
---   inventoryNames                           the sorted allow-list of own inventories
---   readInventory(name) -> set or nil        one own inventory as { [itemId] = count }
---   readVar(name) -> number or nil           one player variable's value
---   transport.post(player, header, body, callback)  send one post (lib/http.lua); see `answered` below
---   ready() -> boolean                       whether posting is allowed at all (token saved, permission granted)
---   loadInterval() / saveInterval(seconds)   the post interval the server last asked for, kept across sessions
---   configHash, fingerprint, syncConfigs     the config hashes and upload (lib/canon.lua, lib/configsync.lua)
---   pluginVersion                            sent with a full post
function M.start(deps)
    local bits, json = deps.bits, deps.json
    -- Everything the wire remembers while it runs:
    local S = {
        player = nil,        -- the character this state belongs to (set at login)
        layout = nil,        -- the wire layout built at login (lib/manifest.lua)
        slots = {},          -- current packed slot values
        invs = {},           -- current inventory sets, by name; absent = not loaded by the client yet
        acked = nil,         -- the last state the server accepted: { slots, invs, hash }; nil = send the full state next
        ready = false,       -- true once the login baseline has read everything
        inFlight = false,    -- a post is on its way; no second one until it is answered
        seq = 0,             -- how many posts this session, sent as `seq`
        holdTicks = 0,       -- server ticks to stay silent after a failed or refused post
        intervalTicks = POST_INTERVAL_TICKS,  -- server ticks between posts (the server may change it)
        untilPost = POST_INTERVAL_TICKS,      -- server ticks left until the next post
    }

    -- The packing functions above, applied to this session's layout and state.
    local function canonical(slots, invs)
        return packState(bits, S.layout, deps.inventoryNames, slots, invs)
    end
    local function stateHash(slots, invs)
        return bits.fnv1a32(canonical(slots, invs), bits.FNV_OFFSET ~ S.layout.hash)
    end
    local function unpackCanonical(bytes)
        return unpackState(bits, S.layout, deps.inventoryNames, bytes)
    end
    local function diff()
        return changesSince(S.layout, deps.inventoryNames, S.slots, S.invs, S.acked)
    end
    local function deltaBody(changedSlots, changedItems)
        return packDelta(bits, S.layout, S.slots, changedSlots, changedItems)
    end

    -- `bytes`: the acknowledged state's canonical bytes (the post already packed them for its hash).
    local function saveAcked(hash, bytes)
        deps.saveJson("wire:" .. S.player .. ":acked", json.encode({
            layout = S.layout.hash, hash = hash, state = bits.base64(bytes) }))
    end

    -- A client-only variable is a copy the client keeps of something the plugin API cannot read directly
    -- (the statue collection bag's contents). Until the client has loaded it this session (for the bag:
    -- until the bag is opened) it reads -1, which means "not known yet", not a value. So -1 never replaces
    -- what is already held: the slot keeps the last value read, or the one the server last accepted.
    -- A hand-listed value as its slot holds it: the low `width` bits (a VarBit's field is never wider; a
    -- whole variable is 32 bits).
    local function fieldValue(slot, value)
        value = type(value) == "number" and math.tointeger(value) or 0
        return value & ((1 << slot.width) - 1)
    end

    local function readClientOnly(i, slot)
        local value = deps.readVar(slot.name)
        if type(value) == "number" and value ~= -1 then S.slots[i] = value & 0xFFFFFFFF end
    end

    -- What the transport reports back for a post (`sent`: the state as it was posted, { player, slots, invs,
    -- hash, bytes }):
    --   accepted == true    the server took it: `sent` becomes the accepted state deltas are made against
    --   accepted == false   the server refused it because its copy differs: the next post is the full state
    --   accepted == nil     no usable answer (the request failed, or the token was refused): keep the accepted
    --                       state, stay silent for `holdTicks` server ticks, then try again. If the server did
    --                       apply the post after all, the next delta is refused and the full state follows.
    -- `info` is the server's reply: `interval` (seconds between posts, its wish) and, when it lacks this
    -- game version's configs, `need = "configs"`.
    local function answered(sent, accepted, holdTicks, info)
        S.inFlight = false
        local wanted = info and secondsToTicks(info.interval, MIN_INTERVAL_TICKS, MAX_INTERVAL_TICKS)
        if wanted and wanted ~= S.intervalTicks then
            S.intervalTicks = wanted
            S.untilPost = wanted  -- the next post is one new interval from now
            if deps.saveInterval then deps.saveInterval(info.interval) end
            log(string.format("Rune Advisor: the API asked for a post every %.0f s", wanted * TICK_SECONDS))
        end
        -- The answer to a post made for another character (it logged out, this one logged in, before the
        -- answer arrived) says nothing about this character's state: drop it.
        if sent.player ~= S.player then return end
        if accepted == true then
            if sent.check then log("Rune Advisor: the API holds the last post: carrying on with changes only") end
            S.check = false
            S.acked = { slots = sent.slots, invs = sent.invs, hash = sent.hash }
            saveAcked(sent.hash, sent.bytes)
        elseif accepted == false then
            S.check = false
            S.acked = nil
        elseif info and info.need == "configs" and deps.syncConfigs then
            -- The server can't decode this layout yet: send it the configs it lacks, then post the
            -- full state again. Nothing is posted meanwhile.
            S.holdTicks = MAX_INTERVAL_TICKS
            deps.syncConfigs(S.layout, info, function(ok, reason)
                if ok then
                    log("Rune Advisor: configs accepted by the API; posting the full state")
                    S.acked, S.holdTicks, S.untilPost = nil, 0, 1
                else
                    log("Rune Advisor: config upload failed (" .. tostring(reason) .. "): retrying in a minute")
                    S.holdTicks = RETRY_HOLD_TICKS
                end
            end)
        else
            S.holdTicks = holdTicks or RETRY_HOLD_TICKS
        end
    end

    -- Make one post, if one is due: the full state (`kind` "full") or the changes since the accepted state.
    local notReadySaid = false
    local function post(kind)
        if S.inFlight or S.holdTicks > 0 then return end
        if deps.ready and not deps.ready() then
            -- Said once per spell of not being ready, so a silent exporter explains itself in the console.
            if not notReadySaid then
                log("Rune Advisor: not posting: no API token saved or httpAccess not granted (see the plugin's settings panel)")
                notReadySaid = true
            end
            return
        end
        notReadySaid = false
        -- Client-only variables announce no changes: read them fresh before posting.
        for i, slot in ipairs(S.layout.slots) do
            if slot.clientOnly and not slot.absent then readClientOnly(i, slot) end
        end
        -- A delta with nothing in it is not posted, so look for changes before packing and hashing anything. The
        -- one exception is the first post after resuming from a saved state (`S.check`): it goes out even if empty,
        -- since only the server can say whether it still holds that state. It may not: another API address, a
        -- server that lost its copy, a post from elsewhere since. Its base hash makes the server answer 409 then,
        -- and the full state follows at once, instead of the server keeping stale data until something changes.
        local changedSlots, changedItems
        if kind ~= "full" then
            changedSlots, changedItems = diff()
            if #changedSlots == 0 and #changedItems == 0 and not S.check then return end
        end
        -- The state is packed once per post: the bytes give the hash, are the body of a full post, and are
        -- what gets saved as the acknowledged state if the server accepts.
        local stateBytes = canonical(S.slots, S.invs)
        local newHash = bits.fnv1a32(stateBytes, bits.FNV_OFFSET ~ S.layout.hash)
        local header = { kind = kind, layout = S.layout.hash, new = newHash, seq = S.seq + 1,
                         base = S.acked and S.acked.hash or nil }
        local body
        if kind == "full" then
            body = stateBytes
            header.manifest = S.layout.manifestHash
            if #S.layout.absent > 0 then header.absent = S.layout.absent end
            header.inventories = deps.inventoryNames
            header.skills = deps.skillNames
            header.configs = S.layout.fingerprint
            header.plugin = deps.pluginVersion
        else
            body = deltaBody(changedSlots, changedItems)
            header.slots, header.items = #changedSlots, #changedItems
        end
        S.inFlight = true
        S.seq = S.seq + 1
        -- A copy of what is being posted: the state may change again before the answer arrives.
        local sent = { player = S.player, slots = copySlots(S.slots), invs = copyInvs(S.invs), hash = newHash,
                       bytes = stateBytes, check = S.check and kind ~= "full" }
        deps.transport.post(S.player, header, body, function(accepted, holdTicks, info)
            answered(sent, accepted, holdTicks, info)
        end)
    end

    -- Baseline, once per login: read every player variable the layout names (those a quest or achievement
    -- tests, and the hand-listed ones), the skills, membership and the own inventories the client has
    -- loaded. As a job, a slice per tick: there are ~1,200 tested variables and ~1,700 hand-listed ones.
    local BASELINE_VARS_PER_SLICE = 500
    local function baseline()
        local reads = {}
        for i, slot in ipairs(S.layout.slots) do
            if slot.kind == "base" or (slot.kind == "named" and not slot.absent) then reads[#reads + 1] = i end
        end
        deps.jobs.add({
            name = "wire: baseline", items = reads, perTick = BASELINE_VARS_PER_SLICE,
            each = function(i)
                local slot = S.layout.slots[i]
                local value = deps.readVar(slot.name)
                if slot.kind == "base" then
                    S.slots[i] = deps.manifest.packBase(slot, value or 0)
                elseif slot.clientOnly then
                    readClientOnly(i, slot)  -- left unset while unknown; settled below, once the accepted state is loaded
                else
                    S.slots[i] = fieldValue(slot, value)
                end
            end,
            done = function()
                for _, name in ipairs(deps.skillNames) do
                    local skill = SkillType[name]
                    local ok, xp = pcall(function() return Player.GetXPUncapped(skill) end)
                    S.slots[S.layout.skillSlot[name]] = (ok and xp or 0) & 0xFFFFFFFF
                end
                local okM, members = pcall(function() return World.IsMembers() end)
                S.slots[S.layout.membersSlot] = (okM and members) and 1 or 0
                for _, name in ipairs(deps.inventoryNames) do
                    local set = deps.readInventory(name)
                    if set then S.invs[name] = set end
                end
                -- The interval the server last asked for survives a restart (PersistentDB, via deps).
                if deps.loadInterval then
                    local saved = secondsToTicks(deps.loadInterval(), MIN_INTERVAL_TICKS, MAX_INTERVAL_TICKS)
                    if saved then S.intervalTicks, S.untilPost = saved, saved end
                end
                -- Resume from the last acknowledged state if it was posted under this layout. Inventories the
                -- client hasn't loaded yet (the bank until it's opened) start as the server last saw them, so
                -- the first delta doesn't report them emptied; the first real read replaces the copy.
                S.acked = nil
                local saved = deps.loadJson("wire:" .. S.player .. ":acked")
                if saved then
                    local okDecode, acked = pcall(json.decode, saved)
                    if okDecode and type(acked) == "table" and acked.layout == S.layout.hash and acked.state then
                        local okUnpack, slots, invs = pcall(unpackCanonical, bits.unbase64(acked.state))
                        if okUnpack then
                            S.acked = { slots = slots, invs = invs, hash = acked.hash }
                            for name, set in pairs(invs) do
                                if S.invs[name] == nil then S.invs[name] = copyInvs({ [name] = set })[name] end
                            end
                        end
                    end
                end
                -- Client-only variables the client hasn't loaded yet: as the server last had them, else unknown (-1).
                for i, slot in ipairs(S.layout.slots) do
                    if slot.absent then S.slots[i] = 0 end  -- an entry this client lacks: 0, reported absent
                    if slot.clientOnly and S.slots[i] == nil then
                        S.slots[i] = S.acked and S.acked.slots[i] or 0xFFFFFFFF
                    end
                end
                S.ready = true
                S.check = S.acked ~= nil  -- resumed: the first post asks the server whether it agrees (see post)
                S.untilPost = 1  -- the first post of a login goes out at the next server tick, not an interval later
                log(string.format("Rune Advisor: wire ready: %d slots (%d bits), layout %08x, %s",
                    #S.layout.slots, S.layout.totalBits, S.layout.hash, S.acked and "resuming from the last post" or "posting the full state"))
            end,
        })
    end

    -- A character is in the game: forget the previous session's state, find the hand-listed variables this
    -- client has, build the layout from the game's configs, then take the baseline.
    local function login()
        S.player = deps.playerName()
        if not S.player then return end
        S.ready, S.slots, S.invs, S.acked = false, {}, {}, nil
        deps.namedVars(function(fields)
            deps.manifest.build({ configs = deps.configs, jobs = deps.jobs, bits = bits, fields = fields,
                                  skillNames = deps.skillNames,
                                  configHash = deps.configHash, fingerprint = deps.fingerprint }, function(layout)
                S.layout = layout
                baseline()
            end)
        end)
    end

    -- Events: the only places the state changes after the baseline ---------------------------------

    -- A player variable changed: repack the slots cut from it.
    Event.VarTransmit.Subscribe("runeadvisor-wire-var", function(e)
        if not S.ready then return end
        local ok, name, value = pcall(function() return e.var.configName, e.newValue end)
        if not ok or not name then return end
        for _, i in ipairs(S.layout.byBase[name] or {}) do
            local slot = S.layout.slots[i]
            if slot.kind == "base" then
                if type(value) == "number" then S.slots[i] = deps.manifest.packBase(slot, value) end
            elseif slot.kind == "named" then
                if slot.start and type(value) == "number" and math.tointeger(value) then
                    -- A VarBit of this base: its bits of the new value (the same as reading the VarBit).
                    S.slots[i] = fieldValue(slot, math.tointeger(value) >> slot.start)
                elseif slot.name == name and type(value) == "number" then
                    S.slots[i] = fieldValue(slot, value)  -- the variable itself
                else
                    S.slots[i] = fieldValue(slot, deps.readVar(slot.name))  -- the variable itself, or no value: read it
                end
            end
        end
    end)
    -- A skill's xp changed.
    Event.SkillTransmit.Subscribe("runeadvisor-wire-skill", function(e)
        if not S.ready then return end
        local ok, skill, xp = pcall(function() return e.skill, e.newXP end)
        if not ok then return end
        for _, name in ipairs(deps.skillNames) do
            if SkillType[name] == skill and type(xp) == "number" then
                S.slots[S.layout.skillSlot[name]] = xp & 0xFFFFFFFF
            end
        end
    end)
    -- One of the client's inventories was loaded or changed: if it is one of the player's own (the
    -- allow-list), read it again. Any other inventory (a shop, a trade screen) is ignored here.
    local function onInventory(e)
        if not S.ready then return end
        local ok, name = pcall(function() return e.inv.configName end)
        if not ok or not name then return end
        for _, tracked in ipairs(deps.inventoryNames) do
            if tracked == name then
                local set = deps.readInventory(name)
                if set then S.invs[name] = set end
            end
        end
    end
    Event.InventoryAdded.Subscribe("runeadvisor-wire-inv-added", onInventory)
    Event.InventoryUpdated.Subscribe("runeadvisor-wire-inv", onInventory)
    -- Logged in: start over. Lobby or logged out: stop posting.
    Event.GameStateChanged.Subscribe("runeadvisor-wire-state", function(e)
        if e.newState == GameState.GAME then
            login()
        else
            S.ready = false
        end
    end)
    -- The clock: count server ticks down to the next post.
    Event.ServerLogic.Subscribe("runeadvisor-wire-tick", function()
        if not S.ready then return end
        if S.holdTicks > 0 then S.holdTicks = S.holdTicks - 1 end
        S.untilPost = S.untilPost - 1
        if S.untilPost > 0 then return end
        S.untilPost = S.intervalTicks
        post(S.acked and "delta" or "full")
    end)
    if Player.IsAvailable() then login() end

    --- Post on the next tick (e.g. the moment a token is accepted) instead of waiting out the interval.
    local function nudge()
        S.untilPost = math.min(S.untilPost, 1)
    end

    -- `nudge` is what main.lua uses; the rest is for the test harness.
    return { state = S, post = post, diff = diff, nudge = nudge, hash = function() return stateHash(S.slots, S.invs) end }
end

return M
