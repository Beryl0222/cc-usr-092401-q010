"""“教会、勤练、常赛”可解释覆盖规则。

不以单一体测分数评价学生；覆盖只看“经交叉确认的实际授课事件”是否兑现
方案中各技能目标的计划周次。每项判定都输出依据（场次列表）与缺口原因。

口径：

- 教会：在该技能 teach_weeks 内（或挂接该周缺课的、被采纳的补课证据），
  至少有 1 场经确认、且实际教授该技能的体育课。自由活动/纯应考训练不计。
- 勤练：practice_weeks 内，体育课/大课间/课后服务中实际练习该技能的
  确认场次命中的计划周数占比（按周去重）。
- 常赛：match_weeks 内，经确认的班级赛事场次命中的计划周数占比。
- 调课：以审批通过的 ScheduleChange 为准，按调整后的时间核对。
- 补课：只有被账本仲裁**采纳**的补课证据（一次原场次至多一场）才按其
  makeup_for 指向的原场次所在周归类，补上即兑现原周目标；未被采纳的补课
  （重复排补、已取消、未确认）不回填原周，其在自身场次实际开展的活动只按
  自身周次计，绝不重复归入原教学周。

完成率分母只来自账本展开的计划场次（plan_occasions）：补课场次若本身也是
计划场次，只按一场计入；在非计划时段举办的补课不进分母。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import SessionMode
from .models import ActivityKind

# 可产生“教会/勤练”技能覆盖的形态（自由活动、纯应考训练排除）
SKILL_GRANTING_MODES = {
    SessionMode.NORMAL,
    SessionMode.RAIN_ALT,
    SessionMode.HEAT_ALT,
    SessionMode.RESCHEDULED,
    SessionMode.MAKEUP,
}

# 计入待补的状态（教研未确认场次单独统计，见 ClassCoverage.in_review）
PENDING_STATE_NAMES = ("taken_over", "missing", "weather_pending")


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

    @property
    def completion_ratio(self) -> float:
        return round(self.completed / self.total_occasions, 3) if self.total_occasions else 0.0


def _week_of(occasion_key: str) -> int:
    return int(occasion_key.rsplit("w", 1)[1])


def compute_skill_coverage(
    goal,
    confirmed_sessions,
    *,
    rescheduled_weeks=None,
    plan_keys=None,
    accepted_makeup_hosts=None,
):
    """confirmed_sessions: ledger 重建后的已确认会话列表（对象含 occasion/taught_skill/mode/kind）。

    rescheduled_weeks: 调课生效后，原 slot 在某周改期的集合（视为该周仍有安排），
    用于避免把合规调课误判为缺口。
    plan_keys: 本班计划场次键集合；补课会话只有发生在计划场次上，或作为被采纳的
    补课证据（accepted_makeup_hosts）时才参与覆盖。
    accepted_makeup_hosts: 被账本采纳的补课场次键集合；只有这些补课按原周归类，
    其余补课（重复/取消/未被采纳）按自身周次计，绝不重复回填原教学周。
    """
    rescheduled_weeks = rescheduled_weeks or set()
    plan_keys = set(plan_keys or ())
    accepted_makeup_hosts = set(accepted_makeup_hosts or ())
    evidence: list[str] = []
    taught_weeks_hit: set[int] = set()
    practice_weeks_hit: set[int] = set()
    match_weeks_hit: set[int] = set()
    practiced_planned = len(goal.practice_weeks)
    matched_planned = len(goal.match_weeks)
    gaps: list[str] = []
    seen_evidence: set[str] = set()

    def _add_evidence(tag: str, key: str, week: int) -> None:
        marker = f"{tag}:{key}@w{week}"
        if marker in seen_evidence:
            return
        seen_evidence.add(marker)
        evidence.append(f"{tag}:{key}")

    for s in confirmed_sessions:
        key = s.occasion.key()
        if s.taught_skill != goal.skill_code or s.mode not in SKILL_GRANTING_MODES:
            continue
        is_accepted_makeup = (
            s.mode == SessionMode.MAKEUP
            and s.makeup_for is not None
            and key in accepted_makeup_hosts
        )
        # 非计划时段的会话只有作为被采纳补课证据时才参与覆盖
        if plan_keys and key not in plan_keys and not is_accepted_makeup:
            continue
        # 默认按自身周次；被采纳的补课按挂接原场次所在周归类（只归这一次）
        week = _week_of(key)
        if is_accepted_makeup:
            week = _week_of(s.makeup_for.key())
        if s.kind is ActivityKind.PE_CLASS:
            if week in goal.teach_weeks:
                taught_weeks_hit.add(week)
                _add_evidence("teach", key, week)
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                _add_evidence("practice", key, week)
        elif s.kind in (ActivityKind.DAILY_BREAK, ActivityKind.AFTER_SCHOOL):
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                _add_evidence("practice", key, week)
        elif s.kind is ActivityKind.CLASS_MATCH and week in goal.match_weeks:
            match_weeks_hit.add(week)
            _add_evidence("match", key, week)

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
        evidence=tuple(sorted(evidence)),
        gaps=tuple(gaps),
    )


def compute_class_coverage(
    class_id: str,
    rebuilt: dict,
    skill_goals,
) -> ClassCoverage:
    """rebuilt 为 ledger.rebuild_class() 的输出。

    分母只认账本展开的计划场次（plan_occasions）；补课会话只是履约证据，
    不会让分母多出场次。完成/待补/在审计数与状态表严格对账。
    """
    states = rebuilt["states"]
    plan_occasions = rebuilt.get("plan_occasions") or tuple(sorted(states))
    plan_keys = set(plan_occasions)

    def _state(key: str) -> str:
        return states.get(key, "missing")

    completed = sum(1 for k in plan_keys if _state(k) == "completed")
    pending = sum(1 for k in plan_keys if _state(k) in PENDING_STATE_NAMES)
    in_review = sum(1 for k in plan_keys if _state(k) == "in_review")
    sessions = [s for s in rebuilt["sessions"] if s.confirmed]
    skill_cov = tuple(
        compute_skill_coverage(
            goal,
            sessions,
            plan_keys=plan_keys,
            accepted_makeup_hosts=rebuilt.get("accepted_makeup_hosts", frozenset()),
        )
        for goal in skill_goals
    )
    return ClassCoverage(
        class_id=class_id,
        total_occasions=len(plan_keys),
        completed=completed,
        pending_makeup=pending,
        in_review=in_review,
        skill_coverage=skill_cov,
    )
