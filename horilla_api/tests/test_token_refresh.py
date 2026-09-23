"""
Refresh and logout on the API.

Login previously minted a refresh token and discarded it, so a client had a
60-minute session and no way to extend it -- unusable for a mobile app. These
tests pin the flow that replaces it, and in particular the two properties that
make a 30-day refresh lifetime acceptable: rotation invalidates the spent
token, and logout blacklists the live one.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user

LOGIN_URL = "/api/v1/auth/login/"
REFRESH_URL = "/api/v1/auth/refresh/"
LOGOUT_URL = "/api/v1/auth/logout/"
ME_URL = "/api/v1/employee/list/employees/"

PASSWORD = "secret123"


class TokenRefreshTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        set_selected_company(None)
        self.company = make_company("Refresh Co")
        self.user = make_user("refresh_user", password=PASSWORD)
        make_employee(
            company=self.company,
            email="refresh@test.horilla",
            first_name="Rita",
            user=self.user,
        )

    def _login(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": "refresh_user", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def test_login_returns_a_refresh_token(self):
        """The regression: login used to mint this and throw it away."""
        data = self._login()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertTrue(data["refresh"])

    def test_refresh_returns_a_working_access_token(self):
        refresh = self._login()["refresh"]
        response = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 200, response.content)

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
        self.assertEqual(client.get(ME_URL).status_code, 200)

    def test_rotation_issues_a_new_refresh_and_kills_the_old_one(self):
        """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION: one use only."""
        first = self._login()["refresh"]

        rotated = self.client.post(REFRESH_URL, {"refresh": first}, format="json")
        self.assertEqual(rotated.status_code, 200, rotated.content)
        second = rotated.data.get("refresh")
        self.assertTrue(second, "rotation must return a replacement refresh token")
        self.assertNotEqual(first, second)

        replayed = self.client.post(REFRESH_URL, {"refresh": first}, format="json")
        self.assertEqual(
            replayed.status_code, 401, "a spent refresh token must not work twice"
        )

        still_good = self.client.post(REFRESH_URL, {"refresh": second}, format="json")
        self.assertEqual(still_good.status_code, 200, still_good.content)

    def test_logout_blacklists_the_refresh_token(self):
        refresh = self._login()["refresh"]

        response = self.client.post(LOGOUT_URL, {"refresh": refresh}, format="json")
        self.assertIn(response.status_code, (200, 204), response.content)

        after = self.client.post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(
            after.status_code, 401, "a logged-out refresh token must not refresh"
        )

    def test_garbage_refresh_token_is_rejected(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": "not-a-token"}, format="json"
        )
        self.assertEqual(response.status_code, 401)


class RefreshHonoursPasswordRevocationTests(TestCase):
    """
    A password change must stop the refresh chain, not just the access token.

    ``CHECK_REVOKE_TOKEN`` is enforced upstream only in
    ``JWTAuthentication.get_user()``, which never runs on the refresh path. So
    before this was fixed, refreshing after a password change returned 200 and
    a fresh pair; the access token was then rejected, but the refresh token
    could be re-spent indefinitely for its whole 30-day life. A credential the
    owner cannot revoke is the thing a 30-day lifetime cannot afford.
    """

    def setUp(self):
        self.client = APIClient()
        set_selected_company(None)
        self.company = make_company("Revoke Co")
        self.user = make_user("revoke_user", password=PASSWORD)
        make_employee(
            company=self.company,
            email="revoke@test.horilla",
            first_name="Rue",
            user=self.user,
        )

    def _refresh_token(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": "revoke_user", "password": PASSWORD},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.data["refresh"]

    def test_refresh_is_rejected_after_the_password_changes(self):
        refresh = self._refresh_token()

        self.user.set_password("a-different-secret")
        self.user.save()

        response = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(
            response.status_code,
            401,
            "a refresh token minted before the password change must not be "
            f"exchangeable; got {response.status_code}: {response.content}",
        )

    def test_refresh_still_works_when_the_password_is_unchanged(self):
        """The guard must not break the ordinary path it sits on."""
        refresh = self._refresh_token()
        response = APIClient().post(REFRESH_URL, {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("access", response.data)
