"""pe_domain 领域规则测试：方案版本、三方确认、防虚增、还原、复核与可见性。"""

import unittest

from pe_domain.coverage import compute_class_coverage
from pe_domain.events import (
    MIN_SAMPLE_RATIO,
    Occasion,
    SampleAttendance,
    SessionMode,
    TeacherReport,
    VenueObservation,
    assess_session,
    pseudonym,
)
from pe_domain.ledger import EventLedger
from pe_domain.models import (
    ActivityKind,
    InjuryAdaptation,
    PlanSlot,
    SemesterPlan,
    SkillGoal,
    Teacher,
    Venue,
    VenueType,
    WeatherAlternative,
)
from pe_domain.plans import PlanRegistry, validate_plan
from pe_domain.review import AnomalyKind, ReviewBoard, ReviewStatus, detect_anomalies
from pe_domain.visibility import (
    AccessDenied,
    IdentityVault,
    build_parent_view,
    build_public_summary,
)

# ---------------------------------------------------------------- 夹具

FIELD = Venue("V-FIELD", "室外操场", VenueType.OUTDOOR, 50)
FIELD2 = Venue("V-FIELD-2", "第二操场", VenueType.OUTDOOR, 50)
FIELD3 = Venue("V-FIELD-3", "西操场", VenueType.OUTDOOR, 50)
GYM = Venue("V-GYM", "体育馆", VenueType.INDOOR, 45)
ROOM = Venue("V-ROOM", "形体房", VenueType.INDOOR, 20)
VENUES = {v.venue_id: v for v in (FIELD, FIELD2, FIELD3, GYM, ROOM)}
CLASS_VENUE = {"C1": "V-FIELD", "C2": "V-FIELD-2", "C3": "V-FIELD-3"}

T_WANG = Teacher("T-WANG", "王老师", frozenset({"BB", "急救"}))
T_CHEN = Teacher("T-CHEN", "陈老师", frozenset({"BB", "急救"}))
T_ZHAO = Teacher("T-ZHAO", "赵老师", frozenset({"BB", "急救"}))
T_LI = Teacher("T-LI", "李老师", frozenset({"田径"}))
TEACHERS = {t.teacher_id: t for t in (T_WANG, T_CHEN, T_ZHAO, T_LI)}
CLASS_TEACHER = {"C1": "T-WANG", "C2": "T-CHEN", "C3": "T-ZHAO"}

RAIN_GYM = WeatherAlternative("rain", "V-GYM", "室内球性练习", "POL-RAIN-01")
HEAT_GYM = WeatherAlternative("heat", "V-GYM", "室内低强度活动", "POL-HEAT-01")

GOAL_BB = SkillGoal("BB", "篮球", teach_weeks=(1, 2), practice_weeks=(1, 2, 3, 4), match_weeks=(3, 4))
HEADCOUNT = 40


def pe_slot(slot_id, class_id, weekday, teacher=T_WANG, skill="BB", venue="V-FIELD", alts=(RAIN_GYM, HEAT_GYM)):
    return PlanSlot(
        slot_id=slot_id, class_id=class_id, weekday=weekday,
        kind=ActivityKind.PE_CLASS, week_parity="all",
        venue_id=venue, teacher_id=teacher.teacher_id, skill_code=skill,
        headcount=HEADCOUNT, weather_alternatives=tuple(alts),
    )


def five_pe_slots(class_id):
    return [pe_slot(f"{class_id}-PE-{d}", class_id, d,
                    teacher=TEACHERS[CLASS_TEACHER[class_id]],
                    venue=CLASS_VENUE[class_id])
            for d in range(1, 6)]


def make_plan(class_ids=("C1",), version=0, status="draft"):
    slots = [s for cid in class_ids for s in five_pe_slots(cid)]
    return SemesterPlan(
        school_id="S", semester="2026-1", version=version,
        slots=tuple(slots), skill_goals=(GOAL_BB,), status=status,
    )


def att(token, occ, when, source="online", received=None):
    return SampleAttendance(occ, token, when, received or when, source)


class FixtureTest(unittest.TestCase):
    def test_fixtures_satisfy_validation(self):
        issues = validate_plan(make_plan(["C1", "C2", "C3"]), VENUES, TEACHERS)
        self.assertEqual(issues, [])


# ---------------------------------------------------------------- 方案校验与版本

class PlanValidationTest(unittest.TestCase):
    def test_daily_pe_shortfall(self):
        plan = make_plan()
        # 删掉周五的课 -> 每周仅 4 天
        slots = tuple(s for s in plan.slots if s.slot_id != "C1-PE-5")
        issues = validate_plan(SemesterPlan("S", "2026-1", 0, slots, (GOAL_BB,)), VENUES, TEACHERS)
        self.assertTrue(any(i.code == "daily_pe_shortfall" for i in issues))

    def test_capacity_and_qualification(self):
        bad_slot = pe_slot("X", "C1", 1, teacher=T_WANG, venue="V-ROOM")
        other_slots = [s for s in five_pe_slots("C1") if s.weekday != 1]
        plan = SemesterPlan("S", "2026-1", 0, tuple(other_slots + [bad_slot]), (GOAL_BB,))
        issues = validate_plan(plan, VENUES, TEACHERS)
        codes = {i.code for i in issues}
        self.assertIn("capacity_exceeded", codes)  # 40 > 形体房 20

        unqual = pe_slot("Y", "C1", 1, teacher=T_LI)
        plan2 = SemesterPlan("S", "2026-1", 0,
                             tuple(s for s in five_pe_slots("C1") if s.weekday != 1) + (unqual,),
                             (GOAL_BB,))
        codes2 = {i.code for i in validate_plan(plan2, VENUES, TEACHERS)}
        self.assertIn("qualification_mismatch", codes2)

    def test_venue_conflict_detected(self):
        # 两班同天都用操场
        s1 = pe_slot("C1-PE-1", "C1", 1)
        s2 = pe_slot("C2-PE-1", "C2", 1)
        plan = SemesterPlan("S", "2026-1", 0, (s1, s2), (GOAL_BB,))
        issues = validate_plan(plan, VENUES, TEACHERS)
        self.assertTrue(any(i.code == "venue_conflict" for i in issues))

    def test_outdoor_slot_requires_weather_alternatives(self):
        no_alt = pe_slot("C1-PE-1", "C1", 1, alts=())
        rest = [s for s in five_pe_slots("C1") if s.weekday != 1]
        plan = SemesterPlan("S", "2026-1", 0, tuple(rest + [no_alt]), (GOAL_BB,))
        codes = {i.code for i in validate_plan(plan, VENUES, TEACHERS)}
        self.assertIn("weather_alt_missing", codes)

    def test_invalid_plan_cannot_submit(self):
        registry = PlanRegistry()
        slots = tuple(s for s in make_plan().slots if s.weekday != 5)
        bad = SemesterPlan("S", "2026-1", 0, slots, (GOAL_BB,))
        with self.assertRaises(ValueError):
            registry.submit(bad, VENUES, TEACHERS, submitted_by="admin")

    def test_versions_are_immutable_and_superseded(self):
        registry = PlanRegistry()
        v1 = registry.submit(make_plan(status="draft"), VENUES, TEACHERS, submitted_by="admin")
        self.assertEqual(v1.version, 1)
        self.assertEqual(v1.status, "submitted")
        approved1 = registry.approve("S", "2026-1", 1)
        self.assertEqual(approved1.status, "approved")

        v2 = registry.submit(make_plan(status="draft"), VENUES, TEACHERS, submitted_by="admin")
        registry.approve("S", "2026-1", v2.version)
        self.assertEqual(registry.get("S", "2026-1", 1).status, "superseded")
               # 历史完整保留，可对照阴阳课表
        self.assertEqual(len(registry.history("S", "2026-1")), 2)
        # 冻结方案不可直接改字段
        with self.assertRaises(Exception):
            approved1.status = "draft"


# ---------------------------------------------------------------- 三方确认与时长

class ConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.occ = Occasion("C1-PE-1", 1)
        self.when = "2026-09-07T10:00:00"

    def _report(self, mode=SessionMode.NORMAL, minutes=40, skill="BB",
                venue_id="V-FIELD", **kw):
        return TeacherReport(
            self.occ, "T-WANG", ActivityKind.PE_CLASS, skill, minutes, mode,
            venue_id, **kw,
        )

    def test_three_party_confirmation(self):
        tokens = [f"tok{i}" for i in range(4)]
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [att(t, self.occ, self.when) for t in tokens],
            HEADCOUNT, {}, {},
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(result.effective_minutes, 40)
        self.assertEqual(set(result.per_student_minutes), set(tokens))

    def test_missing_venue_observation_is_not_confirmed(self):
        result = assess_session(
            self._report(), None,
            [att(f"t{i}", self.occ, self.when) for i in range(4)],
            HEADCOUNT, {}, {},
        )
        self.assertFalse(result.confirmed)
        self.assertEqual(result.effective_minutes, 0)
        self.assertTrue(any("场地" in r for r in result.reasons))

    def test_sample_below_minimum_not_confirmed(self):
        required = max(3, int(HEADCOUNT * MIN_SAMPLE_RATIO + 0.999))
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [att(f"t{i}", self.occ, self.when) for i in range(required - 1)],
            HEADCOUNT, {}, {},
        )
        self.assertFalse(result.confirmed)
        self.assertTrue(any("抽样" in r for r in result.reasons))

    def test_duplicate_signin_does_not_inflate_minutes_or_count(self):
        t = "tokA"
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [
                att(t, self.occ, self.when),
                att(t, self.occ, self.when),  # 同场次重复签到
                att("tokB", self.occ, self.when),
                att("tokC", self.occ, self.when),
                att("tokD", self.occ, self.when),
            ],
            HEADCOUNT, {}, {},
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(len(result.per_student_minutes), 4)  # 去重后 4 人
        self.assertTrue(any(f.startswith(f"duplicate_signin:{t}") for f in result.flags))

    def test_offline_late_signin_is_logged_but_not_counted(self):
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [
                att("late", self.occ, self.when, source="offline",
                    received="2026-09-09T11:00:00"),  # 49 小时后
                att("ok1", self.occ, self.when),
                att("ok2", self.occ, self.when),
                att("ok3", self.occ, self.when),
            ],
            HEADCOUNT, {}, {},
        )
        self.assertFalse(result.confirmed)  # 有效样本只剩 3
        self.assertIn("offline_late:late", result.flags)

    def test_offline_signin_within_window_counts(self):
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [
                att("off", self.occ, self.when, source="offline",
                    received="2026-09-08T15:00:00"),  # 29 小时后
                att("ok1", self.occ, self.when),
                att("ok2", self.occ, self.when),
                att("ok3", self.occ, self.when),
            ],
            HEADCOUNT, {}, {},
        )
        self.assertTrue(result.confirmed)

    def test_reported_minutes_capped_to_standard(self):
        result = assess_session(
            self._report(minutes=120),
            VenueObservation(self.occ, "V-FIELD", 40),
            [att(f"t{i}", self.occ, self.when) for i in range(4)],
            HEADCOUNT, {}, {},
        )
        self.assertEqual(result.effective_minutes, 40)
        self.assertTrue(any("minutes_capped" in f for f in result.flags))

    def test_rain_alternative_requires_basis_and_counts(self):
        no_basis = assess_session(
            self._report(mode=SessionMode.RAIN_ALT, venue_id="V-GYM"),
            VenueObservation(self.occ, "V-GYM", 40),
            [att(f"t{i}", self.occ, self.when) for i in range(4)],
            HEADCOUNT, {}, {},
        )
        self.assertFalse(no_basis.confirmed)

        ok = assess_session(
            self._report(mode=SessionMode.RAIN_ALT, venue_id="V-GYM",
                         basis_ref="POL-RAIN-01"),
            VenueObservation(self.occ, "V-GYM", 40),
            [att(f"t{i}", self.occ, self.when) for i in range(4)],
            HEADCOUNT, {}, {},
        )
        self.assertTrue(ok.confirmed)
        self.assertEqual(ok.mode, SessionMode.RAIN_ALT)

    def test_injury_adaptation_reduces_only_that_student(self):
        adaptation = InjuryAdaptation("student-7", "BB", adjusted_minutes=10,
                                      basis_ref="MED-2026-07", note="踝伤")
        vault_map = {"tok7": "student-7"}
        result = assess_session(
            self._report(),
            VenueObservation(self.occ, "V-FIELD", 40),
            [att("tok7", self.occ, self.when)] +
            [att(f"t{i}", self.occ, self.when) for i in range(3)],
            HEADCOUNT, {"student-7": adaptation}, vault_map,
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(result.per_student_minutes["tok7"], 10)
        self.assertEqual(result.per_student_minutes["t0"], 40)

    def test_free_play_and_exam_drill_flagged(self):
        for mode, flag in ((SessionMode.FREE, "whole_free_play"),
                           (SessionMode.EXAM_DRILL, "exam_drill_only")):
            result = assess_session(
                self._report(mode=mode),
                VenueObservation(self.occ, "V-FIELD", 40),
                [att(f"t{i}", self.occ, self.when) for i in range(4)],
                HEADCOUNT, {}, {},
            )
            self.assertTrue(result.confirmed)
            self.assertIn(flag, result.flags)

    def test_makeup_must_link_original_occasion(self):
        result = assess_session(
            self._report(mode=SessionMode.MAKEUP),
            VenueObservation(self.occ, "V-FIELD", 40),
            [att(f"t{i}", self.occ, self.when) for i in range(4)],
            HEADCOUNT, {}, {},
        )
        self.assertFalse(result.confirmed)
        self.assertTrue(any("补课" in r for r in result.reasons))


# ---------------------------------------------------------------- 账本还原

class LedgerRebuildTest(unittest.TestCase):
    def setUp(self):
        self.ledger = EventLedger()
        self.slots = five_pe_slots("C1")
        self.when = lambda week, day=1: f"2026-09-{day + (week - 1) * 7:02d}T10:00:00"

    def _record_normal(self, slot_id, week, tokens=4, skill="BB"):
        occ = Occasion(slot_id, week)
        self.ledger.append("teacher_report", TeacherReport(
            occ, "T-WANG", ActivityKind.PE_CLASS, skill, 40,
            SessionMode.NORMAL, "V-FIELD"))
        self.ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
        for i in range(tokens):
            self.ledger.append("sample_attendance", att(f"tok{i}", occ, self.when(week)))
        return occ

    def _rebuild(self, current_week=4):
        return self.ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=current_week,
        )

    def test_normal_weeks_complete(self):
        for w in range(1, 4):
            for slot in self.slots:
                self._record_normal(slot.slot_id, w)
        rebuilt = self._rebuild(current_week=3)
        self.assertEqual(
            sum(1 for st in rebuilt["states"].values() if st == "completed"),
            15,
        )
        self.assertEqual(rebuilt["missing_occasions"], ())

    def test_conflict_takeover_offline_chain_is_reconstructable(self):
        # 第 1 周正常
        self._record_normal("C1-PE-1", 1)
        # 第 1 周周二：场地被他班占用 -> 数学课临时占课 -> 事后离线补签也救不回这节
        occ_tue = Occasion("C1-PE-2", 1)
        self.ledger.append("venue_conflict_reported",
                           {"occasion": occ_tue, "with_class": "C3"})
        self.ledger.append("takeover",
                           {"slot_id": "C1-PE-2", "week": 1, "subject": "数学",
                            "ref": ""})
        self.ledger.append("sample_attendance",
                           att("tok0", occ_tue, self.when(1, 2),
                               source="offline", received="2026-09-05T10:00:00"))
        rebuilt = self._rebuild(current_week=1)
        self.assertEqual(rebuilt["states"]["C1-PE-2#w1"], "taken_over")
        self.assertIn("C1-PE-2#w1", rebuilt["pending_makeup"])
        self.assertEqual(len(rebuilt["takeovers"]), 1)

        # 第 2 周安排补课并挂接原场次 -> 缺口被回填
        makeup_occ = Occasion("C1-PE-1", 2)
        self.ledger.append("makeup_plan", {"occasion": makeup_occ, "makeup_for": occ_tue})
        self.ledger.append("teacher_report", TeacherReport(
            makeup_occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.MAKEUP, "V-GYM", basis_ref="MK-001", makeup_for=occ_tue))
        self.ledger.append("venue_observation", VenueObservation(makeup_occ, "V-GYM", 40))
        for i in range(4):
            self.ledger.append("sample_attendance",
                               att(f"mk{i}", makeup_occ, "2026-09-14T10:00:00"))
        rebuilt2 = self._rebuild(current_week=2)
        self.assertEqual(rebuilt2["states"]["C1-PE-2#w1"], "completed")
        self.assertEqual(rebuilt2["makeup_schedule"]["C1-PE-2#w1"], "C1-PE-1#w2")
        self.assertIn("C1-PE-2#w1", rebuilt2["made_up"])

    def test_weather_trigger_without_alternative_is_pending(self):
        occ = Occasion("C1-PE-1", 1)
        self.ledger.append("weather_trigger",
                           {"occasion": occ, "condition": "rain", "policy_ref": "POL-RAIN-01"})
        rebuilt = self._rebuild(current_week=1)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "weather_pending")
        self.assertIn("C1-PE-1#w1", rebuilt["pending_makeup"])

    def test_compliant_reschedule_is_not_missing(self):
        occ = Occasion("C1-PE-1", 1)
        from pe_domain.events import ScheduleChange
        self.ledger.append("schedule_change", ScheduleChange(
            occ, new_weekday=3, new_venue_id="V-GYM",
            new_teacher_id="T-WANG", approver="principal", basis_ref="ADJ-09"))
        rebuilt = self._rebuild(current_week=1)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "rescheduled")
        self.assertIn("C1-PE-1#w1", rebuilt["rescheduled"])


# ---------------------------------------------------------------- 覆盖规则

class CoverageTest(unittest.TestCase):
    def _sessions(self, rebuilt):
        return rebuilt["sessions"]

    def test_full_delivery_has_no_gaps(self):
        ledger = EventLedger()
        slots = five_pe_slots("C1")
        goal = GOAL_BB
        # teach: 周1-2 体育课；practice: 周1-4 每天都有；match: 周3-4 加赛事
        for w in range(1, 5):
            for slot in slots:
                occ = Occasion(slot.slot_id, w)
                ledger.append("teacher_report", TeacherReport(
                    occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                    SessionMode.NORMAL, "V-FIELD"))
                ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
                for i in range(4):
                    ledger.append("sample_attendance", att(f"t{i}", occ, f"2026-09-0{w}T10:00:00"))
        rebuilt = ledger.rebuild_class("C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=4)
        coverage = compute_class_coverage("C1", rebuilt, (goal,))
        bb = coverage.skill_coverage[0]
        self.assertTrue(bb.taught)
        self.assertEqual(bb.practiced_ratio, 1.0)
        # 没有 CLASS_MATCH 事件，常赛为缺口
        self.assertEqual(bb.matched_ratio, 0.0)
        self.assertTrue(any("常赛缺口" in g for g in bb.gaps))

    def test_exam_drill_and_free_play_grant_no_skill_coverage(self):
        ledger = EventLedger()
        slots = five_pe_slots("C1")
        occ = Occasion("C1-PE-1", 1)
        ledger.append("teacher_report", TeacherReport(
            occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.EXAM_DRILL, "V-FIELD"))
        ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
        for i in range(4):
            ledger.append("sample_attendance", att(f"t{i}", occ, "2026-09-07T10:00:00"))
        rebuilt = ledger.rebuild_class("C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=1)
        coverage = compute_class_coverage("C1", rebuilt, (GOAL_BB,))
        bb = coverage.skill_coverage[0]
        self.assertFalse(bb.taught)

    def test_makeup_backfills_teach_week(self):
        ledger = EventLedger()
        slots = five_pe_slots("C1")
        missing = Occasion("C1-PE-1", 1)
        makeup = Occasion("C1-PE-1", 3)
        ledger.append("makeup_plan", {"occasion": makeup, "makeup_for": missing})
        ledger.append("teacher_report", TeacherReport(
            makeup, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.MAKEUP, "V-GYM", basis_ref="MK-01", makeup_for=missing))
        ledger.append("venue_observation", VenueObservation(makeup, "V-GYM", 40))
        for i in range(4):
            ledger.append("sample_attendance", att(f"t{i}", makeup, "2026-09-21T10:00:00"))
        rebuilt = ledger.rebuild_class("C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=3)
        coverage = compute_class_coverage("C1", rebuilt, (GOAL_BB,))
        bb = coverage.skill_coverage[0]
        self.assertTrue(bb.taught)  # 补课按第 1 周归类，教会目标兑现


# ---------------------------------------------------------------- 异常与复核

class ReviewTest(unittest.TestCase):
    def test_yin_yang_and_patterns_open_review_not_penalty(self):
        ledger = EventLedger()
        slots = five_pe_slots("C1")
        # 周一二：只练考试；周三四：整节自由活动；周五：完全无事件（阴阳课表）
        for day, mode in ((1, SessionMode.EXAM_DRILL), (2, SessionMode.EXAM_DRILL),
                          (3, SessionMode.FREE), (4, SessionMode.FREE)):
            occ = Occasion(f"C1-PE-{day}", 1)
            ledger.append("teacher_report", TeacherReport(
                occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40, mode, "V-FIELD"))
            ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
            for i in range(4):
                ledger.append("sample_attendance", att(f"t{i}", occ, "2026-09-07T10:00:00"))
        rebuilt = ledger.rebuild_class("C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=1)
        anomalies = detect_anomalies("C1", rebuilt)
        kinds = {a.kind for a in anomalies}
        self.assertIn(AnomalyKind.YIN_YANG, kinds)      # 周四、五无事件
        self.assertIn(AnomalyKind.EXAM_DRILL, kinds)   # 连续应考
        self.assertIn(AnomalyKind.FREE_PLAY, kinds)

        board = ReviewBoard()
        case = board.open_case("C1", anomalies)
        self.assertEqual(board.open_classes(), ("C1",))
        self.assertEqual(case.status, ReviewStatus.OPEN)
        # 教研复核：确认占课有据 -> 只安排补课，不处罚
        board.resolve(case.case_id, ReviewStatus.MAKEUP_ORDERED, "教研员刘",
                      "两日考试项目训练属实，安排第 3 周补技能课")
        self.assertEqual(board.get(case.case_id).status, ReviewStatus.MAKEUP_ORDERED)
        with self.assertRaises(ValueError):
            board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员刘", "不能二次裁定")

    def test_takeover_chain_detected(self):
        ledger = EventLedger()
        slots = five_pe_slots("C1")
        for day in (1, 2):
            ledger.append("takeover",
                          {"slot_id": f"C1-PE-{day}", "week": 1,
                           "subject": "数学", "ref": ""})
        rebuilt = ledger.rebuild_class("C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=1)
        kinds = {a.kind for a in detect_anomalies("C1", rebuilt)}
        self.assertIn(AnomalyKind.TAKEOVER, kinds)


# ---------------------------------------------------------------- 可见性

class VisibilityTest(unittest.TestCase):
    def test_pseudonym_salt_breaks_cross_semester_link(self):
        a = pseudonym("stu-1", "salt-2026-1")
        b = pseudonym("stu-1", "salt-2026-1")
        c = pseudonym("stu-1", "salt-2026-2")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_parent_can_only_read_own_child(self):
        vault = IdentityVault("salt")
        vault.enroll("stu-1")
        vault.enroll("stu-2")
        vault.link_parent("parent-1-cred", "stu-1")
        with self.assertRaises(AccessDenied):
            build_parent_view("forged-cred", vault, {}, [])
        view = build_parent_view("parent-1-cred", vault,
                                 {"stu-1": (InjuryAdaptation("stu-1", "BB", 10, "MED-1"),)}, [])
        self.assertEqual(view.student_label, "本人子女")
        self.assertEqual(len(view.adaptations), 1)

    def _class_coverage(self, cid, total, completed, pending):
        from pe_domain.coverage import ClassCoverage, SkillCoverage
        return ClassCoverage(
            class_id=cid, total_occasions=total, completed=completed,
            pending_makeup=pending, in_review=0,
            skill_coverage=(SkillCoverage("BB", True, 1.0, 1.0, (), ()),),
        )

    def test_public_summary_suppressed_below_three_classes(self):
        coverages = {"C1": self._class_coverage("C1", 20, 18, 2),
                     "C2": self._class_coverage("C2", 20, 20, 0)}
        summary = build_public_summary("S", coverages, {})
        self.assertFalse(summary.published)

    def test_public_summary_aggregates_and_excludes_open_review(self):
        coverages = {f"C{i}": self._class_coverage(f"C{i}", 20, 18, 2) for i in range(1, 4)}
        board = ReviewBoard()
        case = board.open_case("C3", [])
        # C3 在复核中 -> 整班排除，只剩 2 个班 -> 抑制
        summary = build_public_summary("S", coverages, {"C3": board.get(case.case_id)})
        self.assertFalse(summary.published)

        board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员", "天气替代有据")
        coverages2 = dict(coverages)
        coverages2["C4"] = self._class_coverage("C4", 20, 18, 2)
        cases = {"C3": board.get(case.case_id)}
        summary = build_public_summary("S", coverages2, cases)
        self.assertTrue(summary.published)
        self.assertEqual(summary.classes_counted, 4)
        self.assertAlmostEqual(summary.occasions_completed_ratio, 0.9)


# ---------------------------------------------------------------- 补课口径：唯一分母与履约证据

class MakeupAccountingTest(unittest.TestCase):
    """计划场次是唯一分母；补课只是原场次的履约证据；一次原场次一条有效结果。"""

    def setUp(self):
        self.slots = five_pe_slots("C1")
        self.original = Occasion("C1-PE-1", 1)
        self.host_a = Occasion("C1-PE-1", 2)
        self.host_b = Occasion("C1-PE-1", 3)

    def _fill_all_normal(self, ledger, current_week, *, skip=()):
        """除 skip 的场次外，1..current_week 全部正常交付（已记录的场次不重复追加）。"""
        recorded = {
            e.payload.occasion for e in ledger.entries("teacher_report")
        }
        for w in range(1, current_week + 1):
            for slot in self.slots:
                occ = Occasion(slot.slot_id, w)
                if occ in skip or occ in recorded:
                    continue
                ledger.append("teacher_report", TeacherReport(
                    occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                    SessionMode.NORMAL, "V-FIELD"))
                ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
                for i in range(4):
                    ledger.append("sample_attendance",
                                  att(f"t{w}-{slot.slot_id}-{i}", occ, f"2026-09-0{w}T10:00:00"))

    def _makeup_session(self, ledger, host, *, tokens=4, when="2026-09-14T10:00:00"):
        ledger.append("teacher_report", TeacherReport(
            host, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.MAKEUP, "V-GYM", basis_ref="MK-01",
            makeup_for=self.original))
        ledger.append("venue_observation", VenueObservation(host, "V-GYM", 40))
        for i in range(tokens):
            ledger.append("sample_attendance",
                          att(f"mk-{host.week}-{i}", host, when))

    def _denominator_invariants(self, rebuilt, coverage):
        states = rebuilt["states"]
        # 分母只含计划场次：5 slot × 3 周 = 15，补课场次不另立行
        self.assertEqual(coverage.total_occasions, 15)
        self.assertEqual(len(states), 15)
        # 状态互斥且穷尽：完成 + 待补 + 在复核 + 改期 + 取消 == 总场次
        buckets = (
            coverage.completed + coverage.pending_makeup + coverage.in_review
            + sum(1 for st in states.values() if st == "rescheduled")
            + sum(1 for st in states.values() if st == "cancelled")
        )
        self.assertEqual(buckets, 15)
        # 证据只指向“补课后完成”的原场次，且引用的补课会话真实存在
        self.assertEqual(set(rebuilt["completion_evidence"]), rebuilt["made_up"])
        confirmed_makeup_sessions = {
            s.occasion.key() for s in rebuilt["sessions"]
            if s.confirmed and s.mode == SessionMode.MAKEUP
        }
        for okey, mkey in rebuilt["completion_evidence"].items():
            self.assertEqual(states[okey], "completed")
            self.assertIn(mkey, confirmed_makeup_sessions)

    def test_two_plans_two_confirmed_makeups_only_one_counts(self):
        ledger = EventLedger()
        # 原场次被占课缺课
        ledger.append("takeover", {"slot_id": "C1-PE-1", "week": 1,
                                   "subject": "数学", "ref": ""})
        # 先后两个补课计划（A 在第 2 周，B 在第 3 周），两场补课都实际完成
        ledger.append("makeup_plan", {"occasion": self.host_a, "makeup_for": self.original})
        self._makeup_session(ledger, self.host_a, when="2026-09-14T10:00:00")
        ledger.append("makeup_plan", {"occasion": self.host_b, "makeup_for": self.original})
        self._makeup_session(ledger, self.host_b, when="2026-09-21T10:00:00")
        self._fill_all_normal(ledger, 3, skip=(self.original, self.host_a, self.host_b))

        rebuilt = ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=3)
        coverage = compute_class_coverage("C1", rebuilt, (GOAL_BB,))

        # 原场次由最后的生效安排（B）兑现，只完成一次；旧安排留档 superseded
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "completed")
        self.assertEqual(rebuilt["completion_evidence"]["C1-PE-1#w1"], "C1-PE-1#w3")
        self.assertEqual(rebuilt["makeup_schedule"]["C1-PE-1#w1"], "C1-PE-1#w3")
        self.assertIn(("C1-PE-1#w1", "C1-PE-1#w2"),
                      rebuilt["makeup_plans_superseded"])
        records = {(r.original_key, r.makeup_key): r for r in rebuilt["makeup_records"]}
        self.assertTrue(records[("C1-PE-1#w1", "C1-PE-1#w3")].fulfilled)
        self.assertTrue(records[("C1-PE-1#w1", "C1-PE-1#w3")].effective)
        self.assertFalse(records[("C1-PE-1#w1", "C1-PE-1#w2")].fulfilled)
        self.assertFalse(records[("C1-PE-1#w1", "C1-PE-1#w2")].effective)
        # 待补为 0，分母不被补课场次放大
        self.assertEqual(coverage.pending_makeup, 0)
        self._denominator_invariants(rebuilt, coverage)

        # 技能覆盖：第 1 周教会目标只被生效补课（B）归入一次，A 不产生覆盖
        bb = coverage.skill_coverage[0]
        self.assertTrue(bb.taught)
        self.assertIn("teach:C1-PE-1#w3", bb.evidence)
        self.assertNotIn("teach:C1-PE-1#w2", bb.evidence)
        teach_evidence = [e for e in bb.evidence if e.startswith("teach:")]
        self.assertEqual(len(teach_evidence), len(set(teach_evidence)))

    def test_failed_makeup_keeps_original_pending_then_replan_fulfills(self):
        ledger = EventLedger()
        ledger.append("takeover", {"slot_id": "C1-PE-1", "week": 1,
                                   "subject": "数学", "ref": ""})
        # 第一版补课：三方确认未过（抽样仅 2 人）
        ledger.append("makeup_plan", {"occasion": self.host_a, "makeup_for": self.original})
        self._makeup_session(ledger, self.host_a, tokens=2)
        self._fill_all_normal(ledger, 2, skip=(self.original, self.host_a))

        rebuilt = ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=2)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "makeup_unconfirmed")
        self.assertIn("C1-PE-1#w1", rebuilt["pending_makeup"])
        self.assertEqual(rebuilt["states"]["C1-PE-1#w2"], "in_review")
        self.assertNotIn("C1-PE-1#w1", rebuilt["made_up"])
        cov1 = compute_class_coverage("C1", rebuilt, (GOAL_BB,))
        self.assertEqual(cov1.completion_evidence, {})
        # 失败的补课会话不产生任何技能覆盖证据
        self.assertFalse(
            any(e.endswith("C1-PE-1#w2") for e in cov1.skill_coverage[0].evidence))

        # 失败后重排：第 3 周新计划并确认通过 -> 原场次回填完成
        ledger.append("makeup_plan", {"occasion": self.host_b, "makeup_for": self.original})
        self._makeup_session(ledger, self.host_b, when="2026-09-21T10:00:00")
        self._fill_all_normal(ledger, 3, skip=(self.original, self.host_a, self.host_b))
        rebuilt2 = ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=3)
        self.assertEqual(rebuilt2["states"]["C1-PE-1#w1"], "completed")
        self.assertEqual(rebuilt2["completion_evidence"]["C1-PE-1#w1"], "C1-PE-1#w3")
        self.assertNotIn("C1-PE-1#w1", rebuilt2["pending_makeup"])
        # 失败的补课场次保留在复核状态，不被抹掉
        self.assertEqual(rebuilt2["states"]["C1-PE-1#w2"], "in_review")
        cov2 = compute_class_coverage("C1", rebuilt2, (GOAL_BB,))
        self._denominator_invariants(rebuilt2, cov2)
        self.assertTrue(cov2.skill_coverage[0].taught)
        self.assertIn("teach:C1-PE-1#w3", cov2.skill_coverage[0].evidence)
        self.assertNotIn("teach:C1-PE-1#w2", cov2.skill_coverage[0].evidence)

    def test_cancellation_reschedule_takeover_weather_keep_distinct_states(self):
        ledger = EventLedger()
        from pe_domain.events import ScheduleChange
        ledger.append("cancellation", {"occasion": Occasion("C1-PE-1", 1),
                                       "reason": "考务统一安排", "ref": "ADM-1"})
        ledger.append("schedule_change", ScheduleChange(
            Occasion("C1-PE-2", 1), new_weekday=3, new_venue_id="V-GYM",
            new_teacher_id="T-WANG", approver="principal", basis_ref="ADJ-09"))
        ledger.append("takeover", {"slot_id": "C1-PE-3", "week": 1,
                                   "subject": "数学", "ref": ""})
        ledger.append("weather_trigger",
                       {"occasion": Occasion("C1-PE-4", 1),
                        "condition": "rain", "policy_ref": "POL-RAIN-01"})
        self._fill_all_normal(ledger, 1, skip=tuple(
            Occasion(f"C1-PE-{d}", 1) for d in (1, 2, 3, 4)))
        rebuilt = ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=1)
        states = rebuilt["states"]
        self.assertEqual(states["C1-PE-1#w1"], "cancelled")
        self.assertEqual(states["C1-PE-2#w1"], "rescheduled")
        self.assertEqual(states["C1-PE-3#w1"], "taken_over")
        self.assertEqual(states["C1-PE-4#w1"], "weather_pending")
        # 取消不构成待补缺口；占课/天气待替代才待补
        pending = set(rebuilt["pending_makeup"])
        self.assertNotIn("C1-PE-1#w1", pending)
        self.assertNotIn("C1-PE-2#w1", pending)
        self.assertIn("C1-PE-3#w1", pending)
        self.assertIn("C1-PE-4#w1", pending)

    def test_already_delivered_occasion_is_not_overwritten_by_makeup(self):
        ledger = EventLedger()
        self._fill_all_normal(ledger, 3)  # 原场次第 1 周已正常完成
        ledger.append("makeup_plan", {"occasion": self.host_a, "makeup_for": self.original})
        self._makeup_session(ledger, self.host_a)
        rebuilt = ledger.rebuild_class(
            "C1", self.slots, HEADCOUNT, {}, {}, VENUES, current_week=3)
        # 原场次维持自行完成，补课证据不回填、不重复计完成
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "completed")
        self.assertNotIn("C1-PE-1#w1", rebuilt["completion_evidence"])
        self.assertNotIn("C1-PE-1#w1", rebuilt["made_up"])


# ---------------------------------------------------------------- 乱序与迟到事件重放

class ReplayDeterminismTest(unittest.TestCase):
    """乱序追加、迟到补签、计划晚于会话到达，重放结论唯一确定。"""

    def _events_scenario(self):
        slots = five_pe_slots("C1")
        original = Occasion("C1-PE-2", 1)
        host = Occasion("C1-PE-1", 2)

        def make_events():
            evs = []
            evs.append(("takeover", {"slot_id": "C1-PE-2", "week": 1,
                                     "subject": "数学", "ref": ""}))
            # 补课会话三方事件
            evs.append(("teacher_report", TeacherReport(
                host, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                SessionMode.MAKEUP, "V-GYM", basis_ref="MK-01",
                makeup_for=original)))
            evs.append(("venue_observation", VenueObservation(host, "V-GYM", 40)))
            for i in range(4):
                evs.append(("sample_attendance",
                            att(f"mk{i}", host, "2026-09-14T10:00:00")))
            # 一条超时离线补签（49 小时），无论相对在线签到先后都只留痕
            evs.append(("sample_attendance",
                        att("late", host, "2026-09-14T10:00:00",
                            source="offline", received="2026-09-16T11:00:00")))
            # 补课计划晚于会话事件才入账
            evs.append(("makeup_plan", {"occasion": host, "makeup_for": original}))
            # 其余计划场次正常交付
            for w in range(1, 3):
                for slot in slots:
                    occ = Occasion(slot.slot_id, w)
                    if occ in (original, host):
                        continue
                    evs.append(("teacher_report", TeacherReport(
                        occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                        SessionMode.NORMAL, "V-FIELD")))
                    evs.append(("venue_observation", VenueObservation(occ, "V-FIELD", 40)))
                    for i in range(4):
                        evs.append(("sample_attendance",
                                    att(f"t{w}-{i}", occ, f"2026-09-0{w + 6}T10:00:00")))
            return evs
        return slots, original, host, make_events

    def _fingerprint(self, ledger, slots):
        rebuilt = ledger.rebuild_class(
            "C1", slots, HEADCOUNT, {}, {}, VENUES, current_week=2)
        coverage = compute_class_coverage("C1", rebuilt, (GOAL_BB,))
        return (
            tuple(sorted(rebuilt["states"].items())),
            rebuilt["pending_makeup"],
            tuple(sorted(rebuilt["made_up"])),
            tuple(sorted(rebuilt["completion_evidence"].items())),
            tuple(sorted((s.occasion.key(), s.flags) for s in rebuilt["sessions"])),
            coverage.total_occasions,
            coverage.completed,
            coverage.pending_makeup,
            tuple((c.skill_code, c.taught, c.practiced_ratio, c.matched_ratio, c.evidence)
                  for c in coverage.skill_coverage),
        )

    def test_out_of_order_and_late_replay_is_deterministic(self):
        import random
        slots, original, host, make_events = self._events_scenario()

        fingerprints = set()
        for seed in range(8):
            ledger = EventLedger()
            evs = make_events()
            rng = random.Random(seed)
            rng.shuffle(evs)
            for et, p in evs:
                ledger.append(et, p)
            fingerprints.add(self._fingerprint(ledger, slots))
        self.assertEqual(len(fingerprints), 1)

        fp = next(iter(fingerprints))
        states_items, pending, made_up, evidence, flags, total, completed, pending_n, skills = fp
        states = dict(states_items)
        # 计划晚于会话到达：原场次仍被该补课兑现；超时补签不影响结论
        self.assertEqual(states["C1-PE-2#w1"], "completed")
        self.assertEqual(dict(evidence)["C1-PE-2#w1"], "C1-PE-1#w2")
        self.assertEqual(pending, ())
        self.assertEqual(total, 10)  # 5 slot × 2 周
        self.assertEqual(completed, 10)
        self.assertTrue(any("offline_late:late" in fl for _, fl in flags))


# ---------------------------------------------------------------- 公众汇总一致性

class PublicSummaryConsistencyTest(unittest.TestCase):
    def _three_class_coverages(self):
        """用真实账本构造 3 个班，每班 1 周 5 场：3 完成 / 1 占课待补 / 1 天气待补。"""
        coverages = {}
        evidence_total = 0
        for cid, teacher, venue in (
            ("C1", T_WANG, "V-FIELD"),
            ("C2", T_CHEN, "V-FIELD-2"),
            ("C3", T_ZHAO, "V-FIELD-3"),
        ):
            ledger = EventLedger()
            slots = five_pe_slots(cid)
            for day in (1, 2, 3):
                occ = Occasion(f"{cid}-PE-{day}", 1)
                ledger.append("teacher_report", TeacherReport(
                    occ, teacher.teacher_id, ActivityKind.PE_CLASS, "BB", 40,
                    SessionMode.NORMAL, venue))
                ledger.append("venue_observation", VenueObservation(occ, venue, 40))
                for i in range(4):
                    ledger.append("sample_attendance", att(f"t{i}", occ, "2026-09-07T10:00:00"))
            ledger.append("takeover", {"slot_id": f"{cid}-PE-4", "week": 1,
                                       "subject": "数学", "ref": ""})
            ledger.append("weather_trigger",
                          {"occasion": Occasion(f"{cid}-PE-5", 1),
                           "condition": "rain", "policy_ref": "POL-RAIN-01"})
            rebuilt = ledger.rebuild_class(
                cid, slots, HEADCOUNT, {}, {}, VENUES, current_week=1)
            cov = compute_class_coverage(cid, rebuilt, (GOAL_BB,))
            # 每班口径自洽：5 = 完成 3 + 待补 2
            self.assertEqual((cov.total_occasions, cov.completed, cov.pending_makeup), (5, 3, 2))
            evidence_total += len(cov.completion_evidence)
            coverages[cid] = cov
        return coverages

    def test_public_totals_match_class_counts_and_evidence(self):
        coverages = self._three_class_coverages()
        summary = build_public_summary("S", coverages, {})
        self.assertTrue(summary.published)
        self.assertEqual(summary.classes_counted, 3)
        self.assertAlmostEqual(summary.occasions_completed_ratio, 9 / 15)
        # 待补共 6 场 >= 单元格抑制线 5，如实发布且与各班待补之和一致
        self.assertEqual(summary.pending_makeup, 6)

    def test_open_review_class_excluded_then_included_after_resolution(self):
        coverages = self._three_class_coverages()
        board = ReviewBoard()
        case = board.open_case("C3", [])
        # C3 复核未结案：整班排除，只剩 2 班 -> 抑制发布（不审不判）
        suppressed = build_public_summary("S", coverages, {"C3": board.get(case.case_id)})
        self.assertFalse(suppressed.published)
        self.assertEqual(suppressed.classes_counted, 2)

        board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员", "天气替代有据")
        summary = build_public_summary(
            "S", coverages, {"C3": board.get(case.case_id)})
        self.assertTrue(summary.published)
        self.assertEqual(summary.classes_counted, 3)
        self.assertAlmostEqual(summary.occasions_completed_ratio, 9 / 15)

    def test_makeup_completion_flows_into_public_ratio_once(self):
        # 一个班：原场次占课待补 -> 补课兑现；公众分母只计计划场次
        cid = "C1"
        slots = five_pe_slots(cid)
        ledger = EventLedger()
        original = Occasion("C1-PE-4", 1)
        host = Occasion("C1-PE-1", 2)
        ledger.append("takeover", {"slot_id": "C1-PE-4", "week": 1,
                                   "subject": "数学", "ref": ""})
        for day in (1, 2, 3, 5):
            occ = Occasion(f"C1-PE-{day}", 1)
            ledger.append("teacher_report", TeacherReport(
                occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                SessionMode.NORMAL, "V-FIELD"))
            ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
            for i in range(4):
                ledger.append("sample_attendance", att(f"t{i}", occ, "2026-09-07T10:00:00"))
        ledger.append("makeup_plan", {"occasion": host, "makeup_for": original})
        ledger.append("teacher_report", TeacherReport(
            host, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.MAKEUP, "V-GYM", basis_ref="MK-01", makeup_for=original))
        ledger.append("venue_observation", VenueObservation(host, "V-GYM", 40))
        for i in range(4):
            ledger.append("sample_attendance", att(f"mk{i}", host, "2026-09-14T10:00:00"))
        # 第 2 周其余 4 个计划场次正常交付（补课占用的是 PE-1 场次）
        for day in (2, 3, 4, 5):
            occ = Occasion(f"C1-PE-{day}", 2)
            ledger.append("teacher_report", TeacherReport(
                occ, "T-WANG", ActivityKind.PE_CLASS, "BB", 40,
                SessionMode.NORMAL, "V-FIELD"))
            ledger.append("venue_observation", VenueObservation(occ, "V-FIELD", 40))
            for i in range(4):
                ledger.append("sample_attendance", att(f"w2t{i}", occ, "2026-09-14T10:00:00"))
        rebuilt = ledger.rebuild_class(
            cid, slots, HEADCOUNT, {}, {}, VENUES, current_week=2)
        cov = compute_class_coverage(cid, rebuilt, (GOAL_BB,))
        # 10 个计划场次（补课场次不另立分母），10 场全部完成，0 待补
        self.assertEqual((cov.total_occasions, cov.completed, cov.pending_makeup), (10, 10, 0))
        self.assertEqual(cov.completion_evidence, {"C1-PE-4#w1": "C1-PE-1#w2"})


if __name__ == "__main__":
    unittest.main()
