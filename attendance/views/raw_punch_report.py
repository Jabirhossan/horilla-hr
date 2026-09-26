"""
Raw biometric punch report.
"""

from datetime import date, datetime, time, timedelta

import pandas as pd
from django.http import HttpResponse
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from attendance.models import BiometricPunchLog
from base.methods import get_session_company
from biometric.models import BiometricDevices
from base.models import Department
from employee.models import Employee, EmployeeWorkInformation
from horilla.decorators import login_required, permission_required


def _report_queryset(request):
    company = get_session_company(request)
    qs = BiometricPunchLog.objects.all().select_related(
        "employee_id", "device_id", "shift_id", "attendance_id"
    )
    if company is not None:
        qs = qs.filter(
            Q(employee_id__employee_work_info__company_id=company)
            | Q(device_id__company_id=company)
        )

    start = request.GET.get("start_date")
    end = request.GET.get("end_date")
    employee_id = request.GET.get("employee_id")
    department_id = request.GET.get("department_id")
    device_id = request.GET.get("device_id")
    direction = request.GET.get("direction")
    window_status = request.GET.get("window_status")
    used = request.GET.get("used_for_attendance")
    search = request.GET.get("search", "").strip()

    try:
        if start:
            qs = qs.filter(punch_datetime__date__gte=datetime.strptime(start, "%Y-%m-%d").date())
        if end:
            qs = qs.filter(punch_datetime__date__lte=datetime.strptime(end, "%Y-%m-%d").date())
    except ValueError:
        pass

    if employee_id:
        qs = qs.filter(employee_id_id=employee_id)
    if department_id:
        qs = qs.filter(employee_id__employee_work_info__department_id=department_id)
    if device_id:
        qs = qs.filter(device_id_id=device_id)
    if direction:
        qs = qs.filter(direction=direction)
    if window_status:
        qs = qs.filter(window_status=window_status)
    if used in {"yes", "no"}:
        qs = qs.filter(used_for_attendance=(used == "yes"))
    if search:
        qs = qs.filter(
            Q(employee_id__employee_first_name__icontains=search)
            | Q(employee_id__employee_last_name__icontains=search)
            | Q(employee_id__employee_id__icontains=search)
            | Q(biometric_user_id__icontains=search)
        )

    return qs.order_by("-punch_datetime", "-id")


@login_required
@permission_required("attendance.view_attendance")
def raw_punch_report(request):
    qs = _report_queryset(request)

    from django.core.paginator import Paginator
    paginator = Paginator(qs, int(request.GET.get("per_page", 25) or 25))
    page = paginator.get_page(request.GET.get("page", 1))

    company = get_session_company(request)
    employees = Employee.objects.filter(is_active=True)
    if company is not None:
        employees = employees.filter(employee_work_info__company_id=company)
    employees = employees.order_by("employee_first_name", "employee_last_name").distinct()

    work_info_qs = EmployeeWorkInformation.objects.filter(
        department_id__isnull=False,
    )
    if company is not None:
        work_info_qs = work_info_qs.filter(company_id=company)
    department_ids = work_info_qs.values_list(
        "department_id", flat=True
    ).distinct()
    departments = Department.objects.filter(
        id__in=department_ids
    ).order_by("department")

    devices = BiometricDevices.objects.all()
    if company is not None:
        devices = devices.filter(company_id=company)
    devices = devices.order_by("name")

    stats_qs = qs
    total = stats_qs.count()
    within = stats_qs.filter(within_window=True).count()
    outside = total - within
    final_in = stats_qs.filter(selection_role="FINAL_IN").count()
    final_out = stats_qs.filter(selection_role="FINAL_OUT").count()

    context = {
        "punches": page,
        "paginator": paginator,
        "employees": employees,
        "departments": departments,
        "devices": devices,
        "total": total,
        "within": within,
        "outside": outside,
        "final_in": final_in,
        "final_out": final_out,
        "window_choices": BiometricPunchLog.WINDOW_STATUS_CHOICES,
        "query": request.GET,
    }
    return render(request, "attendance/raw_punch_report/raw_punch_report.html", context)


@login_required
@permission_required("attendance.view_attendance")
def raw_punch_report_export(request):
    qs = _report_queryset(request)
    rows = []
    for punch in qs:
        employee = punch.employee_id
        rows.append({
            "Employee": employee.get_full_name() if employee else "Unmapped",
            "Employee ID": employee.employee_id if employee else "",
            "Device": punch.device_id.name if punch.device_id else "",
            "Device User ID": punch.biometric_user_id,
            "Punch Date": timezone.localtime(punch.punch_datetime).strftime("%Y-%m-%d"),
            "Punch Time": timezone.localtime(punch.punch_datetime).strftime("%I:%M:%S %p"),
            "Punch Code": punch.punch_code,
            "In/Out": punch.direction,
            "Window Status": punch.get_window_status_display(),
            "Used for Attendance": "Yes" if punch.used_for_attendance else "No",
            "Final Status": punch.selection_role or "",
            "Attendance ID": punch.attendance_id_id or "",
        })
    df = pd.DataFrame(rows)
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = 'attachment; filename="raw_biometric_punch_report.xlsx"'
    df.to_excel(response, index=False)
    return response
