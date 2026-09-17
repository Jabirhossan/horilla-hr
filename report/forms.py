"""Form for creating/editing report subscriptions."""

from __future__ import annotations

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.forms import ModelForm
from employee.filters import EmployeeFilter
from employee.models import Employee
from horilla_widgets.widgets.horilla_multi_select_field import HorillaMultiSelectField
from horilla_widgets.widgets.select_widgets import HorillaMultiSelectWidget
from report.delivery import compute_schedule_anchor
from report.models import ReportSubscription

_SELECT_CLASS = "oh-select oh-select-2 select2-hidden-accessible"

WEEKDAY_CHOICES = [
    (0, _("Monday")),
    (1, _("Tuesday")),
    (2, _("Wednesday")),
    (3, _("Thursday")),
    (4, _("Friday")),
    (5, _("Saturday")),
    (6, _("Sunday")),
]
DAY_OF_MONTH_CHOICES = [(day, str(day)) for day in range(1, 32)]


class ReportSubscriptionForm(ModelForm):

    weekday = forms.TypedChoiceField(
        label=_("Weekday"),
        choices=WEEKDAY_CHOICES,
        coerce=int,
        required=False,
        widget=forms.Select(attrs={"class": _SELECT_CLASS}),
    )
    day_of_month = forms.TypedChoiceField(
        label=_("Date"),
        choices=DAY_OF_MONTH_CHOICES,
        coerce=int,
        required=False,
        widget=forms.Select(attrs={"class": _SELECT_CLASS}),
    )

    format = forms.ChoiceField(
        label=_("Attachment"),
        choices=[("xlsx", _("Excel")), ("pdf", _("PDF"))],
        initial="xlsx",
    )

    cols = {"recipients_employees": 12}

    class Meta:
        model = ReportSubscription
        fields = ["report_slug", "name", "frequency", "recipients_employees"]
        labels = {
            "report_slug": _("Report"),
            "name": _("Name"),
            "frequency": _("Frequency"),
            "recipients_employees": _("Recipients"),
        }

    def __init__(self, *args, report_choices=None, lock_report=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["recipients_employees"] = HorillaMultiSelectField(
            queryset=Employee.objects.all(),
            required=False,
            widget=HorillaMultiSelectWidget(
                filter_route_name="employee-widget-filter",
                filter_class=EmployeeFilter,
                filter_instance_context_name="f",
                filter_template_path="employee_filters.html",
                instance=self.instance,
            ),
            label=_("Recipients"),
        )

        editing = bool(self.instance and self.instance.pk)
        if editing:
            # Report is locked once a subscription exists.
            self.fields["report_slug"] = forms.ChoiceField(
                label=_("Report"),
                choices=[(self.instance.report_slug, self.instance.report_name)],
                disabled=True,
                widget=forms.Select(attrs={"class": _SELECT_CLASS}),
            )
            self.initial.setdefault(
                "format", (self.instance.filters or {}).get("format") or "xlsx"
            )
        elif lock_report:
            from report.registry import get_report

            definition = get_report(lock_report)
            display = str(definition.name) if definition else lock_report
            self.fields["report_slug"] = forms.ChoiceField(
                label=_("Report"),
                choices=[(lock_report, display)],
                disabled=True,
                widget=forms.Select(attrs={"class": _SELECT_CLASS}),
            )
        elif report_choices is not None:
            self.fields["report_slug"] = forms.ChoiceField(
                label=_("Report"),
                choices=[("", _("Select a report…"))] + report_choices,
                widget=forms.Select(attrs={"class": _SELECT_CLASS}),
            )

        self.fields["recipients_employees"] = self.fields.pop("recipients_employees")

    def clean(self):
        """Resolve the posted employee ids and require at least one recipient."""
        cleaned_data = super().clean()
        if isinstance(self.fields.get("recipients_employees"), HorillaMultiSelectField):
            self.errors.pop("recipients_employees", None)
            employee_data = self.fields["recipients_employees"].queryset.filter(
                id__in=self.data.getlist("recipients_employees")
            )
            cleaned_data["recipients_employees"] = employee_data
            if not employee_data.exists():
                self.add_error(
                    "recipients_employees", _("Select at least one recipient.")
                )
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        filters = dict(instance.filters or {})
        filters["format"] = self.cleaned_data.get("format") or "xlsx"
        instance.filters = filters
        if not instance.last_run_display:
            instance.last_run_at = compute_schedule_anchor(
                instance.frequency,
                timezone.now(),
                weekday=self.cleaned_data.get("weekday"),
                day_of_month=self.cleaned_data.get("day_of_month"),
            )
        if commit:
            instance.save()
            self.save_m2m()
        return instance
