"""补课本钱修复后的口径测试。

证明：
- 计划场次是唯一分母：补课后分母不增加、完成率不下降；
- 补课只是原场次的履约证据：一次原场次最多接受一个有效补课结果；
- 多次排补、补课失败后重排、补课取消、乱序/迟到重放结论确定；
- 教会/勤练/常赛按挂接原周次只计一次，不重复归入原教学周；
- 取消、改期、占课、降雨高温未替代、未确认补课保留各自状态；
- 伤病适配只影响本人时长，不改变班级完成；
- 未结教研复核班级整班排除出公众汇总，小样本抑制规则不变；
- 总场次、完成数、待补数与证据引用严格一致。
"""

import random
import unittest
from copy import deepcopy

from pe_domain.coverage import compute_class_coverage
from pe_domain.events import (
    Occasion,
    SampleAttendance,
    SessionMode,
    TeacherReport,
    VenueObservation,
)
from pe_domain.ledger import EventLedger
from pe_domain.models import (
    ActivityKind,
    InjuryAdaptation,
    PlanSlot,
    SkillGoal,
)
from pe_domain.review import ReviewBoard, ReviewStatus
from pe_domain.visibility import build_public_summary

from test_pe_domain import (
    CLASS_TEACHER,
    CLASS_VENUE,
    GOAL_BB,
    HEADCOUNT,
    TEACHERS,
    VENUES,
    att,
    five_pe_slots,
)


def normal_events(ledger: EventLedger, occ: Occasion, when: str, skill: str = "BB"):
    """一场正常确认课的教师/场地/抽样事件。"""
    ledger.append("teacher_report", TeacherReport(
        occ, "T-WANG", ActivityKind.PE_CLASS, skill, 40,
        SessionMode.NORMAL, "V-FIELD"))
    ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
    for i in range(4):
        ledger.append("sample_attendance", att(f"t{i}", occ, when))


def record_makeup(ledger: EventLedger, host: Occasion, original: Occasion, when: str,
                  *, planned=True, canceled=False, confirmed=True,
                  venue="V-GYM", basis="MK-001", tokens=None):
    """排一场补课并（默认）补齐三方确认事件。"""
    if planned:
        ledger.append("makeup_plan", {"occasion": host, "makeup_for": original})
        if canceled:
            ledger.append("makeup_cancel", {"occasion": host, "makeup_for": original})
    ledger.append("teacher_report", TeacherReport(
        host, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
        SessionMode.MAKEUP, venue, basis_ref=basis, makeup_for=original))
    if confirmed:
        ledger.append("venue_observation", VenueObservation(host, venue, 40))
        for i, tok in enumerate(tokens or ("mk0", "mk1", "mk2", "mk3")):
            ledger.append("sample_attendance", att(tok, host, when))


def rebuild(ledger: EventLedger, slots, week, *, adaptations=None, token_map=None):
    return ledger.rebuild_class(
        "C1", slots, HEADCOUNT, adaptations or {}, token_map or {},
        VENUES, current_week=week,
    )


def coverage_of(rebuilt, slots, goals=(GOAL_BB,)):
    return compute_class_coverage("C1", rebuilt, goals)


class MakeupAccountingTest(unittest.TestCase):
    def setUp(self):
        self.slots = five_pe_slots("C1")
        self.orig = Occasion("C1-PE-1", 1)

    def _takeover(self, ledger, occ):
        ledger.append("takeover",
                      {"slot_id": occ.slot_id, "week": occ.week, "subject": "数学", "ref": ""})

    def _rest_of_week_normal(self, ledger, week):
        for slot in self.slots:
            if slot.weekday == 1:
                continue  # 周一由各用例安排（缺课/补课 host）
            occ = Occasion(slot.slot_id, week)
            normal_events(ledger, occ, f"2026-09-{slot.weekday + (week - 1) * 7:02d}T10:00:00")

    # ---------------------------------------------------------------- 1
    def test_multiple_makeup_plans_only_one_backfills_original(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host_a = Occasion("C1-PE-2", 2)  # 第一次排补：第 2 周
        host_b = Occasion("C1-PE-3", 3)  # 第二次排补：第 3 周（重复）
        record_makeup(ledger, host_a, self.orig, "2026-09-14T10:00:00")
        record_makeup(ledger, host_b, self.orig, "2026-09-21T10:00:00")

        rebuilt = rebuild(ledger, self.slots, 3)
        cov = coverage_of(rebuilt, self.slots)

        # 分母严格等于计划场次：3 周 × 5 场，补课不增加任何行
        self.assertEqual(len(rebuilt["plan_occasions"]), 15)
        self.assertEqual(cov.total_occasions, 15)
        self.assertNotIn("completed_makeup", rebuilt["states"].values())
        self.assertEqual(set(rebuilt["states"]), set(rebuilt["plan_occasions"]))

        # 原场次只被最早的有效补课回填一次
        self.assertEqual(rebuilt["states"][self.orig.key()], "completed")
        self.assertEqual(rebuilt["accepted_makeup"], {self.orig.key(): host_a.key()})
        self.assertEqual(rebuilt["makeup_schedule"][self.orig.key()], host_a.key())
        self.assertEqual(rebuilt["made_up"], {self.orig.key()})

        # 第二个补课自身的计划场次照实完成，但带 duplicate_makeup 留痕、不回填
        self.assertEqual(rebuilt["states"][host_b.key()], "completed")
        host_b_session = next(s for s in rebuilt["sessions"] if s.occasion == host_b)
        self.assertIn("duplicate_makeup", host_b_session.flags)

        # 完成数对账：第 1 周 4 场普通课 + 原场次回填 = 5，两个 host 各 1 场 = 7；
        # 第 2、3 周其余 8 个无事件场次为待补。
        self.assertEqual(cov.completed, 7)
        self.assertEqual(cov.pending_makeup, 8)
        self.assertEqual(cov.in_review, 0)
        self.assertEqual(
            cov.completed + cov.pending_makeup + cov.in_review,
            cov.total_occasions,
        )
        # 若补课被算成第二分母，完成率会从 7/15 被错误拉低
        self.assertEqual(cov.completion_ratio, round(7 / 15, 3))

        # 原周技能覆盖只引用被采纳的一场，第二场补课不重复归入第 1 周
        bb = cov.skill_coverage[0]
        self.assertTrue(bb.taught)
        makeup_teach = [
            e for e in bb.evidence
            if e in (f"teach:{host_a.key()}", f"teach:{host_b.key()}")
        ]
        self.assertEqual(makeup_teach, [f"teach:{host_a.key()}"])

    # ---------------------------------------------------------------- 2
    def test_failed_makeup_then_replanned_uses_second_result(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host_a = Occasion("C1-PE-2", 2)  # 第一次补课：缺场地观测，三方确认失败
        host_b = Occasion("C1-PE-3", 3)  # 重排：完整确认
        record_makeup(ledger, host_a, self.orig, "2026-09-14T10:00:00", confirmed=False)
        record_makeup(ledger, host_b, self.orig, "2026-09-21T10:00:00")

        rebuilt = rebuild(ledger, self.slots, 3)
        cov = coverage_of(rebuilt, self.slots)

        # 失败补课不回填；重排的成功补课回填原场次
        self.assertEqual(rebuilt["states"][self.orig.key()], "completed")
        self.assertEqual(rebuilt["accepted_makeup"], {self.orig.key(): host_b.key()})
        self.assertEqual(rebuilt["makeup_schedule"][self.orig.key()], host_b.key())
        # 失败补课占用的计划场次自身未兑现：在审（单列，不算缺口待补）
        self.assertEqual(rebuilt["states"][host_a.key()], "in_review")
        self.assertNotIn(host_a.key(), rebuilt["pending_makeup"])
        self.assertEqual(cov.in_review, 1)
        self.assertEqual(
            cov.completed + cov.pending_makeup + cov.in_review,
            cov.total_occasions,
        )

        bb = cov.skill_coverage[0]
        self.assertIn(f"teach:{host_b.key()}", bb.evidence)
        self.assertNotIn(f"teach:{host_a.key()}", bb.evidence)

    # ---------------------------------------------------------------- 3
    def test_canceled_makeup_plan_is_not_valid_evidence(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host_a = Occasion("C1-PE-2", 2)  # 已取消的排补（即便事件齐全也不作数）
        host_b = Occasion("C1-PE-3", 3)
        record_makeup(ledger, host_a, self.orig, "2026-09-14T10:00:00", canceled=True)
        record_makeup(ledger, host_b, self.orig, "2026-09-21T10:00:00")

        rebuilt = rebuild(ledger, self.slots, 3)
        self.assertEqual(rebuilt["states"][self.orig.key()], "completed")
        self.assertEqual(rebuilt["accepted_makeup"], {self.orig.key(): host_b.key()})
        self.assertEqual(rebuilt["makeup_schedule"][self.orig.key()], host_b.key())
        canceled_session = next(s for s in rebuilt["sessions"] if s.occasion == host_a)
        self.assertIn("makeup_rejected", canceled_session.flags)

    def test_only_canceled_makeup_leaves_original_pending(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host = Occasion("C1-PE-2", 2)
        record_makeup(ledger, host, self.orig, "2026-09-14T10:00:00", canceled=True)

        rebuilt = rebuild(ledger, self.slots, 2)
        # 原场次仍未兑现：保持占课状态并挂待补；补课 host 自身完成但不回填
        self.assertEqual(rebuilt["states"][self.orig.key()], "taken_over")
        self.assertIn(self.orig.key(), rebuilt["pending_makeup"])
        self.assertEqual(rebuilt["accepted_makeup"], {})
        self.assertEqual(rebuilt["states"][host.key()], "completed")
        self.assertEqual(rebuilt["makeup_schedule"], {})  # 无有效排补

    # ---------------------------------------------------------------- 4
    def test_replay_is_deterministic_under_shuffled_and_late_events(self):
        def build_entries():
            ledger = EventLedger()
            ledger.append("takeover",
                          {"slot_id": self.orig.slot_id, "week": self.orig.week,
                           "subject": "数学", "ref": ""})
            for slot in self.slots:
                if slot.weekday == 1:
                    continue
                occ = Occasion(slot.slot_id, 1)
                normal_events(ledger, occ, f"2026-09-{slot.weekday:02d}T10:00:00")
            host_a = Occasion("C1-PE-2", 2)
            host_b = Occasion("C1-PE-3", 3)
            record_makeup(ledger, host_a, self.orig, "2026-09-14T10:00:00")
            record_makeup(ledger, host_b, self.orig, "2026-09-21T10:00:00")
            return ledger.entries()

        ordered = build_entries()

        def replay(entries):
            ledger = EventLedger()
            for e in entries:
                ledger.append(e.event_type, e.payload)
            rebuilt = rebuild(ledger, self.slots, 3)
            cov = coverage_of(rebuilt, self.slots)
            return (
                deepcopy(rebuilt["states"]),
                rebuilt["accepted_makeup"],
                rebuilt["makeup_schedule"],
                tuple(sorted(rebuilt["made_up"])),
                tuple(sorted(rebuilt["pending_makeup"])),
                (cov.total_occasions, cov.completed, cov.pending_makeup, cov.in_review),
                cov.skill_coverage[0].evidence,
            )

        baseline = replay(ordered)
        for seed in range(20):
            shuffled = list(ordered)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(replay(shuffled), baseline, f"乱序 seed={seed} 结论不一致")

    def test_late_correction_report_converges_regardless_of_arrival(self):
        occ = self.orig
        good = TeacherReport(
            occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.RAIN_ALT, "V-GYM", basis_ref="POL-RAIN-01")
        venue = VenueObservation(occ, "V-GYM", 40)
        attendance = [att(f"t{i}", occ, "2026-09-07T10:00:00") for i in range(4)]

        def replay(order):
            ledger = EventLedger()
            for item in order:
                kind, payload = item
                ledger.append(kind, payload)
            return rebuild(ledger, self.slots, 1)

        events = [
            ("teacher_report", good),
            ("venue_observation", venue),
            *[("sample_attendance", a) for a in attendance],
        ]
        forward = replay(events)
        backward = replay(list(reversed(events)))
        self.assertEqual(forward["states"][occ.key()], "completed")
        self.assertEqual(backward["states"][occ.key()], "completed")
        self.assertEqual(
            coverage_of(forward, self.slots).completed,
            coverage_of(backward, self.slots).completed,
        )

    # ---------------------------------------------------------------- 5
    def test_makeup_on_extra_slot_is_pure_evidence_not_a_denominator_row(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host = Occasion("C1-MAKEUP-SAT", 2)  # 非方案时段（周六补课）
        record_makeup(ledger, host, self.orig, "2026-09-12T09:00:00")

        rebuilt = rebuild(ledger, self.slots, 2)
        cov = coverage_of(rebuilt, self.slots)

        self.assertNotIn(host.key(), rebuilt["states"])          # 不进状态表
        self.assertEqual(cov.total_occasions, 10)                # 2 周 × 5 场
        self.assertEqual(rebuilt["states"][self.orig.key()], "completed")
        self.assertEqual(rebuilt["accepted_makeup"], {self.orig.key(): host.key()})
        self.assertEqual(rebuilt["makeup_schedule"][self.orig.key()], host.key())
        bb = cov.skill_coverage[0]
        self.assertIn(f"teach:{host.key()}", bb.evidence)       # 证据可引用

    # ---------------------------------------------------------------- 6
    def test_distinct_statuses_are_preserved(self):
        from pe_domain.events import ScheduleChange

        ledger = EventLedger()
        taken = Occasion("C1-PE-1", 1)
        moved = Occasion("C1-PE-2", 1)
        rained = Occasion("C1-PE-3", 1)
        failed = Occasion("C1-PE-4", 1)
        ledger.append("takeover",
                      {"slot_id": taken.slot_id, "week": 1, "subject": "数学", "ref": ""})
        ledger.append("schedule_change", ScheduleChange(
            moved, new_weekday=3, new_venue_id="V-GYM",
            new_teacher_id="T-WANG", approver="principal", basis_ref="ADJ-09"))
        ledger.append("weather_trigger",
                      {"occasion": rained, "condition": "rain", "policy_ref": "POL-RAIN-01"})
        # 周五：一场补课式记录但三方不成立（未挂原场次时直接在审）
        record_makeup(
            ledger,
            Occasion("C1-PE-2", 2), failed,
            "2026-09-14T10:00:00", confirmed=False,
        )

        rebuilt = rebuild(ledger, self.slots, 2)
        self.assertEqual(rebuilt["states"][taken.key()], "taken_over")
        self.assertEqual(rebuilt["states"][moved.key()], "rescheduled")
        self.assertEqual(rebuilt["states"][rained.key()], "weather_pending")
        self.assertEqual(rebuilt["states"]["C1-PE-2#w2"], "in_review")
        # 失败补课不回填原场次：周五仍 missing（无任何事件）
        self.assertEqual(rebuilt["states"][failed.key()], "missing")
        pending = set(rebuilt["pending_makeup"])
        # 各缺口状态各自保留、独立可查（在审场次不混入待补）
        self.assertIn(taken.key(), pending)
        self.assertIn(rained.key(), pending)
        self.assertIn(failed.key(), pending)
        self.assertNotIn("C1-PE-2#w2", pending)
        self.assertNotIn(moved.key(), pending)

    # ---------------------------------------------------------------- 7
    def test_injury_adaptation_affects_only_that_student_not_class_completion(self):
        ledger = EventLedger()
        self._takeover(ledger, self.orig)
        self._rest_of_week_normal(ledger, 1)
        host = Occasion("C1-PE-2", 2)
        adaptation = InjuryAdaptation("student-7", "BB", adjusted_minutes=10,
                                      basis_ref="MED-2026-07", note="踝伤")
        record_makeup(
            ledger, host, self.orig, "2026-09-14T10:00:00",
            tokens=("tok7", "mk1", "mk2", "mk3"),
        )
        rebuilt = rebuild(
            ledger, self.slots, 2,
            adaptations={"student-7": adaptation},
            token_map={"tok7": "student-7"},
        )
        cov = coverage_of(rebuilt, self.slots)

        # 班级层面：原场次照常回填完成，完成数不被个人折减改变
        self.assertEqual(rebuilt["states"][self.orig.key()], "completed")
        self.assertEqual(cov.completed, 6)  # 第 1 周 5 场 + 补课 host
        # 个人层面：仅 tok7 折减到 10 分钟
        session = next(s for s in rebuilt["sessions"] if s.occasion == host)
        self.assertEqual(session.per_student["tok7"], 10)
        self.assertEqual(session.per_student["mk1"], 40)


class PublicSummaryMakeupTest(unittest.TestCase):
    def _class_ledger(self, cid: str, *, with_takeover_makeup=False, pending_gap=0):
        slots = five_pe_slots(cid)
        ledger = EventLedger()
        makeup_host = Occasion(f"{cid}-PE-5", 2) if with_takeover_makeup else None
        for w in range(1, 3):
            for slot in slots:
                occ = Occasion(slot.slot_id, w)
                if with_takeover_makeup and occ == Occasion(f"{cid}-PE-1", 1):
                    ledger.append("takeover",
                                  {"slot_id": occ.slot_id, "week": 1,
                                   "subject": "数学", "ref": ""})
                    continue
                if occ == makeup_host:
                    continue  # 该课时留给补课
                if pending_gap and (w == 2 and slot.weekday <= pending_gap):
                    continue  # 留白：制造待补场次
                teacher = TEACHERS[CLASS_TEACHER[cid]]
                ledger.append("teacher_report", TeacherReport(
                    occ, teacher.teacher_id, ActivityKind.PE_CLASS, "BB", 40,
                    SessionMode.NORMAL, CLASS_VENUE[cid]))
                ledger.append("venue_observation",
                              VenueObservation(occ, CLASS_VENUE[cid], HEADCOUNT))
                for i in range(4):
                    ledger.append("sample_attendance",
                                  att(f"{cid}-t{i}", occ,
                                      f"2026-09-{slot.weekday + (w - 1) * 7:02d}T10:00:00"))
        if with_takeover_makeup:
            orig = Occasion(f"{cid}-PE-1", 1)
            record_makeup(ledger, makeup_host, orig, "2026-09-14T10:00:00",
                          venue=CLASS_VENUE[cid])
        return ledger, slots

    def _coverages(self, **kw):
        out = {}
        for cid in ("C1", "C2", "C3"):
            ledger, slots = self._class_ledger(cid, **kw)
            rebuilt = ledger.rebuild_class(
                cid, slots, HEADCOUNT, {}, {}, VENUES, current_week=2)
            out[cid] = compute_class_coverage(cid, rebuilt, (GOAL_BB,))
        return out

    def test_completed_makeup_keeps_school_ratio_from_dropping(self):
        coverages = self._coverages()
        # C1 缺课且已补：分子分母都仍是 10，完成率 1.0；补课没有造出第二分母
        c1 = coverages["C1"]
        self.assertEqual((c1.total_occasions, c1.completed), (10, 10))
        self.assertEqual(c1.completion_ratio, 1.0)
        self.assertEqual(c1.pending_makeup, 0)

        summary = build_public_summary("S", coverages, {})
        self.assertTrue(summary.published)
        self.assertEqual(summary.classes_counted, 3)
        self.assertEqual(summary.occasions_completed_ratio, 1.0)

    def test_open_review_class_excluded_and_small_sample_rules_unchanged(self):
        # 每班留 2 个待补：3 班合计 6（>=5，单元格可公布）
        coverages = self._coverages(pending_gap=2)
        board = ReviewBoard()
        case = board.open_case("C3", [])
        summary = build_public_summary("S", coverages, {"C3": board.get(case.case_id)})
        # C3 在复核：整班排除后只剩 2 班 -> 整体抑制
        self.assertFalse(summary.published)
        self.assertEqual(summary.classes_counted, 2)

        board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员", "调课有据")
        cases = {"C3": board.get(case.case_id)}
        summary = build_public_summary("S", coverages, cases)
        self.assertTrue(summary.published)
        self.assertEqual(summary.classes_counted, 3)
        # 总场次 30、完成 24、待补 6
        self.assertEqual(summary.occasions_completed_ratio, round(24 / 30, 3))
        self.assertEqual(summary.pending_makeup, 6)

        # 单元格抑制不变：待补合计 < 5 时不公布具体数
        few_gaps = self._coverages(pending_gap=1)  # 每班 1，合计 3
        summary_hidden = build_public_summary("S", few_gaps, {})
        self.assertTrue(summary_hidden.published)
        self.assertEqual(summary_hidden.pending_makeup, 0)


if __name__ == "__main__":
    unittest.main()
