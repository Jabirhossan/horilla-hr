# Generated manually for the employee ID field.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("employee", "0007_remove_policy_specific_employees_policy_department_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="employee_id",
            field=models.CharField(
                blank=True,
                max_length=50,
                null=True,
                unique=True,
                verbose_name="Employee ID / User ID",
            ),
        ),
    ]
