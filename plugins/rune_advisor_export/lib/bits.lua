-- Bit-level packing, base64 and the FNV-1a hash, for packing the state into bytes (lib/wire.lua).
--
-- Values are written LSB-first into a little-endian byte string: bit i of a value written at bit offset o
-- lands at blob bit o + i. The server reads the same layout by treating the whole blob as one little-endian
-- integer.
-- Blobs travel as base64. Lua strings hold NUL bytes fine, but the client cuts both a PersistentDB string
-- and an HTTP request body at the first NUL byte (tried on the client), so neither can take the raw bytes.
--
-- These run over the whole state (~18 KB) at every post, so they work in batches: the writer emits four
-- bytes at a time, base64 looks up two characters per 12 bits, FNV reads eight bytes per call.

local M = {}

-- Writer -------------------------------------------------------------------------------------------

function M.writer()
    return { acc = 0, n = 0, parts = {}, count = 0 }
end

--- Append the low `width` bits of `value` (width 1..32).
function M.write(w, value, width)
    -- At most 31 bits are pending, so the accumulator never needs more than 63.
    local acc = w.acc | ((value & ((1 << width) - 1)) << w.n)
    local n = w.n + width
    if n >= 32 then
        local count = w.count + 1
        w.parts[count] = string.char(acc & 0xFF, (acc >> 8) & 0xFF, (acc >> 16) & 0xFF, (acc >> 24) & 0xFF)
        w.count = count
        acc = acc >> 32
        n = n - 32
    end
    w.acc, w.n = acc, n
end

--- The bytes written so far; a last partial byte is padded with zero bits.
function M.finish(w)
    while w.n > 0 do
        w.count = w.count + 1
        w.parts[w.count] = string.char(w.acc & 0xFF)
        w.acc = w.acc >> 8
        w.n = w.n - 8
    end
    w.acc, w.n = 0, 0
    return table.concat(w.parts)
end

-- Reader -------------------------------------------------------------------------------------------

function M.reader(s)
    return { s = s, pos = 1, acc = 0, n = 0 }
end

function M.read(r, width)
    while r.n < width do
        local b = r.s:byte(r.pos) or 0
        r.pos = r.pos + 1
        r.acc = r.acc | (b << r.n)
        r.n = r.n + 8
    end
    local v = r.acc & ((1 << width) - 1)
    r.acc = r.acc >> width
    r.n = r.n - width
    return v
end

-- Base64 -------------------------------------------------------------------------------------------

local ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local CHAR, DECODE = {}, {}
for i = 1, #ALPHABET do
    CHAR[i - 1] = ALPHABET:sub(i, i)
    DECODE[ALPHABET:byte(i)] = i - 1
end
-- The two characters for every 12-bit value, so three bytes take two lookups.
local PAIR = {}
for hi = 0, 63 do
    for lo = 0, 63 do PAIR[hi << 6 | lo] = CHAR[hi] .. CHAR[lo] end
end

function M.base64(s)
    local out, k = {}, 0
    local whole = #s - #s % 3
    for i = 1, whole, 3 do
        local a, b, c = s:byte(i, i + 2)
        local n = (a << 16) | (b << 8) | c
        out[k + 1] = PAIR[n >> 12]
        out[k + 2] = PAIR[n & 0xFFF]
        k = k + 2
    end
    -- One or two bytes left over: padded with "=".
    if whole < #s then
        local a, b = s:byte(whole + 1, whole + 2)
        local n = (a << 16) | ((b or 0) << 8)
        out[k + 1] = PAIR[n >> 12]
        out[k + 2] = b and (CHAR[(n >> 6) & 63] .. "=") or "=="
    end
    return table.concat(out)
end

--- Characters outside the alphabet (padding, line breaks) are skipped.
function M.unbase64(s)
    s = s:gsub("[^%w+/]", "")
    local out, k = {}, 0
    local whole = #s - #s % 4
    for i = 1, whole, 4 do
        local a, b, c, d = s:byte(i, i + 3)
        local n = (DECODE[a] << 18) | (DECODE[b] << 12) | (DECODE[c] << 6) | DECODE[d]
        k = k + 1
        out[k] = string.char(n >> 16, (n >> 8) & 0xFF, n & 0xFF)
    end
    -- Two or three characters left over are one or two bytes (a single one carries no whole byte).
    local left = #s - whole
    if left >= 2 then
        local a, b, c = s:byte(whole + 1, whole + 3)
        local n = (DECODE[a] << 18) | (DECODE[b] << 12) | ((c and DECODE[c] or 0) << 6)
        out[k + 1] = left == 3 and string.char(n >> 16, (n >> 8) & 0xFF) or string.char(n >> 16)
    end
    return table.concat(out)
end

-- FNV-1a 32 ----------------------------------------------------------------------------------------

M.FNV_OFFSET, M.FNV_PRIME = 0x811C9DC5, 0x01000193

--- FNV-1a over a string. `seed` replaces the offset basis (the state hash is seeded with the layout hash).
function M.fnv1a32(s, seed)
    local h = seed or M.FNV_OFFSET
    local prime = M.FNV_PRIME
    -- No mask inside the loop: integer multiplication wraps at 64 bits, and the low 32 bits of a product
    -- depend only on the low 32 bits of its factors (the xor with a byte touches only those), so the low
    -- half stays exact whatever accumulates above it. One mask at the end gives the 32-bit hash.
    local whole = #s - #s % 8
    for i = 1, whole, 8 do
        local a, b, c, d, e, f, g, j = s:byte(i, i + 7)
        h = (h ~ a) * prime
        h = (h ~ b) * prime
        h = (h ~ c) * prime
        h = (h ~ d) * prime
        h = (h ~ e) * prime
        h = (h ~ f) * prime
        h = (h ~ g) * prime
        h = (h ~ j) * prime
    end
    for i = whole + 1, #s do
        h = (h ~ s:byte(i)) * prime
    end
    return h & 0xFFFFFFFF
end

return M
