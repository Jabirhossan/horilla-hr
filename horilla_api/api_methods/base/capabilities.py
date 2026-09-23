"""
One payload describing what a client may show the signed-in user.

Two problems this solves, both from building a mobile client against this API.

**Role and permissions.** The app's navigation is role-adaptive -- which tabs
exist, which band shows on the home screen -- and the only tool for that was
``GET /base/check-user-level?perm=<one perm>``, which answers for a single
permission at a time. Deciding a five-tab bar meant a dozen round trips before
the first screen painted. This returns the whole set once.

**Feature detection.** A client cannot discover whether an optional feature is
available by probing its endpoint: DRF answers 404 both for "no such route"
and "no such object", and 403 for "not allowed", so the three cases are
indistinguishable, and probing costs speculative requests on every cold start.
``features`` states it explicitly instead.

These are **UI hints, not authorization**. Every endpoint still enforces its
own permissions; a client that lies to itself about ``role`` gets 403s, not
access. Nothing here may be used as the basis for a security decision.
"""

from django.apps import apps

from base.methods import is_reportingmanager
from horilla.__version__ import __version__

# Optional apps the mobile client hides features for when absent. Each maps to
# the app label; ``apps.is_installed`` is the whole check. Note that geofencing
# and facedetection are installed from horilla_api/__init__.py rather than
# settings, so a settings grep will wrongly suggest they are missing.
OPTIONAL_APPS = (
    "asset",
    "attendance",
    "facedetection",
    "geofencing",
    "helpdesk",
    "leave",
    "offboarding",
    "payroll",
    "pms",
    "project",
    "recruitment",
)


def _resolve_role(user):
    """
    Collapse permissions into the single role the design's layouts key off.

    Deliberately coarse and ordered most-privileged first, because the design
    shows one band per user, not a union. A client rendering the HR-executive
    band has no business also rendering the manager band.

    ``newhire`` is absent on purpose: the design's onboarding screen wants a
    task checklist for an *Employee*, and the onboarding app is entirely
    Candidate-scoped, so there is nothing to resolve it from yet. Adding it
    later is additive and needs no client change to the other roles.
    """
    if user.is_superuser or user.has_perm("employee.change_employee"):
        return "hrexec"
    if is_reportingmanager(_FakeRequest(user)):
        return "manager"
    return "employee"


class _FakeRequest:
    """``is_reportingmanager`` takes a request; this carries the one attribute
    it reads, so the check stays shared with the web path rather than forked."""

    def __init__(self, user):
        self.user = user


def build_capabilities(user):
    """Return the capability payload for ``user``."""
    profile_edit = apps.get_model("employee", "ProfileEditFeature")
    edit_own_profile = profile_edit.objects.filter(is_enabled=True).exists()

    return {
        "version": __version__,
        "role": _resolve_role(user),
        "permissions": {
            "approve_leave": user.has_perm("leave.change_leaverequest"),
            "approve_attendance": user.has_perm("attendance.change_attendance"),
            "approve_shift_request": user.has_perm("base.change_shiftrequest"),
            "view_team": is_reportingmanager(_FakeRequest(user)),
            "edit_employee": user.has_perm("employee.change_employee"),
            "view_payslip": user.has_perm("payroll.view_payslip"),
            "edit_own_profile": edit_own_profile,
        },
        "features": {label: apps.is_installed(label) for label in OPTIONAL_APPS},
    }
