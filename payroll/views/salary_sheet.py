"""
Salary sheet report for monthly payroll.
"""

from calendar import monthrange
from datetime import date

import pandas as pd
from django.db.models import Q\nfrom django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_GET

from employee.models import Employee
from horilla.decorators import login_required, permission_required
from payroll.models.models import Payslip


def _parse_month(value):
    try:
        year, month = (int(part) for part in value.split("-"))
        if 1 <= month <= 12:
            return date(year, month, 1)
    except (AttributeError, TypeError, ValueError):
        pass
    today = date.today()
    return date(today.year, today.month, 1)


def _month_range(month_start):
    last_day = monthrange(month_start.year, month_start.month)[1]
    return month_start, date(month_start.year, month_start.month, last_day)


def _employee_label(employee):
    return employee.get_full_name()


def build_salary_sheet(month_value="", employee_id=""):
    month_start = _parse_month(month_value)
    month_start, month_end = _month_range(month_start)

    employee_qs = Employee.objects.filter(is_active=True).order_by(
        "employee_first_name", "employee_last_name"
    )
    if employee_id:
        try:
            employee_qs = employee_qs.filter(pk=int(employee_id))
        except (TypeError, ValueError):
            employee_qs = employee_qs.none()

    employees = list(employee_qs)
    employee_ids = [employee.pk for employee in employees]

    payslips = (
        Payslip.objects.select_related("employee_id")
        .filter(
            employee_id_id__in=employee_ids,
            start_date__lte=month_end,
            end_date__gte=month_start,
        )
        .order_by(
            "employee_id__employee_first_name",
            "employee_id__employee_last_name",
            "-end_date",
        )
    )

    # Prefer an exact monthly payslip when overlapping periods exist.
    payslip_map = {}
    for payslip in payslips:
        employee_pk = payslip.employee_id_id
        current = payslip_map.get(employee_pk)
        if current is None:
            payslip_map[employee_pk] = payslip
        elif (
            payslip.start_date == month_start
            and payslip.end_date == month_end
            and not (
                current.start_date == month_start
                and current.end_date == month_end
            )
        ):
            payslip_map[employee_pk] = payslip

    from payroll.models.models import Contract

    contracts = (
        Contract.objects.filter(
            employee_id_id__in=employee_ids,
            contract_status="active",
            contract_start_date__lte=month_end,
        )
        .filter(
            Q(contract_end_date__isnull=True) | Q(contract_end_date__gte=month_start)
        )
        .order_by("-contract_start_date")
    )
    contract_map = {}
    for contract in contracts:
        contract_map.setdefault(contract.employee_id_id, contract)

    try:
        from attendance.models import WorkRecords

        records = WorkRecords.objects.filter(
            employee_id_id__in=employee_ids,
            date__gte=month_start,
            date__lte=month_end,
        ).values("employee_id_id", "work_record_type", "is_leave_record")
    except Exception:
        records = []

    attendance = {}
    for record in records:
        key = record["employee_id_id"]
        stats = attendance.setdefault(
            key, {"present": 0, "absent": 0, "leave": 0, "half_day": 0}
        )
        if record["is_leave_record"]:
            stats["leave"] += 1
        elif record["work_record_type"] == "FDP":
            stats["present"] += 1
        elif record["work_record_type"] == "ABS":
            stats["absent"] += 1
        elif record["work_record_type"] == "HDP":
            stats["half_day"] += 1

    rows = []
    total_basic = total_deduction = total_net = 0.0
    generated_count = 0

    for employee in employees:
        payslip = payslip_map.get(employee.pk)
        contract = contract_map.get(employee.pk)
        stats = attendance.get(
            employee.pk,
            {"present": 0, "absent": 0, "leave": 0, "half_day": 0},
        )

        if payslip:
            basic = float(payslip.basic_pay or 0)
            deduction = float(payslip.deduction or 0)
            net = float(payslip.net_pay or 0)
            status = payslip.get_status()
            generated_count += 1
            total_basic += basic
            total_deduction += deduction
            total_net += net
        else:
            basic = float(contract.wage or 0) if contract else 0
            deduction = None
            net = None
            status = _("Not Generated")

        rows.append(
            {
                "employee": _employee_label(employee),
                "employee_id": employee.badge_id or employee.pk,
                "present": stats["present"],
                "absent": stats["absent"],
                "leave": stats["leave"],
                "half_day": stats["half_day"],
                "basic_salary": basic,
                "deduction": deduction,
                "net_payable": net,
                "status": status,
            }
        )

    return {
        "month_start": month_start,
        "month_end": month_end,
        "month_value": month_start.strftime("%Y-%m"),
        "rows": rows,
        "total_employees": len(rows),
        "generated_count": generated_count,
        "total_basic": total_basic,
        "total_deduction": total_deduction,
        "total_net": total_net,
    }

@login_required
@permission_required("payroll.view_payslip")
@require_GET
def salary_sheet(request):
    month_value = request.GET.get("month", "")
    data = build_salary_sheet(month_value, request.GET.get("employee_id", ""))

    employees = (
        Employee.objects.filter(
            id__in=Payslip.objects.filter(
                start_date__lte=data["month_end"],
                end_date__gte=data["month_start"],
            ).values_list("employee_id", flat=True)
        )
        .order_by("employee_first_name", "employee_last_name")
    )

    return render(
        request,
        "payroll/salary_sheet/salary_sheet.html",
        {
            **data,
            "employees": employees,
            "selected_employee": request.GET.get("employee_id", ""),
        },
    )


@login_required
@permission_required("payroll.view_payslip")
@require_GET
def salary_sheet_export(request):
    data = build_salary_sheet(
        request.GET.get("month", ""),
        request.GET.get("employee_id", ""),
    )
    export_rows = []
    for row in data["rows"]:
        export_rows.append(
            {
                "Employee": row["employee"],
                "Employee ID": row["employee_id"],
                "Present": row["present"],
                "Absent": row["absent"],
                "Leave": row["leave"],
                "Half Day": row["half_day"],
                "Basic Salary": row["basic_salary"],
                "Deduction": row["deduction"],
                "Net Payable": row["net_payable"],
                "Payslip Status": row["status"],
            }
        )

    df = pd.DataFrame(export_rows)
    if not df.empty:
        total = {
            "Employee": "TOTAL",
            "Employee ID": "",
            "Present": int(df["Present"].sum()),
            "Absent": int(df["Absent"].sum()),
            "Leave": int(df["Leave"].sum()),
            "Half Day": int(df["Half Day"].sum()),
            "Basic Salary": float(df["Basic Salary"].sum()),
            "Deduction": float(df["Deduction"].sum()),
            "Net Payable": float(df["Net Payable"].sum()),
            "Payslip Status": "",
        }
        df = pd.concat([df, pd.DataFrame([total])], ignore_index=True)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = f"salary-sheet-{data['month_value']}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    with pd.ExcelWriter(response, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Salary Sheet")
        workbook = writer.book
        worksheet = writer.sheets["Salary Sheet"]
        money = workbook.add_format({"num_format": "#,##0.00"})
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, max(len(df), 1), max(len(df.columns) - 1, 0))
        for col in ["Basic Salary", "Deduction", "Net Payable"]:
            if col in df.columns:
                idx = df.columns.get_loc(col)
                worksheet.set_column(idx, idx, 16, money)
        worksheet.set_column(0, 0, 28)
        worksheet.set_column(1, 1, 18)

    return response
