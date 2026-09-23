"""
decorator functions for payroll
"""

from django.utils.translation import gettext_lazy as _

from horilla.methods import handle_no_permission
from payroll.models.models import EncashmentGeneralSettings

decorator_with_arguments = (
    lambda decorator: lambda *args, **kwargs: lambda func: decorator(
        func, *args, **kwargs
    )
)


def leave_encashment_visible_to(request):
    """
    Whether the requesting user should see the Leave Encashments section at
    all: off entirely if the feature is disabled in settings, or if it's
    enabled but not yet applied to anyone (not "apply to all" and no
    employees/department/job position picked) -- that half-configured state
    is hidden for everyone, admins included, until someone actually sets
    who it applies to. Once it is configured, anyone who manages
    reimbursements (add/view permission) always sees it, while a plain
    self-service employee only sees it if they're personally targeted.
    """
    settings = EncashmentGeneralSettings.objects.first()
    if not settings:
        return True
    if not settings.leave_encashment_enabled:
        return False
    if settings.is_applicable_to_all:
        return True
    has_targeting = (
        settings.employees.exists()
        or settings.department.exists()
        or settings.job_position.exists()
    )
    if not has_targeting:
        return False
    if request.user.has_perm("payroll.add_reimbursement") or request.user.has_perm(
        "payroll.view_reimbursement"
    ):
        return True
    employee = getattr(request.user, "employee_get", None)
    if not employee:
        return False
    return settings.filtered_employees.filter(pk=employee.pk).exists()


@decorator_with_arguments
def is_leave_encashment_enabled(func=None, *args, **kwargs):
    def function(request, *args, **kwargs):
        """
        This function checks whether the leave encashment feature is enabled
        for, and applicable to, the requesting user.
        """
        if leave_encashment_visible_to(request):
            return func(request, *args, **kwargs)

        return handle_no_permission(
            request, message=_("Sorry, Leave Encashment is not enabled.")
        )

    return function
