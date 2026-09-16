-- Band 3 domain model: orgs -> projects -> hashed API keys, plans, per-key
-- limits, model allow-lists. Per wiki/Band-3-Identity-Tenancy.md: this is our
-- own domain model, not the IdP's (Keycloak/Phase 2.5 handles human auth into
-- a portal; this is machine-to-API auth, a plain hashed-key lookup).

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE orgs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Plan defaults; a project's own rpm/tpm columns can override these per D-something
-- ("per-key limits" in the wiki) without needing a new plan row per customer.
CREATE TABLE plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    rpm_limit INT NOT NULL,
    tpm_limit INT NOT NULL,
    spend_cap_cents INT  -- NULL = uncapped
);

CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    plan_id UUID NOT NULL REFERENCES plans(id),
    rpm_limit_override INT,
    tpm_limit_override INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, name)
);

-- Keys are hashed (sha256 hex), never stored in plaintext. key_prefix is just
-- the first few chars of the real key, kept for display/lookup hints
-- (e.g. "sk-live-ab12...") -- never enough to reconstruct or brute-force the key.
CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE TABLE model_allowlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    UNIQUE (project_id, model_name)
);

CREATE INDEX idx_api_keys_project ON api_keys(project_id);
CREATE INDEX idx_projects_org ON projects(org_id);
CREATE INDEX idx_model_allowlist_project ON model_allowlist(project_id);

-- Seed data for the POC: one org, one project, one plan, one active key,
-- one allowed model -- matches the mock pool's model name so a real
-- end-to-end auth check has something to allow.
INSERT INTO plans (name, rpm_limit, tpm_limit, spend_cap_cents)
    VALUES ('free', 60, 10000, 500);

INSERT INTO orgs (name) VALUES ('acme-poc');

INSERT INTO projects (org_id, name, plan_id)
    SELECT o.id, 'acme-poc-default', p.id
    FROM orgs o, plans p
    WHERE o.name = 'acme-poc' AND p.name = 'free';

-- Plaintext key for the POC is "sk-poc-test-key-do-not-use-in-prod" --
-- only its sha256 hash and a display prefix are stored here.
INSERT INTO api_keys (project_id, key_hash, key_prefix)
    SELECT pr.id,
           encode(digest('sk-poc-test-key-do-not-use-in-prod', 'sha256'), 'hex'),
           'sk-poc-test'
    FROM projects pr WHERE pr.name = 'acme-poc-default';

INSERT INTO model_allowlist (project_id, model_name)
    SELECT pr.id, 'mock'
    FROM projects pr WHERE pr.name = 'acme-poc-default';
