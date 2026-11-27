-- The player's own item storage: the inventories (config.Inv names) whose contents are posted. Only the
-- names listed here are read, each one by its exact name. Every other inventory the client holds is left
-- alone: shops, other interfaces, and especially trade screens, which contain another player's offer and
-- must not leave the client.

local M = {}

M.EXPORTED = {
    BANK = true,
    INV = true,                    -- backpack
    WORN = true,                   -- equipment
    METAL_BANK = true,
    MONEY_POUCH = true,
    CURRENCY_POUCH = true,
    PREMIUM_CURRENCY_INV = true,   -- the Currencies window's premium column
    WOODCUTTING_WOODBOX = true,
    MELEE_SPIKE_HARNESS = true,    -- Nodon spike harness (holds armour spikes)
    ARCH_SOIL_BOX = true,
    ARCH_MATERIAL_STORAGE = true,
    ARCH_RESTORATION_WORKBENCH_STORAGE = true,  -- damaged artefacts kept at the archaeologist's workbench
    ARCH_RESEARCH_REWARDS = true,  -- what the research team brought back, waiting to be collected
    ARCH_TETRACOMPASS_REWARDS = true,  -- tetracompass rewards waiting to be collected
    DINO_BASE_CAMP_SLAYER_HELMET_STAND = true,
    DINO_REWARD_HERB_BAG = true,
    DINO_BAIT_BOX = true,
    BIG_GAME_HUNTER_HAVENHYTHE_BAIT_BOX = true,
    INVENT_GIZMO_BAG = true,
    LORE_BOB = true,               -- beast of burden familiar
    WORN_DISPLAY = true,           -- cosmetic overrides shown over the equipment
    NECROMANCY_NEXUS = true,       -- the necromancy nexus's storage
    NECRO_RITUAL_FOCUS_INV = true, -- the ritual site's focus storage (bones, ashes and ritual outputs sent there)
    NECRO_RITUAL_REWARD_INV = true, -- the ritual site's reward chest (ritual loot waiting to be collected)
    CON_TOOLKIT = true,            -- plank box
    WENDLE_STASH_INV = true,       -- Raz's stash (own items; seen only once the client has loaded it)
}

return M
