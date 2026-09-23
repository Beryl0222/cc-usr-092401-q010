"""只追加事件账本与班级实况还原。

连续发生“场地冲突 → 临时占课 → 离线补签”后，仍能按时间顺序重放事件，
还原每个班真正完成的内容、缺口与补课安排。账本只追加、不修改不删除；
所有判定（确认、异常、覆盖）都是重放的派生结果，可随时重新计算。

事件类型：

- teacher_report / venue_observation / sample_attendance
- schedule_change（合规调课）
- takeover（临时占课：其他学科占用，记录占用科目与依据单号，可为空表示突发）
- weather_trigger（降雨/高温触发，关联预案）
- cancellation（整场取消，记录原因与依据；取消不构成待补缺口）
- makeup_plan（补课安排，挂接缺课场次；同一原场次可多次排补）
- review_resolved（教研结论）

口径（与 coverage / visibility 共享同一份事实）：

- **计划场次是唯一分母**：states 只包含生效方案在当前周之前展开的
  slot × 教学周；补课会话不是独立分母行，只是原场次的履约证据。
- **一次原场次最多接受一个有效补课结果**：同一原场次先后收到多个
  makeup_plan 时，账本序号最后的安排为生效安排（早先安排保留为
  superseded 记录）；只有发生在生效安排场次上、且经三方确认的补课
  会话才把原场次回填为 completed。补课失败（三方未过）保留
  makeup_unconfirmed 状态，原场次仍属待补，可再次重排。
- 取消、改期、占课、降雨高温触发未替代、未确认补课各自保留独立状态。
- 全部结论在收集完事件后一次性派生，事件先后到达（乱序追加、迟到
  补签）不影响重放结论；所有列表/标记均按确定顺序输出。
"""

from __future__ import annotations

from collections import defaultdict
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
from .models import ActivityKind

# 原场次尚未兑现、仍需补课的状态
PENDING_STATES = frozenset((
    "taken_over",         # 临时占课
    "missing",            # 课表有、无任何有效交付（疑似阴阳课表）
    "weather_pending",    # 触发降雨高温但未执行替代
    "makeup_unconfirmed", # 已安排补课但补课会话三方确认未过
))


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


@dataclass(frozen=True)
class MakeupRecord:
    """一条补课履约证据（不论是否被采纳为原场次的有效结果）。"""

    original_key: str    # 被补原场次 key
    makeup_key: str      # 补课会话所在场次 key
    confirmed: bool      # 补课会话本身是否通过三方确认
    effective: bool      # 是否属于该原场次的生效补课安排
    fulfilled: bool      # 是否就是回填原场次的那一条有效结果
    reasons: tuple[str, ...]


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
        返回结构化实况：场次状态（仅计划场次）、会话、缺课、
        补课安排与履约证据、调课、占课、取消、标记。
        """
        # ---- 展开计划场次（只核对到当前教学周；未来周次不算缺口）----
        planned_occasions: dict[str, Occasion] = {}
        for slot in class_slots:
            for week in range(1, current_week + 1):
                if not slot.active_in_week(week):
                    continue
                occ = Occasion(slot.slot_id, week)
                planned_occasions[occ.key()] = occ
        planned_keys = set(planned_occasions)
        slot_ids = {s.slot_id for s in class_slots}

        # ---- 第一遍：收集事件，不做任何结论（乱序安全）----
        own_reports: dict[Occasion, TeacherReport] = {}
        makeup_reports: dict[Occasion, TeacherReport] = {}
        observations: dict[Occasion, VenueObservation] = {}
        attendances: dict[Occasion, list[SampleAttendance]] = defaultdict(list)
        rescheduled: dict[Occasion, str] = {}   # -> 依据
        takeovers: list[dict] = []
        weather: dict[Occasion, str] = {}       # -> 预案依据
        cancellations: dict[Occasion, str] = {}  # -> 取消原因/依据
        makeup_plans: list[tuple[int, Occasion, Occasion]] = []  # (seq, 原场次, 补课场次)

        for entry in self._entries:
            p = entry.payload
            et = entry.event_type
            if et == "teacher_report":
                # 归属本班的补课证据：挂接的原场次属于本班计划
                is_makeup_evidence = (
                    p.mode == SessionMode.MAKEUP
                    and p.makeup_for is not None
                    and p.makeup_for.key() in planned_keys
                )
                if is_makeup_evidence:
                    makeup_reports[p.occasion] = p
                elif p.mode == SessionMode.MAKEUP and p.makeup_for is not None:
                    # 补课证据挂接的是他班原场次：不计入本班自身交付
                    continue
                elif p.occasion.slot_id in slot_ids:
                    # 本场自身的交付（含未挂接原场次的无效补课，按未确认处理）
                    own_reports[p.occasion] = p
            elif et == "venue_observation":
                observations[p.occasion] = p
            elif et == "sample_attendance":
                attendances[p.occasion].append(p)
            elif et == "schedule_change" and p.occasion.slot_id in slot_ids:
                rescheduled[p.occasion] = p.basis_ref
            elif et == "takeover" and p["slot_id"] in slot_ids:
                takeovers.append(p)
            elif et == "weather_trigger" and p["occasion"].slot_id in slot_ids:
                weather[p["occasion"]] = p["policy_ref"]
            elif et == "cancellation" and p["occasion"].slot_id in slot_ids:
                cancellations[p["occasion"]] = p.get("reason", "取消")
            elif et == "makeup_plan" and p["makeup_for"].key() in planned_keys:
                makeup_plans.append((entry.seq, p["makeup_for"], p["occasion"]))

        # ---- 补课安排：账本序号最后一条为生效安排，其余留档 superseded ----
        plans_by_original: dict[Occasion, list[tuple[int, Occasion]]] = defaultdict(list)
        for seq, original, makeup_occ in makeup_plans:
            plans_by_original[original].append((seq, makeup_occ))
        effective_plan: dict[Occasion, Occasion] = {
            original: sorted(plans, key=lambda x: x[0])[-1][1]
            for original, plans in plans_by_original.items()
        }
        superseded_plans = tuple(
            (original.key(), host.key())
            for original, plans in plans_by_original.items()
            for seq, host in sorted(plans, key=lambda x: x[0])[:-1]
        )

        def _build_session(report: TeacherReport) -> ReconstructedSession:
            occ = report.occasion
            obs = observations.get(occ)
            notes: list[str] = []
            if obs is not None:
                venue = venues.get(obs.venue_id)
                if venue is not None and obs.observed_headcount > venue.safe_capacity:
                    notes.append("capacity_breach")
            for entry in self._entries:
                if (
                    entry.event_type == "venue_conflict_reported"
                    and entry.payload["occasion"] == occ
                ):
                    notes.append("venue_conflict_actual")
            result = assessor(
                report, obs, attendances.get(occ, []),
                class_headcount, adaptations, token_to_student,
            )
            return ReconstructedSession(
                occasion=occ, class_id=class_id, kind=report.kind,
                taught_skill=report.taught_skill, mode=report.mode,
                minutes=result.effective_minutes, confirmed=result.confirmed,
                reasons=result.reasons, flags=tuple(sorted(result.flags)),
                notes=tuple(sorted(notes)), makeup_for=report.makeup_for,
                per_student=result.per_student_minutes,
            )

        own_sessions = [_build_session(r) for r in own_reports.values()]
        makeup_sessions = [_build_session(r) for r in makeup_reports.values()]

        sessions = sorted(
            own_sessions + makeup_sessions,
            key=lambda s: (s.occasion.week, s.occasion.slot_id),
        )
        flags_index = {s.occasion.key(): s.flags for s in sessions}

        # ---- 第二遍：对计划场次派生状态（一次性、与事件顺序无关）----
        occasion_states: dict[str, str] = {}

        # 1) 本场自身的有效交付
        for s in own_sessions:
            key = s.occasion.key()
            if key not in planned_keys:
                continue
            occasion_states[key] = "completed" if s.confirmed else "in_review"

        # 2) 补课履约：每个原场次最多采纳一条生效安排上的确认会话
        made_up: set[str] = set()
        completion_evidence: dict[str, str] = {}
        for original, host in effective_plan.items():
            okey = original.key()
            if okey not in planned_keys:
                continue
            if occasion_states.get(okey) == "completed":
                continue  # 原场次已自行兑现，补课证据不再改写结论
            candidates = sorted(
                (s for s in makeup_sessions
                 if s.makeup_for == original and s.occasion == host and s.confirmed),
                key=lambda s: (s.occasion.week, s.occasion.slot_id),
            )
            if candidates:
                chosen = candidates[0]
                occasion_states[okey] = "completed"
                made_up.add(okey)
                completion_evidence[okey] = chosen.occasion.key()
            elif any(s.occasion == host for s in makeup_sessions
                     if s.makeup_for == original):
                # 生效补课场次上有会话但三方确认未过 → 保留独立状态，仍可重排
                occasion_states[okey] = "makeup_unconfirmed"

        # 2b) 补课会话占用了本班计划场次：该场次确实上了一节经确认的课，
        # 按会话确认结果落状态（其教学内容归属原周次，在 coverage 处理），
        # 不能因为本场没有“自己的”报告就误报为 missing（阴阳课表）。
        for s in makeup_sessions:
            hkey = s.occasion.key()
            if hkey in planned_keys and hkey not in occasion_states:
                occasion_states[hkey] = "completed" if s.confirmed else "in_review"

        # 3) 无自身交付的计划场次：改期 / 占课 / 天气待替代 / 取消 / 缺失
        takeover_keys = {
            Occasion(t["slot_id"], t["week"]).key() for t in takeovers
        }
        missing: list[str] = []
        for key, occ in planned_occasions.items():
            if key in occasion_states:
                continue
            if occ in rescheduled:
                occasion_states[key] = "rescheduled"
            elif key in takeover_keys:
                occasion_states[key] = "taken_over"
            elif occ in weather and occ not in own_reports:
                occasion_states[key] = "weather_pending"
            elif occ in cancellations:
                occasion_states[key] = "cancelled"
            else:
                occasion_states[key] = "missing"
                missing.append(key)

        pending_makeup = tuple(sorted(
            k for k, st in occasion_states.items() if st in PENDING_STATES
        ))

        # ---- 补课证据台账：每条会话留档，标明是否生效/是否采纳 ----
        makeup_records = tuple(sorted(
            (
                MakeupRecord(
                    original_key=s.makeup_for.key(),
                    makeup_key=s.occasion.key(),
                    confirmed=s.confirmed,
                    effective=(
                        effective_plan.get(s.makeup_for) == s.occasion
                    ),
                    fulfilled=(
                        completion_evidence.get(s.makeup_for.key()) == s.occasion.key()
                        and s.confirmed
                    ),
                    reasons=s.reasons,
                )
                for s in makeup_sessions
            ),
            key=lambda r: (r.original_key, r.makeup_key),
        ))

        return {
            "class_id": class_id,
            "states": occasion_states,
            "sessions": sessions,
            "missing_occasions": tuple(sorted(missing)),
            "pending_makeup": pending_makeup,
            "in_review": tuple(sorted(
                k for k, st in occasion_states.items() if st == "in_review"
            )),
            "cancelled": {k.key(): reason for k, reason in cancellations.items()},
            "makeup_schedule": {
                original.key(): host.key()
                for original, host in effective_plan.items()
            },
            "makeup_plans_superseded": superseded_plans,
            "makeup_records": makeup_records,
            "completion_evidence": dict(completion_evidence),
            "made_up": made_up,
            "rescheduled": {k.key(): ref for k, ref in rescheduled.items()},
            "takeovers": tuple(sorted(takeovers, key=lambda t: (t["week"], t["slot_id"]))),
            "flags": flags_index,
        }
