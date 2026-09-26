from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0018_editable_shift_window_times"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="employeeshiftschedule",
            name="check_in_window_minutes",
        ),
        migrations.RemoveField(
            model_name="employeeshiftschedule",
            name="check_out_window_minutes",
        ),
    ]
