"""
clock_in_out.py

This module is used register endpoints to the check-in check-out functionalities
"""

import ipaddress
import logging

from django.shortcuts import render

from horilla.http.response import HorillaRedirect

logger = logging.getLogger(__name__)
from datetime import date, datetime, timedelta

def attendance_window_flags(
    now_sec,
    check_in_window_start_sec,
    check_in_window_end_sec,
    check_out_window_start_sec,
    check_out_window_end_sec,
    is_night_shift=False,
):
    """Return whether a punch is outside the explicit biometric windows."""
    current_sec = now_sec
    if is_night_shift and current_sec < 12 * 60 * 60:
        current_sec += 24 * 60 * 60

    in_start = check_in_window_start_sec
    in_end = check_in_window_end_sec
    out_start = check_out_window_start_sec
    out_end = check_out_window_end_sec

    if is_night_shift:
        if in_end < in_start:
            in_end += 24 * 60 * 60
        if out_start < in_start:
            out_start += 24 * 60 * 60
        if out_end < in_start:
            out_end += 24 * 60 * 60

    check_in_absent = not (in_start <= current_sec <= in_end)
    check_out_absent = not (out_start <= current_sec <= out_end)
    return check_in_absent, check_out_absent


from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from attendance.methods.utils import (
    activity_datetime,
    employee_exists,
    format_time,
    overtime_calculation,
    shift_schedule_today,
    strtime_seconds,
)
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceGeneralSetting,
    AttendanceLateComeEarlyOut,
    BiometricPunchLog,
    GraceTime,
)
from attendance.views.views import attendance_validate
from base.context_processors import (
    enable_late_come_early_out_tracking,
    timerunner_enabled,
)
from base.models import AttendanceAllowedIP, Company, EmployeeShiftDay
from horilla.decorators import hx_request_required, login_required
from horilla.horilla_middlewares import _thread_locals


def late_come_create(attendance):
    """
    used to create late come report
    args:
        attendance : attendance object
    """

    if AttendanceLateComeEarlyOut.objects.filter(
        type="late_come", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="late_come", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()

    late_come_obj.type = "late_come"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def late_come(attendance, start_time, end_time, shift):
    """
    this method is used to mark the late check-in  attendance after the shift starts
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        end_time : attendance day shift end time

    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return
    request = getattr(_thread_locals, "request", None)
    now_sec = strtime_seconds(attendance.attendance_clock_in.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")

    # Checking gracetime allowance before creating late come
    if shift and shift.grace_time_id:
        # checking grace time in shift, it has the higher priority
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_in == True
        ):
            # Setting allowance for the check in time
            now_sec -= shift.grace_time_id.allowed_time_in_secs
    # checking default grace time
    elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
        grace_time = GraceTime.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        # Setting allowance for the check in time if grace allocate for clock in event
        if grace_time.allowed_clock_in:
            now_sec -= grace_time.allowed_time_in_secs
    else:
        pass
    if start_time > end_time and start_time != end_time:
        # night shift
        if now_sec < mid_day_sec:
            # Here  attendance or attendance activity for new day night shift
            late_come_create(attendance)
        elif now_sec > start_time:
            # Here  attendance or attendance activity for previous day night shift
            late_come_create(attendance)
    elif start_time < now_sec:
        late_come_create(attendance)
    return True


def clock_in_attendance_and_activity(
    employee,
    date_today,
    attendance_date,
    day,
    now,
    shift,
    minimum_hour,
    start_time,
    end_time,
    in_datetime,
):
    """
    This method is used to create attendance activity or attendance when an employee clocks-in
    args:
        employee        : employee instance
        date_today      : date
        attendance_date : the date that attendance for
        day             : shift day
        now             : current time
        shift           : shift object
        minimum_hour    : minimum hour in shift schedule
        start_time      : start time in shift schedule
        end_time        : end time in shift schedule
    """

    # attendance activity create
    activity = AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_out=None,
    ).first()

    if activity and not activity.clock_out:
        activity.clock_out = in_datetime
        activity.clock_out_date = date_today
        activity.save()

    new_activity = AttendanceActivity.objects.create(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_in=in_datetime,
        in_datetime=in_datetime,
    )
    # create attendance if not exist
    attendance = Attendance.objects.filter(
        employee_id=employee, attendance_date=attendance_date
    )
    if not attendance.exists():
        attendance = Attendance()
        attendance.employee_id = employee
        attendance.shift_id = shift
        attendance.work_type_id = attendance.employee_id.employee_work_info.work_type_id
        attendance.attendance_date = attendance_date
        attendance.attendance_day = day
        attendance.attendance_clock_in = now
        attendance.attendance_clock_in_date = date_today
        attendance.minimum_hour = minimum_hour
        attendance.save()
        # check here late come or not

        attendance = Attendance.find(attendance.id)
        late_come(
            attendance=attendance, start_time=start_time, end_time=end_time, shift=shift
        )
    else:
        attendance = attendance[0]
        attendance.attendance_clock_out = None
        attendance.attendance_clock_out_date = None
        attendance.save()
        # delete if the attendance marked the early out
        early_out_instance = attendance.late_come_early_out.filter(type="early_out")
        if early_out_instance.exists():
            early_out_instance[0].delete()
    return attendance




def attendance_window_status(
    now_sec,
    start_time_sec,
    end_time_sec,
    check_in_window_minutes=0,
    check_out_window_minutes=0,
    check_in_window_start_sec=None,
    check_in_window_end_sec=None,
    check_out_window_start_sec=None,
    check_out_window_end_sec=None,
):
    """Classify a punch against the configured shift windows."""
    is_night_shift = start_time_sec > end_time_sec and start_time_sec != end_time_sec
    if is_night_shift:
        current_sec = now_sec + (24 * 60 * 60 if now_sec < 12 * 60 * 60 else 0)
        start_sec = start_time_sec
        end_sec = end_time_sec + 24 * 60 * 60
    else:
        current_sec = now_sec
        start_sec = start_time_sec
        end_sec = end_time_sec

    if check_in_window_start_sec is not None and check_in_window_end_sec is not None:
        in_start = check_in_window_start_sec
        in_end = check_in_window_end_sec
        if is_night_shift and in_end < in_start:
            in_end += 24 * 60 * 60
    else:
        in_start = start_sec
        in_end = start_sec + max(check_in_window_minutes, 0) * 60

    if check_out_window_start_sec is not None and check_out_window_end_sec is not None:
        out_start = check_out_window_start_sec
        out_end = check_out_window_end_sec
        if is_night_shift and out_end < out_start:
            out_end += 24 * 60 * 60
    else:
        out_start = end_sec - max(check_out_window_minutes, 0) * 60
        out_end = end_sec

    in_window = in_start <= current_sec <= in_end
    out_window = out_start <= current_sec <= out_end

    if in_window:
        return "CHECKIN_WINDOW"
    if out_window:
        return "CHECKOUT_WINDOW"
    if current_sec < in_start:
        return "BEFORE_CHECKIN_WINDOW"
    if current_sec > out_end:
        return "AFTER_CHECKOUT_WINDOW"
    return "BETWEEN_WINDOWS"


def process_biometric_punch(request, punch_code, device=None, source="ZKTeco"):
    """
    Persist every biometric punch, while using only window-valid punches
    for attendance calculation.
    """
    employee, work_info = employee_exists(request)
    if not employee or work_info is None:
        return None

    shift = work_info.shift_id
    date_today = request.date if request.__dict__.get("date") else date.today()
    punch_datetime = (
        request.datetime
        if request.__dict__.get("datetime")
        else timezone.localtime()
    )
    day = EmployeeShiftDay.objects.get(day=date_today.strftime("%A").lower())
    attendance_date = date_today
    now_sec = strtime_seconds(punch_datetime.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")

    minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
        day=day, shift=shift
    )
    shift_schedule = (
        shift.employeeshiftschedule_set.filter(day=day).first() if shift else None
    )

    if start_time_sec > end_time_sec and mid_day_sec > now_sec:
        attendance_date = date_today - timedelta(days=1)
        day = EmployeeShiftDay.objects.get(
            day=attendance_date.strftime("%A").lower()
        )
        minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
            day=day, shift=shift
        )
        shift_schedule = (
            shift.employeeshiftschedule_set.filter(day=day).first()
            if shift
            else None
        )

    def time_to_seconds(value):
        return value.hour * 3600 + value.minute * 60 + value.second

    check_in_start_sec = (
        time_to_seconds(shift_schedule.check_in_window_start)
        if shift_schedule and shift_schedule.check_in_window_start
        else None
    )
    check_in_end_sec = (
        time_to_seconds(shift_schedule.check_in_window_end)
        if shift_schedule and shift_schedule.check_in_window_end
        else None
    )
    check_out_start_sec = (
        time_to_seconds(shift_schedule.check_out_window_start)
        if shift_schedule and shift_schedule.check_out_window_start
        else None
    )
    check_out_end_sec = (
        time_to_seconds(shift_schedule.check_out_window_end)
        if shift_schedule and shift_schedule.check_out_window_end
        else None
    )

    direction = "IN" if punch_code in {0, 3, 4} else "OUT"
    window_status = attendance_window_status(
        now_sec,
        check_in_start_sec,
        check_in_end_sec,
        check_out_start_sec,
        check_out_end_sec,
        is_night_shift=start_time_sec > end_time_sec,
    )
    within_window = (
        window_status == "CHECKIN_WINDOW"
        if direction == "IN"
        else window_status == "CHECKOUT_WINDOW"
    )

    raw_log = BiometricPunchLog.objects.create(
        employee_id=employee,
        device_id=device,
        biometric_user_id=str(
            getattr(request, "biometric_user_id", "")
            or getattr(request, "user_id", "")
            or getattr(request.user, "username", "")
        ),
        punch_code=punch_code,
        direction=direction,
        punch_datetime=punch_datetime,
        attendance_date=attendance_date,
        shift_id=shift,
        window_status=window_status,
        within_window=within_window,
        source=source,
        raw_payload={
            "punch_code": punch_code,
            "datetime": punch_datetime.isoformat(),
            "device_id": str(device.pk) if device else None,
        },
    )

    if not within_window:
        return raw_log

    attendance = Attendance.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
    ).first()

    if direction == "IN":
        # First valid IN wins. Later IN punches are raw logs only.
        if attendance and attendance.attendance_clock_in:
            return raw_log

        if not attendance:
            attendance = Attendance.objects.create(
                employee_id=employee,
                shift_id=shift,
                work_type_id=work_info.work_type_id,
                attendance_date=attendance_date,
                attendance_day=day,
                attendance_clock_in=punch_datetime.time(),
                attendance_clock_in_date=date_today,
                minimum_hour=minimum_hour,
            )
        else:
            attendance.attendance_clock_in = punch_datetime.time()
            attendance.attendance_clock_in_date = date_today
            attendance.minimum_hour = minimum_hour
            attendance.save(
                update_fields=[
                    "attendance_clock_in",
                    "attendance_clock_in_date",
                    "minimum_hour",
                ]
            )

        AttendanceActivity.objects.create(
            employee_id=employee,
            attendance_date=attendance_date,
            shift_day=day,
            clock_in_date=date_today,
            clock_in=punch_datetime.time(),
            in_datetime=punch_datetime,
        )
        late_come(
            attendance=attendance,
            start_time=start_time_sec,
            end_time=end_time_sec,
            shift=shift,
        )
        raw_log.used_for_attendance = True
        raw_log.selection_role = "FINAL_IN"
        raw_log.attendance_id = attendance
        raw_log.save(
            update_fields=["used_for_attendance", "selection_role", "attendance_id"]
        )
        return raw_log

    # OUT requires a valid IN for the same attendance date.
    if not attendance or not attendance.attendance_clock_in:
        return raw_log

    # Latest valid OUT becomes final OUT; old raw rows remain untouched.
    BiometricPunchLog.objects.filter(
        attendance_id=attendance,
        direction="OUT",
        used_for_attendance=True,
    ).update(used_for_attendance=False, selection_role=None)

    activity = AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
    ).order_by("-id").first()
    if not activity:
        return raw_log

    activity.clock_out = punch_datetime.time()
    activity.clock_out_date = date_today
    activity.out_datetime = punch_datetime
    activity.save(update_fields=["clock_out", "clock_out_date", "out_datetime"])

    total_seconds = 0
    for item in AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_out__isnull=False,
    ):
        in_dt, out_dt = activity_datetime(item)
        total_seconds += int((out_dt - in_dt).total_seconds())

    attendance.attendance_clock_out = punch_datetime.time()
    attendance.attendance_clock_out_date = date_today
    attendance.attendance_worked_hour = format_time(total_seconds)
    attendance.attendance_overtime = overtime_calculation(attendance)
    attendance.attendance_validated = attendance_validate(attendance)
    attendance.save(
        update_fields=[
            "attendance_clock_out",
            "attendance_clock_out_date",
            "attendance_worked_hour",
            "attendance_overtime",
            "attendance_validated",
        ]
    )
    early_out(
        attendance=attendance,
        start_time=start_time_sec,
        end_time=end_time_sec,
        shift=shift,
    )

    raw_log.used_for_attendance = True
    raw_log.selection_role = "FINAL_OUT"
    raw_log.attendance_id = attendance
    raw_log.save(
        update_fields=["used_for_attendance", "selection_role", "attendance_id"]
    )
    return raw_log


@login_required
@hx_request_required
def clock_in(request):
    """
    This method is used to mark the attendance once per a day and multiple attendance activities.
    """
    # check wether check in/check out feature is enabled
    selected_company = request.session.get("selected_company")
    if selected_company == "all":
        company = None
        attendance_general_settings = AttendanceGeneralSetting.objects.filter(
            company_id=None
        ).first()
    else:
        company = Company.objects.filter(id=selected_company).first()
        attendance_general_settings = AttendanceGeneralSetting.objects.filter(
            company_id=company
        ).first()
    # request.__dict__.get("datetime")' used to check if the request is from a biometric device
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        allowed_attendance_ips = AttendanceAllowedIP.objects.filter(
            company_id=company
        ).first()

        if (
            not request.__dict__.get("datetime")
            and allowed_attendance_ips
            and allowed_attendance_ips.is_enabled
        ):
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = request.META.get("REMOTE_ADDR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]

            allowed_ips = (allowed_attendance_ips.additional_data or {}).get(
                "allowed_ips", []
            )
            ip_allowed = False
            for allowed_ip in allowed_ips:
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(
                        allowed_ip, strict=False
                    ):
                        ip_allowed = True
                        break
                except ValueError:
                    continue

            if not ip_allowed:
                messages.error(
                    request,
                    _("Check-In Restricted: Your current network is not authorized "),
                )
                return HorillaRedirect(request)

        employee, work_info = employee_exists(request)
        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        if employee and work_info is not None:
            shift = work_info.shift_id
            date_today = date.today()
            if request.__dict__.get("date"):
                date_today = request.date
            attendance_date = date_today
            day = date_today.strftime("%A").lower()
            day = EmployeeShiftDay.objects.get(day=day)
            now = datetime.now().strftime("%H:%M")
            if request.__dict__.get("time"):
                now = request.time.strftime("%H:%M")
            now_sec = strtime_seconds(now)
            mid_day_sec = strtime_seconds("12:00")
            minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                day=day, shift=shift
            )
            shift_schedule = (
                shift.employeeshiftschedule_set.filter(day=day).first()
                if shift
                else None
            )
            if start_time_sec > end_time_sec:
                # night shift
                # ------------------
                # Night shift in Horilla consider a 24 hours from noon to next day noon,
                # the shift day taken today if the attendance clocked in after 12 O clock.

                if mid_day_sec > now_sec:
                    # Here you need to create attendance for yesterday

                    date_yesterday = date_today - timedelta(days=1)
                    day_yesterday = date_yesterday.strftime("%A").lower()
                    day_yesterday = EmployeeShiftDay.objects.get(day=day_yesterday)
                    minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                        day=day_yesterday, shift=shift
                    )
                    shift_schedule = (
                        shift.employeeshiftschedule_set.filter(day=day_yesterday).first()
                        if shift
                        else None
                    )
                    attendance_date = date_yesterday
                    day = day_yesterday

            attendance = clock_in_attendance_and_activity(
                employee=employee,
                date_today=date_today,
                attendance_date=attendance_date,
                day=day,
                now=now,
                shift=shift,
                minimum_hour=minimum_hour,
                start_time=start_time_sec,
                end_time=end_time_sec,
                in_datetime=datetime_now,
            )



            # Refresh employee from DB so template re-evaluates is_clocked_in correctly
            employee.refresh_from_db()
            return render(
                request, "attendance/components/in_out_component.html", {"run": 1}
            )
        messages.error(
            request,
            _(
                "Check-In Unavailable: Your employee profile or work information is incomplete."
            ),
        )
        return HorillaRedirect(request)
    else:
        messages.error(
            request,
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            ),
        )
        return HorillaRedirect(request)


def clock_out_attendance_and_activity(employee, date_today, now, out_datetime=None):
    """
    Clock out the attendance and activity
    args:
        employee    : employee instance
        date_today  : today date
        now         : now
    """

    attendance_activities = AttendanceActivity.objects.filter(
        employee_id=employee,
    ).order_by("attendance_date", "id")
    attendance_activity = None  # Initialize attendance_activity

    if attendance_activities.filter(clock_out__isnull=True).exists():
        attendance_activity = attendance_activities.filter(
            clock_out__isnull=True
        ).last()
        attendance_activity.clock_out = out_datetime
        attendance_activity.clock_out_date = date_today
        attendance_activity.out_datetime = out_datetime
        attendance_activity.save()

        attendance_activities = attendance_activities.filter(
            attendance_date=attendance_activity.attendance_date
        )
        # Here calculate the total durations between the attendance activities

        duration = 0
        for activity in attendance_activities:
            in_datetime, out_datetime = activity_datetime(activity)
            difference = out_datetime - in_datetime
            days_second = difference.days * 24 * 3600
            seconds = difference.seconds
            total_seconds = days_second + seconds
            duration = duration + total_seconds
        duration = format_time(duration)
        # update clock out of attendance
        attendance = Attendance.objects.filter(employee_id=employee).order_by(
            "-attendance_date", "-id"
        )[0]
        attendance.attendance_clock_out = now + ":00"
        attendance.attendance_clock_out_date = date_today
        attendance.attendance_worked_hour = duration
        # Overtime calculation
        attendance.attendance_overtime = overtime_calculation(attendance)

        # Validate the attendance as per the condition
        attendance.attendance_validated = attendance_validate(attendance)
        attendance.save()

        return attendance

    logger.error("No attendance clock in activity found that needs clocking out.")
    return


def early_out_create(attendance):
    """
    Used to create early out report
    args:
        attendance : attendance obj
    """
    if AttendanceLateComeEarlyOut.objects.filter(
        type="early_out", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="early_out", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()
    late_come_obj.type = "early_out"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def early_out(attendance, start_time, end_time, shift):
    """
    This method is used to mark the early check-out attendance before the shift ends
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        start_end : attendance day shift end time
    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return

    clock_out_time = attendance.attendance_clock_out
    if isinstance(clock_out_time, str):
        clock_out_time = datetime.strptime(clock_out_time, "%H:%M:%S")

    now_sec = strtime_seconds(clock_out_time.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")
    # Checking gracetime allowance before creating early out
    if shift and shift.grace_time_id:
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_out == True
        ):
            now_sec += shift.grace_time_id.allowed_time_in_secs
    elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
        grace_time = GraceTime.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        # Setting allowance for the check out time if grace allocate for clock out event
        if grace_time.allowed_clock_out:
            now_sec += grace_time.allowed_time_in_secs
    else:
        pass
    if start_time > end_time:
        # Early out condition for night shift
        if now_sec < mid_day_sec:
            if now_sec < end_time:
                # Early out condition for general shift
                early_out_create(attendance)
        else:
            early_out_create(attendance)
        return
    if end_time > now_sec:
        early_out_create(attendance)
    return


@login_required
@hx_request_required
def clock_out(request):
    """
    This method is used to set the out date and time for attendance and attendance activity
    """
    # check wether check in/check out feature is enabled
    selected_company = request.session.get("selected_company")
    if selected_company == "all":
        company = None
        attendance_general_settings = AttendanceGeneralSetting.objects.filter(
            company_id=None
        ).first()
    else:
        company = Company.objects.filter(id=selected_company).first()
        attendance_general_settings = AttendanceGeneralSetting.objects.filter(
            company_id=company
        ).first()
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        allowed_attendance_ips = AttendanceAllowedIP.objects.filter(
            company_id=company
        ).first()

        if (
            not request.__dict__.get("datetime")
            and allowed_attendance_ips
            and allowed_attendance_ips.is_enabled
        ):
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = request.META.get("REMOTE_ADDR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]

            allowed_ips = (allowed_attendance_ips.additional_data or {}).get(
                "allowed_ips", []
            )
            ip_allowed = False
            for allowed_ip in allowed_ips:
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(
                        allowed_ip, strict=False
                    ):
                        ip_allowed = True
                        break
                except ValueError:
                    continue

            if not ip_allowed:
                messages.error(
                    request,
                    _("Check-Out Restricted: Your current network is not authorized"),
                )
                return HorillaRedirect(request)

        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        employee, work_info = employee_exists(request)
        shift = work_info.shift_id
        date_today = date.today()
        if request.__dict__.get("date"):
            date_today = request.date
        day = date_today.strftime("%A").lower()
        day = EmployeeShiftDay.objects.get(day=day)
        attendance = (
            Attendance.objects.filter(employee_id=employee)
            .order_by("id", "attendance_date")
            .last()
        )
        if attendance is not None:
            if not attendance.attendance_day:
                day_name = attendance.attendance_date.strftime("%A").lower()
                attendance.attendance_day = EmployeeShiftDay.objects.get(day=day_name)
                attendance.save(update_fields=["attendance_day"])
            day = attendance.attendance_day
        now = datetime.now().strftime("%H:%M")
        if request.__dict__.get("time"):
            now = request.time.strftime("%H:%M")
        minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
            day=day, shift=shift
        )
        shift_schedule = (
            shift.employeeshiftschedule_set.filter(day=day).first()
            if shift
            else None
        )

        attendance = clock_out_attendance_and_activity(
            employee=employee, date_today=date_today, now=now, out_datetime=datetime_now
        )
        if attendance:


            early_out_instance = attendance.late_come_early_out.filter(type="early_out")
            is_night_shift = attendance.is_night_shift()
            next_date = attendance.attendance_date + timedelta(days=1)
            if not early_out_instance.exists():
                if is_night_shift:
                    now_sec = strtime_seconds(now)
                    mid_sec = strtime_seconds("12:00")

                    if (attendance.attendance_date == date_today) or (
                        # check is next day mid
                        mid_sec >= now_sec
                        and date_today == next_date
                    ):
                        early_out(
                            attendance=attendance,
                            start_time=start_time_sec,
                            end_time=end_time_sec,
                            shift=shift,
                        )
                elif attendance.attendance_date == date_today:
                    early_out(
                        attendance=attendance,
                        start_time=start_time_sec,
                        end_time=end_time_sec,
                        shift=shift,
                    )

        # Refresh employee from DB so template re-evaluates is_clocked_in correctly
        employee.refresh_from_db()
        return render(
            request, "attendance/components/in_out_component.html", {"run": 1}
        )

    else:
        messages.error(
            request,
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            ),
        )
        return HorillaRedirect(request)
