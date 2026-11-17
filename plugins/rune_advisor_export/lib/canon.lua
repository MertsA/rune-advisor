-- Canonical text and hashes for config entries (the game's quest and achievement definitions as
-- lib/configdump.lua reads them), so the plugin and the Rune Advisor server can tell which definitions they
-- each know by comparing hashes instead of sending the definitions. The server writes the same text for the
-- same entry, so the rules are deliberately small:
--   nil -> n, booleans -> t/f, integers (and whole floats) -> decimal, other floats -> %.14g (what
--   lib/json.lua writes, so a float survives the JSON round trip unchanged), strings -> "..." with only
--   backslash and quote escaped, empty tables -> {} (json.lua writes them as {} too), arrays -> [a,b],
--   objects -> {"k":v,...} with keys sorted as strings.
-- State keys are skipped at every depth (progress, progressType, complete): a config's hash must only
-- change when the config does, not when the player progresses. Entries no longer carry them
-- (lib/configdump.lua); the rule stays because the server also hashes older entries that do.
-- Hashes are FNV-1a 32 over the UTF-8 bytes; the fingerprint of a whole config set is FNV-1a over
-- "q<id>:<hash>;" for every quest then "a<id>:<hash>;" for every achievement, ids ascending.

local M = {}

-- FNV-1a lives in lib/bits.lua, the one implementation both the wire protocol and these hashes use.
local bits = require("lib/bits")
local fnv1a32 = bits.fnv1a32

local STATE_KEYS = { progress = true, progressType = true, complete = true }

local function quote(s)
    if not s:find('[\\"]') then return '"' .. s .. '"' end
    return '"' .. (s:gsub('[\\"]', '\\%0')) .. '"'
end

-- '"key":' for every key met so far: entries share a small vocabulary of keys.
local KEY_TEXT = setmetatable({}, { __index = function(t, k)
    local text = quote(k) .. ":"
    t[k] = text
    return text
end })

local function number(v)
    if math.type(v) == "integer" then return string.format("%d", v) end
    if v ~= v or v == math.huge or v == -math.huge then return "n" end
    local whole = math.tointeger(v)
    if whole then return string.format("%d", whole) end
    return string.format("%.14g", v)
end

-- Append the canonical text of `v` to the list `out` (n pieces so far); returns the new count. The pieces
-- are joined once at the end, so nested tables aren't copied again at every level.
local function append(out, n, v)
    local kind = type(v)
    if kind == "string" then
        out[n + 1] = quote(v)
        return n + 1
    elseif kind == "number" then
        out[n + 1] = number(v)
        return n + 1
    elseif kind == "boolean" then
        out[n + 1] = v and "t" or "f"
        return n + 1
    elseif kind ~= "table" then
        out[n + 1] = "n"  -- nil, and anything that isn't data
        return n + 1
    end
    if next(v) == nil then
        out[n + 1] = "{}"
        return n + 1
    end
    -- An array has exactly the keys 1..count. Without a first element it can't be one, which spares
    -- counting the keys of every object.
    if v[1] ~= nil then
        local count = 0
        for _ in pairs(v) do count = count + 1 end
        local array = true
        for i = 2, count do
            if v[i] == nil then array = false break end
        end
        if array then
            out[n + 1] = "["
            n = n + 1
            for i = 1, count do
                if i > 1 then
                    out[n + 1] = ","
                    n = n + 1
                end
                n = append(out, n, v[i])
            end
            out[n + 1] = "]"
            return n + 1
        end
    end
    local keys, k = {}, 0
    for key in pairs(v) do
        key = tostring(key)
        if not STATE_KEYS[key] then
            k = k + 1
            keys[k] = key
        end
    end
    table.sort(keys)
    out[n + 1] = "{"
    n = n + 1
    for i = 1, k do
        local key = keys[i]
        local value = v[key]
        if value == nil then value = v[tonumber(key)] end
        out[n + 1] = i > 1 and ("," .. KEY_TEXT[key]) or KEY_TEXT[key]
        n = append(out, n + 1, value)
    end
    out[n + 1] = "}"
    return n + 1
end

--- The canonical text of a value.
function M.canonical(v)
    local out = {}
    append(out, 0, v)
    return table.concat(out)
end
local canonical = M.canonical

--- The hash of one config entry (as lib/configdump.lua builds it).
function M.configHash(entry)
    return fnv1a32(canonical(entry))
end

--- The fingerprint of a config set: hashes = { quests = {[id] = hash}, achievements = {[id] = hash} }.
function M.fingerprint(hashes)
    local h = bits.FNV_OFFSET
    for _, kind in ipairs({ "quests", "achievements" }) do
        local ids = {}
        for id in pairs(hashes[kind] or {}) do ids[#ids + 1] = tonumber(id) end
        table.sort(ids)
        local letter = kind:sub(1, 1)
        for _, id in ipairs(ids) do
            local value = hashes[kind][id]
            if value == nil then value = hashes[kind][tostring(id)] end
            h = fnv1a32(string.format("%s%d:%d;", letter, id, value), h)
        end
    end
    return h
end

return M
