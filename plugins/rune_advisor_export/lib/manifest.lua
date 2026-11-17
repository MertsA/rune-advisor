-- The wire layout: which bits of which player variables the plugin keeps and posts, worked out at login
-- from the game's own quest and achievement definitions. The server works the same layout out from the same
-- definitions, and the two must agree bit for bit: a hash of the layout's description travels with every
-- post, and the server refuses a post whose layout it does not share.
--
-- Why bits and not whole variables: a quest's or achievement's state is decided by tests on player
-- variables ("this variable has reached 12", "bit 5 of that variable is set"). The plugin sends only what
-- those tests can read, so the state can be derived on the other side and nothing else in the variable
-- travels. A variable only tested against small numbers is sent in as few bits as hold the largest of them.
--
-- The layout is a list of slots, in this order:
--   base entries   one per player variable ("base var") that a quest or achievement test reads, in name
--                  order. Each is cut from the variable's 32-bit value as `ranges` ({start, stop, size}:
--                  bits start..stop, sent as `size` bits; when size is smaller than the span the value is
--                  capped at the largest number `size` bits hold) followed by single tested `bits`.
--   named vars     the hand-listed variables (lib/exported_vars.lua: currencies, storages, item log...), 32 bits each, by name
--   skills         xp per skill, 32 bits each, in SkillType order
--   members        1 bit
-- Building it walks every quest and achievement, so it runs as jobs, a slice per tick (lib/jobs.lua): one
-- callback may not run longer than 1000 ms, and a long one would freeze the game for as long as it ran.
-- The same walk hashes each definition (deps.configHash(kind, cfg) -> id, hash; lib/canon.lua) and the whole
-- set (layout.configHashes, layout.fingerprint): the full post tells the server which set of definitions the
-- layout was derived from, and the server asks for the ones it lacks (lib/configsync.lua).

local M = {}

-- Configs walked per slice (the plugin runs a slice every client logic tick, 20 ms). On the client an
-- achievement takes about 0.03 ms to read and 0.1 ms to hash, so 40 is ~5 ms.
local CONFIGS_PER_SLICE = 40

--- How many bits it takes to write n (0 for 0).
local function bitLength(n)
    local w = 0
    while n > 0 do n = n >> 1 w = w + 1 end
    return w
end

-- What a test's variable is: its name and id, and where its value sits. A plain player variable is its own
-- base var, bits 0..31. A VarBit is a field of a base var: `start`..`stop` are the field's bits within the
-- base var and `field` its width. Read the moment the config object is met (kept for later, the client's
-- objects read garbage).
local function varInfo(v)
    if v == nil then return nil end
    local ok, info = pcall(function()
        local out = { name = v.configName, id = v.id, base = v.configName, start = 0, stop = 31, field = nil }
        if v.isVarBit then
            local mask = v.varBitMask
            if type(mask) == "number" and mask > 0 then
                local start = 0
                while (mask >> start) & 1 == 0 do start = start + 1 end
                out.start, out.stop = start, start + bitLength(mask >> start) - 1
                out.field = out.stop - out.start + 1
            end
            local okBase, base = pcall(function() return v.baseVar.configName end)
            if okBase and base then out.base = base end
        end
        return out
    end)
    if ok and type(info.id) == "number" and info.name then return info end
    return nil
end

-- What the tests need of one variable, collected over every quest and achievement:
--   count   some test compares the variable's value with a number; `max` is the largest such number
--   bit     some test looks at single bits of it; `bits` is the set of bit indexes tested
local function need(needs, info)
    local n = needs[info.id]
    if not n then
        n = { info = info, max = 0, bits = {}, count = false, bit = false }
        needs[info.id] = n
    end
    return n
end

-- Note the variables one achievement's tests read (its prerequisites or its requirements).
local function addTestVars(needs, tests)
    if not tests then return end
    -- "these variables add up to at least totalValue"
    for _, test in ipairs(tests.varTotals or {}) do
        for _, v in ipairs(test.vars or {}) do
            local info = varInfo(v)
            if info then
                local n = need(needs, info)
                n.count = true
                n.max = math.max(n.max, test.totalValue or 0)
            end
        end
    end
    -- "bit bitIndex of this variable is set"
    for _, test in ipairs(tests.varTestBits or {}) do
        local info = varInfo(test.var)
        if info then
            local n = need(needs, info)
            n.bit = true
            n.bits[test.bitIndex or 0] = true
        end
    end
end

-- From what the tests need of each variable to the base entries, in four steps. Several tested variables
-- can be fields of the same base var, so the pieces are gathered per base var first.

local function isFullSize(range)
    return range.size == range.stop - range.start + 1
end

-- Step 1. The pieces each base var must supply: per base var name, { ranges = {...}, bits = {[bit] = true} }.
--   only compared with numbers:  the field as a range, in as few bits as hold the largest number compared
--   only tested bit by bit:      those single bits
--   both:                        the whole field, every bit of it
local function piecesPerBaseVar(needs)
    local perBase = {}
    for _, n in pairs(needs) do
        local info = n.info
        local pieces = perBase[info.base]
        if not pieces then
            pieces = { ranges = {}, bits = {} }
            perBase[info.base] = pieces
        end
        local span = info.stop - info.start + 1
        if n.count and not n.bit then
            local width = math.max(1, bitLength(n.max))
            if info.field then width = math.min(width, info.field) end
            pieces.ranges[#pieces.ranges + 1] = { start = info.start, stop = info.stop, size = math.min(width, span) }
        elseif n.bit and not n.count then
            for b in pairs(n.bits) do pieces.bits[info.start + b] = true end
        else
            pieces.ranges[#pieces.ranges + 1] = { start = info.start, stop = info.stop, size = span }
        end
    end
    return perBase
end

-- Step 2. A range sent in fewer bits than it spans is capped, which is only right while nothing else
-- reads those bits. A range that overlaps another range, or holds a bit tested on its own, is therefore
-- sent at full size. (Ranges are put in order first: by start, the longer first.)
local function widenClashingRanges(ranges, bits)
    table.sort(ranges, function(a, b)
        if a.start ~= b.start then return a.start < b.start end
        return (a.stop - a.start) > (b.stop - b.start)
    end)
    for _, range in ipairs(ranges) do
        local clash = false
        for b in pairs(bits) do
            if range.start <= b and b <= range.stop then clash = true end
        end
        for _, other in ipairs(ranges) do
            if other ~= range and other.start <= range.stop and range.start <= other.stop then clash = true end
        end
        if clash then range.size = range.stop - range.start + 1 end
    end
end

-- Step 3. Drop a range that says nothing new: one lying inside a full-size range already kept, or one
-- with the same start and stop as a range already kept.
local function dropCoveredRanges(ranges)
    local kept = {}
    for _, range in ipairs(ranges) do
        local covered = false
        for _, k in ipairs(kept) do
            local same = k.start == range.start and k.stop == range.stop
            local inside = k.start <= range.start and range.stop <= k.stop
            if same or (isFullSize(k) and inside) then covered = true end
        end
        if not covered then kept[#kept + 1] = range end
    end
    return kept
end

-- Step 4. Drop a single bit that a kept full-size range already carries; the rest in ascending order.
local function bitsNotCovered(bits, keptRanges)
    local left = {}
    for b in pairs(bits) do
        local covered = false
        for _, k in ipairs(keptRanges) do
            if isFullSize(k) and k.start <= b and b <= k.stop then covered = true end
        end
        if not covered then left[#left + 1] = b end
    end
    table.sort(left)
    return left
end

-- The base entries, one per base var in name order: { kind = "base", name, ranges, bits, width }, `width`
-- being the number of bits the entry takes in the state.
local function baseEntries(needs)
    local perBase = piecesPerBaseVar(needs)
    local names = {}
    for name in pairs(perBase) do names[#names + 1] = name end
    table.sort(names)
    local entries = {}
    for _, name in ipairs(names) do
        local pieces = perBase[name]
        widenClashingRanges(pieces.ranges, pieces.bits)
        local ranges = dropCoveredRanges(pieces.ranges)
        local bits = bitsNotCovered(pieces.bits, ranges)
        local width = #bits
        for _, range in ipairs(ranges) do width = width + range.size end
        entries[#entries + 1] = { kind = "base", name = name, ranges = ranges, bits = bits, width = width }
    end
    return entries
end

--- The layout written out as text, which the layout hash is taken over (the server writes the same text).
local function describe(slots, named, skillNames)
    local parts = {}
    for _, s in ipairs(slots) do
        if s.kind == "base" then
            local rs, bs = {}, {}
            for _, r in ipairs(s.ranges) do rs[#rs + 1] = string.format("%d-%d/%d", r.start, r.stop, r.size) end
            for _, b in ipairs(s.bits) do bs[#bs + 1] = tostring(b) end
            parts[#parts + 1] = s.name .. ":" .. table.concat(rs, ",") .. ":" .. table.concat(bs, ",")
        end
    end
    return table.concat(parts, ";") .. "|named:" .. table.concat(named, ",") .. "|skills:" .. table.concat(skillNames, ",") .. "|members"
end

--- Pack a base entry's slot value from its variable's 32-bit value `u`: each range's bits (capped when the
--- range is sent in fewer bits than it spans), then each single bit, lowest first.
function M.packBase(slot, u)
    u = u & 0xFFFFFFFF
    local acc, n = 0, 0
    for _, r in ipairs(slot.ranges) do
        local span = r.stop - r.start + 1
        local v = (u >> r.start) & ((1 << span) - 1)
        if r.size < span then v = math.min(v, (1 << r.size) - 1) end
        acc = acc | (v << n)
        n = n + r.size
    end
    for _, b in ipairs(slot.bits) do
        acc = acc | (((u >> b) & 1) << n)
        n = n + 1
    end
    return acc
end

--- Build the layout as jobs. deps: configs, jobs, bits, named (sorted list of hand-listed var names),
--- namedBase (name -> base var name, for those that are fields of another variable), clientOnly (set of
--- names the client only has as a client variable), skillNames, configHash, fingerprint.
--- onDone(layout): { slots, offset (bit offset of each slot), totalBits, description, hash,
---   byBase (base var name -> indexes of the slots cut from it, so a change event finds them),
---   skillSlot (skill name -> slot index), membersSlot, configHashes, fingerprint }.
function M.build(deps, onDone)
    local needs = {}
    local hashes = { quests = {}, achievements = {} }
    local function hashConfig(kind, cfg)
        if not deps.configHash then return end
        local ok, id, h = pcall(deps.configHash, kind, cfg)
        if ok and id ~= nil and h ~= nil then hashes[kind][id] = h end
    end
    deps.jobs.add({
        name = "wire: quests", items = deps.configs.all(config.Quest), perTick = CONFIGS_PER_SLICE,
        each = function(q)
            hashConfig("quests", q)
            -- A quest's progress lives in one variable that counts up to its end state.
            pcall(function()
                local m = q.masterQuestVars and q.masterQuestVars[1]
                local info = m and varInfo(m.varPlayer)
                if info then
                    local n = need(needs, info)
                    n.count = true
                    n.max = math.max(n.max, m.endState or 0)
                end
            end)
        end,
    })
    deps.jobs.add({
        name = "wire: achievements", items = deps.configs.all(config.Achievement), perTick = CONFIGS_PER_SLICE,
        each = function(a)
            hashConfig("achievements", a)
            pcall(function() addTestVars(needs, a.prereqs) end)
            pcall(function() addTestVars(needs, a.requirements) end)
        end,
        done = function()
            -- Both walks are done: lay the slots out in the order the header gives.
            local slots = baseEntries(needs)
            local byBase = {}
            for i, s in ipairs(slots) do
                byBase[s.name] = byBase[s.name] or {}
                table.insert(byBase[s.name], i)
            end
            for _, name in ipairs(deps.named) do
                slots[#slots + 1] = { kind = "named", name = name, width = 32,
                                      clientOnly = deps.clientOnly and deps.clientOnly[name] or nil }
                local base = deps.namedBase[name] or name
                byBase[base] = byBase[base] or {}
                table.insert(byBase[base], #slots)
            end
            local skillSlot = {}
            for _, name in ipairs(deps.skillNames) do
                slots[#slots + 1] = { kind = "skill", name = name, width = 32 }
                skillSlot[name] = #slots
            end
            slots[#slots + 1] = { kind = "members", name = "members", width = 1 }
            local offset, total = {}, 0
            for i, s in ipairs(slots) do offset[i] = total total = total + s.width end
            local description = describe(slots, deps.named, deps.skillNames)
            onDone({ slots = slots, byBase = byBase, skillSlot = skillSlot, membersSlot = #slots,
                     totalBits = total, offset = offset, description = description,
                     hash = deps.bits.fnv1a32(description),
                     configHashes = hashes, fingerprint = deps.fingerprint and deps.fingerprint(hashes) or nil })
        end,
    })
end

return M
