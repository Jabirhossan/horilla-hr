"""
Field-level limits on PUT /employee/employees/<pk>/.

Who may edit whom was already enforced. What each caller could *write* was
not: every allowed caller went through ``EmployeeSerializer``, which is
``fields = "__all__"``. A plain employee updating their own phone number
could set ``is_active`` or ``badge_id`` in the same request, and the web UI's
"profile edit" switch was ignored entirely.

Note the manager direction these tests pin: the check reads "is the caller the
target's manager", so a manager may edit a report and not the other way round.
That direction is correct and has been misread as inverted more than once --
these tests exist partly so a future "fix" flips red.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from employee.models import ProfileEditFeature
from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user


def url(employee):
    return f"/api/v1/employee/employees/{employee.pk}/"


class EmployeeSelfEditTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        set_selected_company(None)
        self.company = make_company("SelfEdit Co")

        self.user = make_user("selfedit_user")
        self.employee = make_employee(
            company=self.company,
            email="selfedit@test.horilla",
            first_name="Sam",
            user=self.user,
        )
        # Self-service editing on, unless a test says otherwise.
        ProfileEditFeature.objects.create(is_enabled=True)
        self.client.force_authenticate(user=self.user)

    def test_employee_may_edit_an_allowlisted_field(self):
        response = self.client.put(
            url(self.employee), {"phone": "5551234567"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.phone, "5551234567")

    def test_employee_cannot_deactivate_themselves(self):
        """The regression. Previously is_active went straight through."""
        self.assertTrue(self.employee.is_active)
        self.client.put(
            url(self.employee),
            {"phone": "5550000000", "is_active": False},
            format="json",
        )
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active, "is_active must not be self-writable")

    def test_employee_cannot_rewrite_badge_id_or_name(self):
        original_badge = self.employee.badge_id
        self.client.put(
            url(self.employee),
            {
                "badge_id": "HACKED-1",
                "employee_first_name": "Renamed",
                "email": "elsewhere@test.horilla",
            },
            format="json",
        )
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.badge_id, original_badge)
        self.assertEqual(self.employee.employee_first_name, "Sam")
        self.assertEqual(self.employee.email, "selfedit@test.horilla")

    def test_profile_edit_toggle_is_honoured(self):
        ProfileEditFeature.objects.update(is_enabled=False)
        response = self.client.put(
            url(self.employee), {"phone": "5559999999"}, format="json"
        )
        self.assertEqual(response.status_code, 403, response.content)

    def test_hr_keeps_full_access(self):
        """The restriction must not cost HR the fields it legitimately edits."""
        hr_user = make_user("selfedit_hr")
        make_employee(
            company=self.company,
            email="hr@test.horilla",
            first_name="Hera",
            user=hr_user,
        )
        hr_user.user_permissions.add(Permission.objects.get(codename="change_employee"))

        hr_client = APIClient()
        hr_client.force_authenticate(user=hr_user)
        response = hr_client.put(
            url(self.employee), {"badge_id": "HR-SET-1"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.badge_id, "HR-SET-1")

    def test_manager_still_gets_the_full_serializer_on_a_report(self):
        """
        Pins a deliberate gap, so that closing it is a decision and not a
        surprise. This fix restricts *self*-edit only. A reporting manager
        keeps the full serializer over a subordinate -- including is_active --
        because manager-edits-report is an intended capability here (see
        test_manager_can_edit_subordinate in test_write_permissions.py).
        Narrowing it is defensible but is its own change.
        """
        report = make_employee(
            company=self.company,
            email="report@test.horilla",
            first_name="Rory",
        )
        report.employee_work_info.reporting_manager_id = self.employee
        report.employee_work_info.save()

        response = self.client.put(
            url(report), {"employee_first_name": "Rewritten"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        report.refresh_from_db()
        self.assertEqual(report.employee_first_name, "Rewritten")

    def test_subordinate_cannot_edit_their_manager(self):
        """Direction check: manager -> report is allowed, report -> manager is not."""
        boss_user = make_user("selfedit_boss")
        boss = make_employee(
            company=self.company,
            email="boss@test.horilla",
            first_name="Bea",
            user=boss_user,
        )
        self.employee.employee_work_info.reporting_manager_id = boss
        self.employee.employee_work_info.save()

        response = self.client.put(url(boss), {"phone": "5550001111"}, format="json")
        self.assertEqual(response.status_code, 400)
        boss.refresh_from_db()
        self.assertNotEqual(boss.phone, "5550001111")

    def test_missing_employee_is_404_not_500(self):
        self.assertEqual(
            self.client.put(url_for_missing(), {}, format="json").status_code, 404
        )


def url_for_missing():
    return "/api/v1/employee/employees/99999999/"
