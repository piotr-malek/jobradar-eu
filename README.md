# Job Radar

Your IT job shortlist, ready every morning. Job Radar searches European job boards and company career pages
overnight, keeps the jobs that match your roles, location and seniority, and lines them up best first. In the
morning you open the app, pick the ones worth applying to, and apply.

- **Nothing runs on your computer overnight.** The search runs on GitHub Actions in your own private repository.
  Your computer is only used to pick jobs and apply.
- **About 80 job boards and public APIs** across 19 countries plus pan-European and remote boards, and in Full mode
  about 5,700 company career pages (Greenhouse, Lever, Ashby, Workable, Personio, Teamtailor, Workday and more).
  See [docs/sources.md](docs/sources.md).
- **Role packs** for data, backend, frontend, full-stack, mobile, DevOps/SRE/platform, QA, security, ML/AI,
  product/PM and GTM/RevOps, with a seniority range and your own "never" words.
- **Free to run.** The only possible cost is an LLM, which is optional in Lite.

## Install

You need Python 3.12 or newer and a GitHub account.

```sh
uv tool install jobradar-eu        # or: pipx install jobradar-eu
jr setup
```

`jr setup` opens a setup wizard in your browser. It walks you through every step and tests each account and key as
you add it, and you can close it and continue later. It checks the GitHub CLI and its login, creates your private
config repository, sets its secrets, and schedules the overnight run in your time zone.

After that, each morning:

```sh
jr app
```

The wizard can also put a shortcut on your desktop.

## Lite or Full

| | Lite | Full |
|---|---|---|
| Job boards and public APIs for your countries | ✓ | ✓ |
| Company career pages (ATS boards) | optional, small set | ✓ |
| Jobs ranked best first, pick / skip / later, status up to offer | ✓ | ✓ |
| LLM screen: fit score, summary, watch-outs, salary | optional | ✓ |
| Answer bank, prepared application forms, drafts, CV and cover-letter PDFs | | ✓ |
| Auto-fill in your own Chrome (you always click Submit) | | ✓ |
| Weekly stats: applications, replies by source and role | | ✓ |
| GitHub Actions minutes per month (2,000 free) | about 300-450 | about 1,400 |

You can switch from Lite to Full later with `jr setup --upgrade`. Your jobs and decisions are kept.

## What it costs

| Service | Needed for | Cost |
|---|---|---|
| GitHub account, private repository, Actions | both | free (2,000 minutes a month) |
| GitHub CLI (`gh`) | both | free |
| Job boards, national job services, company career pages | both | free |
| Jooble, TheirStack and ITJobs.pt API keys | optional | free plans |
| Gmail (or any) SMTP/IMAP with an app password: morning e-mail, job-alert e-mails | optional | free |
| Playwright + Chromium for PDFs | Full | free |
| LLM (Mistral, Gemini, Groq, OpenRouter, OpenAI, Anthropic) | optional in Lite, needed in Full | free tiers exist; otherwise pay per use. The wizard shows an estimate per night. |

A local model (Ollama, LM Studio) works for drafts in the app, but not for the overnight screen, which runs on GitHub.

## How it works

1. **Overnight**, the workflow in your config repository installs Job Radar, restores yesterday's database, reads
   the decisions you made in the app, fetches the sources that are due, removes duplicates, and matches every job
   against your profile. Full also polls company boards and screens new matches with the LLM. The results are saved
   as one commit on the repository's `state` branch.
2. **In the morning**, `jr app` downloads that database and shows the shortlist. Each pick, skip or status change is
   saved on your computer and pushed to the `picks` branch, so skipped jobs never come back and the next run knows
   what you applied to.

**Your data.** The config repository is private and holds your matching settings: home city, languages, roles,
countries, sources and LLM choice. API keys go into its encrypted Actions secrets. Your contact details, CV, answer
bank and generated documents stay in `~/.job-radar` on your computer and are never pushed.

## Being a good guest on job boards

Job Radar reads public listings the way a careful person would, and asks the same of anyone changing it:

- It uses public APIs and feeds where a board offers them. About half of the sources also check robots.txt on every
  run; the others were checked against it when they were added.
- Each source has a small request budget per run, and requests are spaced out.
- It never uses proxies, browser impersonation or CAPTCHA solving. A board that blocks GitHub's servers is only
  fetched from your own computer, if you choose to.
- Auto-fill never submits a form. You review it and click Submit yourself.
- Every job links back to the original posting.

Each board's terms are its own. If you run a board and want it left out, open an issue and it will be switched off.

## Status and support

This is a personal project shared as-is. Job boards change their pages often, so a source can break between
releases. `jr doctor` checks your setup, and the app shows how last night's runs went. Bug reports and fixes are
welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

City and country data from [GeoNames](https://www.geonames.org/) (CC BY 4.0). The company board list includes data
from the LastRound AI ATS directory (CC BY 4.0), [open-jobs-data](https://github.com/ConorsCode/open-jobs-data) by
ConorsCode (MIT) and the [Common Crawl](https://commoncrawl.org/) index. Details are in [NOTICE](NOTICE).

Licensed under the [MIT License](LICENSE).
