from django.db import migrations, models
from datetime import datetime, timedelta


def populate_window_times(apps, schema_editor):
    EmployeeShiftSchedule = apps.get_model("base", "EmployeeShiftSchedule")

    for schedule in EmployeeShiftSchedule.objects.all():
        if not schedule.start_time or not schedule.end_time:
            continue

        start_minutes = schedule.start_time.hour * 60 + schedule.start_time.minute
        end_minutes = schedule.end_time.hour * 60 + schedule.end_time.minute

        in_total = start_minutes + (schedule.check_in_window_minutes or 0)
        out_total = end_minutes - (schedule.check_out_window_minutes or 0)

        schedule.check_in_window_start = schedule.start_time
        schedule.check_in_window_end = (
            datetime.min + timedelta(minutes=in_total)
        ).time()
        schedule.check_out_window_start = (
            datetime.min + timedelta(minutes=out_total % 1440)
        ).time()
        schedule.check_out_window_end = schedule.end_time
        schedule.save(
            update_fields=[
                "check_in_window_start",
                "check_in_window_end",
                "check_out_window_start",
                "check_out_window_end",
            ]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0017_shift_attendance_windows"),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_in_window_start",
            field=models.TimeField(
                blank=True,
                null=True,
                verbose_name="Check-in Window Start",
            ),
        ),
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_in_window_end",
            field=models.TimeField(
                blank=True,
                null=True,
                verbose_name="Check-in Window End",
            ),
        ),
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_out_window_start",
            field=models.TimeField(
                blank=True,
                null=True,
                verbose_name="Check-out Window Start",
            ),
        ),
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_out_window_end",
            field=models.TimeField(
                blank=True,
                null=True,
                verbose_name="Check-out Window End",
            ),
        ),
        migrations.RunPython(
            populate_window_times,
            migrations.RunPython.noop,
        ),
    ]
