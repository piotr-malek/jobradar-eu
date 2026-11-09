# Job Radar

You wake up, and the jobs that fit you are already found and read. Every night Job Radar searches European job
boards, company career pages and your job-alert e-mails. It keeps the jobs that match the roles, places and
languages you chose, and has an AI read each one. In the morning they are waiting in your Notion, best fit first,
each with a short summary and anything to watch out for.

- **Any role, any EU/EEA country, on-site jobs too.**
  - Role groups for marketing & content, HR & recruiting, finance & accounting, operations/admin/project
    management, sales & customer success, and eleven tech fields.
  - Or type the positions you want and let the AI list the titles job ads use for them, in your languages.
- **Jobs from everywhere, without searching board by board:**
  - about 77 job boards and public job services (national employment services through EURES, local boards in 27
    countries);
  - Jooble's 22 country sites;
  - about 6,200 company career pages (Greenhouse, Lever, Ashby, Workable, Personio, Teamtailor, Workday and more);
  - the job-alert e-mails of boards that can't be searched automatically: LinkedIn, Pracuj.pl, StepStone, Indeed
    and 90 more, listed for your countries.

  See [docs/sources.md](docs/sources.md).
- **An AI reads every job for you.** It gives a fit score from 1 to 5, what the job is about, the languages and
  experience it asks for, the contract, the salary and the deadline when stated, and what to watch out for.
  - Jobs that came with only a snippet get their full text first, from the company's own careers page or another
    board, so the AI reads the real posting.
  - A big first run (thousands of jobs) is reviewed over the next nights; nothing is dropped.
- **It learns what you don't want.** Mark a job "Not relevant" in Notion. Job Radar proposes a filter rule ("skip
  titles with …"), and you tick the ones you agree with.
- **Nothing runs on your computer.** The search runs at night on GitHub, in your own private repository.
- **Free.** Every service it uses has a free plan, and Google's free AI plan stops at its limit instead of
  charging.

## Two ways to use it

| | Radar | Radar + Apply |
|---|---|---|
| Job boards, company career pages and job-alert e-mails for your countries | ✓ | ✓ |
| AI review of every job: fit, summary, details, watch-outs | ✓ | ✓ |
| Jobs in Notion every morning; set Apply / Skip / Not relevant / Applied there, also on your phone | ✓ | ✓ |
| Filter suggestions from the jobs you mark Not relevant | ✓ | ✓ |
| Weekly summary page: applications, replies by source and role | ✓ | ✓ |
| More sources with extra free keys (Jooble in other countries, Adzuna, TheirStack, ITJobs.pt), a morning e-mail | | ✓ |
| An app on your computer for the jobs you mark Apply: saved answers to application questions, forms read and filled in Chrome (you always press Submit), tailored CV and cover-letter PDFs, AI drafts | | ✓ |

You can move from Radar to Radar + Apply later with `jr setup --upgrade`. Your jobs, Notion database and statuses are
kept.

## Install

You need a computer with macOS, Windows or Linux. You don't need to install Python or anything else first.

**macOS or Linux:** open Terminal (on a Mac: press ⌘ Space, type `Terminal`, press Enter), paste this line and press
Enter:

```sh
curl -LsSf https://raw.githubusercontent.com/piotr-malek/jobradar-eu/main/install.sh | sh
```

**Windows:** open PowerShell (Start menu → type `PowerShell` → Enter), paste this line and press Enter:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/piotr-malek/jobradar-eu/main/install.ps1 | iex"
```

The installer opens the setup wizard in your browser. It takes about 10 minutes, and you can close it and continue
later with `jr setup`.

**The accounts you need, all free, set up in the first steps:**
1. **GitHub**, where the nightly search runs. The wizard can download GitHub's helper program and sign you in.
2. **Google AI Studio**, for the AI review. Create the key in a new project and don't set up billing; then the key
   can't cost anything.
3. **Notion**, where your jobs land. You create an "integration", share one page with it, and Job Radar makes the
   Jobs database there.

**Then the wizard asks about you:**
- where you live and how you want to work (remote, hybrid within a distance, on-site), your languages and contract
  types;
- your CV, optional: the AI drafts a profile from it, and the review compares every job with that profile;
- the positions you want, or ticks on the role groups; a preview shows which job titles would be kept;
- the countries to search.

**Last comes the optional extra most worth a few minutes: job-alert e-mails.**
1. Connect a Gmail address with an app password.
2. The wizard lists the big boards for your countries that can only be followed by e-mail (LinkedIn, Pracuj.pl,
   StepStone, InfoJobs, Indeed and more).
3. Create an alert on each one and tick it.

Finally the wizard creates your private repository and starts the first run. The first run looks 30 days back, so
expect a bigger first batch in Notion; the AI finishes reviewing it over the next nights.

<details><summary>Already use Python tools?</summary>

```sh
uv tool install 'jobradar-eu[full]'    # or: pipx install 'jobradar-eu[full]'
jr setup
```
</details>

## Using it

- **Each morning**, open the Jobs database in Notion. Sort by Fit (the wizard says how to add that view).
  - Set a job's Status to Apply, Skip, Not relevant, Applied, Interview and so on.
  - The next night's run reads the statuses: skipped jobs never come back.
- **Not relevant** is for jobs that shouldn't have been found at all. From those, a "Job Radar: filter suggestions"
  page proposes rules. Tick the ones you want and they apply from the next night.
- **Each week** a summary page appears next to the database. It also lists any job-alert sender that hasn't sent an
  e-mail in a week, so you know when an alert has stopped.
- **Radar + Apply:** run `jr app` (or use the desktop shortcut) to work through the jobs you marked Apply.
- `jr doctor` checks the whole setup; `jr setup` changes any step.

## What it costs

Nothing, on the free plans:

| Service | Needed for | Cost |
|---|---|---|
| GitHub account, private repository, Actions | both | free: 2,000 minutes a month; Job Radar keeps under 1,800 and trims its own search when a month runs long |
| Google AI Studio (Gemini) | both | free: three separate daily quotas on one key (Gemini 3.5 and 3.1 Flash-Lite, then Gemma). It stops at the limit and never charges unless you set up billing. On the free plan, Google doesn't use prompts from the EEA, UK or Switzerland for training. |
| Notion | both | free plan |
| Gmail app password: job-alert e-mails (IMAP) | optional | free |
| Jooble, Adzuna, TheirStack, ITJobs.pt keys; morning e-mail (SMTP) | Radar + Apply, optional | free plans |
| Playwright + Chromium for PDFs | Radar + Apply | free |

Other AI services work too: Mistral, Groq, Cloudflare Workers AI and OpenRouter have free plans; OpenAI and
Anthropic are paid. A local model (Ollama, LM Studio, the Claude Code CLI) works for drafts in the app, but not for
the overnight review, which runs on GitHub.

## How it works

1. **Overnight**, the workflow in your private repository does the following:
   - installs Job Radar and restores yesterday's database;
   - reads the statuses you set in Notion and the filter rules you ticked;
   - fetches the job boards, company career pages and alert e-mails that are due;
   - removes duplicates and matches every job against your roles, places, languages and seniority;
   - finds the full text of jobs that came with a snippet, has the AI review them, and writes the pages to Notion,
     reviewed ones first.

   The database is saved as one commit on the repository's `state` branch.
2. **In the morning** everything is in Notion. With Radar + Apply, `jr app` also downloads the database for the apply
   tools and writes your status changes back to Notion.

**Your data.**
- **In your private repository:** your search settings (home city, languages, roles, countries, sources, AI
  choice). Keys go into its encrypted Actions secrets.
- **Sent to the AI:** each posting and your profile summary, never your contact details.
- **Only on your computer:** your contact details, CV, answer bank and generated documents, in `~/.job-radar`.
  They are never pushed.

## Being a good guest on job boards

Job Radar reads public listings the way a careful person would, and asks the same of anyone changing it:

- It uses public APIs and feeds where a board offers them, and checks robots.txt before opening a job page.
- Each source has a small request budget per run, and requests are spaced out.
- It never uses proxies, browser impersonation or CAPTCHA solving. Boards that block automated visits (LinkedIn,
  Pracuj.pl, StepStone and others) are only read through the job-alert e-mails they send you, and links in those
  e-mails are never opened.
- Auto-fill never submits a form. You review it and click Submit yourself.
- Every job links back to the original posting.

Each board's terms are its own. If you run a board and want it left out, open an issue and it will be switched off.

## Status and support

This is a personal project shared as-is. Job boards change their pages often, so a source can break between
releases. `jr doctor` checks your setup, and a weekly check of every source runs in this repository. Bug reports and
fixes are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

City and country data from [GeoNames](https://www.geonames.org/) (CC BY 4.0). The company board list includes data
from the LastRound AI ATS directory (CC BY 4.0), [open-jobs-data](https://github.com/ConorsCode/open-jobs-data) by
ConorsCode (MIT) and the [Common Crawl](https://commoncrawl.org/) index. Details are in [NOTICE](NOTICE).

Licensed under the [MIT License](LICENSE).
