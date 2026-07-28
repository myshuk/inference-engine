« [Home](Home.md)

# Control Plane B — Identity

**Deferred until the first enterprise SSO ask.** Not part of the banded request path.

## Component

### Keycloak

*(Apache 2.0)* Authenticates **humans** into the portal and into our own internal tooling (Grafana,
Argo CD, admin consoles). SAML / OIDC federation to Okta and Entra ID.

- ⚠ **Never in the inference request path.** API auth stays a hashed-key lookup against Postgres, cached
  in Valkey — see [Band 3](Band-3-Identity-Tenancy.md). Machine-to-API auth and human-to-portal auth are
  different problems with different latency and UX requirements; conflating them means either putting an
  OAuth flow in front of every inference call (customers expect a static bearer key, not that) or putting
  session/MFA/invitation logic somewhere it doesn't belong.
- **MVP:** email + password, behind a thin abstraction so swapping in an IdP later is contained to one
  place.
- **Trigger to actually adopt this:** first enterprise SSO requirement, or internal tooling sprawl
  (Grafana, Argo CD, admin consoles) reaching the point where separate logins are painful.
- **Alternatives considered:** Zitadel, Authentik, Ory, WorkOS (commercial).

## Why deferred, not dropped ([D6](Decision-Log-Index.md#d6))

The org → project → key hierarchy is **our** domain model, living in Postgres, entangled with billing and
quota. Keycloak (or any IdP) should never be asked to model tenancy — it solves a real but separate
problem (human session management, MFA, SSO federation) that we don't need on day one.

## Related

- [Band 3 — Identity, Tenancy, Counters](Band-3-Identity-Tenancy.md) — where the actual API-auth domain model lives
- [D6](Decision-Log-Index.md#d6)
