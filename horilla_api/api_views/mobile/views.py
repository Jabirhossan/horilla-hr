"""
Screen-shaped aggregates for the mobile client.

Everything else in this API is resource-shaped, and rightly so. The home
screen is the exception: it renders punch state, today's hours, a role band,
who is on leave, the latest announcement and an unread badge, which came to
eight or more round trips on a cold start over a mobile network. The previous
Flutter app made fourteen.

Deliberately narrow. This returns exactly what the home screen draws and
nothing else -- it is a view of existing data, not a second API. Resist
growing it into a general-purpose "give me everything" endpoint; if another
screen needs an aggregate, it gets its own, so each one stays readable and
cheap.
"""

from datetime import date

from django.apps import apps
from django.db.models import Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from attendance.models import Attendance, AttendanceActivity
from base.models import Announcement
from horilla_api.api_methods.base.capabilities import build_capabilities


def _format_seconds(seconds):
    seconds = max(int(seconds or 0), 0)
    return f"{seconds // 3600:02}:{(seconds % 3600) // 60:02}:{seconds % 60:02}"


def _todays_activities(employee):
    return list(
        AttendanceActivity.objects.filter(
            employee_id=employee, attendance_date=date.today()
        ).order_by("in_datetime")
    )


def _break_seconds(activities):
    """
    Time between clocking out and clocking back in again today.

    Not a stored field -- Attendance records at_work_second and
    overtime_second but no break total -- so it is derived from the gaps
    between consecutive activity pairs. Computed here rather than in the
    client so every caller agrees on the number.
    """
    total = 0
    for previous, nxt in zip(activities, activities[1:]):
        if previous.clock_out and nxt.clock_in:
            gap = (nxt.in_datetime - previous.out_datetime).total_seconds()
            if gap > 0:
                total += gap
    return int(total)


def _punch_block(employee, activities):
    open_activity = next((a for a in activities if not a.clock_out), None)
    first = activities[0] if activities else None
    return {
        "is_clocked_in": open_activity is not None,
        "clock_in_time": first.clock_in.strftime("%H:%M") if first else None,
        "last_activity_at": (
            open_activity.in_datetime.isoformat() if open_activity else None
        ),
    }


def _geofence_block(employee):
    """Config only. The client sends coordinates and the punch endpoint rules."""
    company = employee.get_company()
    fence = getattr(company, "geo_fencing", None) if company else None
    if fence is None or not fence.start:
        return {"enabled": False}
    return {
        "enabled": True,
        "latitude": fence.latitude,
        "longitude": fence.longitude,
        "radius_in_meters": fence.radius_in_meters,
    }


def _on_leave_today(employee):
    if not apps.is_installed("leave"):
        return []
    LeaveRequest = apps.get_model("leave", "LeaveRequest")
    today = date.today()
    requests = (
        LeaveRequest.objects.filter(
            status="approved", start_date__lte=today, end_date__gte=today
        )
        .select_related("employee_id")
        .exclude(employee_id=employee)[:10]
    )
    return [
        {
            "id": leave.employee_id.id,
            "name": leave.employee_id.get_full_name(),
            "leave_type": getattr(leave.leave_type_id, "leave_type", None),
        }
        for leave in requests
        if leave.employee_id
    ]


def _latest_announcement(employee):
    """
    The newest announcement this employee is actually an audience for.

    Announcements carry targeting (``employees``, ``department``,
    ``job_position``, or the denormalised ``filtered_employees``) and an
    ``expire_date``. Taking ``objects.order_by("-created_at").first()`` ignores
    both, so the home screen served a department-targeted or long-expired
    announcement to everyone in the company. The manager scopes by company, so
    this was never cross-tenant -- but "everyone in the company" is not the
    audience the author chose.

    Mirrors the predicate the web self-service dashboard uses
    (``base/ess_dashboard.py``) so the two surfaces agree on who sees what.
    """
    today = date.today()
    not_expired = Q(expire_date__gte=today) | Q(expire_date__isnull=True)

    if Announcement.objects.filter(filtered_employees=employee).exists():
        qs = Announcement.objects.filter(not_expired, filtered_employees=employee)
    else:
        work_info = getattr(employee, "employee_work_info", None)
        department = getattr(work_info, "department_id", None)
        job_position = getattr(work_info, "job_position_id", None)
        targeted = (
            Q(employees=employee)
            | (Q(department=department) if department else Q())
            | (Q(job_position=job_position) if job_position else Q())
        )
        # An announcement with no targeting at all is addressed to everyone.
        broadcast = (
            Q(employees__isnull=True)
            & Q(department__isnull=True)
            & Q(job_position__isnull=True)
        )
        qs = Announcement.objects.filter(not_expired).filter(targeted | broadcast)

    # distinct(): the targeting filters join three many-to-many tables, so a
    # row matching on more than one comes back more than once.
    announcement = qs.distinct().order_by("-created_at").first()
    if not announcement:
        return None
    return {
        "id": announcement.id,
        "title": announcement.title,
        "created_at": announcement.created_at.isoformat(),
    }


def _unread_notification_count(user):
    return user.notifications.unread().count()


class MobileHomeAPIView(APIView):
    """Everything the home screen draws, in one request."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = request.user.employee_get
        activities = _todays_activities(employee)

        attendance_today = Attendance.objects.filter(
            employee_id=employee, attendance_date=date.today()
        ).first()
        worked_seconds = employee.get_forecasted_at_work().get(
            "forecasted_at_work_seconds", 0
        )

        return Response(
            {
                "employee": {
                    "id": employee.id,
                    "name": employee.get_full_name(),
                    "avatar": employee.get_avatar(),
                },
                "capabilities": build_capabilities(request.user),
                "punch": _punch_block(employee, activities),
                "geofence": _geofence_block(employee),
                "today": {
                    "worked": _format_seconds(worked_seconds),
                    "break": _format_seconds(_break_seconds(activities)),
                    "overtime": _format_seconds(
                        attendance_today.overtime_second if attendance_today else 0
                    ),
                },
                "on_leave_today": _on_leave_today(employee),
                "announcement": _latest_announcement(employee),
                "unread_notifications": _unread_notification_count(request.user),
            },
            status=200,
        )
