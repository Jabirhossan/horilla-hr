"""
Persistent biometric scheduler for Horilla.

A single scheduler process calls this job every minute. Device-specific
intervals are stored on BiometricDevices, so scheduling survives web-worker
restarts and does not depend on an HTTP request owning a BackgroundScheduler.
"""

from datetime import datetime, timedelta

from django.utils import timezone

from horilla.scheduling import register_job

from .models import BiometricDevices


def _duration_seconds(value):
    try:
        hours, minutes = str(value or "00:00").split(":")[:2]
        return (int(hours) * 3600) + (int(minutes) * 60)
    except (TypeError, ValueError):
        return 60


def _device_due(device, now):
    interval = max(_duration_seconds(device.scheduler_duration), 60)

    if not device.last_fetch_date or not device.last_fetch_time:
        return True

    last_fetch = timezone.make_aware(
        datetime.combine(device.last_fetch_date, device.last_fetch_time),
        timezone.get_current_timezone(),
    )
    return (now - last_fetch).total_seconds() >= interval


def sync_scheduled_biometric_devices():
    """
    Fetch all due scheduled biometric devices.

    ZKTeco/eSSL is the primary scheduled backend used by this installation.
    Other supported device types continue to use their existing scheduler
    functions when available.
    """
    from .views import (
        anviz_biometric_attendance_logs,
        cosec_biometric_attendance_logs,
        dahua_biometric_attendance_logs,
        etimeoffice_biometric_attendance_logs,
        zk_biometric_attendance_logs,
    )

    now = timezone.localtime()

    for device in BiometricDevices.objects.filter(is_scheduler=True).order_by("name"):
        if not _device_due(device, now):
            continue

        try:
            if device.machine_type == "zk":
                zk_biometric_attendance_logs(device)
            elif device.machine_type == "anviz":
                anviz_biometric_attendance_logs(device)
            elif device.machine_type == "cosec":
                cosec_biometric_attendance_logs(device)
            elif device.machine_type == "dahua":
                dahua_biometric_attendance_logs(device)
            elif device.machine_type == "etimeoffice":
                etimeoffice_biometric_attendance_logs(device)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Scheduled biometric sync failed: device=%s id=%s",
                device.name,
                device.id,
            )


register_job(
    sync_scheduled_biometric_devices,
    "interval",
    job_id="biometric.sync_scheduled_devices",
    minutes=1,
)
