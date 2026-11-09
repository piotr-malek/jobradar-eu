# Job Radar config

This private repository runs your [Job Radar](https://github.com/piotr-malek/jobradar-eu) every night on GitHub
Actions. You don't need to edit it by hand.

**Set it up with the wizard on your computer:**

```sh
uv tool install jobradar-eu        # or: pipx install jobradar-eu
jr setup
```

The wizard created this repository (keep it private), writes your settings into `user/`, adds your API keys as
Actions secrets and sets the schedule to your time zone. Until `user/profile.yaml` exists, the nightly run does
nothing. Your jobs land in your Notion Jobs database; the statuses you set there are read by the next run.

## What's in here

| Path | What |
|---|---|
| `.github/workflows/jobradar.yml` | The nightly run. The lines tagged `jobradar:` hold the schedule (in UTC). |
| `user/` | Your settings: profile summary, roles, countries and sources, LLM choice. No contact details. |
| `state` branch | The job database, replaced by each run (one commit). |

To change a setting, run `jr setup` again (or `jr setup --step <id>` for one step); it pushes the changed files.
To run now instead of waiting for the night: Actions → jobradar → Run workflow.
