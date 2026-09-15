from swapper import swappable_setting

from .base.models import AbstractNotification


class Notification(AbstractNotification):
    class Meta(AbstractNotification.Meta):
        abstract = False
        swappable = swappable_setting("notifications", "Notification")

    def naturalday(self):
        """
        Shortcut for the ``humanize``.
        Take a parameter humanize_type. This parameter control the which humanize method use.
        Return ``today``, ``yesterday`` ,``now``, ``2 seconds ago``etc.
        """
        from django.contrib.humanize.templatetags.humanize import naturalday

        return naturalday(self.timestamp)

    def naturaltime(self):
        from django.contrib.humanize.templatetags.humanize import naturaltime

        return naturaltime(self.timestamp)

    def get_translated_verb(self):
        from django.utils.translation import gettext

        params = (self.data or {}).get("verb_params") or {}
        text = gettext(self.verb)
        return text % params if params else text
