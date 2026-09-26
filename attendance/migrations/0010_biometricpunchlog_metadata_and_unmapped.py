from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0009_biometricpunchlog"),
        ("horilla_auth", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="biometricpunchlog",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to="horilla_auth.horillauser",
                verbose_name="Created By",
            ),
        ),
        migrations.AddField(
            model_name="biometricpunchlog",
            name="modified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="biometricpunchlog_modified_by",
                to="horilla_auth.horillauser",
                verbose_name="Modified By",
            ),
        ),
        migrations.AlterField(
            model_name="biometricpunchlog",
            name="employee_id",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="biometric_punch_logs",
                to="employee.employee",
                verbose_name="Employee",
            ),
        ),
    ]
