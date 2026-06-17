# Host Accounts Pilot for free on GitHub Codespaces

Run it in the cloud (not your PC), watch the browser, open the URL from any device —
**$0, single user, no app login, nothing shared.** GitHub itself is the gate.

> **The $0 guarantee:** do **not** add a payment method to your GitHub account. Free
> accounts get ~60 Codespaces hours/month; with no card on file GitHub simply *stops*
> the codespace at the limit — it can never charge you. Stop it when you're done; only
> running time counts.

---

## One-time setup

### 1. Put the code in a PRIVATE GitHub repo
Secrets never go in the repo — `.gitignore` already excludes `.env`, `.venv/`, `data/`,
and `storage_state_*.json` (your OTA logins). From the project folder:

```bash
git init
git add -A
git commit -m "Accounts Pilot"
gh repo create accounts-pilot --private --source . --push
# (or create a private repo on github.com and `git remote add origin … && git push -u origin main`)
```

### 2. Add your secrets as Codespaces Secrets
The app reads these as environment variables — they live in GitHub's secret store, only
you can see them, and they're injected at runtime (never committed).

Repo → **Settings → Secrets and variables → Codespaces → New repository secret**, add:

| Secret name | Value |
|---|---|
| `MIS_METABASE_URL` | `https://mis.digistay.co.in` |
| `MIS_METABASE_API_KEY` | your Metabase API key |
| `MIS_METABASE_DB_ID` | `2` |
| `MIS_EDGE_VALUE` | the `X-AP-Auth` secret from Sifat |
| `AZURE_OPENAI_ENDPOINT` | your Azure endpoint |
| `AZURE_OPENAI_KEY` | your Azure key |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.4` |

---

## Every time you onboard (on-demand)

### 3. Open the Codespace
Repo → green **Code** button → **Codespaces** → **Create codespace on main**.
First boot takes a few minutes (it builds the container with Chromium + noVNC). Later
boots are fast.

### 4. Start it (one command)
In the Codespace terminal:
```bash
bash scripts/codespace-start.sh
```
It prints two forwarded ports.

### 5. Open the URLs (Ports tab)
In the **Ports** tab (bottom panel) you'll see:
- **8000 — Accounts Pilot UI** → open it. This is the app.
- **6080 — Watch browser (noVNC)** → open it and add `/vnc.html?autoconnect=1&resize=remote`
  to the URL. This is where you **watch the browser and solve CAPTCHA / OTP**.

Both ports are **Private** → they open only for a browser signed into *your* GitHub
account. Same URL works from your laptop or phone (just be signed into GitHub there).
No app password, nothing shared.

### 6. Stop it when done (saves your free hours)
Codespaces auto-stops after idle, or stop it now: **github.com/codespaces** → ⋯ →
**Stop codespace**. Your logins and data persist for next time; the URL parks until you
start it again.

---

## Notes
- **Open from another device:** same Codespaces URL works anywhere you're signed into your
  GitHub. To open it *without* signing into GitHub on that device, set the port to
  **Public** in the Ports tab — but then the URL is open to anyone who has it, and this app
  drives your real OTA accounts, so only do that with a URL you keep private.
- **Watching is plain (noVNC)** — functional, not fancy. You see the real Chromium and can
  click to solve gates.
- The repo stays private; secrets stay in Codespaces Secrets. The orphan-free start script
  also means nothing lingers eating your hours.
