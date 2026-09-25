# Generated manually for configurable shift attendance windows

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_in_window_minutes",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Number of minutes after shift start during which biometric check-in is allowed.",
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="Check-in Window (Minutes)",
            ),
        ),
        migrations.AddField(
            model_name="employeeshiftschedule",
            name="check_out_window_minutes",
            field=models.PositiveIntegerField(
                default=30,
                help_text="Number of minutes before shift end during which biometric check-out is allowed.",
                validators=[django.core.validators.MinValueValidator(0)],
                verbose_name="Check-out Window (Minutes)",
            ),
        ),
    ]
}
