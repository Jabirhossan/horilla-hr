"""The month hour account is the sum of that month, not a running delta."""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase

from attendance.methods.utils import format_time, strtime_seconds
from attendance.models import Attendance, AttendanceOverTime
from attendance.tests.fixtures import HolidayFixtureMixin


class TestFormatTime(SimpleTestCase):
    def test_negative_seconds_keep_their_real_hour(self):
        # -5340s is -1h29m. Floor division used to render this as -2:31.
        self.assertEqual(format_time(-5340), "-01:29")
        self.assertEqual(format_time(33480), "09:18")
        self.assertEqual(format_time(0), "00:00")
        self.assertEqual(strtime_seconds("-01:29"), -5340)
        self.assertEqual(strtime_seconds("09:18"), 33480)


class TestHourAccountRebuild(HolidayFixtureMixin, TestCase):
    def test_save_sums_the_month_instead_of_the_delta(self):
        """
        A row loaded without save() (fixture date shift, bulk_create) never
        entered the old delta counter. Editing it then subtracted the change
        from zero. The account has to be the capped month sum instead.
        """
        start = self.normal_date
        day = self.shift_day
        rows = [
            # Edited below: was 09:18, becomes 07:49 against an 08:30 minimum.
            ("09:18", 33480, "08:30", 0),
            # Already over the minimum: counts as 08:30, not 09:23.
            ("09:23", 33780, "08:30", 1),
            # bulk_create leaves at_work_second empty; the string still counts.
            ("08:00", None, "08:00", 2),
        ]
        created = Attendance.objects.bulk_create(
            [
                Attendance(
                    employee_id=self.emp_a,
                    attendance_date=start + timedelta(days=offset),
                    shift_id=self.shift,
                    attendance_day=day,
                    attendance_worked_hour=worked,
                    at_work_second=at_work,
                    minimum_hour=minimum,
                    attendance_validated=True,
                )
                for worked, at_work, minimum, offset in rows
            ]
        )

        edited = Attendance.objects.get(pk=created[0].pk)
        edited.attendance_worked_hour = "07:49"
        edited.save()

        account = AttendanceOverTime.objects.get(
            employee_id=self.emp_a,
            month=start.strftime("%B").lower(),
            year=start.year,
        )
        # 07:49 + 08:30 + 08:00 = 24:19. Pending is only the 07:49 shortfall.
        self.assertEqual(account.hour_account_second, 87540)
        self.assertEqual(account.hour_pending_second, 2460)
        self.assertEqual(account.worked_hours, "24:19")
        self.assertEqual(account.pending_hours, "00:41")
