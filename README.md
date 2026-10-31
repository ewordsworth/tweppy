# Tweppy

A small self-hosted dashboard that queues, schedules, and auto-posts tweets to X (Twitter), including posts into X Communities.

You paste tweets in `[square brackets]`, and Tweppy posts them one at a time on an interval you choose, schedules them for a specific time, or posts them right away. It runs as a single Flask app and stores everything in Cloudflare D1, so it fits on a free hosting tier.

## Features

- **Queue and auto-post.** Add many tweets at once, then post one every 30 minutes to 24 hours, in random or oldest-first order.
- **Scheduler.** A month calendar, single scheduled posts, and bulk scheduling that spaces tweets out from a start time.
- **Communities.** Queue posts for an X Community, optionally retweeting each one to your profile.
- **Analytics.** Posts per day and per hour, success rate, an X API spend estimate, and a plain-English error log.
- **Quiet hours.** Auto-posting pauses overnight in your timezone.
- **Safe defaults.** Duplicate tweets are skipped, failed posts back off instead of retrying every minute, and auto-posting resumes after a restart.
- **Live hints while you type.** Character counts, over-limit warnings, and a warning when a link makes a post more expensive.

## Requirements

- Python 3.11 or newer
- An X developer account with an app **inside a Project**, and credits for the X API (it is pay-per-use)
- A Cloudflare account with a D1 database (the free tier is plenty)

Without the Cloudflare settings, Tweppy falls back to a local SQLite file. That is fine for trying it out, but the data is lost on hosts with temporary disks.

## Setup

### 1. Install

```bash
git clone https://github.com/<you>/tweppy.git
cd tweppy
pip install -r requirements.txt
cp .env.example .env
```

### 2. Create the X app

1. In the [X developer portal](https://developer.x.com), create a Project and an app inside it.
2. Open the app's **User authentication settings** and set:
   - App permissions: **Read and write**
   - Type of App: **Web App, Automated App or Bot**
   - Callback URI: `http://127.0.0.1:8080/callback`
   - Website URL: any https address
3. Copy the **OAuth 2.0 Client ID and Client Secret** into `.env` as `CLIENT_ID` and `CLIENT_SECRET`. The API Key, API Secret, and Bearer Token are not used.

### 3. Create the Cloudflare D1 database

1. In the Cloudflare dashboard, go to **Storage & Databases**, then **D1**, and create a database.
2. Create an API token under **My Profile**, **API Tokens**, **Create Custom Token**, with the permission **Account, D1, Edit**.
3. Put the account ID, database ID, and token in `.env`. Tweppy creates its tables on first start.

### 4. Connect your X account

```bash
python authorize.py
```

Open the address it prints, sign in to X with the account you want to post from, and approve the app. The tokens are saved to the database. You only need to do this once.

### 5. Run the dashboard

```bash
python start.py
```

Open `http://127.0.0.1:5000`. If you set `ADMIN_TOKEN` in `.env`, open `http://127.0.0.1:5000/?admin_token=<your token>` once per browser; it sets a 30-day cookie.

## Configuration

All settings live in `.env`. See `.env.example` for descriptions.

| Variable | Required | Purpose |
|---|---|---|
| `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, `CF_API_TOKEN` | Recommended | Cloudflare D1 storage |
| `CLIENT_ID`, `CLIENT_SECRET` | Yes | X OAuth 2.0 app credentials |
| `REDIRECT_URI` | Yes | Must match the app's Callback URI exactly |
| `ADMIN_TOKEN` | On public hosts | Password for the dashboard |
| `SECRET_KEY` | Recommended | Keeps dashboard sessions valid across restarts |

Posting interval, quiet hours, timezone, and cost estimates are set in the dashboard's **Settings** tab.

## Deploying

`render.yaml` and `koyeb.yaml` are included. Set the environment variables above in your host's dashboard. Run `authorize.py` on your own computer, not on the host: it needs a browser, and the tokens it saves go to the shared D1 database.

Free tiers that sleep when idle will pause auto-posting. The `/keepalive` endpoint is there for an uptime pinger.

## Costs

The X API charges per request. As a rough guide, a text post costs about $0.015 and a post containing a link about $0.20. Check the current prices in your X developer console; the Analytics tab estimates your spend from the rates you enter in Settings.

## License

Copyright (C) 2026 ewordsworth

Tweppy is free software, licensed under the [GNU Affero General Public License v3.0](LICENSE).

In short: you may use, study, change, and share it, including commercially. If you distribute a modified version, or **run a modified version as a service that other people use over a network**, you must make your full source code available to those users under the same license. The dashboard shows a "Source code" link for this purpose; if you run a modified copy, point `SOURCE_URL` at your own repository.

The name "Tweppy" identifies this project. Please give forks and hosted versions a different name.
