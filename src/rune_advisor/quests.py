"""Quest bookkeeping that needs more than a quest's current status.

Replayable quests (Dimension of Disaster and its sub-quests, re-enacted at the Loarnab museum)
reset to started/not-started while being replayed, but the quest points stay earned. The plugin
API's `hasFinishedQuest` and the Adventurer's Log quest list both report the current replay state, while
the quest journal's total (VarPlayer "QP", also the Player Quests API's top-level `qp`) still counts the points.
"""

from __future__ import annotations

from .models import PlayerState, QuestStatus

REPLAYABLE_QUESTS = {
    "Dimension of Disaster",
    "Dimension of Disaster: Coin of the Realm",
    "Dimension of Disaster: Curse of Arrav",
    "Dimension of Disaster: Defender of Varrock",
    "Dimension of Disaster: Demon Slayer",
    "Dimension of Disaster: Shield of Arrav",
}


def reconcile_journal_qp(p: PlayerState) -> None:
    """Mark replayable quests as QP-earned when that exactly explains the journal's QP total.

    Only replayable quests are considered, so an unfinished first-time quest (e.g. one worth
    the same points) can never be mistaken for a replay.
    """
    if p.journal_quest_points is None:
        return
    gap = p.journal_quest_points - p.quest_points
    if gap <= 0:
        return
    replays = [q for q in p.quests.values()
               if q.title in REPLAYABLE_QUESTS and q.status != QuestStatus.COMPLETED
               and not q.qp_earned and q.quest_points > 0]
    if sum(q.quest_points for q in replays) == gap:
        for q in replays:
            q.qp_earned = True
        p.notes.append("replaying (QP already earned): " + ", ".join(q.title for q in replays))
