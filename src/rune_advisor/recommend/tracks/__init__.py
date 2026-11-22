from .archaeology_guild import ArchaeologyGuildTrack
from .collection_logs import CollectionLogsTrack
from .darts import DartsTrack
from .dnd_tokens import DndTokensTrack
from .free_xp import FreeXpTrack
from .gear import GearTrack
from .milestones import MilestoneTrack
from .outfits import OutfitsTrack
from .quest_points import QuestPointsTrack
from .runescore import RuneScoreTrack
from .task_sets import TaskSetsTrack
from .toolbelt import ToolbeltTrack

ALL_TRACKS = {t.name: t for t in (QuestPointsTrack, MilestoneTrack, RuneScoreTrack, GearTrack, FreeXpTrack, OutfitsTrack, TaskSetsTrack,
                                         CollectionLogsTrack, ToolbeltTrack, DartsTrack, DndTokensTrack,
                                         ArchaeologyGuildTrack)}
