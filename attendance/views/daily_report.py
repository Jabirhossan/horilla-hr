"""
Daily Attendance Report.

Shows one row per employee per date with shift timing, check-in/out,
late arrival, early departure, worked hours, and daily work status.
"""

import datetime
import io
from collections import defaultdict

import pandas as pd
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from attendance.models import Attendance, WorkRecords
from base.methods import (
    filtersubordinatesemployeemodel,
    get_company_leave_dates,
    get_holiday_dates,
)
from base.models import EmployeeShiftSchedule, Roster
from employee.models import Employee
from horilla.decorators import login_required, manager_can_enter


DAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _parse_date(value, fallback):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _format_seconds(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def _time_seconds(value):
    if value is None:
        return None
    return value.hour * 3600 + value.minute * 60 + value.second


def _duration_between(start_value, end_value):
    if not start_value or not end_value:
        return 0
    start = _time_seconds(start_value)
    end = _time_seconds(end_value)
    if end < start:
        end += 24 * 60 * 60
    return end - start


def _schedule_for_employee_date(employee, date, schedule_map):
    shift = getattr(getattr(employee, "employee_work_info", None), "shift_id", None)
    if not shift:
        return None

    return schedule_map.get((shift.pk, DAY_NAMES[date.weekday()]))


def _attendance_window_violation(attendance, schedule):
    """
    Apply the configured biometric attendance windows to the report.

    This is calculated from the actual punch times as well as requested_data,
    so older attendance records created before the configurable-window feature
    are also reported consistently.
    """
    if not attendance or not schedule:
        return False

    requested_data = attendance.requested_data or {}
    if requested_data.get("checkin_window_absent") or requested_data.get("checkout_window_absent"):
        return True

    check_in = attendance.attendance_clock_in
    check_out = attendance.attendance_clock_out
    start = schedule.start_time
    end = schedule.end_time

    if not start:
        return False

    start_sec = _time_seconds(start)
    end_sec = _time_seconds(end) if end else start_sec
    night = bool(getattr(schedule, "is_night_shift", False)) or start_sec > end_sec

    checkin_window = max(int(getattr(schedule, "check_in_window_minutes", 0) or 0), 0) * 60
    checkout_window = max(int(getattr(schedule, "check_out_window_minutes", 0) or 0), 0) * 60

    if check_in:
        check_in_sec = _time_seconds(check_in)
        if night and check_in_sec < start_sec:
            check_in_sec += 24 * 60 * 60
        if check_in_sec < start_sec or check_in_sec >= start_sec + checkin_window:
            return True

    if check_out:
        check_out_sec = _time_seconds(check_out)
        if night and check_out_sec < end_sec:
            check_out_sec += 24 * 60 * 60
        effective_end = end_sec + (24 * 60 * 60 if night else 0)
        if check_out_sec < effective_end - checkout_window:
            return True

    return False


def _status_label(work_record, attendance, holiday, week_off, scheduled, window_absent=False):
    if holiday:
        return _("Holiday")
    if week_off or not scheduled:
        return _("Weekly Off")
    if window_absent:
        return _("Absent")
    if work_record:
        labels = dict(WorkRecords.choices)
        if work_record.work_record_type in labels:
            return labels[work_record.work_record_type]
        if work_record.message:
            return work_record.message
    if attendance:
        worked = attendance.at_work_second or 0
        minimum = 0
        try:
            h, m, s = (attendance.minimum_hour or "00:00").split(":")
            minimum = int(h) * 3600 + int(m) * 60 + int(s)
        except (ValueError, AttributeError):
            minimum = 0
        if minimum and worked < minimum / 2:
            return _("Absent")
        if minimum and worked < minimum:
            return _("Half Day Present")
        return _("Present")
    return _("Absent")


def build_daily_report(from_date, to_date, employee_qs):
    employee_qs = employee_qs.select_related(
        "employee_work_info__shift_id",
        "employee_work_info__department_id",
        "employee_work_info__company_id",
    )
    employees = list(employee_qs)

    if not employees:
        return [], {
            "total": 0,
            "present": 0,
            "absent": 0,
            "half_day": 0,
            "late": 0,
            "early": 0,
        }

    employee_ids = [e.pk for e in employees]

    attendances = {
        (a.employee_id_id, a.attendance_date): a
        for a in Attendance.objects.filter(
            employee_id__in=employee_ids,
            attendance_date__range=(from_date, to_date),
        ).select_related("shift_id")
    }

    work_records = {
        (w.employee_id_id, w.date): w
        for w in WorkRecords.objects.filter(
            employee_id__in=employee_ids,
            date__range=(from_date, to_date),
        )
    }

    shift_ids = {
        e.employee_work_info.shift_id_id
        for e in employees
        if getattr(e, "employee_work_info", None)
        and e.employee_work_info.shift_id_id
    }
    schedule_map = {}
    if shift_ids:
        for schedule in EmployeeShiftSchedule.objects.filter(
            shift_id__in=shift_ids
        ).select_related("day", "shift_id"):
            schedule_map[(schedule.shift_id_id, (schedule.day.day or "").lower())] = schedule

    roster_map = defaultdict(set)
    for row in Roster.objects.filter(
        employee_id__in=employee_ids,
        date__range=(from_date, to_date),
        is_off=True,
    ).values("employee_id", "date"):
        roster_map[row["employee_id"]].add(row["date"])

    holiday_dates = {
        d for d in get_holiday_dates(from_date, to_date)
        if from_date <= d <= to_date
    }
    company_leave_dates = {
        d
        for d in get_company_leave_dates(from_date.year)
        + get_company_leave_dates(to_date.year)
        if from_date <= d <= to_date
    }

    rows = []
    summary = {
        "total": 0,
        "present": 0,
        "absent": 0,
        "half_day": 0,
        "late": 0,
        "early": 0,
    }

    current = from_date
    while current <= to_date:
        for employee in employees:
            schedule = _schedule_for_employee_date(employee, current, schedule_map)
            attendance = attendances.get((employee.pk, current))
            work_record = work_records.get((employee.pk, current))

            week_off = current in roster_map.get(employee.pk, set())
            holiday = current in holiday_dates or current in company_leave_dates
            scheduled = schedule is not None

            # Do not hide actual biometric/manual attendance even when the
            # employee was rostered off or the date is a holiday.
            if not (scheduled or attendance or work_record or week_off or holiday):
                continue

            shift_start = getattr(schedule, "start_time", None)
            shift_end = getattr(schedule, "end_time", None)
            check_in = attendance.attendance_clock_in if attendance else None
            check_out = attendance.attendance_clock_out if attendance else None

            late_seconds = 0
            early_seconds = 0
            if shift_start and check_in:
                late_seconds = max(
                    0, _time_seconds(check_in) - _time_seconds(shift_start)
                )

            if shift_end and check_out:
                if getattr(schedule, "is_night_shift", False):
                    early_seconds = max(
                        0, _time_seconds(shift_end) - _time_seconds(check_out)
                    )
                else:
                    early_seconds = max(
                        0, _time_seconds(shift_end) - _time_seconds(check_out)
                    )

            worked_seconds = (
                attendance.at_work_second
                if attendance and attendance.at_work_second is not None
                else _duration_between(check_in, check_out)
            )

            window_absent = _attendance_window_violation(attendance, schedule)
            status = str(
                _status_label(
                    work_record,
                    attendance,
                    holiday,
                    week_off,
                    scheduled,
                    window_absent=window_absent,
                )
            )

            row = {
                "date": current,
                "employee": employee,
                "employee_id": employee.badge_id or "",
                "company": getattr(
                    getattr(employee.employee_work_info, "company_id", None),
                    "company",
                    "",
                ),
                "department": getattr(
                    getattr(employee.employee_work_info, "department_id", None),
                    "department",
                    "",
                ),
                "shift": getattr(
                    getattr(employee.employee_work_info, "shift_id", None),
                    "employee_shift",
                    "",
                ),
                "shift_start": shift_start,
                "check_in": check_in,
                "late_seconds": late_seconds,
                "shift_end": shift_end,
                "check_out": check_out,
                "early_seconds": early_seconds,
                "worked_seconds": worked_seconds or 0,
                "status": status,
                "late": _format_seconds(late_seconds) if late_seconds else "00:00",
                "early": _format_seconds(early_seconds) if early_seconds else "00:00",
                "worked": _format_seconds(worked_seconds or 0),
            }
            rows.append(row)

            summary["total"] += 1
            status_lower = status.lower()
            if "absent" in status_lower:
                summary["absent"] += 1
            elif "half day" in status_lower:
                summary["half_day"] += 1
            elif "present" in status_lower:
                summary["present"] += 1
            if late_seconds:
                summary["late"] += 1
            if early_seconds:
                summary["early"] += 1

        current += datetime.timedelta(days=1)

    rows.sort(key=lambda r: (r["date"], r["employee"].get_full_name().lower()), reverse=True)
    return rows, summary


def _get_report_context(request):
    today = datetime.date.today()
    from_date = _parse_date(
        request.GET.get("from_date"), today.replace(day=1)
    )
    to_date = _parse_date(
        request.GET.get("to_date"),
        today,
    )
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    employee_qs = filtersubordinatesemployeemodel(
        request,
        Employee.objects.all(),
        "attendance.view_attendance",
    )

    search = (request.GET.get("search") or "").strip()
    if search:
        terms = [term for term in search.split(",") if term.strip()]
        from django.db.models import Q

        query = Q()
        for term in terms:
            term = term.strip()
            query |= Q(employee_first_name__icontains=term)
            query |= Q(employee_last_name__icontains=term)
            query |= Q(badge_id__icontains=term)
        employee_qs = employee_qs.filter(query)

    employee_ids = request.GET.getlist("employee_id")
    if employee_ids:
        employee_qs = employee_qs.filter(pk__in=employee_ids)

    department_ids = request.GET.getlist("department_id")
    if department_ids:
        employee_qs = employee_qs.filter(
            employee_work_info__department_id__in=department_ids
        )

    shift_ids = request.GET.getlist("shift_id")
    if shift_ids:
        employee_qs = employee_qs.filter(
            employee_work_info__shift_id__in=shift_ids
        )

    employee_qs = employee_qs.distinct()

    rows, summary = build_daily_report(from_date, to_date, employee_qs)

    status_filter = request.GET.get("status", "")
    if status_filter:
        rows = [r for r in rows if r["status"] == status_filter]

    return from_date, to_date, rows, summary


@login_required
@manager_can_enter("attendance.view_attendance")
def attendance_daily_report(request):
    from base.models import Department, EmployeeShift

    today = datetime.date.today()
    from_date = _parse_date(request.GET.get("from_date"), today.replace(day=1))
    to_date = _parse_date(request.GET.get("to_date"), today)
    context = {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "employees": filtersubordinatesemployeemodel(
            request, Employee.objects.all(), "attendance.view_attendance"
        ).order_by("employee_first_name", "employee_last_name"),
        "departments": Department.objects.all().order_by("department"),
        "shifts": EmployeeShift.objects.all().order_by("employee_shift"),
        "status_choices": [
            ("Present", _("Present")),
            ("Absent", _("Absent")),
            ("Half Day Present", _("Half Day Present")),
            ("Holiday", _("Holiday")),
            ("Weekly Off", _("Weekly Off")),
        ],
    }
    return render(request, "attendance/daily_report/daily_report.html", context)


@login_required
@manager_can_enter("attendance.view_attendance")
def attendance_daily_report_table(request):
    from django.core.paginator import Paginator

    from_date, to_date, rows, summary = _get_report_context(request)

    page_number = request.GET.get("page", 1)
    paginator = Paginator(rows, 50)
    page = paginator.get_page(page_number)

    return render(
        request,
        "attendance/daily_report/table_partial.html",
        {
            "rows": page.object_list,
            "page": page,
            "summary": summary,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
        },
    )


@login_required
@manager_can_enter("attendance.view_attendance")
def attendance_daily_report_export(request):
    from_date, to_date, rows, _summary = _get_report_context(request)

    data = []
    for row in rows:
        data.append(
            {
                str(_("Date")): row["date"].isoformat(),
                str(_("Employee")): row["employee"].get_full_name(),
                str(_("Employee ID")): row["employee_id"],
                str(_("Company / Branch")): row["company"],
                str(_("Department")): row["department"],
                str(_("Shift")): row["shift"],
                str(_("Shift Start")): row["shift_start"].strftime("%H:%M") if row["shift_start"] else "",
                str(_("Check-In")): row["check_in"].strftime("%H:%M") if row["check_in"] else "",
                str(_("Late")): row["late"],
                str(_("Shift End")): row["shift_end"].strftime("%H:%M") if row["shift_end"] else "",
                str(_("Check-Out")): row["check_out"].strftime("%H:%M") if row["check_out"] else "",
                str(_("Early")): row["early"],
                str(_("Working Hours")): row["worked"],
                str(_("Status")): row["status"],
            }
        )

    columns = [
        str(_("Date")),
        str(_("Employee")),
        str(_("Employee ID")),
        str(_("Company / Branch")),
        str(_("Department")),
        str(_("Shift")),
        str(_("Shift Start")),
        str(_("Check-In")),
        str(_("Late")),
        str(_("Shift End")),
        str(_("Check-Out")),
        str(_("Early")),
        str(_("Working Hours")),
        str(_("Status")),
    ]
    df = pd.DataFrame(data, columns=columns)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Attendance Report")
        worksheet = writer.sheets["Attendance Report"]
        header = writer.book.add_format(
            {"bold": True, "bg_color": "#404040", "font_color": "#ffffff", "border": 1}
        )
        for col_idx, name in enumerate(df.columns):
            worksheet.write(0, col_idx, name, header)
            max_len = max(len(name), int(df[name].astype(str).map(len).max()) if len(df) else 0)
            worksheet.set_column(col_idx, col_idx, min(max_len + 2, 35))

    output.seek(0)
    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="Attendance_Report_{from_date}_{to_date}.xlsx"'
    )
    return response
