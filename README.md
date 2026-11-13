# Escalations Dashboard

Live tracker for critical ServiceDesk Plus Cloud tickets. It refreshes every 60 s so escalation managers can keep ticket health at 100%.

## Health rules

| State | When |
|---|---|
| ✕ **Breached** | SDP flags `is_overdue` (resolution SLA) or `is_first_response_overdue` |
| ! **At risk** | Resolution or first-response SLA due within `AT_RISK_HOURS` (4h), unassigned, or no update for `STALE_HOURS` (24h). Paused-SLA statuses (Awaiting Client Response, On Hold, RCA Pending…) are only flagged if unassigned |
| ✓ **Healthy** | Everything else |

**Ticket health % = open critical tickets not breached ÷ all open critical tickets.**

The default critical priorities are the P1/P2 incident and P1 service-request priorities. Change them with `CRITICAL_PRIORITIES` in `.env`.

## Setup

1. **API access (Zoho OAuth "Self Client"):**
   1. Go to https://api-console.zoho.com, then **Add Client → Self Client**.
   2. In the *Generate Code* tab, enter scope `SDPOnDemand.requests.READ`, set the duration to 10 minutes, and copy the code.
   3. Exchange the code for a refresh token (within 10 minutes):
      ```bash
      curl -X POST "https://accounts.zoho.com/oauth/v2/token" \
        -d grant_type=authorization_code -d client_id=CLIENT_ID \
        -d client_secret=CLIENT_SECRET -d code=CODE
      ```
   4. Copy `.env.example` to `.env`. Set `SDP_BASE_URL=https://sdpondemand.manageengine.com/app/itdesk` and fill in the client ID, client secret and `refresh_token`.
2. **Run** with the Ruby built into macOS (no install needed):
   ```bash
   ruby server.rb
   ```
   Or with Python, which also supports demo mode: `python3 server.py`. Both read the same `.env` and serve the same dashboard.
   Then open http://localhost:8787. Teammates on your network can use `http://<your-ip>:8787`.

If no credentials are set, the dashboard runs with sample data and shows a **DEMO DATA** badge.
Credentials stay on the server. The browser only calls `/api/tickets`.
