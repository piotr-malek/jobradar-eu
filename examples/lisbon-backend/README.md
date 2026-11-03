# Example: a backend engineer in Lisbon

A fictional user, Ana Ferreira, set up in Full mode. These are the files the setup wizard writes:

- `user/` goes to her private config repo (no contact details): `settings.yaml`, `profile.yaml`, `roles.yaml`,
  `sources.yaml`, `llm.yaml`.
- `home/` stays in `~/.job-radar` on her computer: `experience.md`, `draft_style.md`, `cv.yaml` (the CV source with
  her contact details) and `.env.example` (her keys go in `.env`).

The tests use this persona (`tests/conftest.py: EXAMPLE`). Copy it to try the app without the wizard:

    export JR_USER_DIR=$PWD/examples/lisbon-backend/user JR_HOME=/tmp/jr-demo
    cp examples/lisbon-backend/home/* /tmp/jr-demo/
    jr doctor --offline
