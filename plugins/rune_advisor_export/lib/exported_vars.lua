-- The player variables posted by name, beside the ones quests and achievements test (lib/manifest.lua).
-- Every name here is written out or covered by a prefix whose every member is the same kind of thing, so
-- this file is the complete answer to "which other variables leave the client". Each one is a value the game
-- shows the player somewhere: a currency balance, the contents of a storage kept in variables, the collection
-- log, a tool on the tool belt. Interface state (which window tab is open, which entries are starred), world
-- event timers and migration flags are deliberately not here.
--
-- A name this client doesn't have is skipped. A variable the game adds later is not sent until it is added
-- here, except inside the prefix families below.

local M = {}

M.NAMES = {
    "QP",                                      -- quest points, as the quest journal totals them
    -- Reward currencies and tokens kept in variables
    "FISHCOMP_FISH_TOKENS",                    -- Fish Flingers tokens
    "FISHCOMP_REWARD_TACKLE_BOX_LEVEL",        -- Fish Flingers tackle box upgrade level
    "WARGUILD_TOKENS",                         -- Warriors' Guild tokens
    "HVH_TOKENS",                              -- Heist tokens
    "DINO_GAME_HUNTER_MARKS",                  -- Hunter marks (Anachronia Hunter Mark Shop currency)
    "DINO_GAME_SHOP_ELITE_OUTFIT_BLUEPRINT",   -- trapper outfit blueprint bought from that shop
    "SMKI_SLAYER_POINTS",                      -- Slayer points
    -- The current Slayer assignment, as the Slayer counter shows it
    "SLAYER_TARGET",                           -- which creatures (0 = no assignment)
    "SLAYER_COUNT",                            -- kills left
    "SLAYER_MASTER",                           -- the master who gave it
    "BSLAY_KILLS_LEFT",                        -- boss kills left on a Soul Reaper assignment
    "PVM_HUB_CURRENCY",                        -- Marks of War (War's Retreat)
    "PVM_HUB_MARK_CAP_AMOUNT",                 -- Marks of War earned toward the daily cap
    "RAID1_GOEBIE_REP",                        -- Mazcab (goebie) reputation
    -- Things the player has built up or unlocked
    "DINO_BASE_CAMP_SKILLCAPE_STAND_STATE",    -- Anachronia base camp: the skillcape stand
    "PVM_HUB_CAMPFIRE_STATE",                  -- War's Retreat campfire upgrade tier
    "PLAYER_FORT_REST_PERCENT",                -- Fort Forinthry rested xp, as a percentage (the game says it in
                                               -- chat at login and on entering the fort)
    "FOTG2_DIGSITE_AGILITY_XP",                -- Empty Throne Room agility course: xp earned on it (it runs out)
    "INVENT_CHARGE",                           -- Invention charge pack, in 1/3000ths of a charge (as shown in game)
    "INVENT_HAS_CHARGE_PACK",
    "ELFCITY_ITHELL_TOOLBELT_GILDED_DPICK",    -- the gilded dragon pickaxe added to the tool belt (Ithell)
    -- Penguin Hide and Seek
    "PENG_SPY_PERM1",                          -- spy points in bits 16-23, total penguins spotted in bits 0-15
    "PENG_SPY_SWITCH",                         -- this week: bits 0-11 one per penguin spotted, bit 26 the polar bear
    -- Statue of Dahmaroc (strange rocks) and Statue of Rhiannon (golden rocks)
    "ELFCITY_PERM6",                           -- golden rocks held, one bit per rock
    "ELFCITY_PERM4",                           -- Statue of Rhiannon: pairs placed this round (flags + counter)
    "STATUE_BAG_CLIENT",                       -- the statue collection bag's contents, one bit per strange rock
    "STATUE_BAG_GOLDEN_CLIENT",                -- the same for golden rocks
    -- progress of the statue and of its replica in the player's house; rocks added this round per skill;
    -- LAST_SKILL is the skill of the last rock received (the next one in that skill is half as likely);
    -- OPTED_OUT / RECEIVE_ROCKS: whether the player has turned rock drops off
    "STATUE_OPTED_OUT",
    "STATUE_AGILITY_ADDED", "STATUE_COMPLETION_COUNT", "STATUE_CONSTRUCTION_ADDED", "STATUE_COOKING_ADDED",
    "STATUE_CRAFTING_ADDED", "STATUE_FARMING_ADDED", "STATUE_FIREMAKING_ADDED", "STATUE_FISHING_ADDED",
    "STATUE_FLETCHING_ADDED", "STATUE_HERBLORE_ADDED", "STATUE_HUNTER_ADDED", "STATUE_LAST_SKILL",
    "STATUE_MAIN", "STATUE_MINING_ADDED", "STATUE_PIECES_ADDED", "STATUE_REPLICA_MAIN", "STATUE_REPLICA_STATE",
    "STATUE_RUNECRAFTING_ADDED", "STATUE_SMITHING_ADDED", "STATUE_STATE", "STATUE_THIEVING_ADDED",
    "STATUE_WOODCUTTING_ADDED",
    "ELFCITY_STATUE_AGILITY_ADDED", "ELFCITY_STATUE_CONSTRUCTION_ADDED", "ELFCITY_STATUE_CRAFTING_ADDED",
    "ELFCITY_STATUE_DIVINATION_ADDED", "ELFCITY_STATUE_DUNGEONEERING_ADDED", "ELFCITY_STATUE_FARMING_ADDED",
    "ELFCITY_STATUE_HERBLORE_ADDED", "ELFCITY_STATUE_LAST_SKILL", "ELFCITY_STATUE_MAGIC_ADDED",
    "ELFCITY_STATUE_MELEE_ADDED", "ELFCITY_STATUE_MINING_ADDED", "ELFCITY_STATUE_PIECES_ADDED",
    "ELFCITY_STATUE_PRAYER_ADDED", "ELFCITY_STATUE_RANGED_ADDED", "ELFCITY_STATUE_RECEIVE_ROCKS",
    "ELFCITY_STATUE_SLAYER_ADDED", "ELFCITY_STATUE_SMITHING_ADDED", "ELFCITY_STATUE_STATE",
    "ELFCITY_STATUE_SUMMONING_ADDED", "ELFCITY_STATUE_WOODCUTTING_ADDED",
    -- Banked bonus xp per skill, in tenths of an xp
    "BONUSXP_ATTACK", "BONUSXP_DEFENCE", "BONUSXP_STRENGTH", "BONUSXP_HITPOINTS", "BONUSXP_RANGED",
    "BONUSXP_PRAYER", "BONUSXP_MAGIC", "BONUSXP_COOKING", "BONUSXP_WOODCUTTING", "BONUSXP_FLETCHING",
    "BONUSXP_FISHING", "BONUSXP_FIREMAKING", "BONUSXP_CRAFTING", "BONUSXP_SMITHING", "BONUSXP_MINING",
    "BONUSXP_HERBLORE", "BONUSXP_AGILITY", "BONUSXP_THIEVING", "BONUSXP_SLAYER", "BONUSXP_FARMING",
    "BONUSXP_RUNECRAFTING", "BONUSXP_HUNTER", "BONUSXP_CONSTRUCTION", "BONUSXP_SUMMONING",
    "BONUSXP_DUNGEONEERING", "BONUSXP_DIVINATION", "BONUSXP_INVENTION", "BONUSXP_ARCHAEOLOGY",
    "BONUSXP_NECROMANCY",
    -- The D&D tracker: the player's own counts for the activities with a limit
    "DNDTRACKER_BIGCHINCHOMPA_GAME_PLAYED", "DNDTRACKER_BIGCHINCHOMPA_START", "DNDTRACKER_FISH_FLINGERS",
    "DNDTRACKER_FISH_FLINGERS_LIMIT", "DNDTRACKER_SINKHOLES_START", "DNDTRACKER_TROLL_INVASION_LIMIT",
    -- The Currencies window's balances (elite skilling outfit fragments are a prefix family below)
    "CURRENCY_ARC_CHIMES", "CURRENCY_ARC_TAIJITU", "CURRENCY_BEACH_SAND", "CURRENCY_BRIM_AGIL_TICKET",
    "CURRENCY_CHALLENGE", "CURRENCY_CHRISTMAS_SPIRIT", "CURRENCY_CW_GOLD_TICKET", "CURRENCY_CW_SILVER_TICKET",
    "CURRENCY_DESERT_FARM", "CURRENCY_DSF_MERCHANT_COUPON", "CURRENCY_ECTO_TOKEN", "CURRENCY_EGG_POINTS",
    "CURRENCY_HOG_MOTE", "CURRENCY_MAZCAB_TECI", "CURRENCY_PIECES_OF_EIGHT", "CURRENCY_POF",
    "CURRENCY_RC_GUILD_TOKEN", "CURRENCY_SLAYER_VIP", "CURRENCY_SLISKELION_PIECE", "CURRENCY_SPOOKY_TOKENS",
    "CURRENCY_SPRING_TOKENS", "CURRENCY_SWORLDS", "CURRENCY_TZHAAR_TOKKUL",
    -- The tool belt: which tools are on it, which upgrades were bought and are switched on
    "TOOLBELT_AMMO_MOULD", "TOOLBELT_AMULET_MOULD", "TOOLBELT_BARBARIAN_ROD", "TOOLBELT_BIG_NET",
    "TOOLBELT_BOLT_MOULD", "TOOLBELT_BONECRUSHER", "TOOLBELT_BONECRUSHER_BOUGHT",
    "TOOLBELT_BONECRUSHER_ENABLED", "TOOLBELT_BOOTS_OF_FLIGHT", "TOOLBELT_BRACELET_MOULD",
    "TOOLBELT_CHAIN_MOULD", "TOOLBELT_CHAOS_STAR", "TOOLBELT_CHARMING_IMP", "TOOLBELT_CHARMING_IMP_BOUGHT",
    "TOOLBELT_CHART", "TOOLBELT_CHISEL", "TOOLBELT_CRAYFISH_CAGE", "TOOLBELT_CRYSTAL_CHIMES", "TOOLBELT_DIBBER",
    "TOOLBELT_DOUBLE_AMMO_MOULD", "TOOLBELT_FAIRY_SECATEURS", "TOOLBELT_FAIRY_WATERING_CAN",
    "TOOLBELT_FISHING_EXPLOSIVE", "TOOLBELT_FISHING_ROD", "TOOLBELT_FLY_FISHING_ROD", "TOOLBELT_FREE_TOOLS",
    "TOOLBELT_FRIENDSHIP_BRACELET", "TOOLBELT_FUNGICIDE", "TOOLBELT_GARDENING_TROWEL",
    "TOOLBELT_GILDED_PICKAXE", "TOOLBELT_GLASSBLOWING_PIPE", "TOOLBELT_GOLD_ACC", "TOOLBELT_GOLD_ACC_BOUGHT",
    "TOOLBELT_GOLD_ACC_ENABLED", "TOOLBELT_GRAPPLE_HOOK", "TOOLBELT_HAMMER", "TOOLBELT_HARPOON",
    "TOOLBELT_HATCHET", "TOOLBELT_HATCHET_LEVEL", "TOOLBELT_HERBICIDE", "TOOLBELT_HERBICIDE_BOUGHT",
    "TOOLBELT_HOLY_MOULD", "TOOLBELT_ICE_COOLER", "TOOLBELT_INCENSE_BURNER", "TOOLBELT_INSECT_REPELLENT",
    "TOOLBELT_KEYRING", "TOOLBELT_KNIFE", "TOOLBELT_LEGATUS_PENDANT", "TOOLBELT_LOBSTER_POT",
    "TOOLBELT_LOCKPICK", "TOOLBELT_MACHETE", "TOOLBELT_MACHETTE_LEVEL", "TOOLBELT_MATTOCK",
    "TOOLBELT_MEMBER_TOOLS", "TOOLBELT_MEMBER_TOOLS_2", "TOOLBELT_NECKLACE_MOULD", "TOOLBELT_NEEDLE",
    "TOOLBELT_NOOSE_WAND", "TOOLBELT_OUROBOROS_POUCH", "TOOLBELT_PASS_IKOVIAN", "TOOLBELT_PASS_STORMGUARD",
    "TOOLBELT_PESTLE_MORTAR", "TOOLBELT_PICKAXE", "TOOLBELT_PICKAXE_LEVEL", "TOOLBELT_PONTIFEX_RING",
    "TOOLBELT_RAKE", "TOOLBELT_RING_MOULD", "TOOLBELT_ROCKHAMMER", "TOOLBELT_ROD_MOULD", "TOOLBELT_SALT_BAG",
    "TOOLBELT_SAW", "TOOLBELT_SECATEURS", "TOOLBELT_SEEDICIDE", "TOOLBELT_SEEDICIDE_BOUGHT", "TOOLBELT_SEXTANT",
    "TOOLBELT_SHEARS", "TOOLBELT_SICKLE_MOULD", "TOOLBELT_SLAYER_BELL", "TOOLBELT_SLAYER_TOOLS",
    "TOOLBELT_SMALL_NET", "TOOLBELT_SPADE", "TOOLBELT_STETHOSCOPE", "TOOLBELT_TIARA_MOULD",
    "TOOLBELT_TINDERBOX", "TOOLBELT_TONGS", "TOOLBELT_UNHOLY_MOULD", "TOOLBELT_WATCH", "TOOLBELT_WINGSUIT",
    -- The herb bag: one count per herb, the unlock and upgrade flags, and its packed slots 1-5
    "DINO_HERB_BAG_1", "DINO_HERB_BAG_2", "DINO_HERB_BAG_3", "DINO_HERB_BAG_4", "DINO_HERB_BAG_5",
    "DINO_HERB_BAG_ARBUCK", "DINO_HERB_BAG_AVANTOE", "DINO_HERB_BAG_BLOODWEED", "DINO_HERB_BAG_CADANTINE",
    "DINO_HERB_BAG_DWARF_WEED", "DINO_HERB_BAG_FELLSTALK", "DINO_HERB_BAG_GUAM", "DINO_HERB_BAG_HARRALANDER",
    "DINO_HERB_BAG_IRIT", "DINO_HERB_BAG_KWUARM", "DINO_HERB_BAG_LANTADYME", "DINO_HERB_BAG_MARRENTILL",
    "DINO_HERB_BAG_RANARR", "DINO_HERB_BAG_SNAPDRAGON", "DINO_HERB_BAG_SPIRIT_WEED", "DINO_HERB_BAG_TARROMIN",
    "DINO_HERB_BAG_TOADFLAX", "DINO_HERB_BAG_TORSTOL", "DINO_HERB_BAG_UNLOCKED", "DINO_HERB_BAG_UPGRADED",
    "DINO_HERB_BAG_WERGALI",
}

-- Families posted by prefix. Every variable under each prefix is the same kind of value, so a new member (a
-- new ore, a new collection log item) needs no plugin update.
M.PREFIXES = {
    "MINING_OREBOX_",               -- ore box: per ore the count, the stored stone spirits and the capacity upgrade
    "ARCH_SOIL_BOX_",               -- soil box: one count per soil, and the upgrade
    "INVENT_MATERIAL_",             -- Invention materials storage: one count per part or component
    "ITEMLOG_ITEM_",                -- the collection log: per logged item, how many were ever obtained
    "TOKEN_DD_",                    -- Distractions & Diversions reset tokens and counters
    "CURRENCY_FRAGMENTS_",          -- elite skilling outfit fragments, per outfit
    "CRAMULET_HAS_",                -- the cramulet: which amulets have been absorbed into it
    "ELFCITY_ROCK_",                -- golden rocks in the statue bag, one flag per rock
    "RAND_REWARD_GEM_BAG_UPGRADE_", -- the gem bag: one count per gem
}

return M
