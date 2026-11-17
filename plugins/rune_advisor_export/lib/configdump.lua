-- Config entries: the static shape of quests and achievements as the client sees them, which is what the
-- Rune Advisor planner and server need to work out what player variables mean. The plugin builds entries
-- to hash them (lib/canon.lua) and, after a game update, to send the server the ones it doesn't know yet
-- (lib/configsync.lua).
--
-- An entry holds only what doesn't change while playing, in particular which VarPlayer each achievement
-- test reads and which VarPlayer each quest's progress lives in:
--   quest        progress var (name, id, start/end states), prerequisites, start tiles, and every ParamType
--                value the quest config carries (QUEST_RELEASE_YEAR...)
--   achievement  category ids, membership, reward text, visibility, grace expiry, descriptions (default and
--                ironman variants), prerequisite and requirement tests with their var names, and how many of
--                each group's tests must pass
-- Nothing about the player is in an entry: no var values, no progress, no "complete" flags. The var
-- values are posted separately (lib/wire.lua) and the state is derived from those.
--
-- This file loads no other library.

local M = {}

local function enumName(enumTable, value)
    local ok, name = pcall(function() return enumTable.FromID(value) end)
    return ok and name or nil
end

local function skillNames(skillTypes)
    local names = {}
    for _, skill in ipairs(skillTypes or {}) do
        names[#names + 1] = enumName(SkillType, skill) or tostring(skill)
    end
    return names
end

-- A VarType's identity: name and id, plus the base var and bit mask when it's a VarBit.
local function varInfo(v)
    if v == nil then return nil end
    local ok, info = pcall(function()
        local out = { name = v.configName, id = v.id }
        if v.isVarBit then
            out.bit = true
            out.mask = v.varBitMask
            out.base = v.baseVar and v.baseVar.configName or nil
        end
        return out
    end)
    return ok and info or nil
end

local function coordInfo(c)
    if c == nil then return nil end
    local ok, info = pcall(function() return { c.level, c.x, c.z } end)
    return ok and info or nil
end

local function scalar(value)
    local t = type(value)
    if t == "number" or t == "string" or t == "boolean" then return value end
    if t == "table" or t == "userdata" then
        -- A config proxy (quest, item...): keep its name.
        local ok, name = pcall(function() return value.configName or value.name end)
        if ok and type(name) == "string" then return name end
    end
    return nil
end

-- Every ParamType value a config carries, by param name. The API documents only the lookup
-- (GetParamValue) and the list of params present, so this is the full set for the config.
local function collectParams(cfg)
    local out = {}
    local ok = pcall(function()
        for _, param in ipairs(cfg.params or {}) do
            local value = scalar(cfg:GetParamValue(param))
            if value ~= nil then out[param.configName] = value end
        end
    end)
    return ok and out or {}
end

-- Requirement/prerequisite tests ({ skills, quests, achievements, varTotals, varTestBits }), flattened
-- with the vars they read. A test's isComplete is the player's state and is left out.
local function collectTests(tests)
    local out = {}
    if not tests then return out end
    for _, t in ipairs(tests.skills or {}) do
        out[#out + 1] = { kind = "skill", group = t.group, skills = skillNames(t.skillTypes),
            totalLevels = t.totalLevels }
    end
    -- Names alone are ambiguous: 11 achievement names and "Rune Mysteries" are used by two configs each,
    -- so the referenced config's id rides along.
    for _, t in ipairs(tests.quests or {}) do
        out[#out + 1] = { kind = "quest", group = t.group, quest = t.quest and t.quest.name,
            questId = t.quest and t.quest.id }
    end
    for _, t in ipairs(tests.achievements or {}) do
        out[#out + 1] = { kind = "achievement", group = t.group,
            achievement = t.achievement and t.achievement.name, achievementId = t.achievement and t.achievement.id }
    end
    for _, t in ipairs(tests.varTotals or {}) do
        local vars = {}
        for _, v in ipairs(t.vars or {}) do vars[#vars + 1] = varInfo(v) end
        out[#out + 1] = { kind = "varTotal", group = t.group, vars = vars, totalValue = t.totalValue,
            description = t.description }
    end
    for _, t in ipairs(tests.varTestBits or {}) do
        out[#out + 1] = { kind = "varBit", group = t.group, var = varInfo(t.var), bitIndex = t.bitIndex,
            bitValue = t.bitValue, description = t.description }
    end
    return out
end

-- How many tests each group needs: a group can be "any 2 of these 5". Methods are 0-indexed by group.
local function groupCounts(a, countProperty, method)
    local counts = {}
    local ok = pcall(function()
        local n = a[countProperty] or 0
        for g = 0, n - 1 do counts[#counts + 1] = a[method](a, g) end
    end)
    return ok and counts or nil
end

local function descriptions(a)
    local out = {}
    pcall(function()
        for _, d in ipairs(a.descriptions or {}) do
            out[#out + 1] = { type = enumName(AchievementDescriptionType, d.type) or d.type, text = d.text }
        end
    end)
    return out
end

function M.quest(q)
    local ok, entry = pcall(function()
        local master = q.masterQuestVars and q.masterQuestVars[1]
            local preReqQuests, preReqSkills, starts = {}, {}, {}
            for _, pq in ipairs(q.preReqQuests or {}) do preReqQuests[#preReqQuests + 1] = pq.name end
            for _, ps in ipairs(q.preReqSkills or {}) do
                preReqSkills[#preReqSkills + 1] = { skill = enumName(SkillType, ps.skillType) or ps.skillType,
                    level = ps.minimumLevel }
            end
            for _, c in ipairs(q.startCoordGrids or {}) do starts[#starts + 1] = coordInfo(c) end
            return {
                id = q.id, configName = q.configName, name = q.name, sortName = q.sortName,
                deprecated = q.deprecated, members = q.isMembers, questPoints = q.questPoints,
                type = enumName(QuestType, q.type), difficulty = enumName(QuestDifficulty, q.difficulty),
                progressVar = master and varInfo(master.varPlayer) or nil,
                startState = master and master.startState or nil,
                endState = master and master.endState or nil,
                preReqQuestPoints = q.preReqQuestPoints,
                preReqQuests = preReqQuests,
                preReqSkills = preReqSkills,
                startCoords = starts,
                viaCoord = coordInfo(q.viaCoordGrid),
                params = collectParams(q),
            }
        end)
    if ok then return entry end
    log("Rune Advisor: quest config skipped: " .. tostring(entry))
    return nil
end

function M.achievement(a)
    local ok, entry = pcall(function()
            -- The requirement tests are `a.requirements` (the docs' property table calls them
            -- `achievements`, which reads nil on the client).
            local reqs = a.requirements
            return {
                id = a.id, configName = a.configName, name = a.name, runeScore = a.runeScore,
                members = a.isMembers, category = a.category, subCategory = a.subCategory,
                reward = a.reward ~= "" and a.reward or nil,
                hiddenType = enumName(AchievementHiddenType, a.hiddenType),
                graceExpiryRunedate = a.graceExpiryRunedate,
                tallyTarget = a.tallyTarget,
                descriptions = descriptions(a),
                prereqs = collectTests(a.prereqs),
                prereqGroupsNeeded = a.completionCountForPrereqGroups,
                prereqSubItems = groupCounts(a, "prereqGroupCount", "GetSubItemCompletionCountForPrereqGroup"),
                requirements = collectTests(reqs),
                requiredGroups = a.completionCountForRequirementGroups,
                requiredSubItems = groupCounts(a, "requirementGroupCount", "GetSubItemCompletionCountForRequirementGroup"),
            }
        end)
    if ok then return entry end
    log("Rune Advisor: achievement config skipped: " .. tostring(entry))
    return nil
end

-- The entry builder for a config kind ("quests" / "achievements") and the config table it walks.
M.KINDS = {
    quests = { table = function() return config.Quest end, build = M.quest, perTick = 100 },
    achievements = { table = function() return config.Achievement end, build = M.achievement, perTick = 150 },
}

return M
