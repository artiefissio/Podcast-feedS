# Podcast-feedS

Rolling podcast feed generator for scheduled KTAL recordings.

## Retention

The feed is intentionally a rolling snapshot, not a permanent archive. By default it keeps 14 days of episodes. Override with:

```sh
RETENTION_DAYS=21 venv/bin/python record_radio_shows.py
```

Generated audio, feeds, logs, and downloaded metadata are ignored by Git. The public feed is published to `gh-pages` from a temporary snapshot so `main` stays source-only.

## Setup

```sh
scripts/bootstrap_venv.sh
```

The existing cron entries expect `venv/bin/python` in this repository.

## VPS Deployment

A VPS is the preferred host because it stays awake and has stable network access. Install `git`, `ffmpeg`, and Python, clone this repository to `/opt/Podcast-feedS`, then run:

```sh
cd /opt/Podcast-feedS
scripts/bootstrap_venv.sh
sudo cp deploy/podcast-feedS.env.example /etc/podcast-feedS.env
sudo cp deploy/podcast-feedS.service /etc/systemd/system/
sudo cp deploy/podcast-feedS.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now podcast-feedS.timer
```

The service needs GitHub push credentials for `origin` so it can publish the rolling `gh-pages` snapshot. On Linux, iMessage notifications are skipped unless replaced with another notifier.
