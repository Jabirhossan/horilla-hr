"""
scheduler.py

This module is used to register scheduled tasks
"""

from datetime import date, timedelta

from django.urls import reverse
from django.utils.translation import gettext_noop

from horilla.scheduling import register_job
from notifications.signals import notify


def notify_expiring_assets():
    """
    Finds all Expiring Assets and send a notification on the notify_before date.
    """
    from asset.models import Asset
    from horilla_auth.models import HorillaUser

    today = date.today()
    assets = Asset.objects.all()

    # Cache bot & superuser once
    bot = HorillaUser.objects.filter(username="Horilla Bot").only("id").first()
    superuser = HorillaUser.objects.filter(is_superuser=True).only("id").first()

    # Query only assets that are expiring today
    assets = Asset.objects.filter(
        expiry_date__isnull=False,
        expiry_date__gte=today,
    )

    for asset in assets:
        if asset.expiry_date:
            expiry_date = asset.expiry_date
            notify_date = expiry_date - timedelta(days=asset.notify_before)
            recipient = getattr(asset.owner, "employee_user_id", None) or superuser
            if notify_date == today and recipient:
                notify.send(
                    bot,
                    recipient=recipient,
                    verb=gettext_noop(
                        "The Asset '%(asset_name)s' expires in %(notify_before)s days"
                    ),
                    verb_params={
                        "asset_name": str(asset.asset_name),
                        "notify_before": str(asset.notify_before),
                    },
                    redirect=reverse("asset-category-view"),
                    label="System",
                    icon="information",
                )


def mark_expired_assets():
    """
    Finds all assets past their expiry date and sets their status to Not-Available.
    """
    from asset.models import Asset

    today = date.today()
    expired = Asset.objects.filter(
        expiry_date__isnull=False,
        expiry_date__lt=today,
    ).exclude(asset_status="Not-Available")
    for asset in expired:
        asset.asset_status = "Not-Available"
        asset.save()


def notify_expiring_documents():
    """
    Finds all Expiring Documents and send a notification on the notify_before date.
    """
    from horilla_auth.models import HorillaUser
    from horilla_documents.models import Document

    today = date.today()
    documents = Document.objects.all()
    bot = HorillaUser.objects.filter(username="Horilla Bot").first()
    for document in documents:
        if document.expiry_date:
            expiry_date = document.expiry_date
            notify_date = expiry_date - timedelta(days=document.notify_before)

            if notify_date == today:
                notify.send(
                    bot,
                    recipient=document.employee_id.employee_user_id,
                    verb=gettext_noop(
                        "The document ' %(title)s ' expires in %(notify_before)s days"
                    ),
                    verb_params={
                        "title": str(document.title),
                        "notify_before": str(document.notify_before),
                    },
                    redirect=reverse("asset-category-view"),
                    label="System",
                    icon="information",
                )
            if today >= expiry_date:
                document.is_active = False


register_job(notify_expiring_assets, "interval", days=1)
register_job(notify_expiring_documents, "interval", hours=4)
register_job(mark_expired_assets, "interval", days=1)
