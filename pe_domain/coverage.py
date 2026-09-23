"""“教会、勤练、常赛”可解释覆盖规则。

不以单一体测分数评价学生；覆盖只看“经交叉确认的实际授课事件”是否兑现
方案中各技能目标的计划周次。每项判定都输出依据（场次列表）与缺口原因。

口径：

- 教会：在该技能 teach_weeks 内（或回填该周缺课的补课），至少有 1 场
  经确认、且实际教授该技能的体育课。自由活动/纯应考训练不计。
- 勤练：practice_weeks 内，体育课/大课间/课后服务中实际练习该技能的
  确认场次按周去重命中的周数占比。
- 常赛：match_weeks 内，经确认的班级赛事场次占比。
- 调课：以审批通过的 ScheduleChange 为准，按调整后的时间核对。
- 补课：MAKEUP 场次只是原场次的履约证据，**按其挂接原场次所在周归类**；
  一次原场次最多接受一个有效补课结果（账本中该原场次生效安排上、经三方
  确认的那一条，见 ledger.rebuild_class 的 completion_evidence）。被取代
  的旧安排、补课失败的会话不产生任何技能覆盖，同一原周次不会被多条补课
  重复计入。
- 伤病适配只折减学生本人时长，不影响场次成立与否，因此不改变班级覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import SessionMode
from .ledger import PENDING_STATES
from .models import ActivityKind

# 可产生“教会/勤练”技能覆盖的形态（自由活动、纯应考训练排除）
SKILL_GRANTING_MODES = {
    SessionMode.NORMAL,
    SessionMode.RAIN_ALT,
    SessionMode.HEAT_ALT,
    SessionMode.RESCHEDULED,
    SessionMode.MAKEUP,
}


@dataclass(frozen=True)
class SkillCoverage:
    skill_code: str
    taught: bool
    practiced_ratio: float        # 0.0-1.0
    matched_ratio: float
    evidence: tuple[str, ...]     # 命中的场次 key
    gaps: tuple[str, ...]         # 可解释缺口


@dataclass(frozen=True)
class ClassCoverage:
    class_id: str
    total_occasions: int
    completed: int
    pending_makeup: int
    in_review: int
    skill_coverage: tuple[SkillCoverage, ...]
    # 原场次 key -> 履约证据（补课场次 key），与 completed 中“补课后完成”一一对应
    completion_evidence: dict[str, str] = field(default_factory=dict)

    @property
    def completion_ratio(self) -> float:
        return round(self.completed / self.total_occasions, 3) if self.total_occasions else 0.0


def _week_of(occasion_key: str) -> int:
    return int(occasion_key.rsplit("w", 1)[1])


def compute_skill_coverage(goal, confirmed_sessions, *, accepted_makeup_keys=None):
    """confirmed_sessions: ledger 重建后的已确认会话列表（含 occasion/taught_skill/mode/kind）。

    accepted_makeup_keys: 被采纳为原场次有效履约结果的补课场次 key 集合
    （completion_evidence 的值）。MAKEUP 会话只有在其中才计覆盖，
    保证一次原周次至多被一条补课归入。
    """
    accepted_makeup_keys = accepted_makeup_keys or frozenset()
    evidence: list[str] = []
    taught_weeks_hit: set[int] = set()
    practice_weeks_hit: set[int] = set()
    match_weeks_hit: set[int] = set()
    practiced_planned = len(goal.practice_weeks)
    matched_planned = len(goal.match_weeks)
    gaps: list[str] = []

    for s in confirmed_sessions:
        key = s.occasion.key()
        if s.taught_skill != goal.skill_code or s.mode not in SKILL_GRANTING_MODES:
            continue
        if s.mode == SessionMode.MAKEUP:
            # 仅采纳生效安排上、确认通过的那一条；按挂接原场次所在周归类
            if s.makeup_for is None or key not in accepted_makeup_keys:
                continue
            week = _week_of(s.makeup_for.key())
        else:
            week = _week_of(key)
        if s.kind is ActivityKind.PE_CLASS:
            if week in goal.teach_weeks:
                taught_weeks_hit.add(week)
                evidence.append(f"teach:{key}")
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                evidence.append(f"practice:{key}")
        elif s.kind in (ActivityKind.DAILY_BREAK, ActivityKind.AFTER_SCHOOL):
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                evidence.append(f"practice:{key}")
        elif s.kind is ActivityKind.CLASS_MATCH and week in goal.match_weeks:
            match_weeks_hit.add(week)
            evidence.append(f"match:{key}")

    taught = bool(taught_weeks_hit)
    practiced = len(practice_weeks_hit & set(goal.practice_weeks))
    matched = len(match_weeks_hit & set(goal.match_weeks))

    if not taught:
        gaps.append(f"技能 {goal.skill_code} 在计划教授周 {sorted(goal.teach_weeks)} 内无确认授课")
    if practiced < practiced_planned:
        gaps.append(f"勤练缺口：{practiced}/{practiced_planned} 周")
    if matched < matched_planned:
        gaps.append(f"常赛缺口：{matched}/{matched_planned} 场")

    return SkillCoverage(
        skill_code=goal.skill_code,
        taught=taught,
        practiced_ratio=min(1.0, round(practiced / practiced_planned, 3)) if practiced_planned else 1.0,
        matched_ratio=min(1.0, round(matched / matched_planned, 3)) if matched_planned else 1.0,
        evidence=tuple(sorted(set(evidence))),
        gaps=tuple(gaps),
    )


def compute_class_coverage(
    class_id: str,
    rebuilt: dict,
    skill_goals,
) -> ClassCoverage:
    """rebuilt 为 ledger.rebuild_class() 的输出。

    总场次 = 计划场次（states 的全部键）；补课场次不是独立分母行。
    """
    states = rebuilt["states"]
    completed = sum(1 for st in states.values() if st == "completed")
    pending = sum(1 for st in states.values() if st in PENDING_STATES)
    in_review = sum(1 for st in states.values() if st == "in_review")

    # 只有被采纳的补课证据才能产生技能覆盖（一次原场次一条）
    completion_evidence: dict[str, str] = rebuilt.get("completion_evidence", {})
    accepted_makeup_keys = frozenset(completion_evidence.values())
    sessions = [
        s for s in rebuilt["sessions"]
        if s.confirmed and (
            s.mode is not SessionMode.MAKEUP or s.occasion.key() in accepted_makeup_keys
        )
    ]
    skill_cov = tuple(
        compute_skill_coverage(
            goal, sessions, accepted_makeup_keys=accepted_makeup_keys,
        )
        for goal in skill_goals
    )
    return ClassCoverage(
        class_id=class_id,
        total_occasions=len(states),
        completed=completed,
        pending_makeup=pending,
        in_review=in_review,
        skill_coverage=skill_cov,
        completion_evidence=dict(completion_evidence),
    )
