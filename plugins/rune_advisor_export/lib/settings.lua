-- Settings: the Rune Advisor API token and the consent choice under it, on the client's own settings layer
-- (the settings icon on this plugin in the Plugin Hub's Manage tab; `"hasSettingsIcon": true` in the
-- manifest; the client fires Event.SettingsLayerReady with an empty layer to fill). The token is created
-- on the Rune Advisor website (one per account owner) and pasted into the field. It is only saved (this
-- device's PersistentDB, `settings:apiToken`) once the API has accepted it: a couple of seconds after the
-- last edit the plugin asks the API whether the token is known, and a mistyped or cut-short token is
-- reported instead of stored. While the API can't be reached no token can be saved (the line under the
-- field says so). Nothing is posted until a token is saved and the httpAccess permission is granted.
--
-- The field is plain text on purpose: a masked field (ui.TextContentVisibilityMode other than VISIBLE) is
-- "restricted" and its content reads back as an empty string (the InputField docs say so), so a hidden token
-- could never be checked or saved. A saved token
-- isn't shown in the field (the status line says one is set) until the user asks for it: the button under
-- the status line puts the saved token into the field for half a minute, so that it can be selected and
-- copied out again (the token is shown only once on the website, and the plugin API has no clipboard
-- function to copy it for the user). Pressing the button again, or the half minute passing, empties the
-- field; the saved token is untouched either way.
--
-- An empty field never deletes a saved token. The client fires ONCONTENTCHANGED with empty content on its
-- own when the layer is opened (an earlier version lost its token that way: opening the panel, with nothing
-- typed, wiped it), so emptiness can't be read as the user's wish. Emptying the field only cancels
-- a pasted token that hasn't been checked yet; the saved one goes only when the server says it was revoked
-- (M.forget) or a different token is pasted and accepted.
--
-- Consent to analysis. Under the token sits one choice: whether the data of the character being played may
-- be analysed to improve the planner for everyone. It is off until the user turns it on, and it changes
-- nothing about what is sent. The answer belongs to the character as the server stores it, where the
-- website can change it too; the plugin keeps nothing of it on this computer, only (in memory) what the
-- server last said for the character logged in: the reply to every accepted post carries it (M.noteConsent),
-- and a login always posts. The button asks the server to change it and shows the new answer only once the
-- server has confirmed it, so the panel never claims a choice the server doesn't have.
--
-- PersistentDB calls are always made from Lua code (PersistentDB:GetString(...) inside a closure), never
-- by handing the method to pcall: `pcall(PersistentDB.GetString, PersistentDB, key)` shuts the whole
-- game down when tried on the client.

local M = {}

local TOKEN_KEY = "settings:apiToken"
local INTERVAL_KEY = "settings:postInterval"  -- seconds between posts, as the server last asked
local CHECK_DELAY_TICKS = 3  -- ~2 s after the last edit before the token is checked with the API
local SHOW_TICKS = 50  -- how long "Show the saved token" leaves it in the field: ~30 s of server ticks
local SHOW_LABEL, HIDE_LABEL = "Show the saved token", "Hide the token"

-- The line shown under the field when something needs the user's attention (nil: all is well).
M.problem = nil
-- What the user typed but the API hasn't confirmed yet: { token, ticks }.
local pending = nil
local checking = false
-- Server ticks left before a token shown in the field is hidden again; nil while it isn't shown.
local shown = nil

-- The server's last word on a character's consent to analysis: { player, value }, in memory only. Then the
-- consent line's trouble, if any (nil: all is well), and whether a change is on its way to the server.
local consentKnown = nil
local consentProblem = nil
local consentBusy = false

local function trim(s)
    return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", ""))
end

--- The saved API token, or nil when none is set.
function M.token()
    local ok, value = pcall(function() return PersistentDB:GetString(TOKEN_KEY) end)
    if not ok or type(value) ~= "string" then return nil end
    value = trim(value)
    return value ~= "" and value or nil
end

local function saveToken(value)
    value = trim(value)
    if value == "" then
        pcall(function() PersistentDB:Delete(TOKEN_KEY) end)
    else
        pcall(function() PersistentDB:SetString(TOKEN_KEY, value) end)
    end
end

local function refresh()
    if M.refresh then M.refresh() end
end

local function currentPlayer()
    local ok, name = pcall(function() return M.player and M.player() end)
    return ok and type(name) == "string" and name ~= "" and name or nil
end

--- The logged-in character's consent to analysis as the server last told it: true, false, or nil when it
--- hasn't yet (not logged in, no token, or no post accepted since login).
function M.consent()
    local player = currentPlayer()
    if player and consentKnown and consentKnown.player == player then return consentKnown.value end
    return nil
end

--- The server's word on a character's consent (a post's reply, or its answer to the button).
function M.noteConsent(player, value)
    if type(player) ~= "string" or type(value) ~= "boolean" then return end
    consentKnown = { player = player, value = value }
    consentProblem = nil
    refresh()
end

local function forgetConsent()
    consentKnown, consentProblem, consentBusy = nil, nil, false
end

--- The consent button: ask the server to flip the character's consent. Nothing changes here until it answers.
function M.toggleConsent()
    local player = currentPlayer()
    if M.token() == nil then
        consentProblem = "Paste a token first"
    elseif player == nil then
        consentProblem = "Log in to a character first: the choice is per character"
    elseif not consentBusy and M.setConsent then
        consentBusy, consentProblem = true, nil
        M.setConsent(player, not (M.consent() == true), function(now, why)
            consentBusy = false
            if type(now) == "boolean" then
                M.noteConsent(player, now)
            elseif why == "unposted" then
                consentProblem = "This character hasn't been sent yet: try again in a minute"
            else
                consentProblem = "The Rune Advisor API didn't confirm it: nothing was changed"
            end
            refresh()
        end)
    end
    refresh()
end

-- Stop showing the saved token: empty the field (only if it still holds that token: something the user
-- pasted over it stays) and put the button's label back. The saved token itself is not touched.
local function hideToken()
    shown = nil
    pcall(function()
        if M.field and trim(M.field.content) == (M.token() or "") then M.field.content = "" end
        if M.revealButton then M.revealButton.label = SHOW_LABEL end
    end)
end

--- The button under the status line: put the saved token into the field so the user can select and copy
--- it, or take it out again if it is showing. With no token saved it only says so.
function M.toggleShown()
    if shown then
        hideToken()
    elseif M.token() == nil then
        M.problem = "No token is saved yet"
    else
        pending, M.problem = nil, nil  -- showing the saved token is not a new token to check
        shown = SHOW_TICKS
        pcall(function()
            M.field.content = M.token()
            M.revealButton.label = HIDE_LABEL
        end)
    end
    refresh()
end

--- Drop the token (the server revoked it): nothing is posted until a new one is pasted and accepted.
function M.forget(reason)
    saveToken("")
    forgetConsent()
    M.problem = reason
    pending = nil
    shown = nil
    if M.revealButton then pcall(function() M.revealButton.label = SHOW_LABEL end) end
    if M.field then pcall(function() M.field.content = "" end) end
    refresh()
end

--- The post interval (seconds) the server last asked for, or nil. Kept as a string: only the string
--- accessors of PersistentDB are known to be safe on the client (the typed ones are untested there).
function M.interval()
    local ok, value = pcall(function() return PersistentDB:GetString(INTERVAL_KEY) end)
    return (ok and type(value) == "string") and tonumber(value) or nil
end

function M.setInterval(seconds)
    pcall(function() PersistentDB:SetString(INTERVAL_KEY, tostring(seconds)) end)
end

local MESSAGES = {
    unknown = "Token not recognised: check you copied all of it",
    revoked = "That token was revoked: make a new one at runeadvisor.com/account",
    offline = "Can't reach the Rune Advisor API: try again later",
    ["no-permission"] = "Grant httpAccess to check and save the token",
}

--- Called every server tick: once the user has stopped editing, check the pending token with the API
--- (deps.check) and save it only when the API knows it.
function M.tick()
    if shown then  -- a shown token goes back into hiding by itself
        shown = shown - 1
        if shown <= 0 then
            hideToken()
            refresh()
        end
    end
    if not pending or checking or not M.check then return end
    pending.ticks = pending.ticks - 1
    if pending.ticks > 0 then return end
    local token = pending.token
    pending = nil
    checking = true
    M.check(token, function(result, reason)
        checking = false
        if result == "ok" then
            saveToken(token)
            forgetConsent()  -- another token may be another account: the first post's reply tells its answer
            M.problem = nil
            log("Rune Advisor: API token accepted and saved")
            if M.onSaved then M.onSaved() end
        else
            M.problem = MESSAGES[result] or MESSAGES.offline
            log("Rune Advisor: API token not saved (" .. tostring(result) .. (reason and (": " .. tostring(reason)) or "") .. ")")
        end
        refresh()
    end)
end

-- Fill the settings layer the client hands us: a title, the token field and the status line. Built the
-- way the UI tutorial does it (sizes as pixels or fractions of the parent).
local function populate(component, deps)
    -- A layer of our own filling the component the client hands over, then the controls in it.
    local layer = ui.Layer.new(component)
    layer:SetSize(0, 0, 1, 1)

    local title = ui.Text.new(layer)
    title.font = id.Font.MUSEO_SANS_15PT_REGULAR
    title:SetSize(-20, 20, 1)
    title:SetPos(10, 10)
    title.content = "Rune Advisor API token (make one at runeadvisor.com/account)"

    local field = ui.InputField.new(layer)
    field.stylesheetID = id.StyleSheet.INPUT_DEFAULT
    field.emptyText = "Paste the token here"
    field:SetSize(-20, 26, 1)
    field:SetPos(10, 36)

    local line = ui.Text.new(layer)
    line.font = id.Font.MUSEO_SANS_15PT_REGULAR
    line:SetSize(-20, 20, 1)
    line:SetPos(10, 70)

    M.refresh = function()
        local okText = pcall(function()
            if pending or checking then
                line.content = "Checking the token with the API..."
            elseif shown then
                line.content = "Your saved token is in the field: select it and copy it (Ctrl+C). It hides itself again."
            else
                line.content = M.problem or deps.status()
            end
        end)
        if not okText then M.refresh = nil end  -- the layer was closed; the next SettingsLayerReady rebuilds it
    end
    field:Subscribe(ui.Hook.ONCONTENTCHANGED, "runeadvisor-export-token", function(f)
        local value = trim(f.content)
        if value == "" then
            -- Not a request to drop the saved token (see the header): just forget what was being typed.
            M.problem, pending = nil, nil
        elseif value == M.token() then
            pending = nil
        else
            -- Something else than the saved token is in the field now: it is a new token to check, and the
            -- "show" state (if any) is over without emptying what the user typed.
            if shown then
                shown = nil
                pcall(function() M.revealButton.label = SHOW_LABEL end)
            end
            pending = { token = value, ticks = CHECK_DELAY_TICKS }
        end
        M.refresh()
    end)

    -- "Show the saved token": the way to get a token back out of the plugin (see the header).
    local reveal = ui.Button.new(layer)
    reveal.stylesheetID = id.StyleSheet.BUTTON_DEFAULT
    reveal:SetSize(200, 26)
    reveal:SetPos(10, 98)
    reveal.label = SHOW_LABEL
    reveal:Subscribe(ui.Hook.ONBUTTONPRESSED, "runeadvisor-export-show-token", function() M.toggleShown() end)

    -- Consent to analysis (see the header): what is asked, the answer the server has, and the button.
    local function text(y, content)
        local t = ui.Text.new(layer)
        t.font = id.Font.MUSEO_SANS_15PT_REGULAR
        t:SetSize(-20, 20, 1)
        t:SetPos(10, y)
        t.content = content
        return t
    end
    -- The client keeps a plugin's settings per computer, not per login (the PersistentDB docs: records are
    -- shared by "all characters that are played from the device"), so the token posts for whoever plays here.
    text(132, "A saved token sends every character played on this computer.")
    text(164, "Optional: allow Rune Advisor to analyse this character's data")
    text(184, "to improve the planner for everyone. Nothing more is sent either way,")
    text(204, "and you can change your mind here or on the site (runeadvisor.com/privacy).")
    local consentLine = text(230, "")
    local consentButton = ui.Button.new(layer)
    consentButton.stylesheetID = id.StyleSheet.BUTTON_DEFAULT
    consentButton:SetSize(200, 26)
    consentButton:SetPos(10, 256)
    consentButton:Subscribe(ui.Hook.ONBUTTONPRESSED, "runeadvisor-export-consent", function() M.toggleConsent() end)

    local refreshToken = M.refresh
    M.refresh = function()
        if refreshToken then refreshToken() end
        local okText = pcall(function()
            local consent = M.consent()
            if consentBusy then
                consentLine.content = "Asking the API..."
            elseif consentProblem then
                consentLine.content = consentProblem
            elseif M.token() == nil then
                consentLine.content = "Analysis: not allowed (no token)"
            elseif consent == nil then
                consentLine.content = "Analysis: your choice shows here once this character has been sent"
            else
                consentLine.content = (consent and "Analysis: allowed for " or "Analysis: not allowed for ") .. currentPlayer()
            end
            consentButton.label = consent and "Stop allowing analysis" or "Allow analysis"
        end)
        if not okText then M.refresh = nil end
    end

    shown = nil  -- a freshly opened panel never starts with the token showing
    M.field, M.line, M.revealButton = field, line, reveal
    M.consentLine, M.consentButton = consentLine, consentButton
    M.refresh()
end

--- Hook the settings layer. deps: status() -> the line under the field when nothing is wrong, check(token,
--- cb) (see tick), setConsent(player, wanted, cb(consent or nil, why)), player() -> the logged-in character's
--- name or nil, onSaved() (optional). Returns a handle for tests: {populate(layer)}.
function M.build(deps)
    M.check, M.onSaved, M.setConsent, M.player = deps.check, deps.onSaved, deps.setConsent, deps.player
    local handle = { populate = function(layer) populate(layer, deps) end }
    Event.SettingsLayerReady.Subscribe("runeadvisor-export-settings-layer", function(e)
        local ok, err = pcall(function() populate(e.component, deps) end)
        if not ok then log("Rune Advisor: settings layer not built: " .. tostring(err)) end
    end)
    return handle
end

return M
