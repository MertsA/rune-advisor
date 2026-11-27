from .advance_time import AdvanceTimeTrack
from .archaeology_guild import ArchaeologyGuildTrack
from .base_camp import BaseCampTrack
from .collection_logs import CollectionLogsTrack
from .darts import DartsTrack
from .dg_boss import DgBossTrack
from .dnd_tokens import DndTokensTrack
from .colossi import ColossiTrack
from .fairy_rings import FairyRingsTrack
from .fort import FortTrack
from .free_xp import FreeXpTrack
from .gear import GearTrack
from .milestones import MilestoneTrack
from .outfits import OutfitsTrack
from .quest_points import QuestPointsTrack
from .runescore import RuneScoreTrack
from .scrolls import ScrollsTrack
from .task_sets import TaskSetsTrack
from .toolbelt import ToolbeltTrack

ALL_TRACKS = {t.name: t for t in (QuestPointsTrack, MilestoneTrack, RuneScoreTrack, GearTrack, FreeXpTrack, OutfitsTrack, TaskSetsTrack,
                                         CollectionLogsTrack, ToolbeltTrack, DartsTrack, DndTokensTrack,
                                         ArchaeologyGuildTrack, ScrollsTrack, DgBossTrack, FortTrack, BaseCampTrack, AdvanceTimeTrack, FairyRingsTrack,
                                         ColossiTrack)}
