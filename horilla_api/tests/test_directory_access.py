"""
The employee directory over the API tracks the web's accessibility setting.

employee.views.employee_view is guarded by
@enter_if_accessible(feature="employee_view", ...), so an admin governs the
directory through the accessibility app. EmployeeListAPIView ignored that and
allowed only perm holders, managers and self -- so someone who could browse
the directory on the web saw only themselves through the API, and the mobile
directory screen had no data source.

These also pin the payload: widening which rows are listed must not widen
which *fields* come back.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from accessibility.models import DefaultAccessibility
from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user

LIST_URL = "/api/v1/employee/list/employees/"


class DirectoryAccessTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        set_selected_company(None)
        self.company = make_company("Directory Co")

        self.user = make_user("dir_user")
        self.employee = make_employee(
            company=self.company,
            email="dir@test.horilla",
            first_name="Dana",
            user=self.user,
        )
        for i in range(3):
            make_employee(
                company=self.company,
                email=f"colleague{i}@test.horilla",
                first_name=f"Colleague{i}",
            )
        self.client.force_authenticate(user=self.user)

    def test_unconfigured_feature_grants_the_directory(self):
        """
        The web's existing default: with no DefaultAccessibility row,
        check_is_accessible returns True. Pinned so that changing it is a
        deliberate act rather than a silent change of who sees whom.
        """
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(response.data["count"], 1)

    def test_excluded_feature_falls_back_to_self_only(self):
        DefaultAccessibility.objects.create(
            feature="employee_view", is_enabled=True, exclude_all=True, filter="{}"
        )
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.employee.id)

    def test_perm_holder_is_unaffected_by_exclusion(self):
        DefaultAccessibility.objects.create(
            feature="employee_view", is_enabled=True, exclude_all=True, filter="{}"
        )
        self.user.user_permissions.add(Permission.objects.get(codename="view_employee"))
        # Re-authenticate so the permission cache is rebuilt.
        client = APIClient()
        client.force_authenticate(user=type(self.user).objects.get(pk=self.user.pk))
        response = client.get(LIST_URL)
        self.assertGreater(response.data["count"], 1)

    def test_manager_still_sees_subordinates_when_excluded(self):
        DefaultAccessibility.objects.create(
            feature="employee_view", is_enabled=True, exclude_all=True, filter="{}"
        )
        report = make_employee(
            company=self.company,
            email="dir_report@test.horilla",
            first_name="Rudi",
        )
        report.employee_work_info.reporting_manager_id = self.employee
        report.employee_work_info.save()

        response = self.client.get(LIST_URL)
        ids = {row["id"] for row in response.data["results"]}
        self.assertIn(report.id, ids)

    def test_payload_exposes_only_directory_fields(self):
        """Widening the rows must not widen the columns."""
        row = self.client.get(LIST_URL).data["results"][0]
        for leaked in ("dob", "address", "phone", "marital_status", "children"):
            self.assertNotIn(leaked, row, f"{leaked} must not appear in the directory")
