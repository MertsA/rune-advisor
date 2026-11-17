"""Quest points, with the quest cape as a set-completion bonus.

Each incomplete quest becomes a bundle: the quest plus its incomplete prerequisite quests.
Cost = quest lengths + whatever it takes to meet the bundle's highest skill requirements.
If finishing the bundle leaves no cape-relevant quests incomplete, it also earns the quest
cape bonus, which is how "only Heralds of Crimson left" floats to the top.
"""

from __future__ import annotations

from ...models import Quest
from ..base import Candidate, Context
from ..plans import incomplete_quests, quest_bundle, quests_plan

QUEST_CAPE_BONUS = 25.0  # in QP-equivalents

# Holiday quests that the Adventurer's Log lists but the quest cape doesn't require. The plugin export
# carries QuestType.type (SEASONAL) and sets Quest.seasonal, which supersedes this list.
SEASONAL_QUESTS = {"A Christmas Reunion", "Cold Front", "Corporate Egg-spionage"}


def counts_for_cape(q: Quest) -> bool:
    return not (q.seasonal or q.title in SEASONAL_QUESTS or q.title.endswith("(miniquest)"))


def counts_as_qp_goal(q: Quest) -> bool:
    # Replays of quests whose QP is already earned are worth nothing here; their value
    # (e.g. museum re-enactment achievements) belongs to the RuneScore track.
    return not q.qp_earned


class _QuestPoints:
    name = "quest_points"
    description = "Quest points per hour, including prerequisites; bonus for unlocking the quest cape"

    def candidates(self, ctx: Context) -> list[Candidate]:
        incomplete = {t: q for t, q in incomplete_quests(ctx).items() if counts_as_qp_goal(q)}
        # The quest cape needs every quest, members' included: no cape goal on a free-to-play account.
        cape_set = {t for t, q in incomplete.items() if counts_for_cape(q)} if ctx.player.members else set()
        out = [self._candidate(ctx, title, quest_bundle(ctx, title), cape_set) for title in incomplete]
        out = [c for c in out if c.value > 0]  # 0-QP quests (seasonal, replays) aren't quest point goals
        if len(cape_set) > 1:
            out.append(self._candidate(ctx, "Quest point cape (all quests)", sorted(cape_set), cape_set))
        return out

    def _candidate(self, ctx: Context, title: str, bundle: list[str], cape_set: set[str]) -> Candidate:
        plan = quests_plan(ctx, bundle)
        details = list(plan.steps)
        value = float(plan.quest_points)
        why = [f"{plan.quest_points} QP from {len(plan.quests)} quest{'s' * (len(plan.quests) != 1)}"]
        if cape_set and set(bundle) >= cape_set:
            value += QUEST_CAPE_BONUS
            details.insert(0, "unlocks quest cape")
            why.insert(0, "unlocks the quest cape")
        # xp rewards are worth the training they save: net them off the cost, like a by-product.
        if note := plan.reward_note():
            details.append(note)
            why.append(note)
        return Candidate(track=self.name, title=title, value=value, unit="QP", cost_hours=plan.net_hours, play_hours=plan.hours,
                         details=details, uncertain=plan.uncertain, why="; ".join(why), **plan.guide_fields())


QuestPointsTrack = _QuestPoints()
