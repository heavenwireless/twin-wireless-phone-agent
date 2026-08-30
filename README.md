# Twin Wireless phone agent

Answers calls to the shop's Twilio number, has a live conversation using
Claude, and texts Murad a message for anything it can't resolve itself
(pricing not on the published list, status checks, or anyone who wants to
talk to a person).

## Deploy on Render (free tier)

1. Create a new **public** GitHub repo and upload these files (app.py,
   requirements.txt, this README) via GitHub's "Add file > Upload files" in
   the browser -- no git command line needed.
2. On Render: New > Web Service > "Public Git Repository" tab > paste the
   repo's URL.
3. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn app:app`
4. Add these environment variables in Render's dashboard (Environment tab):
   - `ANTHROPIC_API_KEY` -- from console.anthropic.com (needs credits added
     before it will actually respond)
   - `TWILIO_ACCOUNT_SID` -- from the Twilio console
   - `TWILIO_AUTH_TOKEN` -- from the Twilio console (same page as the SID)
   - `TWILIO_FROM_NUMBER` -- the Twilio number in +1XXXXXXXXXX format
     (+13187239666)
   - `OWNER_PHONE` -- Murad's cell number in +1XXXXXXXXXX format, where
     message texts get sent
5. Deploy. Render gives you a URL like `https://twin-wireless-phone-agent.onrender.com`.
6. In Twilio: Phone Numbers > Manage > Active Numbers > click the number >
   under "Voice Configuration," set "A call comes in" to Webhook,
   `https://<your-render-url>/voice`, HTTP POST. Save.

Call the number to test.

## Known limitations (v1)

- Conversation state is kept in memory per call, not a database -- fine for
  a single live call, but restarts/redeploys clear anything in progress.
- Render's free tier sleeps after inactivity and takes up to ~50 seconds to
  wake on the next request -- a caller could hit dead air if the service
  was asleep. Render's cheapest paid tier keeps it always-on.
- Hours logic uses a fixed UTC-5 offset for Central time as an
  approximation (doesn't auto-adjust for the one hour where DST briefly
  makes this wrong twice a year) -- fine for now, worth revisiting later.
- No repair-ticket lookup -- status checks always become a callback
  message, since there's no connection to CellPoint Pro (or any other POS)
  yet.
