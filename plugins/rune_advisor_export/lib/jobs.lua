-- Work split across game ticks. The client caps one Lua function execution at 1000 ms ("Maximum function
-- execution time of 1000 ms exceeded"), and a long callback freezes the game while it runs. So anything
-- that walks thousands of configs (the walk over every quest and achievement at login) runs as a job: a
-- list of items processed a slice at a time from a tick event (main.lua: Event.Logic, 50 a second), then a
-- `done` callback. Jobs run one after another in the order added, so a task in several stages is several
-- jobs sharing upvalues.
--
--   jobs.add({ name = "...", items = list, perTick = n, each = function(item, i) end, done = function() end })
--   jobs.tick()   -- from the tick event: one slice of the first job in the queue
-- An error in `each` or `done` is logged and ends that job (the rest of the queue still runs).

local M = { queue = {}, verbose = false }  -- verbose: log every slice (a debugging aid, off)

function M.add(job)
    job.i = 1
    M.queue[#M.queue + 1] = job
end

function M.busy()
    return #M.queue > 0
end

function M.tick()
    local job = M.queue[1]
    if not job then return end
    local items = job.items or {}
    local stop = math.min(#items, job.i + (job.perTick or 200) - 1)
    if M.verbose then log(formatStr("Rune Advisor job {}: items {}-{} of {}", tostring(job.name), job.i, stop, #items)) end
    local ok, err = pcall(function()
        for i = job.i, stop do job.each(items[i], i) end
    end)
    if not ok then
        log(formatStr("Rune Advisor: job {} stopped at item {}: {}", tostring(job.name), job.i, tostring(err)))
        table.remove(M.queue, 1)
        return
    end
    job.i = stop + 1
    if job.i > #items then
        table.remove(M.queue, 1)
        if job.done then
            local okDone, errDone = pcall(job.done)
            if not okDone then log(formatStr("Rune Advisor: job {} failed to finish: {}", tostring(job.name), tostring(errDone))) end
        end
    end
end

return M
