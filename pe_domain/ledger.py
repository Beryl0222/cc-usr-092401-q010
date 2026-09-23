"""只追加事件账本与班级实况还原。

连续发生“场地冲突 → 临时占课 → 离线补签”后，仍能按时间顺序重放事件，
还原每个班真正完成的内容、缺口与补课安排。账本只追加、不修改不删除；
所有判定（确认、异常、覆盖）都是重放的派生结果，可随时重新计算。

口径（修复后）：

- **计划场次是唯一分母**：states 中每个键恰好对应生效方案中的一个计划场次
  （slot × 教学周，且不超过当前教学周）。在非计划时段举办的补课不产生状态行；
  补课占用某节计划课时，该课时仍只按一场计入，不会多出“补课场次”这一行；
- **补课只是原场次的履约证据**：一场经三方确认的 MAKEUP 会话可以把其
  ``makeup_for`` 指向的原场次从待补状态回填为 completed；它能否回填由仲裁决定，
  被占用课时自身的完成结论与原场次的回填相互独立；
- **一次原场次最多接受一个有效补课结果**：同一原场次挂了多个补课计划/结果时，
  按确定性顺序只取一场，其余补课保留会话与状态但不产生任何回填或技能覆盖；
- 取消、改期、占课、降雨/高温触发未替代、未确认补课各自保留独立状态，
  重放结论只取决于事件内容而不依赖事件到达顺序（迟到/乱序重放结论确定）。

事件类型：

- teacher_report / venue_observation / sample_attendance
- schedule_change（合规调课）
- takeover（临时占课：其他学科占用，记录占用科目与依据单号，可为空表示突发）
- weather_trigger（降雨/高温触发，关联预案）
- makeup_plan（补课安排，挂接缺课场次）
- makeup_cancel（补课安排取消；取消只追加，不删除原计划）
- review_resolved（教研结论）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from .events import (
    ConfirmationResult,
    Occasion,
    SampleAttendance,
    SessionMode,
    TeacherReport,
    VenueObservation,
    assess_session,
)
from .models import STANDARD_MINUTES, ActivityKind

# 尚无履约结论、挂待补课的缺口状态
GAP_STATES = ("taken_over", "missing", "weather_pending")
# 可接受补课回填的状态：缺口 + 三方确认未过（在审）
PENDING_STATES = GAP_STATES + ("in_review",)


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    event_type: str
    payload: Any  # 领域对象或 dict（均为不可变）


@dataclass(frozen=True)
class ReconstructedSession:
    occasion: Occasion
    class_id: str
    kind: ActivityKind
    taught_skill: str
    mode: SessionMode
    minutes: int
    confirmed: bool
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    notes: tuple[str, ...]
    makeup_for: Optional[Occasion]
    per_student: dict[str, int]


class EventLedger:
    def __init__(self):
        self._entries: list[LedgerEntry] = []

    def append(self, event_type: str, payload) -> LedgerEntry:
        entry = LedgerEntry(len(self._entries) + 1, event_type, payload)
        self._entries.append(entry)
        return entry

    def entries(self, event_type: str | None = None) -> tuple[LedgerEntry, ...]:
        if event_type is None:
            return tuple(self._entries)
        return tuple(e for e in self._entries if e.event_type == event_type)

    # ------------------------------------------------------------------
    def rebuild_class(
        self,
        class_id: str,
        class_slots,
        class_headcount: int,
        adaptations: dict,
        token_to_student: dict[str, str],
        venues: dict,
        current_week: int,
        *,
        assessor: Callable[..., ConfirmationResult] = assess_session,
    ) -> dict:
        """重放账本，还原单班实况。

        class_slots: 该班的 PlanSlot 集合（生效方案版本）。
        返回结构化实况：场次状态、会话、缺课、补课挂接、调课、占课、标记。
        states 只含计划场次；补课场次在任何统计中都不单独成行。
        """
        from collections import defaultdict

        # 展开计划场次（只核对到当前教学周；未来周次不算缺口）
        reports_by_occ: dict[Occasion, list[TeacherReport]] = defaultdict(list)
        observations_by_occ: dict[Occasion, list[VenueObservation]] = defaultdict(list)
        attendances: dict[Occasion, list[SampleAttendance]] = defaultdict(list)
        rescheduled: dict[Occasion, list[str]] = defaultdict(list)  # -> 依据列表
        takeovers: list[dict] = []
        weather: dict[Occasion, list[str]] = defaultdict(list)
        # 补课安排：同一原场次可多次排补/取消，全部保留
        makeup_plans: list[dict] = []
        canceled_plan_keys: set[tuple[Occasion, Occasion]] = set()

        slot_ids = {s.slot_id for s in class_slots}
        # 本班全部计划场次键（分母的唯一来源）
        plan_keys = {
            Occasion(slot.slot_id, week).key()
            for slot in class_slots
            for week in range(1, current_week + 1)
            if slot.active_in_week(week)
        }

        # 先扫一遍补课安排：补课可能安排在非计划时段（slot 不属于本班方案），
        # 只要挂接的原场次属于本班，其教师/场地/学生事件仍需纳入核对。
        external_makeup_hosts: set[Occasion] = set()
        for entry in self._entries:
            if entry.event_type == "makeup_plan" and entry.payload["makeup_for"].slot_id in slot_ids:
                if entry.payload["occasion"].slot_id not in slot_ids:
                    external_makeup_hosts.add(entry.payload["occasion"])

        for entry in self._entries:
            p = entry.payload
            et = entry.event_type
            if et == "teacher_report":
                in_plan = p.occasion.slot_id in slot_ids
                is_makeup_evidence = (
                    p.mode == SessionMode.MAKEUP
                    and p.makeup_for is not None
                    and p.makeup_for.slot_id in slot_ids
                )
                if in_plan or is_makeup_evidence:
                    reports_by_occ[p.occasion].append(p)
            elif et == "venue_observation" and (
                p.occasion.slot_id in slot_ids or p.occasion in external_makeup_hosts
            ):
                observations_by_occ[p.occasion].append(p)
            elif et == "sample_attendance" and (
                p.occasion.slot_id in slot_ids or p.occasion in external_makeup_hosts
            ):
                attendances[p.occasion].append(p)
            elif et == "schedule_change" and p.occasion.slot_id in slot_ids:
                rescheduled[p.occasion].append(p.basis_ref)
            elif et == "takeover" and p["slot_id"] in slot_ids:
                takeovers.append(p)
            elif et == "weather_trigger" and p["occasion"].slot_id in slot_ids:
                weather[p["occasion"]].append(p["policy_ref"])
            elif et == "makeup_plan" and p["makeup_for"].slot_id in slot_ids:
                makeup_plans.append({
                    "occasion": p["occasion"],
                    "makeup_for": p["makeup_for"],
                })
            elif et == "makeup_cancel" and p["makeup_for"].slot_id in slot_ids:
                canceled_plan_keys.add((p["makeup_for"], p["occasion"]))

        sessions: list[ReconstructedSession] = []
        occasion_states: dict[str, str] = {}
        takeover_keys = {Occasion(t["slot_id"], t["week"]).key() for t in takeovers}
        flags_index: dict[str, tuple[str, ...]] = {}
        conflict_keys: set[str] = {
            e.payload["occasion"].key()
            for e in self._entries
            if e.event_type == "venue_conflict_reported"
        }
        # 补课会话先不写入状态：host_key -> (会话, 三方确认结果, 原场次)
        makeup_sessions: dict[str, tuple[ReconstructedSession, ConfirmationResult, Occasion]] = {}
        # 承载补课的计划场次（无论证据是否被采纳，该场次都有实际活动，不是缺口）
        makeup_hosts: set[str] = set()

        def _mode_rank(mode: SessionMode) -> int:
            # 同场次多份教师记录时的确定性次序：按正式授课形态优先
            order = {
                SessionMode.NORMAL: 0,
                SessionMode.RAIN_ALT: 1,
                SessionMode.HEAT_ALT: 1,
                SessionMode.RESCHEDULED: 2,
                SessionMode.MAKEUP: 3,
                SessionMode.FREE: 4,
                SessionMode.EXAM_DRILL: 5,
            }
            return order.get(mode, 9)

        # 按场次做三方确认。同场次可能存在多份教师记录/场地观测（含迟到、乱序
        # 重放的追加事件）：枚举全部组合，优先取三方确认成立的结论，平局按内容
        # 排序，使结论只取决于事件内容、与到达顺序无关。
        all_occasions = sorted(
            set(reports_by_occ) | set(observations_by_occ) | set(attendances),
            key=lambda o: (o.week, o.slot_id),
        )
        for occ in all_occasions:
            if occ.week > current_week:
                continue  # 未来周次不参与核对，避免把“还没上”提前计入
            occ_reports = reports_by_occ.get(occ, [])
            if not occ_reports:
                # 只有观测/签到、没有教师记录的计划场次留待状态展开时归类
                continue
            occ_attendance = attendances.get(occ, [])
            best: Optional[tuple] = None
            for report in occ_reports:
                obs_candidates = observations_by_occ.get(occ) or [None]
                for obs in obs_candidates:
                    result = assessor(
                        report, obs, occ_attendance,
                        class_headcount, adaptations, token_to_student,
                    )
                    rank = (
                        1 if result.confirmed else 0,
                        -_mode_rank(report.mode),
                        result.effective_minutes,
                        report.taught_skill,
                        report.teacher_id,
                        report.basis_ref,
                        obs.venue_id if obs else "",
                        obs.observed_headcount if obs else -1,
                    )
                    if best is None or rank > best[0]:
                        best = (rank, report, obs, result)
            _rank, report, obs, result = best
            key = occ.key()
            flags_index[key] = result.flags

            notes: list[str] = []
            if obs is not None:
                venue = venues.get(obs.venue_id)
                if venue is not None and obs.observed_headcount > venue.safe_capacity:
                    notes.append("capacity_breach")
            if key in conflict_keys:
                notes.append("venue_conflict_actual")

            session = ReconstructedSession(
                occasion=occ, class_id=class_id, kind=report.kind,
                taught_skill=report.taught_skill, mode=report.mode,
                minutes=result.effective_minutes, confirmed=result.confirmed,
                reasons=result.reasons, flags=result.flags,
                notes=tuple(notes), makeup_for=report.makeup_for,
                per_student=result.per_student_minutes,
            )
            sessions.append(session)

            if report.mode == SessionMode.MAKEUP and report.makeup_for is not None:
                # 补课会话不作为独立完成行：它是原场次的候选履约证据。
                makeup_sessions[key] = (session, result, report.makeup_for)
                if key in plan_keys:
                    makeup_hosts.add(key)
                continue

            occasion_states[key] = "completed" if result.confirmed else "in_review"

        # 展开计划场次状态（计划有但无普通授课报告）
        missing: list[str] = []
        for slot in class_slots:
            for week in range(1, current_week + 1):
                if not slot.active_in_week(week):
                    continue
                occ = Occasion(slot.slot_id, week)
                key = occ.key()
                if key in occasion_states:
                    continue
                # 承载补课的计划场次最后再归类，避免误报为缺口
                if key in makeup_hosts:
                    continue
                if occ in rescheduled:
                    occasion_states[key] = "rescheduled"
                    continue
                if key in takeover_keys:
                    occasion_states[key] = "taken_over"
                    continue
                if occ in weather and occ not in reports_by_occ:
                    occasion_states[key] = "weather_pending"  # 触发但未执行替代
                    continue
                occasion_states[key] = "missing"
                missing.append(key)

        # ---- 补课证据仲裁：一次原场次最多接受一个有效补课结果 ----
        # 候选条件：三方确认通过、补课计划未取消、原场次属于本班且已到周、
        # 原场次当前处于待补状态（已完成/已改期等终态不再接受补课）。
        candidates: list[tuple[Occasion, str, ReconstructedSession]] = []
        for host_key, (session, _result, original) in makeup_sessions.items():
            original_key = original.key()
            if not session.confirmed:
                continue  # 未确认补课保留为 host 场次自身状态，不回填原场次
            if (original, session.occasion) in canceled_plan_keys:
                continue  # 已取消的排补不产生履约
            if original_key not in plan_keys:
                continue  # 不挂本班计划场次的补课不作数
            if occasion_states.get(original_key) not in PENDING_STATES:
                continue  # 原场次无需补（已完成/已改期）
            candidates.append((original, host_key, session))

        # 确定性顺序：先按原场次，再按补课场次时间与槽位；与事件到达顺序无关
        candidates.sort(key=lambda c: (
            c[0].week, c[0].slot_id, c[2].occasion.week, c[2].occasion.slot_id,
        ))

        made_up: set[str] = set()
        accepted_makeup: dict[str, str] = {}          # 原场次 key -> 采用的补课场次 key
        accepted_host_keys: set[str] = set()
        rejected_hosts: dict[str, str] = {}
        for original, host_key, session in candidates:
            original_key = original.key()
            if original_key in made_up:
                rejected_hosts[host_key] = "duplicate_makeup"
                continue
            occasion_states[original_key] = "completed"
            made_up.add(original_key)
            accepted_makeup[original_key] = host_key
            accepted_host_keys.add(host_key)

        # 承载补课的计划场次归类：
        # - 三方确认通过：该计划场次确实开展了活动，计 completed（它本身是分母中的
        #   一场计划课）；但只有被仲裁采纳的那一场才能回填原场次、授予原周技能覆盖；
        # - 未通过三方确认：该场次自身计划未兑现，计 in_review 并继续待补。
        # 非计划时段举办的补课不进入状态表，纯粹作为原场次的履约证据。
        for host_key in sorted(makeup_hosts):
            session, result, _original = makeup_sessions[host_key]
            if result.confirmed:
                occasion_states[host_key] = "completed"
                if host_key not in accepted_host_keys:
                    # 重复/取消而未被采纳的补课：活动照实计完成，但留痕说明
                    # 其补课主张未被采纳，便于教研证据引用。
                    note = rejected_hosts.get(host_key, "makeup_rejected")
                    idx = next(i for i, s in enumerate(sessions) if s.occasion.key() == host_key)
                    sessions[idx] = ReconstructedSession(
                        occasion=session.occasion, class_id=session.class_id, kind=session.kind,
                        taught_skill=session.taught_skill, mode=session.mode,
                        minutes=session.minutes, confirmed=session.confirmed,
                        reasons=session.reasons,
                        flags=tuple(list(session.flags) + [note]),
                        notes=session.notes, makeup_for=session.makeup_for,
                        per_student=session.per_student,
                    )
            else:
                occasion_states[host_key] = "in_review"

        # 占课 / 无事件 / 天气未替代 -> 待补课；三方确认未过单列在审（states=in_review）。
        # 已由补课回填的原场次已是 completed，不会出现在这里。
        pending_makeup = [
            k for k, st in occasion_states.items() if st in GAP_STATES
        ]

        # 当前有效补课安排（与事件到达顺序无关的确定性选择）：
        # 原场次已有被采纳证据时指向该证据场次；否则在未取消的计划中
        # 优先取已有三方确认补课会话的场次，再按补课场次时间取最早一条。
        def _plan_rank(plan: dict):
            host_key = plan["occasion"].key()
            session_info = makeup_sessions.get(host_key)
            confirmed = bool(session_info and session_info[0].confirmed)
            return (
                0 if confirmed else 1,
                plan["occasion"].week,
                plan["occasion"].slot_id,
            )

        active_plans: dict[str, list[dict]] = defaultdict(list)
        for plan in makeup_plans:
            if (plan["makeup_for"], plan["occasion"]) in canceled_plan_keys:
                continue
            active_plans[plan["makeup_for"].key()].append(plan)

        makeup_schedule: dict[str, str] = {}
        for original_key, plans in active_plans.items():
            if original_key in accepted_makeup:
                makeup_schedule[original_key] = accepted_makeup[original_key]
            else:
                chosen = min(plans, key=_plan_rank)
                makeup_schedule[original_key] = chosen["occasion"].key()

        return {
            "class_id": class_id,
            "states": occasion_states,
            # 完成率唯一分母：本班截至当前周的全部计划场次键
            "plan_occasions": tuple(sorted(plan_keys)),
            "sessions": sessions,
            "missing_occasions": tuple(sorted(missing)),
            "pending_makeup": tuple(sorted(pending_makeup)),
            "makeup_schedule": makeup_schedule,
            "made_up": made_up,
            # 原场次 key -> 被采纳的补课证据场次 key（一次原场次至多一条）
            "accepted_makeup": dict(sorted(accepted_makeup.items())),
            # 被采纳为履约证据的补课场次 key 集合（coverage 只认这些补课会话）
            "accepted_makeup_hosts": frozenset(accepted_host_keys),
            "rescheduled": {k.key(): sorted(refs)[0] for k, refs in rescheduled.items()},
            "takeovers": tuple(sorted(takeovers, key=lambda t: (t["week"], t["slot_id"]))),
            "flags": flags_index,
        }
