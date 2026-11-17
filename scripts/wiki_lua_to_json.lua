-- Load RuneScape Wiki data modules (Lua source saved from the wiki) and print their plain data as JSON.
--
-- Usage: lua5.4 scripts/wiki_lua_to_json.lua <dir with Module_*.txt files> <Module name> [<Module name> ...]
--
-- Wiki modules expect MediaWiki's Scribunto environment (mw.loadData, require('Module:...')).
-- This provides just enough of it to evaluate data modules: requires of other saved modules load
-- them from <dir>; price/argument helpers are stubbed (prices aren't needed). Functions are dropped
-- from the output. Data is from the RuneScape Wiki (CC BY-NC-SA 3.0).

local dir = arg[1]

local function file_for(name)
    return dir .. "/" .. name:gsub("[:/ ]", "_") .. ".txt"
end

local stubs = {
    ["Module:ExchangeLite"] = setmetatable({}, { __index = function() return function() return 0 end end }),
    ["Module:Arguments"] = { getArgs = function() return {} end },
    ["Module:Yesno"] = function(v) return v end,
}

local loaded = {}
local function load_module(name)
    name = name:gsub("_", " ")
    if stubs[name] then return stubs[name] end
    if loaded[name] ~= nil then return loaded[name] end
    local path = file_for(name)
    local fh = io.open(path, "r")
    if not fh then error("missing module source " .. path) end
    local src = fh:read("a")
    fh:close()
    local chunk = assert(load(src, name, "t", setmetatable({ require = load_module, mw = mw }, { __index = _G })))
    loaded[name] = chunk()
    return loaded[name]
end

mw = { loadData = load_module, loadJsonData = function() return {} end, getContentLanguage = function() return {} end }

local function encode(v, seen)
    local t = type(v)
    if t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        return string.format("%.10g", v)
    elseif t == "string" then
        return '"' .. v:gsub('[%c"\\]', function(c)
            return ({ ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n", ["\t"] = "\\t" })[c] or string.format("\\u%04x", c:byte())
        end) .. '"'
    elseif t == "boolean" then
        return tostring(v)
    elseif t == "table" then
        seen = seen or {}
        if seen[v] then return "null" end
        seen[v] = true
        local n = #v
        local is_array = n > 0
        for k in pairs(v) do
            if type(k) ~= "number" or k < 1 or k > n or k % 1 ~= 0 then is_array = false break end
        end
        local parts = {}
        if is_array then
            for i = 1, n do parts[#parts + 1] = encode(v[i], seen) end
            seen[v] = nil
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for k, val in pairs(v) do
            if type(val) ~= "function" then keys[#keys + 1] = k end
        end
        table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
        for _, k in ipairs(keys) do
            parts[#parts + 1] = encode(tostring(k)) .. ":" .. encode(v[k], seen)
        end
        seen[v] = nil
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return "null"
end

local out = {}
for i = 2, #arg do
    out[#out + 1] = encode(arg[i]) .. ":" .. encode(load_module(arg[i]))
end
print("{" .. table.concat(out, ",") .. "}")
