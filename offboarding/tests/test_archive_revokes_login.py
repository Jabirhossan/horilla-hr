"""
Offboarding someone must take their login away, not just their employee row.

Authentication reads ``HorillaUser.is_active``; ``Employee.is_active`` is a
separate flag, and ``Employee.sync_login_access()`` is what mirrors one onto
the other. Moving people into an "archived" offboarding stage did it with a
queryset ``update()``, which never calls ``save()`` and so never calls that --
leaving a leaver with working credentials on both the web and the API. The API
issues 30-day refresh tokens, so the window was up to a month after the last
day.

These drive the real view, not a reimplementation of it, so that reaching for
``update()`` again fails here.
"""

from django.test import TestCase
from django.urls import reverse

from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from offboarding.models import Offboarding, OffboardingEmployee, OffboardingStage


class ArchivedStageRevokesLoginTests(TestCase):
    def setUp(self):
        set_selected_company(None)
        self.company = make_company("Leaver Co")

        self.leaver_user = make_user("leaver_user")
        self.leaver = make_employee(
            company=self.company,
            email="leaver@test.horilla",
            first_name="Lee",
            user=self.leaver_user,
        )

        self.admin_user = make_user("offboard_admin", is_superuser=True)
        make_employee(
            company=self.company,
            email="offboard_admin@test.horilla",
            first_name="Ada",
            user=self.admin_user,
        )

        self.offboarding = Offboarding.objects.create(
            title="Q3 exits",
            description="leavers",
            company_id=self.company,
        )
        self.notice_stage = OffboardingStage.objects.create(
            title="Notice", type="notice_period", offboarding_id=self.offboarding
        )
        self.archived_stage = OffboardingStage.objects.create(
            title="Archived", type="archived", offboarding_id=self.offboarding
        )
        self.record = OffboardingEmployee.objects.create(
            employee_id=self.leaver, stage_id=self.notice_stage
        )

        self.client.force_login(self.admin_user)

    def _move_to(self, stage):
        return self.client.get(
            reverse("offboarding-change-stage"),
            {"employee_ids": [self.record.pk], "stage_id": stage.pk},
            HTTP_HX_REQUEST="true",
        )

    def test_moving_to_archived_disables_the_login_account(self):
        self.assertTrue(self.leaver_user.is_active)

        response = self._move_to(self.archived_stage)
        self.assertEqual(response.status_code, 200, response.content)

        self.leaver_user.refresh_from_db()
        self.assertFalse(
            self.leaver_user.is_active,
            "an offboarded employee's login account must be disabled -- "
            "authentication checks HorillaUser.is_active, not "
            "Employee.is_active, so leaving it True keeps their password and "
            "any live refresh token working after their last day",
        )

    def test_moving_to_archived_deactivates_the_employee_row(self):
        self._move_to(self.archived_stage)
        self.leaver.refresh_from_db()
        self.assertFalse(self.leaver.is_active)

    def test_a_non_archived_stage_leaves_access_alone(self):
        """Serving notice is not leaving; they still have to be able to work."""
        self._move_to(self.notice_stage)

        self.leaver.refresh_from_db()
        self.leaver_user.refresh_from_db()
        self.assertTrue(self.leaver.is_active)
        self.assertTrue(self.leaver_user.is_active)

    def test_moving_back_out_of_archived_restores_access(self):
        """
        The sync runs both ways.

        ``change_stage`` computes ``target_state`` from the stage type, so it
        also un-archives. If the login were only ever revoked and never
        restored, reinstating someone would silently leave them locked out.
        """
        self._move_to(self.archived_stage)
        self.leaver_user.refresh_from_db()
        self.assertFalse(self.leaver_user.is_active)

        self._move_to(self.notice_stage)

        self.leaver.refresh_from_db()
        self.leaver_user.refresh_from_db()
        self.assertTrue(self.leaver.is_active)
        self.assertTrue(
            self.leaver_user.is_active,
            "moving someone out of the archived stage must give their login "
            "back, not just their employee record",
        )
