# Deploying the Escalations Dashboard for the team

The dashboard is a single small web server (`server.rb`) plus one HTML page. Run it on any always-on machine your team can reach, such as an internal Linux VM or a cloud VM behind your VPN. It needs:

- **Outbound HTTPS** to `accounts.zoho.com` and `sdpondemand.manageengine.com`
- **One inbound port** (8787 by default) for teammates' browsers
- **Very little capacity:** ~50 MB RAM and negligible CPU. It calls ServiceDesk Plus at most once a minute, however many people are viewing.

## Before you deploy

1. **Copy `.env` to the server** (it holds the Zoho credentials). Keep it private: `chmod 600 .env`.
2. **Set a password:** put `DASHBOARD_PASSWORD=...` in `.env`. Teammates get a browser login prompt (user `escalations` by default).
3. **Rotate the Zoho client secret (recommended):** the current one was shared in a chat. In https://api-console.zoho.com, go to Self Client → Client Secret → regenerate. Then generate a new code and refresh token (see README) and update `.env`.

## Option 0 - Render (public https link, no server of your own)

1. Create a **private** GitHub repo and upload the contents of `github-upload/` (never `.env`).
2. In Render, go to **New → Blueprint**, connect GitHub, and pick the repo. `render.yaml` sets up everything.
3. When Render prompts, paste `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET` and `ZOHO_REFRESH_TOKEN` from your local `.env`, and choose a `DASHBOARD_PASSWORD` of 8+ characters.
4. Share `https://escalations-dashboard-xxxx.onrender.com` and the password with the team.

`REQUIRE_PASSWORD=true` makes the server refuse to start without a password, so ticket data is never public by accident. To change priorities or levels later, edit them under the service's **Environment** tab in Render; it redeploys automatically.

## Option A - Docker (recommended)

Copy this folder to the server (without needing git) and run:

```bash
docker compose up -d --build
```

Then:
- **Check health:** `curl http://localhost:8787/healthz` should return `"ok":true` and the ticket count.
- **View logs:** `docker compose logs -f`
- **Apply changes:** after editing `.env` (priorities, levels, password), run `docker compose up -d` again.

## Option B - plain Linux (no Docker)

```bash
sudo apt-get install -y ruby curl     # or: dnf install ruby curl
sudo gem install webrick              # needed on Ruby 3+
sudo mkdir -p /opt/escalations && sudo cp -r server.rb public .env /opt/escalations/
```

Next, create `/etc/systemd/system/escalations.service`:

```ini
[Unit]
Description=Escalations Dashboard
After=network-online.target

[Service]
WorkingDirectory=/opt/escalations
ExecStart=/usr/bin/ruby server.rb
Restart=always
User=nobody

[Install]
WantedBy=multi-user.target
```

Then start it:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now escalations
```

## HTTPS and a friendly URL

Basic auth over plain HTTP is fine inside a VPN or office network. For anything wider, put the dashboard behind your existing reverse proxy or load balancer (nginx, Caddy, IIS, an Azure/AWS LB) so it's served over HTTPS, e.g. `https://escalations.yourcompany.internal` → `http://server:8787`.

Example with Caddy, which gets certificates automatically:

```
escalations.example.com {
    reverse_proxy localhost:8787
}
```

## Monitoring

`/healthz` (no login required) returns HTTP 200 while data is fresh. It returns 503 if the last ServiceDesk Plus fetch failed, e.g. an expired or revoked token. Point your uptime monitor at it.
