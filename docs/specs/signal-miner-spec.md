# Spec: Signal-Mining Pipeline v1

Draft for approval, 2026-11-09. Plan: `/home/user/data-mining-workflow.md`. (unverified) = not in official docs; (opinion) = design choice.

## 1. Objective

v1 is a weekly Python job that ranks newly funded US startups from 4 free sources. It writes a lead CSV and a cited newsletter draft for B2B vendors that sell to those startups. The sources cost $0. Search APIs stay under 90% of their free tiers. Claude spend has a weekly cap.

v1 is done when 3 things hold:
- All ACs pass in CI.
- Two weekly runs in a row finish without manual fixes. Each produces at least 25 leads and a 10-company draft.
- A manual check finds the right domain and a real signal for at least 80% of the top 25.

## 2. Users and outputs

| User | Output |
|---|---|
| Outbound operator | `leads_{week}.csv`: rank, score, company, domain, domain_confidence, state, industry_group, signal_types[], amount_sold, filed_date, first_sale_date, open_roles, tools_named[], why_now, what_they_do, evidence_urls[] |
| Newsletter editor | `newsletter_{week}.md`: top 10, Form D week in numbers, tools rising |
| Pipeline owner | Run report: counts per source, canaries, spend per provider, runner minutes, errors |

- `why_now` and `evidence_urls` come from `signals`.
- `what_they_do` comes only from verified claims.
- The pipeline sends nothing. A person approves each output.

## 3. Scope

In v1: the 4 sources, every stage in section 6, the budget ledger, canaries (warnings only) and the suppression list.

Out of v1:
- LinkedIn and Indeed, through any tool. Their terms ban scraping, and hiQ paid LinkedIn $500,000.
- Apify, G2, Clay, contact data, sending, a paid tier, a UI and Form D person names.
- BigQuery (no DML in the sandbox, no credentials here), the EDGAR Atom feed (100-entry cap) and efts search (undocumented).
- supabase-py (PostgREST cannot do conditional merges), an LLM orchestrator and Haiku tie-breaks.

## 4. Data sources

All requests use one `httpx==0.28.1` client:
- Token buckets per host.
- Retry on 429 and 5xx, at most 5 tries, honoring `Retry-After`.
- A descriptive User-Agent.
- A `robots.txt` check before homepage and careers fetches.

Fields kept are the columns in section 5.

### 4.1 SEC Form D
- Index: `https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx`. Keep rows `D` and `D/A`. Read column offsets from the header lines. The file list is at `.../QTR{n}/index.json`.
- XML: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_dashes}/primary_doc.xml`. Never use the `xslFormDX01/` path, which returns HTML.
- File number: `https://data.sec.gov/submissions/CIK{10-digit}.json` → `filings.recent.fileNumber`, cached per CIK.
- Cadence: daily, with catch-up for missed business days up to 10 back. 2026-11-06 had 172 D and 116 D/A rows.
- Limits: at most 10 requests per second, with calls spaced 0.15 s apart. Every request sends `User-Agent: <Name> <email>` and gzip. A request with no User-Agent gets 403.
- Startup filter (config), all must hold:
  - withinFiveYears or yetToBeFormed.
  - Industry is Computers, Telecommunications, Other Technology or Business Services.
  - Not a Pooled Investment Fund or Limited Partnership.
  - The name does not contain `fund`, `spv` or ` lp`.
- Dedupe: an offering is (cik, file_number), and its latest filing is the current state. CIK 2163963 filed 2 offerings on 2026-11-06.

### 4.2 HN "Ask HN: Who is hiring?"
- Thread: `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring`. Keep titles that start with `Ask HN: Who is hiring?`.
- Posts: `https://hn.algolia.com/api/v1/items/{id}`. `children[]` holds the live top-level posts.
- Cadence: weekly, for the current and previous month, upserted by comment_id. November 2026 (50338728) has 208 posts, 188 with pipes. Algolia allows 10,000 requests per hour per IP.
- Parse steps:
  1. Run `html.unescape`, turn `<p>` into newlines and strip tags.
  2. Split line 1 on `|`.
  3. Take domain and ATS candidates from links.
  4. Match the funding regex `Series [A-E]|seed|raised|\$\d+[mM]`.
- Author handles are not stored.

### 4.3 ATS postings
| ATS | Keyless endpoint | Date | Unknown slug |
|---|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `updated_at` | 404 |
| Lever | `https://api.lever.co/v0/postings/{site}?mode=json`, then `api.eu.lever.co` | `createdAt` | 404 |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | `publishedAt` | 404 |
| Workable | `https://www.workable.com/api/accounts/{subdomain}?details=true` | (unverified) | (unverified) |

- Candidates: domain_confidence of medium or higher and a signal in the last 30 days. Cap: 300 per week.
- Board discovery, in order:
  1. Links in HN posts.
  2. Grep `/`, `/careers` and `/jobs` for `boards.greenhouse.io`, `job-boards.greenhouse.io`, `boards-api.greenhouse.io`, `jobs.lever.co`, `jobs.eu.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com` and `{s}.workable.com`.
  3. Render JavaScript pages through TinyFetch.
  4. Probe slugs such as `acme` and `acme-inc`.
- A probed board is `confirmed` only when its job URLs or text contain the company's domain. Unconfirmed boards emit no signal.
- Postings from a board's first fetch are `is_baseline` and never count as new.
- Ashby jobs with `isListed=false` are excluded.
- No ATS publishes a read limit. The client uses 1 request per second per host. Workable gets 1 request every 2 s, with backoff on Cloudflare 429.

### 4.4 GitHub org activity
- Source: `https://data.gharchive.org/YYYY-MM-DD-H.json.gz`. The hour runs 0 to 23 UTC with no padding. No key is needed, and files are streamed.
- Cadence: weekly, 168 files, plus a one-time 4-week backfill.
- Per org.id per week, count: events, PushEvent, actors, repos, repository CreateEvent, ReleaseEvent and the all-org total. Commit counts are not used, because GitHub removed them on 2025-10-07.
- For the top 200 orgs by ratio, fetch GraphQL `websiteUrl email isVerified name`, with REST `GET /orgs/{org}` as the fallback for `blog`. One PAT allows 5,000 REST requests and 5,000 GraphQL points per hour.
- The 2026-11-08 12:00 file had 5,756 events, and 24% had an org. Volume fell about 7x in 2026, so acceleration uses each org's share of events.

## 5. Data model

Schema `sm` is not exposed through the Data API.
- Role `pipeline_writer` owns the schema.
- RLS is on with no policies and no FORCE.
- `anon` and `authenticated` have all privileges revoked.
- Views use `security_invoker = true`.
- `pg_trgm` lives in schema `extensions`.
- Writes use psycopg `ON CONFLICT DO UPDATE` with `LEAST`/`GREATEST`. Each batch is deduped in Python first.

Tables:
- `runs`(run_id uuid PK, job, started_at, finished_at, status ∈ running/ok/partial/failed, stats jsonb, error)
- `source_health`(source, run_id, metric, value, threshold, ok) PK (source, run_id, metric)
- `companies`(company_id uuid PK, name, normalized_name, domain UNIQUE, domain_confidence ∈ high/medium/low/none, hq_state); GIN `extensions.gin_trgm_ops` on normalized_name
- `company_aliases`(source, external_id, company_id FK cascade, match_method, match_score) PK (source, external_id)
- `er_review_queue`(id PK, source, external_id, candidate_id, score, decision ∈ pending/merge/distinct)
- `form_d_filings`(accession PK, cik, file_number, form_type ∈ D, D/A, filed_date, previous_accession, amount_sold)
- `form_d_offerings`(cik, file_number, latest_accession FK, latest_filed_date, entity_name, entity_type, city, state, year_of_inc_bucket, industry_group, revenue_range, offering_amount, offering_indefinite, amount_sold, date_of_first_sale, first_sale_yet_to_occur, related_person_count, passes_startup_filter, company_id) PK (cik, file_number)
- `hn_threads`(story_id PK, month UNIQUE)
- `hn_posts`(comment_id PK, story_id FK, created_at, header_raw, company_raw, stage_hint, remote_flags[], links[], funding_hint, tool_ids[], body_trunc, parse_confidence ∈ high/low, last_seen_at, company_id)
- `tool_dictionary`(tool_id PK, name UNIQUE, patterns[], is_competitor)
- `ats_boards`(ats, slug, region, company_id, discovered_via, confirmed, fail_count, checked_at) PK (ats, slug, region)
- `job_postings`(ats, slug, region, job_id, title, url, posted_at, updated_at, is_baseline, tool_ids[], migration_phrase, first_seen_at, last_seen_at, closed_at) PK (ats, slug, region, job_id), FK board
- `gh_org_weekly`(org_id, week_start, login, total, push, actors, repos, repo_creates, releases, window_total, share, ratio) PK (org_id, week_start), kept 12 weeks
- `gh_orgs`(org_id PK, login, website_url, email_domain, is_verified, domain, domain_confidence, company_id)
- `signals`(signal_id PK, company_id FK, source, signal_type, source_ref, observed_at, strength 0–1, evidence_url NOT NULL, details jsonb) UNIQUE (source, signal_type, source_ref)
- `weekly_scores`(week_start, company_id, score 0–100, rank, components jsonb) PK (week_start, company_id)
- `enrichments`(company_id, week_start, status ∈ ok/partial/rejected/skipped_budget, claims jsonb, dropped_claims jsonb, proposed_domain, cost_usd) PK (company_id, week_start)
- `api_budget`(provider, period, cap) PK (provider, period)
- `api_usage`(id PK, run_id, provider, units, est_usd, reserved_at, committed)
- `outputs`(week_start, kind ∈ leads_csv/newsletter_md/run_report, content, approved_by) PK (week_start, kind)
- `suppression`(domain PK, reason, added_at)
- View `weekly_leads` applies the eligibility rules in section 7.

Raw XML, HTML and GH Archive files are never stored. An alert fires at 400 MB of the 500 MB cap.

## 6. Pipeline stages

Jobs run on GitHub Actions in a private repo. They connect through the Supavisor session pooler on port 5432, which is reachable over IPv4.

| Workflow | Cron (UTC) | Steps |
|---|---|---|
| `daily` | `23 7 * * *` | Form D ingest, size check, heartbeat against project pausing |
| `weekly` | `17 9 * * 1` | All stages |
| `backup` | `47 10 * * 1` | `pg_dump` as the owner role |

1. **Ingest.** The loader validates pydantic models, redacts emails and phone numbers, then upserts.
2. **Normalize.**
   - Names: lowercase, NFKC, strip parentheticals and the suffixes inc, llc, corp, co, ltd, plc and gmbh. Keep "labs" and "technologies".
   - Domains: reduce with `tldextract`. Deny github.io, medium.com, linktr.ee, x.com, twitter.com, linkedin.com, crunchbase.com, sec.gov, ATS hosts and free mail.
3. **Domain resolution** for issuers that pass the filter:
   - Search `"<entity_name>" <city> <state>` and fetch the top 5 homepages.
   - Accept a homepage at medium confidence when every name token of 3 or more characters appears in its title, h1 or footer.
   - When 2 candidates pass, the issuer goes to review.
4. **GitHub ladder.**
   - isVerified gives high.
   - Website and email domains that agree give medium.
   - Only one of them present gives low. Low becomes medium when the homepage links to `github.com/<login>`.
   - A github.io website gives none.
5. **Entity resolution (ER)**, in order:
   1. Exact external id.
   2. Exact domain.
   3. Same-source name similarity of at least 0.85, with the same known state.

   Cross-source name-only matches go to review. So do matches from 0.60 to 0.85. Agent output never sets a domain and never triggers a merge.
6. **Score** (section 7).
7. **Enrich** the top 25 eligible companies.
   - Isolation: a separate `enrich` process gets only ANTHROPIC_API_KEY, PATH, HOME, ROUTER_URL and ROUTER_TOKEN. The parent process keeps the provider keys and serves `web_search` and `web_fetch` as a local MCP server.
   - Runs: one `claude-agent-sdk==0.2.179` `query()` per company, 2 at a time, with `max_turns=12`, at most 6 searches and at most 10 fetches.
   - Lockdown, 3 layers:
     - `allowed_tools` lists only `web_search` and `web_fetch`.
     - `disallowed_tools` lists Bash, Write, Edit, NotebookEdit, WebFetch, WebSearch and Task.
     - `can_use_tool` denies and logs any other tool.
   - `web_fetch` rules:
     - https only.
     - Private, loopback and link-local IPs are blocked after DNS resolution.
     - linkedin.com and indeed.com are blocked.
     - Pages return as 20 KB of text wrapped as `untrusted_page`, and the text is cached.
   - Output: `{what_they_do, proposed_domain, claims[{text, url, quote}]}`.
   - Verifier: drops a claim whose URL was not fetched this run, or whose quote is missing from the cached text. A newsletter claim also needs a sec.gov URL or 2 domains.
8. **Output.**
   - The CSV and the Markdown draft are built from `weekly_leads`.
   - Each newsletter company gets a structured line (amount, signals, EDGAR link), even when 0 claims survive.
   - A banner lists degraded sources.

## 7. Signal scoring formula

| Signal | Fires when | Strength s |
|---|---|---|
| form_d_raised | Filtered offering, original D in window, sold > 0 | `0.5 + 0.5a`, `a = clamp(log10(sold/250,000)/2, 0, 1)` |
| form_d_filed | Original D, sold 0 or yetToOccur | 0.4, labelled "filed, $0 sold" |
| form_d_amend | D/A with sold above the previous filing | `0.3 + 0.3a` |
| hn_post | Thread from the last 45 days | `0.6 + 0.2·funding + 0.2·tool` |
| jobs_tools | Confirmed board | `0.7·min(1, n_tool/3) + 0.3·min(1, n_new/10)`; migration phrase → 1 |
| gh_accel | ratio ≥ 2, events ≥ 30, actors ≥ 3, 4 non-zero baseline weeks, domain ≥ medium | `min(1, log2(ratio)/3)` |

- A Form D signal whose first sale came more than 90 days before filing gets half strength.
- `ratio = (share_W + 1e-7) / (median(share_{W−4..W−1}) + 1e-7)`. Days with fewer than 20 hourly files are left out of the baseline.
- Decay is `d = 0.5^(age_days/14)`. The window is 28 days, or 45 days for HN.
- Score:
  - `p_src = max(w·s·d)`, with weights form_d 0.6, jobs 0.5, hn 0.4 and github 0.3 (opinion).
  - `score = 100·(1 − Π(1 − p_src))`.
  - One Form D signal with a = 1 at age 0 scores 60.
- Ties break on source count, then latest observed_at, then company_id.
- A company is eligible when all of these hold:
  - Score is at least 20.
  - Domain confidence is medium or higher.
  - The domain is not suppressed.
  - At least 1 signal comes from a confirmed source.

## 8. Search API routing and budget caps

Each paid call first reserves units in `api_usage` inside a transaction. A reservation over the cap is refused, and the router moves to the next provider. When every provider is spent, the item gets `skipped_budget` and the run ends `partial`.

| Call | Chain, first to last |
|---|---|
| Search | TinyFish → Tavily basic (1 credit) → Exa Lite ($4/1k) → Parallel ($1/1k, off without a card) |
| Fetch | httpx → TinyFetch → Firecrawl scrape, markdown only |

| Provider | Free tier | v1 cap |
|---|---|---|
| TinyFish Search | 30/min, 500/h | 450/h |
| TinyFetch | 150/min, 1,000/day | 900/day |
| Tavily | 1,000 credits/month | 900/month |
| Firecrawl | 1,000 credits/month, 10/min, JSON 5/page | 900/month, JSON banned |
| Exa | $10/month + $10 bonus | $9/month |
| Parallel | $5/month with card | $4.50/month |
| GitHub | 5,000 REST/h, 5,000 GraphQL/h | 1,200/week |
| Claude | none (unverified) | $0.40/company, $10/week (opinion) |

## 9. Security and compliance

- Secrets live in GitHub Actions secrets and are masked in logs. v1 uses no Supabase API key.
- The agent process holds no DB credentials, PAT or provider keys. Injected text can reach an output only through a fetched URL with a matching quote.
- No person names or HN handles are stored. Emails and phone numbers are redacted.
- Deleting a company cascades to every related row.
- `suppression` records CAN-SPAM opt-outs and blocks those domains from export.
- A GDPR legitimate-interest assessment and a CCPA data-broker review come before the first outbound use (Q5).

## 10. Acceptance criteria

Fixtures are trimmed real responses from 2026-11-09. [live] marks a check that runs outside CI.

1. SEC client:
   - Without `SEC_USER_AGENT` it fails and makes 0 calls.
   - 20 sec.gov calls take at least 2.8 s.
   - A 429 response's `Retry-After` is honored.
2. The `form.20261106.idx` fixture yields 172 D and 116 D/A rows. A copy with columns shifted 3 characters parses the same. A weekend or holiday date returns `ok` with 0 rows.
3. XML fixtures (2163963, and 2153109/000215310926000002) parse as follows:
   - `Indefinite` gives null plus a flag.
   - `yetToOccur` gives a flag.
   - The previous accession is 0002153109-26-000001.
   - Malformed XML is counted, not raised.
4. The CIK 0002153109 submissions fixture yields file number 021-596118. That file number produces 1 offering, with the D/A as latest. CIK 2163963 produces 2 offerings. Reloading a day changes 0 rows.
5. The startup filter excludes a fund, an overFiveYears issuer, an LP and "Acme Fund I LP". It includes a withinFiveYears Computers issuer.
6. HN discovery picks 50338728 and never 50338726. The loader stores one row per top-level child, and a re-run changes 0 rows.
7. HN parser:
   - The Shepherd header gives `Shepherd`, `Series B` and ONSITE.
   - A header with no pipe gives `low`.
   - No `&#x2F;` or `&amp;` is stored.
8. `jane@acme.io` and `+1 415 555 0100` are stored as `[email]` and `[phone]`. No text in `sm` matches an email regex.
9. Careers fixtures yield ashby/ramp, ashby/linear and greenhouse/vercel.
10. A 404 counts as a miss. Lever is probed in both regions. A same-name board on another company's domain stays unconfirmed.
11. A board's first fetch yields 0 new postings. A second fetch with 3 added postings yields 3 new. A posting missing from a later fetch gets closed_at.
12. A Workable 429 with error 1015 triggers backoff. [live] Workable stays in v1 only if 3 known boards return 200.
13. The tool matcher finds ClickHouse and OpenTofu, does not match `go` inside `google`, and flags "migrating from Datadog".
14. Ledger:
   - A reservation over the cap is refused.
   - 20 concurrent reservations never exceed a cap.
   - When every provider is spent, the result is `skipped_budget`.
15. Domain resolution:
   - Denylisted domains are rejected.
   - 2 passing candidates add a review row.
   - A path that robots.txt disallows is skipped.
16. Auto-merge precision is at least 0.95 on a 50-pair Form D gold set, which slices 3 and 7 extend. Cross-source name-only matches never merge, and Acme Labs never merges with Acme Technologies.
17. The GitHub ladder gives high for a verified org and none for a github.io website. It raises low to medium when the homepage links to the org.
18. GitHub aggregation:
   - A 500-event excerpt of `2026-11-08-12.json.gz` matches a jq reference.
   - Baseline shares 0.0010, 0.0012, 0.0009 and 0.0011 with W at 0.0030 give ratio 2.857 and s 0.505.
   - An org with 3 baseline weeks gets 0.
19. Scoring:
   - 6 hand-computed fixtures match within 0.01. One case is a single Form D with a = 1 at age 0, which scores 60.0.
   - 100 property cases stay in [0, 100] and never fall when a signal is added.
20. Lockdown without a model: the built options list exactly 2 allowed tools and 7 disallowed tools. `can_use_tool` denies and logs Bash, Write and WebFetch. [live] A run against 3 injection pages executes 0 other tools.
21. The enrich process asserts at start that no env var matches `SUPABASE|DATABASE|PG|GITHUB_PAT|TAVILY|EXA|FIRECRAWL|PARALLEL|TINYFISH`.
22. `web_fetch` and the verifier refuse `http://`, `file://`, loopback, link-local, 10.0.0.0/8, hosts that resolve to private IPs, linkedin.com and indeed.com.
23. Verifier:
   - It drops a claim with an unfetched URL.
   - It drops a misquote ("$5B" quoted for "$500M").
   - It drops a newsletter claim backed by 1 non-SEC domain.
   - `proposed_domain` never reaches `companies`.
24. A stubbed cost of $0.30 per turn stops enrichment at the cap. [live] cost_usd is measured on 2 companies before the cap is set.
25. CSV:
   - It has exactly the columns in section 2.
   - Every evidence URL exists in `signals`.
   - No row has a low-confidence or suppressed domain.
   - No row rests only on unconfirmed signals.
26. An end-to-end fixture run produces a byte-identical `leads.csv` twice.
27. Database:
   - `pipeline_writer` reads and writes every table.
   - `anon` gets 42501.
   - RLS is on for every table.
   - No column holds email, phone or address.
   - `pg_dump` restores into an empty Postgres 15.
28. Canary fixtures mark the source degraded for 0 idx rows on a business day, a pipe share under 70% and ATS errors over 20%.
29. No log line holds a secret. linkedin.com and indeed.com appear only in denylists.
30. [live] A weekly run takes under 45 minutes, stays within caps and yields at least 25 leads, two weeks in a row.

## 11. Slices

Each slice is built test-first, and CI must be green before merge. CI uses a Postgres 15 container.

Pins (Python 3.12):

| Package | Version |
|---|---|
| httpx | 0.28.1 |
| psycopg[binary] | 3.3.8 |
| pydantic | 2.14.3 |
| claude-agent-sdk | 0.2.179 |
| tinyfish | 0.11.0 |
| tavily-python | 0.8.5 |
| exa-py | 2.28.0 |
| firecrawl-py | 4.55.0 |
| parallel-web | 1.3.7 |
| pytest | 9.2.0 |
| respx | 0.23.1 |

| # | Slice | Days | ACs |
|---|---|---|---|
| 0 | Skeleton, HTTP client, migrations, roles, ledger, CI. SDK spike on MCP tools and `can_use_tool`; its pass or fail picks the enrichment design | 1.5 | 1, 14, 27 |
| 1 | Form D to a Form D-only ranked CSV | 2 | 2–5, 19 |
| 2 | Router, domain resolution, normalizer, ER core | 1.5 | 15, 16 |
| 3 | HN | 1 | 6–8 |
| 4 | ATS jobs | 2 | 9–13 |
| 5 | Enrichment and verifier | 2 | 20–24 |
| 6 | Full scoring, outputs, canaries, workflows, backup | 1.5 | 25, 26, 28–30 |
| 7 | GitHub acceleration | 2 | 17, 18 |

The slices total 13.5 days. If slice 7 slips, the github term scores 0 and the other terms are unchanged.

## 12. Open questions

1. Which vendor's tools seed `tool_dictionary`?
2. Which name and email go in the SEC User-Agent?
3. Will a card go on file for Parallel, or does Parallel stay off?
4. Is $10 a week the right Claude cap, and which model IDs?
5. Who runs the GDPR and CCPA review before the first outbound use?
6. Should the filter add Biotechnology and Other Health Care?
7. Supabase Free with the heartbeat, or Pro (price not checked)?
8. Does the private repo have enough Actions minutes (not checked)? If not, the backfill runs locally.

## 13. Unverified assumptions

- Lever and Ashby return 200 with `[]` for empty boards.
- Workable's date field and its reply for an unknown slug.
- Read terms for Greenhouse, Ashby and Workable.
- How reliable slug probing is.
- The org-to-domain ladder is a recommended design, not a GitHub method.
- CreateEvent `ref_type` survived the 2025-10-07 payload trim.
- PullRequestEvent is no longer emitted on merge (community report).
- 168 hourly files come to about 0.7 to 0.9 GB (extrapolated).
- The size of a 2026 BigQuery day table.
- In SDK 0.2.179: whether `allowed_tools` now restricts built-in tools (issue #361), the HTTP MCP config option, the cost field name and the `max_turns` option.
- The Anthropic API has no free tier.
- A daily heartbeat keeps a Supabase Free project from pausing.
- PostgREST cannot express conditional merges (inferred).
- pg_trgm defaults are 0.3 and 0.6.
- Holidays behave like weekends in the daily index.
- Exa REST endpoints.
- lxml and tldextract versions.
- Startups mostly use the 4 ATSs (opinion).
- Not used in v1 and unchecked: the edgartools Form D class, ats-scrapers, and third-party HN parsers and indexes.

Slice 1 produces the first ranked Form D lead list on day 3.5.