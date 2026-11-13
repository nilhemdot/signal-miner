-- Schema sm, roles and every v1 table from spec section 5.
-- Run as an admin role (Supabase: postgres). Objects end up owned by pipeline_writer.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_writer') THEN
        CREATE ROLE pipeline_writer NOLOGIN;
    END IF;
    -- Supabase ships these roles; a plain Postgres (CI, local) does not.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
END
$$;

GRANT pipeline_writer TO CURRENT_USER;

CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO pipeline_writer;

CREATE SCHEMA sm AUTHORIZATION pipeline_writer;
REVOKE ALL ON SCHEMA sm FROM PUBLIC, anon, authenticated;

SET ROLE pipeline_writer;
SET search_path = sm, extensions;

CREATE TABLE runs (
    run_id       uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    job          text NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    status       text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    stats        jsonb NOT NULL DEFAULT '{}'::jsonb,
    error        text
);

CREATE TABLE source_health (
    source     text NOT NULL,
    run_id     uuid NOT NULL REFERENCES runs ON DELETE CASCADE,
    metric     text NOT NULL,
    value      double precision,
    threshold  double precision,
    ok         boolean NOT NULL,
    PRIMARY KEY (source, run_id, metric)
);

CREATE TABLE companies (
    company_id         uuid PRIMARY KEY DEFAULT extensions.gen_random_uuid(),
    name               text NOT NULL,
    normalized_name    text NOT NULL,
    domain             text UNIQUE,
    domain_confidence  text NOT NULL DEFAULT 'none' CHECK (domain_confidence IN ('high', 'medium', 'low', 'none')),
    hq_state           text
);
CREATE INDEX companies_normalized_name_trgm ON companies USING gin (normalized_name extensions.gin_trgm_ops);

CREATE TABLE company_aliases (
    source        text NOT NULL,
    external_id   text NOT NULL,
    company_id    uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
    match_method  text NOT NULL,
    match_score   real,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE er_review_queue (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source        text NOT NULL,
    external_id   text NOT NULL,
    candidate_id  uuid REFERENCES companies ON DELETE CASCADE,
    score         real,
    decision      text NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'merge', 'distinct')),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE form_d_filings (
    accession           text PRIMARY KEY,
    cik                 bigint NOT NULL,
    file_number         text,
    form_type           text NOT NULL CHECK (form_type IN ('D', 'D/A')),
    filed_date          date NOT NULL,
    previous_accession  text,
    amount_sold         numeric
);

CREATE TABLE form_d_offerings (
    cik                      bigint NOT NULL,
    file_number              text NOT NULL,
    latest_accession         text NOT NULL REFERENCES form_d_filings,
    latest_filed_date        date NOT NULL,
    entity_name              text NOT NULL,
    entity_type              text,
    city                     text,
    state                    text,
    year_of_inc_bucket       text,
    industry_group           text,
    revenue_range            text,
    offering_amount          numeric,
    offering_indefinite      boolean NOT NULL DEFAULT false,
    amount_sold              numeric,
    date_of_first_sale       date,
    first_sale_yet_to_occur  boolean NOT NULL DEFAULT false,
    related_person_count     integer,
    passes_startup_filter    boolean NOT NULL DEFAULT false,
    company_id               uuid REFERENCES companies ON DELETE CASCADE,
    PRIMARY KEY (cik, file_number)
);

CREATE TABLE hn_threads (
    story_id  bigint PRIMARY KEY,
    month     date NOT NULL UNIQUE,
    title     text NOT NULL
);

CREATE TABLE tool_dictionary (
    tool_id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           text NOT NULL UNIQUE,
    patterns       text[] NOT NULL,
    is_competitor  boolean NOT NULL DEFAULT false
);

CREATE TABLE hn_posts (
    comment_id        bigint PRIMARY KEY,
    story_id          bigint NOT NULL REFERENCES hn_threads ON DELETE CASCADE,
    created_at        timestamptz NOT NULL,
    header_raw        text,
    company_raw       text,
    stage_hint        text,
    remote_flags      text[] NOT NULL DEFAULT '{}',
    links             text[] NOT NULL DEFAULT '{}',
    funding_hint      boolean NOT NULL DEFAULT false,
    tool_ids          integer[] NOT NULL DEFAULT '{}',
    body_trunc        text,
    parse_confidence  text NOT NULL CHECK (parse_confidence IN ('high', 'low')),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    company_id        uuid REFERENCES companies ON DELETE CASCADE
);

CREATE TABLE ats_boards (
    ats             text NOT NULL CHECK (ats IN ('greenhouse', 'lever', 'ashby', 'workable')),
    slug            text NOT NULL,
    region          text NOT NULL DEFAULT 'us',
    company_id      uuid REFERENCES companies ON DELETE CASCADE,
    discovered_via  text NOT NULL,
    confirmed       boolean NOT NULL DEFAULT false,
    fail_count      integer NOT NULL DEFAULT 0,
    checked_at      timestamptz,
    PRIMARY KEY (ats, slug, region)
);

CREATE TABLE job_postings (
    ats               text NOT NULL,
    slug              text NOT NULL,
    region            text NOT NULL,
    job_id            text NOT NULL,
    title             text NOT NULL,
    url               text NOT NULL,
    posted_at         timestamptz,
    updated_at        timestamptz,
    is_baseline       boolean NOT NULL DEFAULT false,
    tool_ids          integer[] NOT NULL DEFAULT '{}',
    migration_phrase  boolean NOT NULL DEFAULT false,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    PRIMARY KEY (ats, slug, region, job_id),
    FOREIGN KEY (ats, slug, region) REFERENCES ats_boards ON DELETE CASCADE
);

CREATE TABLE gh_org_weekly (
    org_id        bigint NOT NULL,
    week_start    date NOT NULL,
    login         text NOT NULL,
    total         integer NOT NULL,
    push          integer NOT NULL,
    actors        integer NOT NULL,
    repos         integer NOT NULL,
    repo_creates  integer NOT NULL,
    releases      integer NOT NULL,
    window_total  bigint NOT NULL,
    share         double precision NOT NULL,
    ratio         double precision,
    PRIMARY KEY (org_id, week_start)
);

CREATE TABLE gh_orgs (
    org_id             bigint PRIMARY KEY,
    login              text NOT NULL,
    website_url        text,
    email_domain       text,
    is_verified        boolean NOT NULL DEFAULT false,
    domain             text,
    domain_confidence  text NOT NULL DEFAULT 'none' CHECK (domain_confidence IN ('high', 'medium', 'low', 'none')),
    company_id         uuid REFERENCES companies ON DELETE CASCADE
);

CREATE TABLE signals (
    signal_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id    uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
    source        text NOT NULL,
    signal_type   text NOT NULL,
    source_ref    text NOT NULL,
    observed_at   timestamptz NOT NULL,
    strength      real NOT NULL CHECK (strength BETWEEN 0 AND 1),
    evidence_url  text NOT NULL,
    details       jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source, signal_type, source_ref)
);

CREATE TABLE weekly_scores (
    week_start  date NOT NULL,
    company_id  uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
    score       real NOT NULL CHECK (score BETWEEN 0 AND 100),
    rank        integer NOT NULL,
    components  jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (week_start, company_id)
);

CREATE TABLE enrichments (
    company_id       uuid NOT NULL REFERENCES companies ON DELETE CASCADE,
    week_start       date NOT NULL,
    status           text NOT NULL CHECK (status IN ('ok', 'partial', 'rejected', 'skipped_budget')),
    claims           jsonb NOT NULL DEFAULT '[]'::jsonb,
    dropped_claims   jsonb NOT NULL DEFAULT '[]'::jsonb,
    proposed_domain  text,
    cost_usd         numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (company_id, week_start)
);

CREATE TABLE api_budget (
    provider  text NOT NULL,
    period    text NOT NULL CHECK (period IN ('minute', 'hour', 'day', 'week', 'month')),
    cap       numeric NOT NULL CHECK (cap >= 0),
    PRIMARY KEY (provider, period)
);

CREATE TABLE api_usage (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       uuid REFERENCES runs ON DELETE SET NULL,
    provider     text NOT NULL,
    units        numeric NOT NULL CHECK (units > 0),
    est_usd      numeric NOT NULL DEFAULT 0,
    reserved_at  timestamptz NOT NULL DEFAULT now(),
    committed    boolean NOT NULL DEFAULT false
);
CREATE INDEX api_usage_provider_time ON api_usage (provider, reserved_at);

CREATE TABLE outputs (
    week_start   date NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('leads_csv', 'newsletter_md', 'run_report')),
    content      text NOT NULL,
    approved_by  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (week_start, kind)
);

CREATE TABLE suppression (
    domain    text PRIMARY KEY,
    reason    text NOT NULL,
    added_at  timestamptz NOT NULL DEFAULT now()
);

-- RLS on, no policies, no FORCE: the owner (pipeline_writer) bypasses it, every other role sees nothing.
DO $$
DECLARE t text;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'sm' LOOP
        EXECUTE format('ALTER TABLE sm.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON sm.%I FROM PUBLIC, anon, authenticated', t);
    END LOOP;
END
$$;

ALTER DEFAULT PRIVILEGES IN SCHEMA sm REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;

RESET ROLE;

-- Migration bookkeeping belongs to pipeline_writer too, so the owner-role backup (pg_dump) can read it.
ALTER SCHEMA sm_meta OWNER TO pipeline_writer;
ALTER TABLE sm_meta.applied OWNER TO pipeline_writer;

-- Pipeline jobs log in as pipeline_login, which only inherits pipeline_writer.
-- Its password is set out of band (ALTER ROLE ... PASSWORD), never in a migration.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pipeline_login') THEN
        CREATE ROLE pipeline_login LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT;
    END IF;
END
$$;
GRANT pipeline_writer TO pipeline_login;
ALTER ROLE pipeline_login SET search_path = sm, extensions;
