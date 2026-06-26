# ZOE — WhatsApp-controlled Home Assistant assistant

ZOE receives WhatsApp messages from your number, asks Claude which Home
Assistant device/service to call, executes it via the HA REST API, and
replies on WhatsApp with the result. Risky actions (currently: unlocking
`lock.front_door`) require an explicit "yes"/"confirm" reply before they run.

Runs as a Home Assistant Add-on on your HA Green (HAOS), so no separate
server is needed. WhatsApp Cloud API was chosen over unofficial WhatsApp
libraries to avoid ToS/ban risk for something you'll depend on long-term.

## 1. WhatsApp Cloud API setup (Meta)

1. Go to [developers.facebook.com](https://developers.facebook.com) → create a
   Meta Developer account if you don't have one → **Create App** → type
   "Business".
2. In the app dashboard, add the **WhatsApp** product. Meta gives you a free
   test phone number automatically — use that (don't use your personal
   number).
3. Under WhatsApp → API Setup, note:
   - **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`
   - **Temporary access token** (24h) — for production, generate a
     **permanent token** instead: App → System Users → create a system user
     with `whatsapp_business_messaging` permission → generate token →
     `WHATSAPP_ACCESS_TOKEN`
4. Under App Settings → Basic, copy **App Secret** → `WHATSAPP_APP_SECRET`.
5. Pick any random string yourself for `WHATSAPP_VERIFY_TOKEN` (you'll enter
   it twice: here in your config and in Meta's webhook setup screen).
6. In WhatsApp → Configuration, you'll set the **Callback URL** to your
   Cloudflare Tunnel URL (step 4 below) + `/webhook`, and the **Verify token**
   to the value above. Don't do this until the add-on is running.
7. Send a free-form test message **from your own WhatsApp number to the test
   number first** — Meta requires the user to message the business first
   before the business can reply freely.

## 2. Home Assistant long-lived access token

1. In Home Assistant, click your profile (bottom-left) → **Security** tab.
2. Scroll to **Long-Lived Access Tokens** → **Create Token** → name it `zoe`
   → copy the value immediately (shown once) → `HA_LONG_LIVED_TOKEN`.

## 3. Install the ZOE add-on

1. Push this repo to GitHub (e.g. `github.com/<you>/zoe-ha-bridge`).
2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ (top right) →
   Repositories** → paste your repo URL → **Add**.
3. The "ZOE" add-on appears in the store → **Install**.
4. Go to the add-on's **Configuration** tab and fill in all options
   (`anthropic_api_key`, `ha_long_lived_token`, `whatsapp_phone_number_id`,
   `whatsapp_access_token`, `whatsapp_verify_token`, `whatsapp_app_secret`,
   `allowed_sender_number` — your own WhatsApp number in E.164 without `+`,
   e.g. `972501234567`).
5. **Start** the add-on. Check the **Log** tab — you should see ZOE's
   startup log with no errors.

## 4. Expose the webhook with Cloudflare Tunnel

Meta needs an HTTPS URL it can reach; don't port-forward your home network.

1. Install the official **Cloudflare Tunnel** add-on from the Home Assistant
   community add-ons store (or the standalone `cloudflared` add-on).
2. Follow its setup to log in to your Cloudflare account and create a tunnel
   pointing a subdomain (e.g. `zoe.yourdomain.com`) at
   `http://localhost:8000` (ZOE's add-on port).
3. Take the resulting `https://zoe.yourdomain.com/webhook` URL back to Meta's
   WhatsApp → Configuration screen from step 1.6 and click **Verify and
   Save** — Meta will call `GET /webhook` once; ZOE answers automatically if
   the verify token matches.

## 5. Edit your device list

Edit `addon/config/entities.yaml` with your real entity IDs (find them in
Home Assistant under **Settings → Devices & Services → Entities**). Mark
`risky: true` for anything that should require a "yes" confirmation. Commit,
push, and reinstall/rebuild the add-on (Add-on page → **Rebuild**) to pick up
config changes — entities.yaml is baked into the image at build time.

## 6. Test it end-to-end

1. From your phone, message the WhatsApp test number: *"close the living
   room shutter"*. Expect: shutter closes, and you get a WhatsApp reply like
   "Living room shutter: close_cover ✅". Check the add-on log to see the
   inbound message, the resolved action, and the HA call result.
2. Message: *"unlock the front door"*. Expect: ZOE replies asking you to
   confirm, and does **not** unlock yet. Reply *"yes"*. Expect: it unlocks
   and confirms. Check the log shows the pending-action / confirmation flow.
3. Try an unrelated/ambiguous message (e.g. "what's the weather"). Expect:
   ZOE replies asking for clarification instead of guessing an action.

## Local development (without HA)

```bash
cd addon
pip install -r requirements.txt
cp .env.example .env   # fill in real values; for local testing point
                        # HA_BASE_URL at http://homeassistant.local:8123
                        # instead of http://supervisor/core
uvicorn app.main:app --reload --port 8000
```

You can simulate Meta's webhook handshake with:
```bash
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=<your token>&hub.challenge=test123"
# should return: test123
```
