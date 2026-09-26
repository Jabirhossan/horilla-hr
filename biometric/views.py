"""
Module for managing biometric devices and employee attendance.

Includes classes and functions for adding, editing, and deleting biometric devices,
as well as scheduling attendance capture. Also provides views for managing employees,
registered on biometric devices.
"""

import json
import logging
from datetime import datetime, timedelta
from threading import Event, Thread
from urllib.parse import parse_qs, unquote

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone as django_timezone
from django.utils.html import format_html
from django.utils.translation import gettext as __
from django.utils.translation import gettext_lazy as _
from zk import ZK
from zk import exception as zk_exception

from attendance.methods.utils import Request
from attendance.models import AttendanceActivity, BiometricPunchLog
from attendance.views.clock_in_out import (
    clock_in,
    clock_out,
    persist_raw_biometric_punch,
    process_biometric_punch,
)
from base.methods import get_key_instances, get_pagination
from employee.models import Employee, EmployeeWorkInformation
from horilla.decorators import (
    hx_request_required,
    install_required,
    login_required,
    permission_required,
)
from horilla.filters import HorillaPaginator
from horilla.http.response import HorillaRedirect
from horilla.scheduling import register_job
from horilla.settings import TIME_ZONE

from .anviz import CrossChexCloudAPI
from .cosec import COSECBiometric
from .dahua import DahuaAPI
from .etimeoffice import ETimeOfficeAPI
from .filters import BiometricDeviceFilter
from .forms import (
    BiometricDeviceForm,
    BiometricDeviceSchedulerForm,
    CosecUserAddForm,
    COSECUserForm,
    DahuaUserForm,
    EmployeeBiometricAddForm,
    MapBioUsers,
)
from .models import BiometricDevices, BiometricEmployees, COSECAttendanceArguments

logger = logging.getLogger(__name__)

# Long-running biometric live-capture threads owned by this Django worker.
_LIVE_BIO_THREADS = {}


def str_time_seconds(time):
    """
    this method is used reconvert time in H:M formate string back to seconds and return it
    args:
        time : time in H:M format
    """

    ftr = [3600, 60, 1]
    return sum(a * b for a, b in zip(ftr, map(int, time.split(":"))))


def paginator_qry(qryset, page_number):
    """
    This method is used to paginate query set
    """
    paginator = HorillaPaginator(qryset, get_pagination())
    qryset = paginator.get_page(page_number)
    return qryset


def biometric_paginator_qry(data_list, page_number, per_page=25):
    """
    This function is used to paginate a list of dictionaries.
    """
    start_index = (page_number - 1) * per_page
    end_index = page_number * per_page
    paginated_data = {}
    paginated_data["users"] = data_list[start_index:end_index]

    total_items = len(data_list)
    total_pages = (total_items + per_page - 1) // per_page
    has_previous = page_number > 1
    has_next = page_number < total_pages

    paginated_data["paginator"] = {
        "number": page_number,
        "previous_page_number": page_number - 1 if has_previous else None,
        "next_page_number": page_number + 1 if has_next else None,
        "num_pages": total_pages,
        "has_previous": has_previous,
        "has_next": has_next,
    }
    return paginated_data


def biometric_set_time(conn):
    """
    Sets the time on the biometric device using the provided connection.

    Parameters:
    - conn: The connection to the biometric device.

    Returns:
    None
    """
    new_time = datetime.today()
    conn.set_time(new_time)


class ZKBioAttendance(Thread):
    """Continuously capture live attendance events from a ZKTeco device."""

    def __init__(self, machine_ip, port_no, password, device_id=None):
        super().__init__(daemon=True)
        self.machine_ip = machine_ip
        self.port_no = port_no
        self.password = int(password)
        self.device_id = device_id
        self._stop_event = Event()
        self.conn = None

    def _process_attendance(self, device, attendance):
        if not attendance:
            return

        user_id = str(attendance.user_id)
        punch_code = attendance.punch
        date_time = attendance.timestamp

        if date_time.tzinfo is None:
            date_time = django_timezone.make_aware(
                date_time, django_timezone.get_current_timezone()
            )
        else:
            date_time = date_time.astimezone(django_timezone.get_current_timezone())

        bio_id = BiometricEmployees.objects.filter(
            user_id=user_id, device_id=device
        ).select_related("employee_id__employee_user_id").first()

        request_data = Request(
            user=bio_id.employee_id.employee_user_id if bio_id else None,
            date=date_time.date(),
            time=date_time.time(),
            datetime=date_time,
        )
        request_data.biometric_user_id = user_id

        try:
            if punch_code in {0, 3, 4, 1, 2, 5}:
                if not bio_id:
                    existing_log = BiometricPunchLog.objects.filter(
                        device_id=device,
                        employee_id__isnull=True,
                        biometric_user_id=user_id,
                        punch_datetime=date_time,
                        punch_code=punch_code,
                    ).first()
                    if existing_log is None:
                        persist_raw_biometric_punch(
                            request_data,
                            punch_code,
                            device=device,
                            source="ZKTeco",
                            employee=None,
                        )
                    logger.warning(
                        "Live biometric punch stored as raw-only: device=%s user_id=%s is not mapped",
                        device.name if device else self.machine_ip,
                        user_id,
                    )
                else:
                    process_biometric_punch(
                        request_data,
                        punch_code,
                        device=device,
                        source="ZKTeco",
                    )
            else:
                logger.warning(
                    "Live biometric punch has unsupported punch code: user_id=%s punch=%s",
                    user_id,
                    punch_code,
                )
                return

            device.last_fetch_date = date_time.date()
            device.last_fetch_time = date_time.time()
            device.save(update_fields=["last_fetch_date", "last_fetch_time"])
            logger.info(
                "Live biometric attendance processed: device=%s user_id=%s at=%s",
                device.name if device else self.machine_ip,
                user_id,
                date_time,
            )
        except Exception:
            logger.exception(
                "Live biometric attendance processing failed: device=%s user_id=%s",
                device.name if device else self.machine_ip,
                user_id,
            )

    def run(self):
        while not self._stop_event.is_set():
            conn = None
            try:
                device = BiometricDevices.objects.filter(
                    id=self.device_id
                ).first() if self.device_id else BiometricDevices.objects.filter(
                    machine_ip=self.machine_ip, port=self.port_no
                ).first()

                if not device or not device.is_live:
                    break

                zk_device = ZK(
                    self.machine_ip,
                    port=self.port_no,
                    timeout=60,
                    password=self.password,
                    force_udp=False,
                    ommit_ping=True,
                )
                conn = zk_device.connect()
                self.conn = conn

                if not conn:
                    raise ConnectionError("ZKTeco connection returned no connection")

                logger.info(
                    "ZKTeco live capture connected: device=%s ip=%s port=%s",
                    device.name,
                    self.machine_ip,
                    self.port_no,
                )

                conn.enable_device()

                for attendance in conn.live_capture():
                    if self._stop_event.is_set():
                        break
                    self._process_attendance(device, attendance)

            except Exception:
                if self._stop_event.is_set():
                    break
                logger.exception(
                    "ZKTeco live capture connection/error: device=%s ip=%s port=%s",
                    getattr(device, "name", self.machine_ip),
                    self.machine_ip,
                    self.port_no,
                )
                self._stop_event.wait(3)
            finally:
                self.conn = None
                if conn:
                    try:
                        conn.end_live_capture = True
                    except Exception:
                        pass
                    try:
                        conn.disconnect()
                    except Exception:
                        pass

    def stop(self):
        """Stop the live capture thread and close its active ZKTeco connection."""
        self._stop_event.set()
        if self.conn:
            try:
                self.conn.end_live_capture = True
            except Exception:
                pass


class COSECBioAttendanceThread(Thread):
    """
    A thread class that handles the real-time retrieval and processing of
    biometric attendance data from a COSEC biometric device.

    Attributes:
        device_id (int): The ID of the biometric device to interact with.
        _stop_event (threading.Event): An event to signal when to stop the thread.

    Methods:
        run():
            Continuously fetches attendance data from the COSEC device, processes
            it, and updates the last fetched sequence and rollover count.

        stop():
            Signals the thread to stop by setting the _stop_event.
    """

    def __init__(self, device_id):
        super().__init__()
        self.device_id = device_id
        self._stop_event = Event()

    def run(self):
        try:
            device = BiometricDevices.objects.get(id=self.device_id)
            if not device.is_live:
                return

            device_args = COSECAttendanceArguments.objects.filter(
                device_id=device
            ).first()
            last_fetch_roll_ovr_count = (
                int(device_args.last_fetch_roll_ovr_count) if device_args else 0
            )
            last_fetch_seq_number = (
                int(device_args.last_fetch_seq_number) if device_args else 1
            )

            cosec = COSECBiometric(
                device.machine_ip,
                device.port,
                device.bio_username,
                device.bio_password,
                timeout=10,
            )
            while not self._stop_event.is_set():
                attendances = cosec.get_attendance_events(
                    last_fetch_roll_ovr_count, int(last_fetch_seq_number) + 1
                )
                if not isinstance(attendances, list):
                    self._stop_event.wait(5)
                    continue

                for attendance in attendances:
                    ref_user_id = attendance["detail-1"]
                    employee = BiometricEmployees.objects.filter(
                        ref_user_id=ref_user_id
                    ).first()
                    if not employee:
                        continue

                    date_str = attendance["date"]
                    time_str = attendance["time"]
                    attendance_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                    attendance_time = datetime.strptime(time_str, "%H:%M:%S").time()
                    attendance_datetime = datetime.combine(
                        attendance_date, attendance_time
                    )
                    punch_code = attendance["detail-2"]

                    request_data = Request(
                        user=employee.employee_id.employee_user_id,
                        date=attendance_date,
                        time=attendance_time,
                        datetime=django_timezone.make_aware(attendance_datetime),
                    )
                    try:
                        if punch_code in ["1", "3", "5", "7", "9", "0"]:
                            clock_in(request_data)
                        elif punch_code in ["2", "4", "6", "8", "10"]:
                            clock_out(request_data)
                    except Exception as error:
                        logger.error("Error processing attendance: ", error)

                if attendances:
                    last_attendance = attendances[-1]
                    last_fetch_seq_number = last_attendance["seq-No"]
                    last_fetch_roll_ovr_count = last_attendance["roll-over-count"]
                    COSECAttendanceArguments.objects.update_or_create(
                        device_id=device,
                        defaults={
                            "last_fetch_roll_ovr_count": last_fetch_roll_ovr_count,
                            "last_fetch_seq_number": last_fetch_seq_number,
                        },
                    )
                # Sleep to prevent overwhelming the device with requests
                self._stop_event.wait(2)

        except Exception as error:
            device = BiometricDevices.objects.get(id=self.device_id)
            device.is_live = False
            device.save()
            logger.error("Error in COSECBioAttendanceThread: ", error)

    def stop(self):
        """Set the stop event to signal the thread to stop gracefully."""
        self._stop_event.set()


@login_required
@install_required
@permission_required("biometric.view_biometricdevices")
def biometric_devices_view(request):
    """
    Renders and filters the list of biometric devices based on query parameters.

    Handles both initial page load and HTMX-based filter/search requests.

    Template:
    - "biometric/view_biometric_devices.html"

    Context:
    - biometric_form (BiometricDeviceForm): Form for adding new biometric devices.
    - devices (QuerySet): Filtered and paginated queryset of biometric devices.
    - f (BiometricDeviceFilter): Filter form.
    - pd (str): URL-encoded query params for HTMX push.
    - filter_dict (dict): Parsed filter query params.
    """
    previous_data = request.GET.urlencode()
    is_active = request.GET.get("is_active")

    # Apply filters
    filter_form = BiometricDeviceFilter(request.GET)
    biometric_devices = filter_form.qs.order_by("-created_at")

    # Default to is_active=True if not specified or "unknown"
    if not is_active or is_active == "unknown":
        biometric_devices = biometric_devices.filter(is_active=True)

    # Paginate
    biometric_devices = paginator_qry(biometric_devices, request.GET.get("page"))

    # Parse filters for reuse
    data_dict = parse_qs(previous_data)
    get_key_instances(BiometricDevices, data_dict)

    # Render
    return render(
        request,
        "biometric/view_biometric_devices.html",
        {
            "biometric_form": BiometricDeviceForm(),
            "devices": biometric_devices,
            "f": filter_form,
            "pd": previous_data,
            "filter_dict": data_dict,
        },
    )


@login_required
@install_required
@permission_required("biometric.change_biometricdevices")
def biometric_device_schedule(request, device_id):
    """
    Handles scheduling of attendance capture from a biometric device.

    Parameters:
    - request (HttpRequest): The HTTP request object.
    - device_id (uuid): The ID of the biometric device for which scheduling is being done.

    Returns:
    - HttpResponse: HTML response indicating success or failure of the scheduling operation.
    """
    device = BiometricDevices.objects.get(id=device_id)
    initial_data = {"scheduler_duration": device.scheduler_duration}
    scheduler_form = BiometricDeviceSchedulerForm(initial=initial_data)
    context = {
        "scheduler_form": scheduler_form,
        "device_id": device_id,
    }
    if request.method == "POST":
        scheduler_form = BiometricDeviceSchedulerForm(request.POST)
        if scheduler_form.is_valid():
            duration = scheduler_form.cleaned_data["scheduler_duration"]
            if device.machine_type == "zk":
                # Scheduling must not depend on a live connection. The K60
                # keeps attendance logs locally, and the background scheduler
                # will connect and fetch them when network access is available.
                # This also prevents a harmless device command/connection
                # failure from blocking scheduler activation.
                device.scheduler_duration = duration
                device.is_scheduler = True
                device.is_live = False
                device.save(
                    update_fields=[
                        "scheduler_duration",
                        "is_scheduler",
                        "is_live",
                    ]
                )

                # Do not start a BackgroundScheduler from this HTTP request.
                # The registered global Horilla scheduler polls scheduled
                # biometric devices independently.
                return HorillaRedirect(request)
            elif device.machine_type == "anviz":
                device.is_scheduler = True
                device.scheduler_duration = duration
                device.save()
                scheduler = BackgroundScheduler()
                scheduler.add_job(
                    lambda: anviz_biometric_attendance_scheduler(device.id),
                    "interval",
                    seconds=str_time_seconds(device.scheduler_duration),
                )
                scheduler.start()
                return HorillaRedirect(request)
            elif device.machine_type == "dahua":
                device.is_scheduler = True
                device.is_live = False
                device.scheduler_duration = duration
                device.save()
                scheduler = BackgroundScheduler()
                scheduler.add_job(
                    lambda: dahua_biometric_attendance_scheduler(device.id),
                    "interval",
                    seconds=str_time_seconds(device.scheduler_duration),
                )
                scheduler.start()
                return HorillaRedirect(request)
            elif device.machine_type == "cosec":
                device.is_scheduler = True
                device.is_live = False
                device.scheduler_duration = duration
                device.save()
                scheduler = BackgroundScheduler()
                existing_thread = _LIVE_BIO_THREADS.get(device.id)
                if existing_thread:
                    existing_thread.stop()
                    del _LIVE_BIO_THREADS[device.id]
                scheduler.add_job(
                    lambda: cosec_biometric_attendance_scheduler(device.id),
                    "interval",
                    seconds=str_time_seconds(device.scheduler_duration),
                )
                scheduler.start()
                return HorillaRedirect(request)
            elif device.machine_type == "etimeoffice":
                device.is_scheduler = True
                device.is_live = False
                device.scheduler_duration = duration
                device.save()
                scheduler = BackgroundScheduler()
                scheduler.add_job(
                    lambda: etimeoffice_biometric_attendance_scheduler(device.id),
                    "interval",
                    seconds=str_time_seconds(device.scheduler_duration),
                )
                scheduler.start()
                return HorillaRedirect(request)
            else:
                return HorillaRedirect(request)

        context["scheduler_form"] = scheduler_form
        response = render(request, "biometric/scheduler_device_form.html", context)
        return HttpResponse(
            response.content.decode("utf-8")
            + "<script>$('#BiometricDeviceTestModal').removeClass('oh-modal--show');\
            $('#BiometricDeviceModal').toggleClass('oh-modal--show');</script>"
        )
    return render(request, "biometric/scheduler_device_form.html", context)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.change_biometricdevices")
def biometric_device_unschedule(request, device_id):
    """
    Handles unschedule of attendance capture for a biometric device.

    Parameters:
    - request (HttpRequest): The HTTP request object.
    - device_id (uuid): The ID of the biometric device for which unscheduling is being done.

    Returns:
    - HttpResponseRedirect: Redirects to the biometric devices view after unscheduling.
    """
    previous_data = request.GET.urlencode()
    device = BiometricDevices.objects.get(id=device_id)
    device.is_scheduler = False
    device.save()
    messages.success(request, _("Biometric device unscheduled successfully"))
    return redirect(f"/biometric/view-biometric-devices/?{previous_data}")


@login_required
@install_required
@hx_request_required
@permission_required("biometric.add_biometricdevices")
def biometric_device_add(request):
    """
    Handles the addition of a new biometric device.

    Parameters:
    - request (HttpRequest): The HTTP request object containing data about the request.

    Returns:
    - HttpResponse: Renders the 'add_biometric_device.html' template with the biometric device form.
    """
    previous_data = unquote(request.GET.urlencode())
    previous_data = (
        previous_data[3:] if previous_data.startswith("pd=") else previous_data
    )
    biometric_form = BiometricDeviceForm()
    if request.method == "POST":
        biometric_form = BiometricDeviceForm(request.POST)
        if biometric_form.is_valid():
            biometric_form.save()
            messages.success(request, _("Biometric device added successfully."))
            biometric_form = BiometricDeviceForm()
    context = {"biometric_form": biometric_form, "pd": previous_data}
    return render(request, "biometric/biometric_device_form.html", context)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.change_biometricdevices")
def biometric_device_edit(request, device_id):
    """
    Handles the editing of an existing biometric device.

    Parameters:
    - request (HttpRequest): The HTTP request object containing data about the request.
    - device_id (uuid): The ID of the biometric device to be edited.

    Returns:
    - HttpResponse: Renders the 'edit_biometric_device.html' template with the biometric
                    device form pre-filled with existing data.
    """
    device = BiometricDevices.find(device_id)
    if not device:
        messages.error(request, _("Biometric device not found."))
        return render(request, "biometric/biometric_device_form.html")
    biometric_form = BiometricDeviceForm(instance=device)
    if request.method == "POST":
        biometric_form = BiometricDeviceForm(request.POST, instance=device)
        if biometric_form.is_valid():
            biometric_form.save()
            messages.success(request, _("Biometric device updated successfully."))
    context = {
        "biometric_form": biometric_form,
        "device_id": device_id,
    }
    return render(request, "biometric/biometric_device_form.html", context)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.change_biometricdevices")
def biometric_device_archive(request, device_id):
    """
    This method is used to archive or un-archive devices
    """
    previous_data = request.GET.urlencode()
    device_obj = BiometricDevices.find(device_id)
    if not device_obj:
        messages.error(request, _("Biometric device not found."))
        return redirect(f"/biometric/view-biometric-devices/?{previous_data}")
    device_obj.is_active = not device_obj.is_active
    device_obj.save()
    message = _("archived") if not device_obj.is_active else _("un-archived")
    messages.success(request, _("Device is %(message)s") % {"message": message})
    return redirect(f"/biometric/view-biometric-devices/?{previous_data}")


@login_required
@install_required
@hx_request_required
@permission_required("biometric.delete_biometricdevices")
def biometric_device_delete(request, device_id):
    """
    Handles the deletion of a biometric device.

    Parameters:
    - request (HttpRequest): The HTTP request object containing data about the request.
    - device_id (uuid): The ID of the biometric device to be deleted.

    Returns:
    - HttpResponseRedirect: Redirects to the 'view-biometric-devices' page after deleting the
                            biometric device.

    """
    previous_data = request.GET.urlencode()
    device_obj = BiometricDevices.find(device_id)
    if not device_obj:
        messages.error(request, _("Biometric device not found."))
        return redirect(f"/biometric/view-biometric-devices/?{previous_data}")
    device_obj.delete()
    messages.success(request, _("Biometric device deleted successfully."))
    return redirect(f"/biometric/view-biometric-devices/?{previous_data}")


def render_biometric_fetch_response(title, message, icon):
    """
    Return an HTMX event for the Fetch Logs result.

    The loading modal is opened before the device request starts. Once the
    request finishes, the HX-Trigger event lets the page close the loading
    modal and show the final result in a separate SweetAlert popup.
    """
    response = HttpResponse("")
    response["HX-Trigger"] = json.dumps(
        {
            "biometricFetchComplete": {
                "title": str(title),
                "message": str(message),
                "icon": icon,
            }
        }
    )
    return response


def render_connection_response(title, text, icon):
    """
    Helper function to render the connection
    response from device test connection.
    """
    context = {
        "title": title,
        "text": text,
        "icon": icon,
    }
    return render_to_string("biometric/test_connection_script.html", context)


def zk_employees_fetch(device):
    """
    Fetch users from a ZKTeco/eSSL device and enrich them with Horilla
    employee mappings.

    The device user_id is the biometric identifier used by attendance logs.
    Local BiometricEmployees records are matched by device + user_id.
    Unmapped device users are still returned so administrators can see them
    and map/add them from Horilla.
    """
    zk_device = ZK(
        device.machine_ip,
        port=device.port,
        timeout=60,
        password=int(device.zk_password),
        force_udp=False,
        ommit_ping=True,
    )
    conn = None

    try:
        conn = zk_device.connect()
        conn.enable_device()
        device_users = conn.get_users() or []

        mappings = {
            str(mapping.user_id): mapping
            for mapping in BiometricEmployees.objects.filter(
                device_id=device
            ).select_related(
                "employee_id",
                "employee_id__employee_work_info",
            )
        }

        employees = []
        for user in device_users:
            user_id = str(getattr(user, "user_id", "") or "").strip()
            mapping = mappings.get(user_id)
            employee = mapping.employee_id if mapping else None

            work_info = (
                getattr(employee, "employee_work_info", None)
                if employee
                else None
            )
            job_position = getattr(
                getattr(work_info, "job_position_id", None),
                "job_position",
                None,
            )

            employees.append(
                {
                    "uid": getattr(user, "uid", None),
                    "user_id": user_id,
                    "employee": (
                        employee.get_full_name()
                        if employee
                        else _("Not mapped")
                    ),
                    "badge_id": getattr(employee, "badge_id", "") if employee else "",
                    "finger": getattr(user, "finger", []) or [],
                    "work_email": (
                        employee.get_mail()
                        if employee
                        else ""
                    ),
                    "phone": getattr(employee, "phone", "") if employee else "",
                    "job_position": str(job_position or ""),
                }
            )

        return employees
    finally:
        if conn is not None:
            try:
                conn.disable_device()
            except Exception:
                pass
            conn.disconnect()


def test_zkteco_connection(device):
    """Test connection for ZKTeco device."""
    conn = None
    port_no = device.port
    machine_ip = device.machine_ip
    password = device.zk_password
    zk_device = ZK(
        machine_ip,
        port=port_no,
        timeout=60,
        password=int(password),
        force_udp=False,
        ommit_ping=True,
    )
    try:
        conn = zk_device.connect()
        # A successful ZKTeco socket connection is enough for the device test.
        # Do not call device voice APIs or employee-fetch helpers here because
        # either can fail independently of the actual attendance connection.
        conn.get_users()
        return render_connection_response(
            _("Connection Successful"),
            _("ZKTeco test connection successful."),
            "success",
        )
    except zk_exception.ZKErrorResponse:
        return render_connection_response(
            _("Authentication Error"),
            _("Double-check the provided IP, Port, and Password."),
            "warning",
        )
    except Exception:
        logger.error("ZKTeco connection error", exc_info=True)
        return render_connection_response(
            _("Connection unsuccessful"),
            _("Please check the IP, Port, and Password."),
            "warning",
        )
    finally:
        if conn is not None:
            conn.disconnect()


def test_anviz_connection(device):
    """Test connection for Anviz device."""

    try:
        from .anviz import CrossChexCloudAPI

        anviz = CrossChexCloudAPI(
            api_url=device.api_url,
            api_key=device.api_key,
            api_secret=device.api_secret,
            anviz_request_id=device.anviz_request_id,
        )
        test_response = anviz.test_connection()

        if test_response.get("token"):
            device.api_token = test_response.get("token")
            device.api_expires = test_response.get("expires")
            device.save()
            records = anviz.get_attendance_records()

            return render_connection_response(
                _("Connection Successful"),
                _("Anviz test connection successful."),
                "success",
            )
        else:
            return render_connection_response(
                _("Connection unsuccessful"),
                _("API credentials might be incorrect."),
                "warning",
            )
    except Exception as error:
        logger.error("Anviz connection error", exc_info=True)
        return render_connection_response(
            _("Connection unsuccessful"), _("API request failed."), "warning"
        )


def test_cosec_connection(device):
    """Test connection for COSEC device."""
    cosec = COSECBiometric(
        device.machine_ip,
        device.port,
        device.bio_username,
        device.bio_password,
        timeout=10,
    )
    response = cosec.basic_config()
    if response.get("app"):
        find_employees_in_cosec(device.id)
        return render_connection_response(
            _("Connection Successful"),
            _("Matrix test connection successful."),
            "success",
        )
    else:
        return render_connection_response(
            _("Connection unsuccessful"),
            _("Double-check the provided Machine IP, Username, and Password."),
            "warning",
        )


def test_dahua_connection(device):
    """Test connection for Dahua device."""
    dahua = DahuaAPI(
        ip=device.machine_ip,
        username=device.bio_username,
        password=device.bio_password,
    )
    response = dahua.get_system_info()
    if response.get("status_code") == 200:
        return render_connection_response(
            _("Connection Successful"),
            _("Dahua test connection successful."),
            "success",
        )
    else:
        return render_connection_response(
            _("Connection unsuccessful"),
            _("Double-check the provided Machine IP, Username, and Password."),
            "warning",
        )


def test_etimeoffice_connection(device):
    """Test connection for e-TimeOffice device."""
    now = datetime.now()
    etimeoffice = ETimeOfficeAPI(
        username=device.bio_username,
        password=device.bio_password,
    )

    from_date = f"{now.day:02d}/{now.month:02d}/{now.year}_00:00"
    to_date = (
        f"{now.day:02d}/{now.month:02d}/{now.year}_{now.hour:02d}:{now.minute:02d}"
    )

    try:
        response = etimeoffice.download_punch_data(from_date=from_date, to_date=to_date)
        if response.get("Msg") == "Success":
            return render_connection_response(
                _("Connection Successful"),
                _("e-Time Office test connection successful."),
                "success",
            )

        error_msg = response.get("Message")
        return render_connection_response(
            _("Connection unsuccessful"),
            _("Double-check the provided API Url, Username, and Password: {}").format(
                error_msg
            ),
            "warning",
        )

    except Exception as e:
        return render_connection_response(
            _("Connection error"),
            _(f"API request failed with exception: {str(e)}"),
            "danger",
        )


@login_required
@install_required
@hx_request_required
@permission_required("biometric.view_biometricdevices")
def biometric_device_test(request, device_id):
    """
    Test the connection with the specified biometric device.
    """

    # Retrieve device and validate
    device = BiometricDevices.objects.filter(id=device_id).first()
    if not device:
        return HttpResponse("Device not found.", status=404)

    script = ""
    try:
        if device.machine_type == "zk":
            script = test_zkteco_connection(device)
        elif device.machine_type == "anviz":
            script = test_anviz_connection(device)
        elif device.machine_type == "cosec":
            script = test_cosec_connection(device)
        elif device.machine_type == "dahua":
            script = test_dahua_connection(device)
        elif device.machine_type == "etimeoffice":
            script = test_etimeoffice_connection(device)
        else:
            script = render_connection_response(
                "Connection unsuccessful",
                "Please select a valid biometric device.",
                "warning",
            )
    except Exception as error:
        logger.error("Error in biometric_device_test", exc_info=True)
        script = render_connection_response(
            "Connection unsuccessful", "An unexpected error occurred.", "warning"
        )

    return HttpResponse(script)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.view_biometricdevices")
def biometric_device_bulk_fetch_logs(request):
    script = ""
    zk_ids = request.GET.getlist("selected_device_ids")
    zk_devices = BiometricDevices.objects.filter(id__in=zk_ids, machine_type="zk")

    if not zk_devices:
        messages.error(request, "")
        script = render_connection_response(
            _("Biometric device not supported."),
            _(
                "Bulk log fetching is currently available only for ZKTeco / eSSL devices. Support for other biometric systems will be added soon."
            ),
            "warning",
        )
        return HttpResponse(script)

    (
        fetched_count,
        raw_saved_count,
        attendance_used_count,
        raw_only_count,
        error_message,
    ) = zk_biometric_attendance_logs(zk_devices)

    if error_message and "Authentication" in error_message:
        script = render_connection_response(
            _("Authentication Error"),
            _("Double-check the provided IP, Port, and Password."),
            "warning",
        )
    else:
        message = _(
            "Fetched: %(fetched)s | Raw saved: %(raw)s | "
            "Used for attendance: %(used)s | Raw only: %(raw_only)s"
        ) % {
            "fetched": fetched_count,
            "raw": raw_saved_count,
            "used": attendance_used_count,
            "raw_only": raw_only_count,
        }
        if error_message:
            message = f"{message}<br><small>{error_message}</small>"
        script = render_connection_response(
            _("Logs Fetched Successfully"),
            message,
            "success" if not error_message else "warning",
        )
    return HttpResponse(script)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.view_biometricdevices")
def biometric_device_fetch_logs(request, device_id=None):
    """
    Fetch biometric attendance logs and return the final result through HTMX.

    The request itself may take time while the ZKTeco device is contacted.
    The page shows the "Trying to connect..." modal while this request is in
    progress. On completion, an HX-Trigger event opens the final result popup.
    """
    device = BiometricDevices.find(device_id)
    if not device:
        return HttpResponse("Device not found.", status=404)

    if device.machine_type == "zk":
        try:
            (
                fetched_count,
                raw_saved_count,
                attendance_used_count,
                raw_only_count,
                error_message,
            ) = zk_biometric_attendance_logs(device)

            if error_message and "Authentication" in error_message:
                return render_biometric_fetch_response(
                    _("Authentication Error"),
                    _("Double-check the provided IP, Port, and Password."),
                    "warning",
                )

            message = _(
                "Fetched: %(fetched)s | Raw saved: %(raw)s | "
                "Used for attendance: %(used)s | Raw only: %(raw_only)s"
            ) % {
                "fetched": fetched_count,
                "raw": raw_saved_count,
                "used": attendance_used_count,
                "raw_only": raw_only_count,
            }

            if error_message:
                message = f"{message} | {error_message}"

            return render_biometric_fetch_response(
                _("Logs Fetched Successfully"),
                message,
                "success" if not error_message else "warning",
            )

        except Exception as error:
            logger.exception(
                "[Device: %s] Manual ZKTeco fetch failed",
                getattr(device, "name", device_id),
            )
            return render_biometric_fetch_response(
                _("Fetch Failed"),
                _("ZKTeco fetch failed: %(error)s") % {"error": str(error)},
                "error",
            )

    elif device.machine_type == "anviz":
        attendance_count = anviz_biometric_attendance_logs(device)
        if isinstance(attendance_count, int):
            return render_biometric_fetch_response(
                _("Logs Fetched Successfully"),
                _(
                    "Biometric attendance logs fetched successfully. "
                    "Total records: %(count)s"
                ) % {"count": attendance_count},
                "success",
            )
        return render_biometric_fetch_response(
            _("Connection unsuccessful"),
            _("API credentials might be incorrect."),
            "warning",
        )

    elif device.machine_type == "cosec":
        attendance_count = cosec_biometric_attendance_logs(device)
        if isinstance(attendance_count, int):
            return render_biometric_fetch_response(
                _("Logs Fetched Successfully"),
                _(
                    "Biometric attendance logs fetched successfully. "
                    "Total records: %(count)s"
                ) % {"count": attendance_count},
                "success",
            )
        return render_biometric_fetch_response(
            _("Connection unsuccessful"),
            _("Double-check the provided Machine IP, Username, and Password."),
            "warning",
        )

    elif device.machine_type == "dahua":
        attendance_count = dahua_biometric_attendance_logs(device)
        if isinstance(attendance_count, int):
            return render_biometric_fetch_response(
                _("Logs Fetched Successfully"),
                _(
                    "Biometric attendance logs fetched successfully. "
                    "Total records: %(count)s"
                ) % {"count": attendance_count},
                "success",
            )
        return render_biometric_fetch_response(
            _("Connection unsuccessful"),
            _("Double-check the provided Machine IP, Username, and Password."),
            "warning",
        )

    elif device.machine_type == "etimeoffice":
        attendance_count = etimeoffice_biometric_attendance_logs(device)
        if isinstance(attendance_count, int):
            return render_biometric_fetch_response(
                _("Logs Fetched Successfully"),
                _(
                    "Biometric attendance logs fetched successfully. "
                    "Total records: %(count)s"
                ) % {"count": attendance_count},
                "success",
            )
        return render_biometric_fetch_response(
            _("Connection unsuccessful"),
            _("Double-check the provided API Url, Username, and Password"),
            "warning",
        )

    return render_biometric_fetch_response(
        _("Connection unsuccessful"),
        _("Please select a valid biometric device."),
        "warning",
    )


@login_required
@install_required
@permission_required("biometric.view_biometricemployees")
def biometric_device_employees(request, device_id, **kwargs):
    """
    View function to display employees associated with a biometric device.

    Depending on the machine type of the biometric device (either "zk" or "cosec"),
    this function fetches the relevant employees and renders the appropriate template.

    Args:
        request (HttpRequest): The HTTP request object.
        device_id (uuid): The ID of the biometric device.
        **kwargs: Additional keyword arguments.

    Returns:
        HttpResponse: The rendered template response or a redirect to `biometric_devices_view`
                      in case of an error.
    """
    previous_data = request.GET.urlencode()
    device = BiometricDevices.find(device_id)
    if device:
        try:
            if device.machine_type == "zk":
                employee_add_form = EmployeeBiometricAddForm()
                employees = zk_employees_fetch(device)
                employees = paginator_qry(employees, request.GET.get("page"))
                context = {
                    "employees": employees,
                    "device_id": device_id,
                    "device": device,
                    "form": employee_add_form,
                    "pd": previous_data,
                }
                return render(
                    request, "biometric/view_employees_biometric.html", context
                )
            if device.machine_type == "cosec":
                employee_add_form = CosecUserAddForm()
                employees = cosec_employee_fetch(device_id)
                employees = biometric_paginator_qry(
                    employees, int(request.GET.get("page", 1))
                )
                context = {
                    "employees": employees,
                    "device_id": device.id,
                    "form": employee_add_form,
                    "pd": previous_data,
                }
                return render(request, "biometric/view_cosec_employees.html", context)
            if device.machine_type == "dahua":
                employees = BiometricEmployees.objects.filter(device_id=device_id)
                context = {
                    "device_id": device.id,
                    "employees": employees,
                }
                return render(
                    request, "biometric_users/dahua/view_dahua_employees.html", context
                )
            if device.machine_type == "etimeoffice":
                employees = BiometricEmployees.objects.filter(device_id=device_id)
                context = {
                    "device_id": device.id,
                    "employees": employees,
                }
                return render(
                    request,
                    "biometric_users/etimeoffice/view_etimeoffice_employees.html",
                    context,
                )
        except Exception as error:
            logger.error("An error occurred: ", error)
            messages.info(
                request,
                _(
                    "Failed to establish a connection. Please verify the accuracy of the IP\
                    Address , Port No. and Password of the device."
                ),
            )
    else:
        messages.error(request, _("Biometric device not found"))
    return redirect(biometric_devices_view)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.view_biometricemployees")
def search_employee_device(request):
    """
    View function to search for employees associated with a specific biometric device.

    This function handles searching employees based on their first name and the type of
    biometric device (either "zk" or "cosec"). It then renders the appropriate template
    with the filtered employee list.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        HttpResponse: The rendered template response with the context.
    """
    previous_data = request.GET.urlencode()
    device_id = request.GET.get("device")
    device = BiometricDevices.objects.get(id=device_id)
    search = request.GET.get("search")
    if device.machine_type == "zk":
        employees = zk_employees_fetch(device)
        if search:
            search_employees = BiometricEmployees.objects.filter(
                Q(employee_id__employee_first_name__icontains=search)
                | Q(employee_id__employee_last_name__icontains=search)
            )
            search_uids = search_employees.values_list("uid", flat=True)
            employees = [
                employee for employee in employees if employee.uid in search_uids
            ]
        employees = paginator_qry(employees, request.GET.get("page"))
        template = "biometric/list_employees_biometric.html"
        context = {
            "employees": employees,
            "device_id": device_id,
            "pd": previous_data,
        }
    elif device.machine_type == "dahua" or device.machine_type == "etimeoffice":
        search_employees = BiometricEmployees.objects.filter(device_id=device)
        if search:
            search_employees = search_employees.filter(
                Q(employee_id__employee_first_name__icontains=search)
                | Q(employee_id__employee_last_name__icontains=search)
            )
        template = (
            "biometric_users/dahua/list_dahua_employees.html"
            if device.machine_type == "dahua"
            else "biometric_users/etimeoffice/list_etimeoffice_employees.html"
        )
        context = {
            "device_id": device.id,
            "employees": search_employees,
        }

    else:
        employees = cosec_employee_fetch(device_id)
        if search:
            search_employees = BiometricEmployees.objects.filter(
                Q(employee_id__employee_first_name__icontains=search)
                | Q(employee_id__employee_last_name__icontains=search)
            )
        else:
            search_employees = BiometricEmployees.objects.filter(device_id=device)
        queryset_user_ids = [employee.user_id for employee in search_employees]
        filtered_employees = [
            employee
            for employee in employees
            if employee["user_id"] in queryset_user_ids
        ]
        filtered_employees = biometric_paginator_qry(
            filtered_employees, int(request.GET.get("page", 1))
        )
        template = "biometric/list_employees_cosec_biometric.html"
        context = {
            "employees": filtered_employees,
            "device_id": device_id,
            "pd": previous_data,
        }
    return render(request, template, context)


@login_required
@install_required
@permission_required("biometric.delete_biometricemployees")
def delete_biometric_user(request, uid, device_id):
    """
    This function connects to the specified biometric device, deletes the user
    identified by the given UID, and removes the corresponding entry from the
    BiometricEmployees table in the local database

    Args:
        request (HttpRequest): The HTTP request object.
        uid (str): The UID of the user to be deleted from the biometric device.
        device_id (uuid): The ID of the biometric device.

    Returns:
        HttpResponse: A redirect response to the list of employees for the specified
                      biometric device.
    """
    device = BiometricDevices.objects.get(id=device_id)
    zk_device = ZK(
        device.machine_ip,
        port=device.port,
        timeout=60,
        password=int(device.zk_password),
        force_udp=False,
        ommit_ping=True,
    )
    conn = zk_device.connect()
    conn.delete_user(uid=uid)
    employee_bio = BiometricEmployees.objects.filter(uid=uid).first()
    employee_bio.delete()
    messages.success(
        request,
        _("{} successfully removed from the biometric device.").format(
            employee_bio.employee_id
        ),
    )
    redirect_url = f"/biometric/biometric-device-employees/{device_id}/"
    return redirect(redirect_url)


@login_required
@install_required
@permission_required("biometric.change_biometricemployees")
def enable_cosec_face_recognition(request, user_id, device_id):
    """
    View function to enable face recognition for a user on a COSEC biometric device

    Args:
        request (HttpRequest): The HTTP request object.
        user_id (str): The ID of the user for whom face recognition is to be enabled.
        device_id (uuid): The ID of the COSEC biometric device.

    Returns:
        HttpResponse: A redirect response to the list of employees for the specified
                      biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device:
        cosec = COSECBiometric(
            device.machine_ip,
            device.port,
            device.bio_username,
            device.bio_password,
        )
        enable_fr = cosec.enable_user_face_recognition(user_id=user_id, enable_fr=True)
        response_code = enable_fr.get("Response-Code")
        if response_code == "0":
            messages.success(request, _("Face recognition enabled successfully"))
        else:
            messages.error(request, _("Something went wrong when enabling face"))
    else:
        messages.error(request, _("Device not found"))
    return redirect(f"/biometric/biometric-device-employees/{device_id}/")


@login_required
@install_required
@hx_request_required
@permission_required("biometric.change_biometricemployees")
def edit_cosec_user(request, user_id, device_id):
    """
    View function to edit the details of a COSEC biometric user.

    Args:
        request (HttpRequest): The HTTP request object.
        user_id (str): The ID of the user to be edited.
        device_id (uuid): The ID of the COSEC biometric device.

    Returns:
        HttpResponse: The rendered form template for GET requests, and a response with
                      a success message for valid POST requests. Reloads the page after
                      successful update.

    """
    device = BiometricDevices.objects.get(id=device_id)
    cosec = COSECBiometric(
        device.machine_ip,
        device.port,
        device.bio_username,
        device.bio_password,
    )
    user = cosec.get_cosec_user(user_id)
    if user.get("name"):
        year = int(user["validity-date-yyyy"])
        month = int(user["validity-date-mm"])
        day = int(user["validity-date-dd"])
        date_object = datetime(year, month, day)
        formatted_date = date_object.strftime("%Y-%m-%d")
        initial_data = {
            "name": user["name"],
            "user_active": bool(int(user["user-active"])),
            "vip": bool(int(user["vip"])),
            "validity_enable": bool(int(user["validity-enable"])),
            "validity_end_date": formatted_date,
        }

        if "by-pass-finger" in user:
            initial_data["by_pass_finger"] = bool(int(user["by-pass-finger"]))

        form = COSECUserForm(initial=initial_data)

        if request.method == "POST":
            form = COSECUserForm(request.POST)
            if form.is_valid():
                name = form.cleaned_data["name"]
                user_active = form.cleaned_data["user_active"]
                vip = form.cleaned_data["vip"]
                validity_enable = form.cleaned_data["validity_enable"]
                validity_end_date_str = str(form.cleaned_data["validity_end_date"])
                validity_end_date = datetime.strptime(
                    validity_end_date_str, "%Y-%m-%d"
                ).date()
                validity_year = validity_end_date.year
                validity_month = validity_end_date.month
                validity_day = validity_end_date.day
                by_pass_finger = form.cleaned_data["by_pass_finger"]
                update_user = cosec.set_cosec_user(
                    user_id=user["user-id"],
                    ref_user_id=user["ref-user-id"],
                    name=name,
                    user_active=int(user_active),
                    vip=int(vip),
                    by_pass_finger=int(by_pass_finger),
                    validity_enable=int(validity_enable),
                    validity_date_dd=validity_day,
                    validity_date_mm=validity_month,
                    validity_date_yyyy=validity_year,
                )
                if (
                    update_user.get("Response-Code")
                    and update_user.get("Response-Code") == "0"
                ):
                    messages.success(
                        request, _("Biometric user data updated successfully")
                    )
                    return HorillaRedirect(request)
                if update_user.get("error"):
                    error = update_user.get("error")
                    if "validity-date-yyyy" in error:
                        form.add_error(
                            None,
                            _(
                                "This date cannot be used as the Validity End Date for\
                                the COSEC Biometric."
                            ),
                        )
        return render(
            request,
            "biometric/edit_cosec_user.html",
            context={"form": form, "user_id": user_id, "device_id": device_id},
        )


@login_required
@install_required
@permission_required("biometric.delete_biometricemployees")
def delete_horilla_cosec_user(request, user_id, device_id):
    """
    View function to delete a user from a COSEC biometric device and database.

    Args:
        request (HttpRequest): The HTTP request object.
        user_id (str): The ID of the user to be deleted from the COSEC biometric device.
        device_id (uuid): The ID of the COSEC biometric device.

    Returns:
        HttpResponse: A redirect response to the list of employees for the specified
                      biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device:
        employee_bio = BiometricEmployees.objects.filter(
            user_id=user_id, device_id=device
        ).first()
        cosec = COSECBiometric(
            device.machine_ip,
            device.port,
            device.bio_username,
            device.bio_password,
        )
        response = cosec.delete_cosec_user(user_id)
        if response.get("Response-Code") and response.get("Response-Code") == "0":
            employee_bio.delete()
            messages.success(
                request,
                _("{} successfully removed from the biometric device.").format(
                    employee_bio.employee_id
                ),
            )
        else:
            messages.error(request, _("Biometric user not found"))
    else:
        messages.error(request, _("Biometric device not found"))
    redirect_url = (
        f"/biometric/biometric-device-employees/{device_id}/"
        if device
        else "/biometric/view-biometric-devices/"
    )
    return redirect(redirect_url)


@login_required
@install_required
@permission_required("biometric.delete_biometricemployees")
def bio_users_bulk_delete(request):
    """
    View function to delete multiple users from a ZK biometric device and the local database.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        JsonResponse: A JSON response indicating the success of the bulk delete operation.

    """
    conn = None
    json_ids = request.POST["ids"]
    device_id = request.POST["deviceId"]
    ids = json.loads(json_ids)
    device = BiometricDevices.objects.get(id=device_id)
    try:
        zk_device = ZK(
            device.machine_ip,
            port=device.port,
            timeout=60,
            password=int(device.zk_password),
            force_udp=False,
            ommit_ping=True,
        )
        conn = zk_device.connect()
        for user_id in ids:
            user_id = int(user_id)
            conn.delete_user(user_id=user_id)
            employee_bio = BiometricEmployees.objects.filter(user_id=user_id).first()
            employee_bio.delete()
            conn.refresh_data()
            messages.success(
                request,
                _("{} successfully removed from the biometric device.").format(
                    employee_bio.employee_id
                ),
            )
    except Exception as error:
        logger.error("An error occurred: ", error)
    return JsonResponse({"messages": "Success"})


@login_required
@install_required
@permission_required("biometric.delete_biometricemployees")
def cosec_users_bulk_delete(request):
    """
    View function to delete multiple users from a COSEC biometric device and database.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        JsonResponse: A JSON response indicating the success of the bulk delete operation.
    """
    json_ids = request.POST["ids"]
    device_id = request.POST["deviceId"]
    ids = json.loads(json_ids)
    device = BiometricDevices.objects.get(id=device_id)
    try:
        cosec = COSECBiometric(
            device.machine_ip,
            device.port,
            device.bio_username,
            device.bio_password,
        )
        for user_id in ids:
            cosec.delete_cosec_user(user_id=user_id)
            employee_bio = BiometricEmployees.objects.filter(
                user_id=user_id, device_id=device
            ).first()
            if employee_bio:
                employee_bio.delete()
            messages.success(
                request,
                f"{employee_bio.employee_id} "
                + _("successfully removed from the biometric device."),
            )

    except Exception as error:
        logger.error("An error occurred: ", error)
    return JsonResponse({"messages": "Success"})


@login_required
@install_required
@hx_request_required
@permission_required("biometric.add_biometricemployees")
def add_biometric_user(request, device_id):
    """
    View function to add a new user to a biometric device.

    This function adds a new user to the specified biometric device and stores their
    information in the database.

    Args:
        request (HttpRequest): The HTTP request object.
        device_id (uuid): The ID of the biometric device.

    Returns:
        HttpResponse: A JavaScript script to reload the current page after adding the user.

    """
    device = BiometricDevices.objects.get(id=device_id)
    employee_add_form = (
        EmployeeBiometricAddForm()
        if device.machine_type == "zk"
        else CosecUserAddForm()
    )
    if request.method == "POST":
        device = BiometricDevices.objects.get(id=device_id)
        try:
            if device.machine_type == "zk":
                zk_device = ZK(
                    device.machine_ip,
                    port=device.port,
                    timeout=60,
                    password=int(device.zk_password),
                    force_udp=False,
                    ommit_ping=True,
                )
                conn = zk_device.connect()
                conn.enable_device()
                # ZKTeco attendance records identify employees by the
                # device user_id. For this Horilla setup, that ID must always
                # match the employee's badge_id. Reuse an existing device user
                # when the badge_id is already enrolled; otherwise create the
                # device user with the badge_id instead of generating 1000+ IDs.
                device_users = conn.get_users()
                device_users_by_user_id = {
                    str(user.user_id): user for user in device_users
                }
                existing_uids = {user.uid for user in device_users}
                employee_ids = request.POST.getlist("employee_ids")

                for obj_id in employee_ids:
                    employee = Employee.objects.get(id=obj_id)
                    existing_biometric_employee = BiometricEmployees.objects.filter(
                        employee_id=employee, device_id=device
                    ).first()

                    if existing_biometric_employee is not None:
                        messages.info(
                            request,
                            _("{} already added to biometric device").format(employee),
                        )
                        continue

                    badge_id = str(employee.badge_id or "").strip()
                    if not badge_id:
                        messages.error(
                            request,
                            _(
                                "{} cannot be added because Badge ID is empty. "
                                "Set the Badge ID first."
                            ).format(employee),
                        )
                        continue

                    if not badge_id.isdigit():
                        messages.error(
                            request,
                            _(
                                "{} cannot be added because Badge ID must be numeric "
                                "for ZKTeco."
                            ).format(employee),
                        )
                        continue

                    existing_device_user = device_users_by_user_id.get(badge_id)
                    existing_mapping = BiometricEmployees.objects.filter(
                        device_id=device, user_id=badge_id
                    ).first()

                    if existing_mapping and existing_mapping.employee_id_id != employee.id:
                        messages.error(
                            request,
                            _(
                                "Badge ID {} is already mapped to {} on this device."
                            ).format(badge_id, existing_mapping.employee_id),
                        )
                        continue

                    if existing_device_user is not None:
                        # The employee is already enrolled on the K60 with the
                        # correct badge/user ID. Do not create another biometric
                        # user; simply map the existing device user to Horilla.
                        BiometricEmployees.objects.create(
                            uid=existing_device_user.uid,
                            user_id=badge_id,
                            employee_id=employee,
                            device_id=device,
                        )
                        messages.success(
                            request,
                            _(
                                "{} mapped to existing biometric user {} successfully"
                            ).format(employee, badge_id),
                        )
                        continue

                    # Badge ID is not on the device yet, so create the K60 user
                    # using the same ID as the employee badge.
                    uid = 1
                    while uid in existing_uids:
                        uid += 1

                    employee_name = employee.get_full_name()
                    conn.set_user(
                        uid=uid,
                        name=employee_name,
                        password="",
                        group_id="",
                        user_id=badge_id,
                        card=0,
                    )
                    existing_uids.add(uid)
                    device_users_by_user_id[badge_id] = type(
                        "ZKUserRef",
                        (),
                        {"uid": uid, "user_id": badge_id},
                    )()

                    BiometricEmployees.objects.create(
                        uid=uid,
                        user_id=badge_id,
                        employee_id=employee,
                        device_id=device,
                    )
                    messages.success(
                        request,
                        _("{} added to biometric device successfully").format(employee),
                    )
            else:
                cosec = COSECBiometric(
                    device.machine_ip,
                    device.port,
                    device.bio_username,
                    device.bio_password,
                )
                basic = cosec.basic_config()
                if basic.get("app"):
                    employee_ids = request.POST.getlist("employee_ids")
                    cosec_users = BiometricEmployees.objects.filter(device_id=device_id)
                    existing_ref_user_ids = list(
                        cosec_users.values_list("ref_user_id", flat=True)
                    )
                    for obj_id in employee_ids:
                        employee = Employee.objects.get(id=obj_id)
                        employee_name = employee.get_full_name()
                        user_id = employee.badge_id
                        ref_user_id = 100
                        while ref_user_id in existing_ref_user_ids:
                            ref_user_id += 1
                        existing_ref_user_ids.append(ref_user_id)
                        user = cosec.set_cosec_user(
                            user_id=user_id,
                            ref_user_id=ref_user_id,
                            name=employee_name,
                            user_active=True,
                            validity_enable=True,
                            validity_date_dd=1,
                            validity_date_mm=1,
                            validity_date_yyyy=2035,
                        )
                        response = user.get("Response-Code")
                        if response and response == "0":
                            BiometricEmployees.objects.create(
                                ref_user_id=ref_user_id,
                                user_id=user_id,
                                employee_id=employee,
                                device_id=device,
                            )
        except Exception as error:
            if device.machine_type == "zk":
                conn.disable_device()
                logger.error("An error occurred: ", str(error))
        return HorillaRedirect(request)
    return render(
        request,
        "biometric/add_biometric_user.html",
        context={"form": employee_add_form, "device_id": device_id},
    )


@login_required
@install_required
@hx_request_required
def map_biometric_users(request, device_id):
    """
    Maps an horilla employee to a biometric user on a specified biometric device.
    """
    device = BiometricDevices.find(device_id)
    form = MapBioUsers(request.POST or None)
    template = "biometric_users/dahua/map_dahua_users.html"

    if device.machine_type == "etimeoffice":
        template = "biometric_users/etimeoffice/map_etimeoffice_users.html"
        form.fields["user_id"].label = _("Emp Code")

    if request.method == "POST" and form.is_valid():
        user_id = form.cleaned_data["user_id"]
        employee = form.cleaned_data["employee_id"]
        if device and employee:
            BiometricEmployees.objects.create(
                user_id=user_id, employee_id=employee, device_id=device
            )

            messages.success(
                request,
                _("Selected employee successfully mapped to the biometric user"),
            )
            form = MapBioUsers()

            if device.machine_type == "etimeoffice":
                form.fields["user_id"].label = _("Emp Code")

    return render(
        request,
        template,
        {"form": form, "device_id": device_id},
    )


@login_required
@install_required
@hx_request_required
def add_dahua_biometric_user(request, device_id):
    """
    Adds a new employee to a Dahua biometric device.

    This view handles the process of adding an employee as a user to a Dahua biometric device.
    """
    device = BiometricDevices.find(device_id)
    form = DahuaUserForm()
    if request.method == "POST":
        form = DahuaUserForm(request.POST)
        if form.is_valid():
            employee_id = form.cleaned_data["employee"]
            card_no = form.cleaned_data["card_no"]
            user_id = form.cleaned_data["user_id"]
            card_status = form.cleaned_data["card_status"]
            card_type = form.cleaned_data["card_type"]
            password = form.cleaned_data["password"]
            valid_date_end = form.cleaned_data["valid_date_end"]

            try:
                employee = Employee.objects.get(id=employee_id) if employee_id else None
            except Employee.DoesNotExist:
                messages.error(request, _("Employee not found."))
                return render(
                    request,
                    "biometric_users/dahua/add_dahua_user.html",
                    {"form": form, "device_id": device_id},
                )

            dahua = DahuaAPI(
                ip=device.machine_ip,
                username=device.bio_username,
                password=device.bio_password,
            )

            response = dahua.enroll_new_user(
                card_name=employee.get_full_name() if employee else "",
                card_no=card_no,
                user_id=user_id,
                card_status=card_status,
                card_type=card_type,
                password=password,
                valid_date_end=valid_date_end,
            )

            if response.get("status_code") == 200:
                BiometricEmployees.objects.create(
                    dahua_card_no=card_no,
                    user_id=user_id,
                    employee_id=employee,
                    device_id=device,
                )
                messages.success(
                    request,
                    _("{} added to biometric device successfully").format(
                        employee.get_full_name() if employee else ""
                    ),
                )
                form = DahuaUserForm()
            else:
                messages.error(request, _("Failed to add user to biometric device."))
    context = {"form": form, "device_id": device_id}
    return render(request, "biometric_users/dahua/add_dahua_user.html", context)


@login_required
@hx_request_required
@install_required
def find_employee_badge_id(request):
    """
    Retrieves the badge ID of an employee based on their employee ID.
    """
    employee_id = request.GET.get("employee")
    user_id = Employee.objects.get(id=employee_id).badge_id if employee_id else ""
    input_field = f"""
    <input type="text" name="user_id" maxlength="50" class="oh-input w-100"
           placeholder="User ID" required="" id="id_user_id" value="{user_id}">
    """
    return HttpResponse(input_field)


@login_required
@hx_request_required
@install_required
def delete_dahua_user(request, obj_id=None):
    """
    Deletes a Dahua biometric user or multiple users from a device.
    """
    script = "<script>window.location.reload();</script>"
    try:
        if request.method == "POST" and obj_id:
            user = BiometricEmployees.objects.get(id=obj_id)
            user.delete()
            messages.success(
                request, _("{} successfully deleted!").format(user.employee_id)
            )
            script = "<script>reloadMessage();</script>"
        if request.method == "DELETE":
            user_ids = request.GET.getlist("ids")
            device_id = request.GET.get("device_id")
            if device_id:
                # device_id is request-controlled and interpolated into
                # hand-built HTML, where autoescaping does not apply.
                script = format_html(
                    '<span hx-get="/biometric/biometric-device-employees/{}/" '
                    'hx-target="#dahuUsersList" hx-select="#dahuUsersList" '
                    'hx-trigger="load delay:200ms" hx-swap="outerHTML" '
                    'hx-on-htmx-before-request="reloadMessage();"></span>',
                    device_id,
                )
            if user_ids:
                users = BiometricEmployees.objects.filter(user_id__in=user_ids)
                if users:
                    count = users.count()
                    users.delete()
                    messages.success(
                        request, _("{} users successfully deleted!").format(count)
                    ),
                else:
                    messages.warning(
                        request,
                        _("No rows are selected for deleting users from device."),
                    )
    except Exception as e:
        messages.error(request, _("An error occurred: {}").format(str(e)))
    return HttpResponse(script)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.delete_biometricemployees")
def delete_etimeoffice_user(request, obj_id=None):
    """
    Deletes a user or multiple users from the eTimeOffice biometric system.
    """
    script = "<script>window.location.href = '/';</script>"
    if request.method == "POST":
        user = BiometricEmployees.objects.get(id=obj_id)
        device_id = user.device_id.id
        user.delete()
        messages.success(
            request, _("{} successfully deleted!").format(user.employee_id)
        )
        script = "<script>reloadMessage();</script>"
    if request.method == "DELETE":
        user_ids = request.GET.getlist("ids")
        device_id = request.GET.get("device_id")
        if device_id:
            # device_id is request-controlled and interpolated into
            # hand-built HTML, where autoescaping does not apply.
            script = format_html(
                '<span hx-get="/biometric/biometric-device-employees/{}/" '
                'hx-target="#eTimeOfficeUsersList" hx-select="#eTimeOfficeUsersList" '
                'hx-trigger="load delay:200ms" hx-swap="outerHTML" '
                'hx-on-htmx-before-request="reloadMessage();"></span>',
                device_id,
            )
        if user_ids:
            users = BiometricEmployees.objects.filter(user_id__in=user_ids)
            if users:
                count = users.count()
                users.delete()
                messages.success(
                    request, _("{} users successfully deleted!").format(count)
                ),
            else:
                messages.warning(
                    request,
                    _("No rows are selected for deleting users from device."),
                )

    return HttpResponse(script)


@login_required
@install_required
@hx_request_required
@permission_required("biometric.change_biometricdevices")
def biometric_device_live(request):
    """
    Activate or deactivate live capture mode for a biometric device based on the request parameters.

    :param request: The Django request object.
    :return: A JsonResponse containing a script to be executed on the client side.
    """
    is_live = request.GET.get("is_live")
    device_id = request.GET.get("deviceId")
    device = BiometricDevices.objects.get(id=device_id)
    is_live = is_live == "on"
    if is_live:
        port_no = device.port
        machine_ip = device.machine_ip
        password = int(device.zk_password)
        conn = None
        # create ZK instance
        try:
            if device.machine_type == "zk":
                zk_device = ZK(
                    machine_ip,
                    port=port_no,
                    timeout=60,
                    password=int(password),
                    force_udp=False,
                    ommit_ping=True,
                )
                conn = zk_device.connect()
                instance = ZKBioAttendance(machine_ip, port_no, password, device.id)
                conn.test_voice(index=14)
                if conn:
                    device.is_live = True
                    device.is_scheduler = False
                    device.save()
                    instance.start()
                    _LIVE_BIO_THREADS[device.id] = instance
            elif device.machine_type == "cosec":
                cosec = COSECBiometric(
                    device.machine_ip,
                    device.port,
                    device.bio_username,
                    device.bio_password,
                    timeout=10,
                )
                response = cosec.basic_config()
                if response.get("app"):
                    device.is_live = True
                    device.is_scheduler = False
                    device.save()
                    thread = COSECBioAttendanceThread(device.id)
                    thread.start()
                    settings.BIO_DEVICE_THREADS[device.id] = thread
                else:
                    raise TimeoutError
            else:
                pass

            script = """<script>
                    Swal.fire({
                      text: "The live capture mode has been activated successfully.",
                      icon: "success",
                      showConfirmButton: false,
                      timer: 1500,
                      timerProgressBar: true, // Show a progress bar as the timer counts down
                      didClose: () => {
                        location.reload();
                        },
                    });
                    </script>
                """
        except TimeoutError as error:
            device.is_live = False
            device.save()
            logger.error("An error comes in biometric_device_live", error)
            script = """
           <script>
                Swal.fire({
                  title : "Connection unsuccessful",
                  text: "Please double-check the accuracy of the provided IP Address and Port Number for correctness",
                  icon: "warning",
                  showConfirmButton: false,
                  timer: 3000,
                  timerProgressBar: true,
                  didClose: () => {
                    location.reload();
                    },
                });
            </script>
            """
        finally:
            if conn:
                conn.disconnect()
    else:
        device.is_live = False
        device.save()
        if device.machine_type in {"zk", "cosec"}:
            existing_thread = settings.BIO_DEVICE_THREADS.get(device.id)
            if existing_thread:
                existing_thread.stop()
                del settings.BIO_DEVICE_THREADS[device.id]

        script = """
           <script>
                Swal.fire({
                  text: "The live capture mode has been deactivated successfully.",
                  icon: "warning",
                  showConfirmButton: false,
                  timer: 3000,
                  timerProgressBar: true,
                  didClose: () => {
                    location.reload();
                    },
                });
            </script>
            """
    return HttpResponse(script)


def zk_biometric_attendance_logs(device_or_devices):
    """
    Retrieve ZKTeco attendance records and persist every mapped device punch.

    Return:
        (fetched_count, raw_saved_count, attendance_used_count, raw_only_count, error)
    """
    if hasattr(device_or_devices, "__iter__") and not isinstance(
        device_or_devices, dict
    ):
        devices = list(device_or_devices)
    else:
        devices = [device_or_devices]

    errors = []
    combined_attendances = []
    patch_direction = {"in": 0, "out": 1}

    bio_id_map = {
        (bio.device_id_id, str(bio.user_id)): bio
        for bio in BiometricEmployees.objects.filter(device_id__in=devices)
        .select_related("employee_id__employee_user_id", "employee_id__employee_work_info")
    }

    for device in devices:
        zk_device = ZK(
            device.machine_ip,
            port=device.port,
            timeout=60,
            password=int(device.zk_password),
            force_udp=False,
            ommit_ping=True,
        )
        conn = None

        try:
            conn = zk_device.connect()
            conn.enable_device()
            attendances = conn.get_attendance()

            if not attendances:
                continue

            if device.last_fetch_date and device.last_fetch_time:
                # Keep a recovery overlap. Raw rows are deduplicated by the
                # device/user/timestamp/punch-code identity below.
                cursor = datetime.combine(
                    device.last_fetch_date,
                    device.last_fetch_time,
                ) - timedelta(hours=24)
                filtered = [
                    att for att in attendances if att.timestamp > cursor
                ]
            else:
                filtered = attendances

            for attendance in filtered:
                attendance.device = device
                attendance.punch = (
                    patch_direction[device.device_direction]
                    if device.device_direction in patch_direction
                    else attendance.punch
                )
                combined_attendances.append(attendance)

        except zk_exception.ZKErrorResponse as e:
            errors.append(f"[{device.name}] ZKError: {str(e)}")
        except Exception as e:
            logger.exception(f"[{device.name}] General Error")
            errors.append(f"[{device.name}] Error: {str(e)}")
        finally:
            if conn:
                conn.disconnect()

    combined_attendances.sort(key=lambda a: a.timestamp)

    fetched_count = len(combined_attendances)
    raw_saved_count = 0
    attendance_used_count = 0

    for attendance in combined_attendances:
        user_id = str(attendance.user_id)
        punch_code = attendance.punch

        try:
            date_time = attendance.timestamp
            if django_timezone.is_naive(date_time):
                date_time = django_timezone.make_aware(
                    date_time,
                    django_timezone.get_current_timezone(),
                )
            else:
                date_time = date_time.astimezone(
                    django_timezone.get_current_timezone()
                )

            date = date_time.date()
            time = date_time.time()
            device = attendance.device
            bio_id = bio_id_map.get((device.id, user_id))

            if not bio_id:
                request_data = Request(
                    user=None,
                    date=date,
                    time=time,
                    datetime=date_time,
                )
                request_data.biometric_user_id = user_id

                if punch_code not in {0, 3, 4, 1, 2, 5}:
                    device.last_fetch_date = date
                    device.last_fetch_time = time
                    device.save(
                        update_fields=["last_fetch_date", "last_fetch_time"]
                    )
                    continue

                existing_log = BiometricPunchLog.objects.filter(
                    device_id=device,
                    employee_id__isnull=True,
                    biometric_user_id=user_id,
                    punch_datetime=date_time,
                    punch_code=punch_code,
                ).first()
                if existing_log is None:
                    persist_raw_biometric_punch(
                        request_data,
                        punch_code,
                        device=device,
                        source="ZKTeco",
                        employee=None,
                    )
                raw_saved_count += 1
                logger.warning(
                    "[Device: %s] Punch stored as raw-only; no employee mapping: user_id=%s",
                    device.name,
                    user_id,
                )
                device.last_fetch_date = date
                device.last_fetch_time = time
                device.save(
                    update_fields=["last_fetch_date", "last_fetch_time"]
                )
                continue

            request_data = Request(
                user=bio_id.employee_id.employee_user_id,
                date=date,
                time=time,
                datetime=date_time,
            )
            request_data.biometric_user_id = user_id

            if punch_code not in {0, 3, 4, 1, 2, 5}:
                logger.warning(
                    "[Device: %s] Unsupported punch code %s for user %s",
                    device.name,
                    punch_code,
                    user_id,
                )
                # Unsupported records are not attendance events, but they have
                # been consumed from the device successfully.
                device.last_fetch_date = date
                device.last_fetch_time = time
                device.save(
                    update_fields=["last_fetch_date", "last_fetch_time"]
                )
                continue

            existing_log = BiometricPunchLog.objects.filter(
                device_id=device,
                biometric_user_id=user_id,
                punch_datetime=date_time,
                punch_code=punch_code,
            ).first()

            raw_log = process_biometric_punch(
                request_data,
                punch_code,
                device=device,
                source="ZKTeco",
                employee=bio_id.employee_id,
                work_info=getattr(bio_id.employee_id, "employee_work_info", None),
                raw_log=existing_log,
            )

            if raw_log is None:
                raise RuntimeError(
                    f"Raw biometric punch was not persisted for user {user_id}"
                )

            raw_saved_count += 1
            if raw_log.used_for_attendance:
                attendance_used_count += 1

            # Advance the cursor only after the punch has been persisted and
            # its attendance decision has completed.
            device.last_fetch_date = date
            device.last_fetch_time = time
            device.save(
                update_fields=["last_fetch_date", "last_fetch_time"]
            )

        except Exception as error:
            logger.exception(
                "[Device: %s] Punch processing error for user %s",
                getattr(attendance.device, "name", "unknown"),
                user_id,
            )
            errors.append(
                f"[{getattr(attendance.device, 'name', 'unknown')}] "
                f"user {user_id}: {error}"
            )

    raw_only_count = max(raw_saved_count - attendance_used_count, 0)
    return (
        fetched_count,
        raw_saved_count,
        attendance_used_count,
        raw_only_count,
        "; ".join(errors) if errors else None,
    )


def zk_biometric_attendance_scheduler(device_id):
    """
    Scheduler function used for attendance logs
    """
    device = BiometricDevices.find(device_id)
    if device and device.is_scheduler:
        zk_biometric_attendance_logs(device)


def anviz_biometric_attendance_logs(device):
    """
    Retrieves attendance records from an Anviz biometric device
    and processes them based on device direction configuration.
    """

    current_utc_time = datetime.utcnow()

    anviz_device = CrossChexCloudAPI(
        api_url=device.api_url,
        api_key=device.api_key,
        api_secret=device.api_secret,
        anviz_request_id=device.anviz_request_id,
    )

    begin_time = (
        datetime.combine(device.last_fetch_date, device.last_fetch_time)
        if device.last_fetch_date and device.last_fetch_time
        else current_utc_time.replace(hour=0, minute=0, second=0, microsecond=0)
    )

    attendance_records = anviz_device.get_attendance_records(
        begin_time=begin_time,
        token=device.api_token,
    )

    # Update last fetch time immediately
    device.last_fetch_date = current_utc_time.date()
    device.last_fetch_time = current_utc_time.time()
    device.save(update_fields=["last_fetch_date", "last_fetch_time"])

    processed_count = 0

    for attendance in attendance_records.get("list", []):
        badge_id = attendance["employee"]["workno"]
        punch_code = attendance["checktype"]

        date_time_utc = datetime.strptime(
            attendance["checktime"], "%Y-%m-%dT%H:%M:%S%z"
        )
        date_time_obj = date_time_utc.astimezone(django_timezone.get_current_timezone())

        employee = Employee.objects.filter(badge_id=badge_id).first()
        if not employee:
            continue

        request_data = Request(
            user=employee.employee_user_id,
            date=date_time_obj.date(),
            time=date_time_obj.time(),
            datetime=date_time_obj,
        )

        try:
            # --------------------------------------------------
            # SYSTEM DIRECTION (auto based on punch code)
            # --------------------------------------------------
            if device.device_direction == "system":
                if punch_code in {0, 128}:
                    clock_in(request_data)
                else:
                    clock_out(request_data)

            # --------------------------------------------------
            # FORCE IN DEVICE
            # --------------------------------------------------
            elif device.device_direction == "in":
                clock_in(request_data)

            # --------------------------------------------------
            # FORCE OUT DEVICE
            # --------------------------------------------------
            elif device.device_direction == "out":
                clock_out(request_data)

            # --------------------------------------------------
            # ALTERNATE IN / OUT DEVICE
            # --------------------------------------------------
            elif device.device_direction == "alternate":
                last_activity = (
                    AttendanceActivity.objects.filter(
                        employee_id=employee,
                        attendance_date=date_time_obj.date(),
                    )
                    .order_by("-in_datetime", "-out_datetime")
                    .first()
                )

                # If no record or last record has clock_out → IN
                if not last_activity or last_activity.clock_out:
                    clock_in(request_data)
                else:
                    clock_out(request_data)

            processed_count += 1

        except Exception as error:
            logger.error(
                f"Attendance sync failed for employee {employee.id}",
                exc_info=error,
            )

    return processed_count


def anviz_biometric_attendance_scheduler(device_id):
    """
    Schedules the attendance log retrieval for an Anviz biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device and device.is_scheduler:
        anviz_biometric_attendance_logs(device)


def cosec_biometric_attendance_logs(device):
    """
    Retrieves and processes attendance logs from a COSEC biometric device.
    """
    device_args = COSECAttendanceArguments.objects.filter(device_id=device).first()
    last_fetch_roll_ovr_count = (
        int(device_args.last_fetch_roll_ovr_count) if device_args else 0
    )
    last_fetch_seq_number = int(device_args.last_fetch_seq_number) if device_args else 1

    cosec = COSECBiometric(
        device.machine_ip,
        device.port,
        device.bio_username,
        device.bio_password,
        timeout=10,
    )
    attendances = cosec.get_attendance_events(
        last_fetch_roll_ovr_count, int(last_fetch_seq_number) + 1
    )

    if not isinstance(attendances, list):
        return

    for attendance in attendances:
        ref_user_id = attendance["detail-1"]
        employee = BiometricEmployees.objects.filter(ref_user_id=ref_user_id).first()
        if not employee:
            continue

        date_str = attendance["date"]
        time_str = attendance["time"]
        attendance_date = datetime.strptime(date_str, "%d/%m/%Y").date()
        attendance_time = datetime.strptime(time_str, "%H:%M:%S").time()
        attendance_datetime = datetime.combine(attendance_date, attendance_time)
        punch_code = attendance["detail-2"]

        request_data = Request(
            user=employee.employee_id.employee_user_id,
            date=attendance_date,
            time=attendance_time,
            datetime=django_timezone.make_aware(attendance_datetime),
        )

        try:
            if punch_code in ["1", "3", "5", "7", "9", "0"]:
                clock_in(request_data)
            elif punch_code in ["2", "4", "6", "8", "10"]:
                clock_out(request_data)
            else:
                pass
        except Exception as error:
            logger.error("Error processing attendance: ", error)

    if attendances:
        last_attendance = attendances[-1]
        COSECAttendanceArguments.objects.update_or_create(
            device_id=device,
            defaults={
                "last_fetch_roll_ovr_count": last_attendance["roll-over-count"],
                "last_fetch_seq_number": last_attendance["seq-No"],
            },
        )
    return len(attendances)


def cosec_biometric_attendance_scheduler(device_id):
    """
    Retrieve and process attendance events from a COSEC biometric device.

    This function fetches attendance events from the specified COSEC biometric device
    and processes them to record clock-in and clock-out events for employees.

    Args:
        device_id (uuid): The ID of the COSEC biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device and device.is_scheduler:
        cosec_biometric_attendance_logs(device)


def dahua_biometric_attendance_logs(device):
    """
    Retrieves logs from a Dahua biometric device and marks attendance in Horilla.

    This function fetches biometric logs from the specified device, processes the attendance records,
    and updates the attendance system in Horilla. If an employee has an active clock-in record,
    it marks their clock-out; otherwise, it registers a new clock-in entry.

    Args:
        device_id (int): The unique identifier of the biometric device.

    Returns:
        None
    """
    begin_time = (
        datetime.combine(device.last_fetch_date, device.last_fetch_time)
        + timedelta(seconds=1)
        if device.last_fetch_date and device.last_fetch_time
        else datetime.combine(datetime.today(), datetime.min.time())
    )

    dahua = DahuaAPI(
        ip=device.machine_ip, username=device.bio_username, password=device.bio_password
    )
    logs = dahua.get_control_card_rec(start_time=begin_time)

    if logs.get("status_code") == 200:
        for log in logs.get("records", []):
            user_id = log.get("user_id")
            if not user_id:
                continue

            employee = BiometricEmployees.objects.filter(
                user_id=user_id, device_id=device
            ).first()
            if not employee:
                continue

            attendance_datetime = log.get("create_time")
            user_tz = pytz.timezone(TIME_ZONE)
            attendance_datetime = attendance_datetime.astimezone(user_tz)

            last_none_activity = (
                AttendanceActivity.objects.filter(
                    employee_id=employee.employee_id,
                    clock_out=None,
                )
                .order_by("in_datetime")
                .last()
            )

            request_data = Request(
                user=employee.employee_id.employee_user_id,
                date=attendance_datetime.date(),
                time=attendance_datetime.time(),
                datetime=attendance_datetime,
            )

            if last_none_activity:
                clock_out(request_data)
            else:
                clock_in(request_data)

        if logs.get("records"):
            last_log = logs["records"][-1]
            device.last_fetch_date = last_log["create_time"].date()
            device.last_fetch_time = last_log["create_time"].time()
            device.save()
        return len(logs.get("records", []))
    else:
        return "error"


def dahua_biometric_attendance_scheduler(device_id):
    """
    Schedules the attendance log retrieval for a Dahua biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device and device.is_scheduler:
        dahua_biometric_attendance_logs(device)


def etimeoffice_biometric_attendance_logs(device):
    """
    Retrieves and processes attendance logs from an eTimeOffice biometric device.
    """
    now = datetime.now()
    etimeoffice = ETimeOfficeAPI(
        username=device.bio_username,
        password=device.bio_password,
    )

    from_date = (
        f"{(datetime.combine(device.last_fetch_date, device.last_fetch_time) + timedelta(minutes=1)):%d/%m/%Y_%H:%M}"
        if device.last_fetch_date and device.last_fetch_time
        else f"{now:%d/%m/%Y}_00:00"
    )
    to_date = f"{now:%d/%m/%Y_%H:%M}"

    logs = etimeoffice.download_punch_data(from_date=from_date, to_date=to_date)
    if logs.get("Msg") != "Success":
        return "error"

    punch_data = logs.get("PunchData", [])
    if not punch_data:
        return 0

    user_tz = pytz.timezone(TIME_ZONE)

    employee_map = {
        emp.user_id: emp for emp in BiometricEmployees.objects.filter(device_id=device)
    }

    for log in reversed(punch_data):
        user_id = log.get("Empcode")
        if not user_id or user_id not in employee_map:
            continue

        employee = employee_map[user_id]
        attendance_datetime = log["PunchDate"].astimezone(user_tz)

        request_data = Request(
            user=employee.employee_id.employee_user_id,
            date=attendance_datetime.date(),
            time=attendance_datetime.time(),
            datetime=attendance_datetime,
        )

        last_none_activity = (
            AttendanceActivity.objects.filter(
                employee_id=employee.employee_id,
                clock_out=None,
            )
            .order_by("in_datetime")
            .last()
        )

        if last_none_activity:
            clock_out(request_data)
        else:
            clock_in(request_data)

    last_log = punch_data[0]
    device.last_fetch_date, device.last_fetch_time = (
        last_log["PunchDate"].date(),
        last_log["PunchDate"].time(),
    )
    device.save()

    return len(punch_data)


def etimeoffice_biometric_attendance_scheduler(device_id):
    """
    Schedules the attendance log retrieval for an eTimeOffice biometric device.
    """
    device = BiometricDevices.find(device_id)
    if device and device.is_scheduler:
        etimeoffice_biometric_attendance_logs(device)


# Device polling runs as one registered job in the scheduler process rather
# than a BackgroundScheduler started at import. Import happens once per
# gunicorn worker, so the old form started a scheduler per worker per device
# and polled each device N times over. It also queried the database at import
# time and swallowed every failure with a bare `except: pass`.
#
# Devices are read on each tick instead of at registration, so adding or
# reconfiguring a device takes effect without a restart. Interval is the
# shortest configured duration; each device is polled only when its own
# interval has elapsed.
_BIOMETRIC_SCHEDULERS = {
    "anviz": anviz_biometric_attendance_scheduler,
    "zk": zk_biometric_attendance_scheduler,
    "dahua": dahua_biometric_attendance_scheduler,
    "cosec": cosec_biometric_attendance_scheduler,
    "etimeoffice": etimeoffice_biometric_attendance_scheduler,
}

_biometric_last_run: dict[int, float] = {}


def poll_biometric_devices():
    """Poll each scheduler-enabled device when its interval has elapsed."""
    import time

    now = time.monotonic()
    for device in BiometricDevices.objects.filter(is_scheduler=True):
        handler = _BIOMETRIC_SCHEDULERS.get(device.machine_type)
        if handler is None:
            continue
        try:
            interval = str_time_seconds(device.scheduler_duration)
        except Exception:
            logger.exception(
                "Biometric device %s has an unreadable scheduler_duration",
                device.pk,
            )
            continue
        if interval <= 0:
            continue
        last = _biometric_last_run.get(device.pk)
        if last is not None and (now - last) < interval:
            continue
        _biometric_last_run[device.pk] = now
        try:
            # Bind the id per iteration: the previous lambdas closed over the
            # loop variable, so every device could poll the last one's id.
            handler(device.pk)
        except Exception:
            logger.exception(
                "Biometric polling failed for device %s (%s)",
                device.pk,
                device.machine_type,
            )


register_job(
    poll_biometric_devices,
    "interval",
    job_id="biometric.poll_devices",
    minutes=1,
)
