from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0008_attendance_attendance_date_idx_and_more"),
        ("biometric", "0001_initial"),
        ("base", "0017_shift_attendance_windows"),
        ("employee", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BiometricPunchLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, null=True, verbose_name="Created At"
                    ),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True, verbose_name="Is Active"),
                ),
                (
                    "biometric_user_id",
                    models.CharField(max_length=100, verbose_name="Device User ID"),
                ),
                (
                    "punch_code",
                    models.IntegerField(blank=True, null=True, verbose_name="Punch Code"),
                ),
                (
                    "direction",
                    models.CharField(
                        choices=[("IN", "Check In"), ("OUT", "Check Out")],
                        max_length=3,
                    ),
                ),
                (
                    "punch_datetime",
                    models.DateTimeField(verbose_name="Punch Date/Time"),
                ),
                ("attendance_date", models.DateField(blank=True, null=True)),
                (
                    "window_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("BEFORE_CHECKIN_WINDOW", "Before Check-In Window"),
                            ("CHECKIN_WINDOW", "Check-In Window"),
                            ("BETWEEN_WINDOWS", "Between Check-In and Check-Out Windows"),
                            ("CHECKOUT_WINDOW", "Check-Out Window"),
                            ("AFTER_CHECKOUT_WINDOW", "After Check-Out Window"),
                        ],
                        max_length=40,
                        null=True,
                    ),
                ),
                ("within_window", models.BooleanField(default=False)),
                ("used_for_attendance", models.BooleanField(default=False)),
                (
                    "selection_role",
                    models.CharField(
                        blank=True,
                        max_length=20,
                        null=True,
                        verbose_name="Attendance Role",
                    ),
                ),
                (
                    "source",
                    models.CharField(default="ZKTeco", editable=False, max_length=30),
                ),
                (
                    "raw_payload",
                    models.JSONField(blank=True, editable=False, null=True),
                ),
                (
                    "attendance_id",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="biometric_punch_logs",
                        to="attendance.attendance",
                        verbose_name="Attendance",
                    ),
                ),
                (
                    "device_id",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="attendance_punch_logs",
                        to="biometric.biometricdevices",
                        verbose_name="Biometric Device",
                    ),
                ),
                (
                    "employee_id",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="biometric_punch_logs",
                        to="employee.employee",
                        verbose_name="Employee",
                    ),
                ),
                (
                    "shift_id",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="base.employeeshift",
                        verbose_name="Shift",
                    ),
                ),
            ],
            options={
                "verbose_name": "Biometric Punch Log",
                "verbose_name_plural": "Biometric Punch Logs",
                "ordering": ["-punch_datetime", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="biometricpunchlog",
            index=models.Index(
                fields=["employee_id", "punch_datetime"],
                name="bio_punch_emp_dt_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="biometricpunchlog",
            index=models.Index(
                fields=["attendance_date", "employee_id"],
                name="bio_punch_att_date_emp_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="biometricpunchlog",
            index=models.Index(
                fields=["device_id", "punch_datetime"],
                name="bio_punch_device_dt_idx",
            ),
        ),
    ]
