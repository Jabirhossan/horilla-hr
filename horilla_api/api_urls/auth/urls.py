from django.urls import path

from ...api_views.auth.views import (
    LoginAPIView,
    LogoutAPIView,
    PasswordResetAPIView,
    TokenRefreshAPIView,
)

urlpatterns = [
    path("login/", LoginAPIView.as_view()),
    path("refresh/", TokenRefreshAPIView.as_view(), name="api-token-refresh"),
    path("logout/", LogoutAPIView.as_view(), name="api-logout"),
    path("reset-password/", PasswordResetAPIView.as_view(), name="api-reset-password"),
]
