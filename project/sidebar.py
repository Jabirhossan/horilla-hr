"""
project/sidebar.py
"""

from django.contrib.auth.context_processors import PermWrapper
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from base.templatetags.basefilters import is_reportingmanager
from project.methods import (
    any_project_manager,
    any_task_manager,
    any_task_member,
    get_all_project_members_and_managers,
    has_subordinates,
)

MENU = _("Project")
IMG_SRC = "images/ui/project.png"
ACCESSIBILITY = "project.sidebar.menu_accessibilty"

SUBMENUS = [
    {
        "menu": _("Dashboard"),
        "redirect": reverse("project-dashboard-view"),
        "accessibility": "project.sidebar.dashboard_accessibility",
    },
    {
        "menu": _("Projects"),
        "redirect": reverse("project-view"),
        "accessibility": "project.sidebar.project_accessibility",
        # project-detailed-view/<pk>/ and task-view/<project_id>/ (a
        # project's own task board) are sibling URLs (not sub-paths of
        # project-view/), so they need explicit prefixes for the sidebar's
        # path-based active-link highlighting to match them.
        "match_prefixes": [
            "/project/project-detailed-view/",
            "/project/task-view/",
        ],
    },
    {
        "menu": _("Tasks"),
        "redirect": reverse("task-all"),
        "accessibility": "project.sidebar.task_accessibility",
        # task-detail-view/<pk>/ is a sibling URL (not a sub-path of task-all/),
        # so it needs an explicit prefix for the sidebar's path-based
        # active-link highlighting to match it.
        "match_prefixes": ["/project/task-detail-view/"],
    },
    {
        "menu": _("Timesheet"),
        "redirect": reverse("view-time-sheet"),
        "accessibility": "project.sidebar.timesheet_accessibility",
        # time-sheet-detail-view/<pk>/ is a sibling URL (not a sub-path of
        # view-time-sheet/), so it needs an explicit prefix for the sidebar's
        # path-based active-link highlighting to match it.
        "match_prefixes": ["/project/time-sheet-detail-view/"],
    },
]


def menu_accessibilty(
    request, _menu: str = "", user_perms: PermWrapper = [], *args, **kwargs
) -> bool:
    user = request.user
    return (
        "project" in user_perms
        # or has_subordinates(request)
        or any_project_manager(user)
        or any_task_manager(user)
        or any_task_member(user)
    )


def dashboard_accessibility(request, submenu, user_perms, *args, **kwargs):
    user = request.user
    if (
        user.has_perm("project.view_project")
        # or has_subordinates(request)
        or is_reportingmanager(user)
        or any_project_manager(user)
        or any_task_manager(user)
    ):
        return True
    else:
        return False


def project_accessibility(request, submenu, user_perms, *args, **kwargs):
    user = request.user
    if (
        user.has_perm("project.view_project")
        # or has_subordinates(request)
        or any_project_manager(user)
        or any_task_manager(user)
        or any_task_member(user)
    ):
        return True
    else:
        return False


def task_accessibility(request, submenu, user_perms, *args, **kwargs):
    user = request.user
    if (
        user.has_perm("project.view_task")
        # or has_subordinates(request)
        or any_project_manager(user)
        or any_task_manager(user)
        or any_task_member(user)
    ):
        return True
    else:
        return False


def timesheet_accessibility(request, submenu, user_perms, *args, **kwargs):
    user = request.user
    if (
        user.has_perm("project.view_timesheet")
        # or has_subordinates(request)
        or any_project_manager(user)
        or any_task_manager(user)
        or any_task_member(user)
    ):
        return True
    else:
        return False
