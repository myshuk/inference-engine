"""Phase 2.5 -- human signup via Keycloak, no SSO. Idempotent by design: dev
mode's H2 database doesn't survive a pod restart (confirmed the hard way once
already), so this recreates the developers realm if it's missing rather than
assuming it exists. Uses a unique email per run so re-running this suite never
collides with a previous run's signup.
"""

import base64
import hashlib
import re
import secrets

import requests

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin-poc-only"
REALM = "developers"


def _admin_token(kc_port):
    r = requests.post(
        f"http://localhost:{kc_port}/auth/realms/master/protocol/openid-connect/token",
        data={"client_id": "admin-cli", "username": ADMIN_USER,
              "password": ADMIN_PASSWORD, "grant_type": "password"},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _ensure_developers_realm(kc_port):
    token = _admin_token(kc_port)
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"http://localhost:{kc_port}/auth/admin/realms/{REALM}", headers=headers)
    if r.status_code == 404:
        create = requests.post(f"http://localhost:{kc_port}/auth/admin/realms", headers=headers, json={
            "realm": REALM, "enabled": True, "registrationAllowed": True,
            "registrationEmailAsUsername": True, "verifyEmail": False,
            "resetPasswordAllowed": True, "loginWithEmailAllowed": True,
        })
        assert create.status_code == 201, create.text
    return token


def test_realm_has_self_registration_enabled(keycloak_port):
    token = _ensure_developers_realm(keycloak_port)
    r = requests.get(f"http://localhost:{keycloak_port}/auth/admin/realms/{REALM}",
                      headers={"Authorization": f"Bearer {token}"})
    data = r.json()
    assert data["registrationAllowed"] is True
    assert data["registrationEmailAsUsername"] is True
    assert data["verifyEmail"] is False


def test_human_signup_via_public_registration_form(keycloak_port):
    """Drives the actual self-registration form a human would use -- not the
    admin API, which is a different code path that skips CSRF entirely."""
    _ensure_developers_realm(keycloak_port)
    session = requests.Session()

    verifier = secrets.token_urlsafe(64)[:64]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    login_page = session.get(
        f"http://localhost:{keycloak_port}/auth/realms/{REALM}/protocol/openid-connect/auth",
        params={
            "client_id": "account-console",
            "redirect_uri": f"http://localhost:{keycloak_port}/auth/realms/{REALM}/account/",
            "response_type": "code",
            "scope": "openid",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    reg_match = re.search(r'href="([^"]*login-actions/registration[^"]*)"', login_page.text)
    assert reg_match, "no registration link on the login page -- is registrationAllowed still true?"
    reg_link = reg_match.group(1).replace("&amp;", "&")

    # Keycloak marks its session cookies (KC_RESTART, AUTH_SESSION_ID) Secure,
    # so requests' cookie jar correctly refuses to resend them over plain
    # http:// on subsequent requests -- attach them explicitly instead of
    # relying on Session's automatic (and here, too-strict-for-local-http) jar.
    cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())

    reg_page = session.get(f"http://localhost:{keycloak_port}{reg_link}",
                            headers={"Cookie": cookie_header})
    form_match = re.search(r'<form[^>]*action="([^"]*)"', reg_page.text)
    assert form_match, "no registration form found"
    form_action = form_match.group(1).replace("&amp;", "&")

    email = f"pytest-{secrets.token_hex(4)}@example.com"
    resp = session.post(form_action, headers={"Cookie": cookie_header}, data={
        "firstName": "Pytest", "lastName": "User", "email": email,
        "password": "TestPassw0rd!23", "password-confirm": "TestPassw0rd!23",
    }, allow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert "code=" in resp.headers.get("Location", ""), "expected an auth code in the redirect"

    token = _admin_token(keycloak_port)
    users = requests.get(
        f"http://localhost:{keycloak_port}/auth/admin/realms/{REALM}/users",
        headers={"Authorization": f"Bearer {token}"},
        params={"email": email},
    ).json()
    assert len(users) == 1
    assert users[0]["enabled"] is True
