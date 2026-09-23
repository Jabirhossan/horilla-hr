from django.urls import path

from ...api_views.mobile.views import MobileHomeAPIView

urlpatterns = [
    path("home/", MobileHomeAPIView.as_view(), name="api-mobile-home"),
]
