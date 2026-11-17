-- Minimal JSON encoder and decoder: the text form of the posts (lib/http.lua) and of what is kept in
-- PersistentDB. Nothing here reads from the game or sends anything.
-- Arrays are Lua tables with consecutive integer keys starting at 1; everything else is an object.

local json = {}

local escapes = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
    ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encodeString(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        return escapes[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
end

local function isArray(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    for i = 1, n do
        if t[i] == nil then return false end
    end
    return true, n
end

local encodeValue

local function encodeTable(t)
    local array, n = isArray(t)
    local parts = {}
    if array and n > 0 then
        for i = 1, n do parts[i] = encodeValue(t[i]) end
        return "[" .. table.concat(parts, ",") .. "]"
    end
    for k, v in pairs(t) do
        parts[#parts + 1] = encodeString(tostring(k)) .. ":" .. encodeValue(v)
    end
    -- An empty table encodes as {}; callers wanting [] should not send empty arrays.
    return "{" .. table.concat(parts, ",") .. "}"
end

function encodeValue(v)
    local kind = type(v)
    if kind == "nil" then return "null"
    elseif kind == "boolean" then return tostring(v)
    elseif kind == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        if math.type(v) == "integer" then return tostring(v) end
        return string.format("%.14g", v)
    elseif kind == "string" then return encodeString(v)
    elseif kind == "table" then return encodeTable(v)
    end
    error("cannot encode value of type " .. kind)
end

json.encode = encodeValue

-- Decoder: the subset the plugins write (objects, arrays, strings with \uXXXX escapes, numbers, true,
-- false, null -> nil). Returns the value, or raises on malformed input.
local decodeValue

local function skipSpace(s, i)
    local _, j = s:find("^[ \t\r\n]*", i)
    return j + 1
end

local unescapes = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }

local function utf8char(code)
    if code < 0x80 then return string.char(code) end
    if code < 0x800 then return string.char(0xC0 | (code >> 6), 0x80 | (code & 0x3F)) end
    if code < 0x10000 then return string.char(0xE0 | (code >> 12), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F)) end
    return string.char(0xF0 | (code >> 18), 0x80 | ((code >> 12) & 0x3F), 0x80 | ((code >> 6) & 0x3F), 0x80 | (code & 0x3F))
end

local function decodeString(s, i)
    local out, j = {}, i + 1
    while true do
        local c = s:sub(j, j)
        if c == "" then error("unterminated string") end
        if c == '"' then return table.concat(out), j + 1 end
        if c == "\\" then
            local e = s:sub(j + 1, j + 1)
            if e == "u" then
                local code = tonumber(s:sub(j + 2, j + 5), 16)
                if code >= 0xD800 and code <= 0xDBFF and s:sub(j + 6, j + 7) == "\\u" then
                    local low = tonumber(s:sub(j + 8, j + 11), 16)
                    code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                    j = j + 6
                end
                out[#out + 1] = utf8char(code)
                j = j + 6
            else
                out[#out + 1] = unescapes[e] or e
                j = j + 2
            end
        else
            out[#out + 1] = c
            j = j + 1
        end
    end
end

function decodeValue(s, i)
    i = skipSpace(s, i)
    local c = s:sub(i, i)
    if c == "{" then
        local obj = {}
        i = skipSpace(s, i + 1)
        if s:sub(i, i) == "}" then return obj, i + 1 end
        while true do
            local key
            key, i = decodeString(s, skipSpace(s, i))
            i = skipSpace(s, i)
            assert(s:sub(i, i) == ":", "expected ':'")
            local value
            value, i = decodeValue(s, i + 1)
            obj[key] = value
            i = skipSpace(s, i)
            local d = s:sub(i, i)
            if d == "}" then return obj, i + 1 end
            assert(d == ",", "expected ',' in object")
            i = i + 1
        end
    elseif c == "[" then
        local arr = {}
        i = skipSpace(s, i + 1)
        if s:sub(i, i) == "]" then return arr, i + 1 end
        while true do
            local value
            value, i = decodeValue(s, i)
            arr[#arr + 1] = value
            i = skipSpace(s, i)
            local d = s:sub(i, i)
            if d == "]" then return arr, i + 1 end
            assert(d == ",", "expected ',' in array")
            i = i + 1
        end
    elseif c == '"' then
        return decodeString(s, i)
    elseif s:sub(i, i + 3) == "true" then return true, i + 4
    elseif s:sub(i, i + 4) == "false" then return false, i + 5
    elseif s:sub(i, i + 3) == "null" then return nil, i + 4
    else
        local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
        assert(num and #num > 0, "unexpected character at " .. i)
        return tonumber(num), i + #num
    end
end

function json.decode(s)
    local value = decodeValue(s, 1)
    return value
end

return json
