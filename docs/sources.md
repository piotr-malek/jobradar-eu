# Sources

Generated from `src/jobradar/defaults/sources.yaml` by `jr sources --markdown > docs/sources.md`; edit the YAML
(or an adapter's docstring), not this page.

Which of these run for you depends on your setup (`jr sources` lists them with on/off):

- **Covers**: countries the board lists jobs for. `EU` boards (pan-European or remote) always run. A country board
  runs when that country is in your `profile.yaml` (home or work countries).
- **Needs**: `key` sources stay off until the named environment variables exist (repository secrets on GitHub).
  `opt-in` sources run only when you switch that track on in `user/sources.yaml`. `packs` sources run only when
  one of those role packs is active; "IT roles only" / "business roles only" sources need an active pack of that kind.
- **Runs on**: `github` runs in your config repo's nightly workflow. `local` boards block GitHub's IP addresses,
  so they run only from your own computer (`jr run --runner local`). `any` runs in both places.
- **Every**: how many days between runs.

When the same job appears on several sources, the link kept is the source with the lowest rank: company ATS 10 ·
open job datasets 15 · national employment services 35 · national boards 40 · regional boards 45 · remote boards
and agencies 50 · aggregators 55-60.

Company ATS boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, Personio, Teamtailor, Workday
and others) are not listed here: Full mode polls the boards in `defaults/ats_registry.csv.gz` with `jr ats`.

## Pan-European and remote

| Source | Covers | How | Every | Runs on | Needs | Notes |
|---|---|---|---|---|---|---|
| Arbeitnow (`arbeitnow`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github |  |  |
| 80,000 Hours job board (`eighty_k_hours`) | EU | Generic Algolia search adapter (public search-only keys embedded in the boards' own frontends). | 3d | github |  |  |
| Job-alert e-mails (IMAP) (`email_alerts`) | EU | Job-alert e-mails read over IMAP: every job in the mails from the senders the user listed in the wizard (`user/sources.yaml: alert_senders`), for boards we can't or don't fetch (LinkedIn: robots.txt; boards behind Cloudflare such as pracuj.pl, jobs.bg, eJobs, StepStone). | 1d | github | key: `JR_IMAP_USER`, `JR_IMAP_PASSWORD` | every job in the mails from the senders listed in the wizard (LinkedIn, boards behind Cloudflare, any board's alerts); no descriptions, links never opened |
| Escape the City (`escapethecity`) | EU | Generic Algolia search adapter (public search-only keys embedded in the boards' own frontends). | 3d | github |  |  |
| EURES (`eures`) | EU | EURES job-vacancy search (the portal's own public JSON backend, no key). | 2d | github |  |  |
| GetDataEngineeringJobs (`getdataengineeringjobs`) | EU | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 1d | github | packs: data | getdataengineeringjobs.com: data-engineering jobs only, aggregated from ATS boards and job boards; listing filters (EU countries, remote open to the home country, last 7 days) -> JobPosting JSON-LD + facts list (work mode, remote eligibility); links are the board's page (robots disallow its /apply/ redirect) |
| Get on Board (`getonbrd`) | EU | Get on Board public API (LATAM-centric tech board, no key). | 3d | github | IT roles only | LATAM-centric; kept for fully-remote roles |
| Getro VC portfolio boards (`getro`) | EU | Getro-hosted VC portfolio job boards (Atomico, Point Nine, Creandum, Speedinvest, Earlybird, MMC, OpenOcean, firstminute, byFounders, Techstars, Accel …) — company jobs from the boards' own public pages. | 1d | github |  |  |
| Himalayas (`himalayas`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github |  |  |
| Hacker News "Who is hiring" (`hn_whoishiring`) | EU | Remote-job boards with free public JSON/RSS feeds. | 7d | github | IT roles only |  |
| Jobgether (`jobgether`) | EU | Jobgether's public API for agents (remote-friendly jobs by country). | 2d | github |  | official agents API (robots allows /astroapi/ai/jobs.json, crawl-delay 2); title-only rows |
| Jobicy (`jobicy`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github |  |  |
| Indeed (JobSpy) (`jobspy_indeed`) | EU | Indeed via python-jobspy (optional dependency: `pip install jobradar-eu[jobspy]`). | 2d | local | **off by default** |  |
| Landing.jobs (`landingjobs`) | EU | Landing.jobs (PT/EU tech board). | 2d | local | IT roles only |  |
| LinkedIn (Apify) (`linkedin_apify`) | EU | Sources that need the user's API keys: TheirStack, Apify (LinkedIn), ITJobs.pt. | 2d | github | key: `APIFY_API_KEY`; **off by default** | LinkedIn via Apify actor curious_coder/linkedin-jobs-scraper; $0.002/result on the free plan; ~100 results/run (5 searches × 20) every 2 days |
| LinkedIn (guest search) (`linkedin_guest`) | EU | LinkedIn's logged-out job search pages. | 1d | github | **off by default** |  |
| Open Jobs Data (`open_jobs_data`) | EU | ConorsCode/open-jobs-data: a daily GitHub-Actions snapshot of ~38k postings from ~9 ATS platforms (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, ...). | 1d | github |  | ConorsCode/open-jobs-data daily snapshot (new-jobs.json); titles/locations only, no descriptions |
| RemoteLeaf (`remoteleaf`) | EU | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github |  | remote jobs; applicantLocationRequirements -> countries/remote_scope; slug carries the country; USD salaries |
| Remote OK (`remoteok`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github |  |  |
| Remote Rocketship (`remoterocketship`) | EU | Remote Rocketship (remoterocketship.com) — remote-only jobs from the SSR `__NEXT_DATA__` of its listing pages. | 2d | github |  |  |
| Remotive (`remotive`) | EU | Remotive public API, one call per category or search term. | 1d | github |  |  |
| RevPath (RevOps / GTM jobs) (`revpath`) | EU | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 1d | github | packs: gtm-revops | revpath.dealhub.io (DealHub): RevOps/GTM-only board, ~8.8k jobs, US-heavy with UK/EU; 3 job sitemaps without lastmod, numeric ids grow with recency; JobPosting JSON-LD; apply links go to the company |
| TheirStack (`theirstack`) | EU | TheirStack job API (key; one credit per job returned): recent jobs in eligible countries whose titles match the active packs. | 7d | github | key: `THEIR_STACK_API_KEY`; Apply mode only |  |
| The Muse (`themuse`) | EU | The Muse public API (no key: 500 req/h). | 2d | github |  |  |
| We Work Remotely (`weworkremotely`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github | **off by default** |  |
| Workable job search (`workable_search`) | EU | jobs.workable.com, the cross-company search over every Workable career page. | 2d | github |  | jobs.workable.com cross-company search: full descriptions; Europe + EU/EEA country slices, all English |
| Working Nomads (`workingnomads`) | EU | Remote-job boards with free public JSON/RSS feeds. | 1d | github |  |  |

## Country boards

| Source | Covers | How | Every | Runs on | Needs | Notes |
|---|---|---|---|---|---|---|
| Jooble (`jooble_<cc>`) | AT, BE, BG, HR, CZ, DK, FI, FR, DE, GR, HU, IE, IT, NL, PL, PT, RO, SK, ES, SE, NO, CH | Jooble REST API (free key on request). | 1d | github | key: `JOOBLE_{CC}_API_KEY` | {cc}.jooble.org: republishes national boards we can't fetch (Cloudflare) and company pages |
| Adzuna (`adzuna`) | AT, BE, DE, ES, FR, IT, NL, PL, CH | Adzuna search API (developer.adzuna.com; free key: ADZUNA_APP_ID + ADZUNA_APP_KEY). | 1d | github | key: `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`; Apply mode only | free key (developer.adzuna.com); job links must be Adzuna's redirect_url (their terms); ~500-character snippets |
| karriere.at (`karriere_at`) | AT | karriere.at (Austria) — the site's own JSON search endpoint + schema.org JobPosting on detail pages. | 3d | github |  |  |
| Engineering.jobs Belgium (`engineeringjobs_be`) | BE | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | IT roles only | be.engineering.jobs: IT + engineering, English; apply needs a board login |
| ICTjob.be (`ictjob_be`) | BE | ICTjob.be — Belgian IT job board (server-rendered HTML with schema.org microdata). | 3d | github | IT roles only |  |
| DEV.BG (`devbg`) | BG | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | IT roles only | dev.bg: Bulgaria's main IT board (~1.3k live jobs), WordPress job_listing sitemap with lastmod, JobPosting JSON-LD; English slugs |
| ICTjobs.ch (`ictjobs_ch`) | CH | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 3d | github | IT roles only | ictjobs.ch: IT-only Swiss board (~180 jobs), some English; apply on company sites |
| jobs.ch (`jobsch`) | CH | jobs.ch (Switzerland) — server-rendered search pages + schema.org JobPosting JSON-LD. | 3d | github |  |  |
| jobup.ch (`jobup_ch`) | CH | jobs.ch (Switzerland) — server-rendered search pages + schema.org JobPosting JSON-LD. | 3d | github |  | jobup.ch, the French-speaking JobCloud site (same markup as jobs.ch); ~half English titles, little overlap with jobs.ch |
| SwissDevJobs (`swissdevjobs`) | CH | Generic RSS / Atom job-feed adapter (WordPress, WP Job Manager, Hugo, custom feeds). | 2d | local | IT roles only |  |
| CyprusJobs (`cyprusjobs`) | CY | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github |  | cyprusjobs.com: English, gaming/fintech tech jobs, work format Remote/Hybrid in the page; board apply form |
| StartupJobs.cz (`startupjobs_cz`) | CZ | StartupJobs.cz (Czechia/Slovakia startups) via the API Platform backend its Nuxt frontend calls. | 3d | github |  |  |
| Bundesagentur für Arbeit (`arbeitsagentur`) | DE | Bundesagentur für Arbeit Jobsuche (official, public client id). | 2d | github |  |  |
| Berlin Startup Jobs (`berlinstartupjobs`) | DE | Generic RSS / Atom job-feed adapter (WordPress, WP Job Manager, Hugo, custom feeds). | 1d | github |  |  |
| GermanTechJobs (`germantechjobs`) | DE | Generic RSS / Atom job-feed adapter (WordPress, WP Job Manager, Hugo, custom feeds). | 2d | github | IT roles only |  |
| The Hub (Nordics) (`thehub`) | DK, SE, NO, FI | The Hub (thehub.io) — Nordic/European startup jobs via the public JSON API its Nuxt frontend uses. | 2d | github |  |  |
| Jobindex (`jobindex`) | DK | Jobindex.dk (Denmark) — public RSS search feed. | 3d | github |  |  |
| CV.ee (`cv_ee`) | EE | CV-Online platform — cv.ee (Estonia), cv.lv (Latvia) and cvonline.lt (Lithuania): one adapter, one entry per site. | 2d | github |  |  |
| BarcelonaJobs (`barcelonajobs`) | ES | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github |  | barcelonajobs.com (Job Boardly): English, multinationals in Barcelona; apply links go to the company ATS |
| JobFluent (`jobfluent`) | ES | Jobfluent (Spain; English-friendly startup/tech jobs) — city sitemaps + server-rendered detail pages. | 2d | github |  |  |
| Tecnoempleo (`tecnoempleo`) | ES | Tecnoempleo (Spain, largest IT board) — search result pages + schema.org JobPosting detail pages. | 2d | github | IT roles only |  |
| Welcome to the Jungle (`wttj`) | FR | Welcome to the Jungle (welcometothejungle.com) via the public Algolia search index its frontend uses. | 1d | github |  |  |
| CV.lt (`cv_lt`) | LT | cv.lt — Lithuanian job board (server-rendered HTML, no JSON-LD). | 2d | github |  |  |
| CVonline.lt (`cvonline_lt`) | LT | CV-Online platform — cv.ee (Estonia), cv.lv (Latvia) and cvonline.lt (Lithuania): one adapter, one entry per site. | 2d | github |  |  |
| Unicorns.lt (`unicorns_lt`) | LT | Unicorns Lithuania (unicorns.lt) — job board of the Lithuanian startup association. | 2d | github |  |  |
| CV.lv (`cv_lv`) | LV | CV-Online platform — cv.ee (Estonia), cv.lv (Latvia) and cvonline.lt (Lithuania): one adapter, one entry per site. | 2d | github |  |  |
| JobsInMalta (`jobsinmalta`) | MT | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github |  | jobsinmalta.com: English, iGaming/fintech/IT; apply needs a board login |
| Engineering.jobs Netherlands (`engineeringjobs_nl`) | NL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | IT roles only | nl.engineering.jobs: IT + engineering; ~40% English (the matcher's language rule drops Dutch-required); apply needs a board login |
| Magnet.me (`magnet_me`) | NL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github |  | magnet.me (NL): English sitemap (~4.8k), JobPosting JSON-LD; robots disallow only /applyExternally, /page/, ?query= |
| NAV (Norway) (`nav_no`) | NO | NAV Job Vacancy Feed (pam-stilling-feed): a change feed, not a search API. | 1d | github |  | change feed; public rotating token unless NAV_FEED_TOKEN is set (free on request) |
| Bulldog Job (`bulldogjob`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 1d | github | IT roles only |  |
| Experis Poland (`experis_pl`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | opt-in: polish_only; IT roles only | Experis PL (agency flag via matching.yaml); sitemap lists /en/ and /pl/ copies of every job -> only /en/ is fetched |
| Hays Poland (`hays_pl`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | opt-in: polish_only | recruitment agency (agency flag via matching.yaml); hiringOrganization is always Hays; JSON-LD salary unit can be wrong (DAY vs PLN/h) |
| Just Join IT (`justjoin`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 1d | github | IT roles only |  |
| Michael Page Poland (`michaelpage_pl`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | opt-in: polish_only | Michael Page PL (agency flag via matching.yaml); keyword listings (all results on one page, no paging) -> job-detail JSON-LD |
| No Fluff Jobs (`nofluffjobs`) | PL | NoFluffJobs: server-rendered listing pages → (title gate) → offer pages' JSON-LD. | 1d | github | IT roles only |  |
| Rocket Jobs (business roles) (`rocketjobs`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | business roles only |  |
| Sii Poland (`sii_pl`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 2d | github | opt-in: polish_only; IT roles only | Sii Polska (agency flag via matching.yaml); robots disallow only /oferty-pracy/all/all/* and /*/feed/ |
| Solid.Jobs (`solidjobs`) | PL | Generic robots-friendly board adapter: XML sitemap → URL pre-filter → detail pages → schema.org JobPosting. | 1d | github | IT roles only |  |
| ITJobs.pt (`itjobs_pt`) | PT | Sources that need the user's API keys: TheirStack, Apify (LinkedIn), ITJobs.pt. | 2d | github | key: `ITJOBS_PT_API_KEY`; Apply mode only; IT roles only |  |
| Teamlyzer (`teamlyzer`) | PT | Teamlyzer (Portugal, IT-only) — list pages + schema.org JobPosting detail pages. | 2d | github | IT roles only |  |
| Arbetsförmedlingen (JobTech) (`jobtech_se`) | SE | Arbetsförmedlingen / JobTech JobSearch (open, no key): GET /search?q=&limit<=100&offset=. | 3d | github |  |  |

## Purpose-driven employers (opt-in)

| Source | Covers | How | Every | Runs on | Needs | Notes |
|---|---|---|---|---|---|---|
| ESA (`esa`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
| EU institutions (EPSO) (`eu_careers`) | EU | EU institutions + agencies. | 2d | github | opt-in: purpose | EU institutions + agencies; server-rendered list (/jobs_en?page=N, 20/page) + detail pages for title-relevant, unstored vacancies |
| ICRC (`icrc`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
| ILO (`ilo`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
| Impactpool (`impactpool`) | EU | Impactpool (https://www.impactpool.org): aggregator of UN / INGO jobs (UNICEF, WFP, UNDP, UNHCR, …). | 2d | github | opt-in: purpose |  |
| ITU (`itu`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
| UN Careers (`un_careers`) | EU | UN Secretariat + funds/programmes on careers.un.org. | 2d | github | opt-in: purpose | careers.un.org public JSON list (POST, 100/page, full description in list; INT internships skipped); robots.txt is a CloudFront 403 |
| UNESCO (`unesco`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
| UNIDO (`unido`) | EU | SAP SuccessFactors Career Site Builder (UN agencies: ITU, UNESCO, ILO, …): sitemap → detail pages. | 3d | github | opt-in: purpose |  |
