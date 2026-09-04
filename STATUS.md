# Status — 2026-09-03

## 2026-09-04 — phone number normalization, new-appointment SMS, one real incident

- **Bug found via Kristy Moffett's real appointment**: none of the 5 real
  appointments in the system have phone numbers in E.164 (`13184619641`, not
  `+13184619641` — some are missing the country code entirely). This would
  have silently failed every real follow-up send. Added `_normalize_phone()`
  in `app.py`, used both when creating a `followups.php` record and when
  actually sending. Unit-tested against the real data shapes before shipping.
- **Real incident**: the first deploy of that fix crashed the service ~70s
  after boot (`Handling signal: term` in Render's logs, no Python traceback,
  no corresponding Render platform event) — rolled back immediately,
  confirmed stable, then redeployed the same commit with 2+ minutes of close
  monitoring; it stayed solid the second time. No repeat since. Root cause
  never conclusively identified — most likely a one-off Render-side hiccup,
  not a bug in the diff itself (the change was a small, dependency-free
  regex helper).
- **Kristy Moffett backfilled**: PATCHed her appointment (already
  `Fulfilled` before this system existed, so it had no `fulfilledAt`) to
  stamp the timestamp now. Confirmed the poller picked her up. Sunny Lee is
  in the identical situation and was deliberately left alone — not asked
  for.
- **Callback-to-owner SMS verified working**: confirmed via Twilio's own
  delivery log (not just app logs) that the "Follow-up callback requested"
  text from earlier live testing actually reached +1 (773) 679-7766,
  `status: delivered`.
- **New: SMS the owner the instant a customer books**, not just on
  Fulfilled. `appointments.php`'s existing `send_notification()` (which
  already emails `help@twin-wireless.com`) now also calls a new
  `/notify-new-appointment` webhook on the phone-agent app, authenticated
  with a shared secret (`NOTIFY_WEBHOOK_SECRET`, both sides). Twilio
  credentials stay in exactly one place (the phone-agent app) rather than
  being duplicated onto the website server. Curl call from PHP uses a 5s
  timeout so a slow/down phone-agent can never delay a customer's booking
  confirmation. Tested end-to-end with a real appointment POST — SMS
  delivered in under 2 seconds, test appointment deleted afterward.
- **Bulk review-request texting (ad-hoc, from POS-pulled numbers)**: Murad
  wants this chat/Routines-driven, not a website feature (see memory
  `murad-adhoc-agent-tools-via-chat.md`). No new deployment for this — just
  give Claude a list of numbers (typed or a file) in conversation and it
  sends each one the same review-request text via the existing Twilio
  client, any time, not only when a routine fires.

## Automated customer follow-up agent — added 2026-09-03/04

Unattended agent: watches the Admin panel's appointments for the status
changing to **Fulfilled**, waits a configurable delay, then texts the
customer a check-in, a Google review ask, and a live (never invented)
service recommendation — from Mia's existing number. Handles replies:
thanks, complaints/callback requests, opt-out (STOP), and other questions.

### What was found (audit, done against the live server before writing code)

- Appointments are a flat JSON file (`data/appointments.json` on
  twin-wireless.com), read/written via `admin-api/appointments.php`
  (`GET`/`POST`/`PATCH`/`DELETE`, `flock`-guarded, no database). Status enum
  is exact-string Title Case: `Requested`, `Confirmed`, `In Progress`,
  `Ready for Pickup`, `Fulfilled`, `Cancelled`, `No Show`. No timestamp was
  recorded on status change.
- Production's admin panel and `admin-api/*` are Basic-Auth-protected
  (`/etc/apache2/.htpasswd-twin-admin-live`) for every method. `/api/`
  mirrors some of the same files with narrower rules — e.g.
  `/api/appointments.php` allows public `POST` (customer booking) but
  requires auth for `GET`/`PATCH`/`DELETE`. **`/api/catalog.php` is also
  fully Basic-Auth-protected**, part of a pre-existing "Private Shop Phase
  2C" restriction — not something this feature introduced.
- Google review link and website URL were already established elsewhere
  (`twin-wireless-phone-agent/app.py`'s `REVIEW_LINK` and the site's own
  canonical URL) — reused verbatim, not invented.
- No follow-up/tracking system, customer opt-out handling, or SMS-sending
  capability existed on the website side. The Twilio client, credentials,
  and proactive-SMS pattern already existed in this repo (`send_review_link_sms`,
  `send_message_sms`) and were reused directly.
- **5 real appointments already existed** in production, created before this
  feature — including 2 already marked `Fulfilled` (customers "Kristy" and
  "Sunny Lee") from before `fulfilledAt` existed. See "Needs a decision"
  below — they will not auto-trigger a follow-up as-is.

### What changed

- `twin-wireless.com` server (`heavenwireless`'s droplet, via the existing
  `twin-assets` SSH key):
  - `public/api/appointments.php` — `PATCH` now stamps `fulfilledAt` (once,
    idempotent) the first time status becomes `Fulfilled`.
  - `public/admin-api/followups.php` — **new**. The tracking store (spec's
    duplicate-prevention table): one record per appointment id, keyed by
    that id for idempotency. `GET`/`POST`/`PATCH`. Deliberately lives only
    under `/admin-api/` (blanket auth-protected already) rather than also
    under `/api/`, where it would have had no protecting rule.
  - `app/admin/follow-ups/FollowUpManager.jsx` + `page.jsx` +
    `followups-admin.module.css` — **new**. Dashboard at
    `/admin/follow-ups`: Pending / Sent / Responded / Callback Requested /
    Opted Out / Failed / Needs Staff Attention, plus a settings panel
    (delay, sending hours, review URL, website URL, max retries, service
    recommendations toggle) writing to the existing `site-settings.php`.
  - `app/admin/page.jsx` — added the "Customer Follow-Ups" sidebar link.
  - Settings live under a new `followUps` key in the existing
    `site-settings.json` — no new settings endpoint needed.
  - Rebuilt (`npm run build`) and the static export now serves all of the
    above. **Caught and fixed during this**: an early `cp -r` backup of
    `app/admin` accidentally created a second, unauthenticated,
    publicly-reachable copy of the whole admin UI at
    `/admin.backup-followup-...` (Next.js treats any `app/` subdirectory
    with a `page.jsx` as a real route, and the Apache rule only matches
    `/admin` and `/admin/...`). Moved out of `app/` and rebuilt before
    anything else touched the live site. No data was exposed — its own API
    calls still hit the protected `/admin-api/*` and 401'd.
- `twin-wireless-phone-agent` repo (this one):
  - `app.py` — added: a background poller (`APScheduler`, every 5 minutes,
    single gunicorn worker so it runs exactly once) that finds newly
    `Fulfilled` appointments, schedules/sends the follow-up, and records the
    result; a `request_callback` tool (parallel to `take_message`) used
    only when replying to a follow-up thread; an opt-out check in `/sms`
    that runs before Claude, unconditionally; a `FOLLOWUP_REPLY_NOTE`
    appended to the system prompt only for replies to a follow-up thread;
    a `/followups/status` debug route.
  - `requirements.txt` — added `requests`, `APScheduler`.
  - New env vars on Render: `ADMIN_API_USER` / `ADMIN_API_PASS` — a
    **dedicated** Basic Auth credential (`followup-agent`, in
    `.htpasswd-twin-admin-live` alongside Murad's own `twinadmin` login),
    not a shared one, so it can be revoked/audited independently.

### How it works

`Fulfilled` set in Admin → `appointments.php` stamps `fulfilledAt` → next
poll (≤5 min) creates a `followups.php` record (idempotent — a second poll
or a service restart never double-creates) → poll computes
`followupScheduledAt = fulfilledAt + delayHours` once → once that time has
passed **and** it's within the configured sending hours **and** the
appointment is re-verified still `Fulfilled` **and** the phone isn't opted
out → sends via the existing Twilio client → records `followupSentAt` /
`sent`. A reply to that number checks for an open follow-up thread first:
`STOP`/etc. short-circuits to an opt-out, recorded and never re-messaged;
anything else gets Claude with the extra follow-up context, which uses
`request_callback` (visible on the dashboard **and** texted to
`OWNER_PHONE`) for complaints/callback asks, or just replies naturally
otherwise. Every state transition is written to `followups.php`, not to
this app's in-memory `sessions{}`, so a Render restart/redeploy can't cause
a duplicate send or lose track of what already happened.

### Testing — done live, not just locally

Local smoke tests (mocked admin-api/Twilio) covered: scheduling math,
duplicate-prevention on re-poll, business-hours gating, opt-out
short-circuit — all passed first try. **Live testing against the real
server, using Murad's own number as the test customer, caught two real
bugs local mocks couldn't**:

1. `get_followup_settings()` and the catalog lookup both called
   Basic-Auth-protected paths with `auth=False`, silently 401'd, and fell
   back to defaults (24h delay) instead of the real configured value — a
   test record set to a 0-hour delay sat "pending" indefinitely. Fixed by
   using the client's default `auth=True` for both.
2. On a complaint reply, Claude apologized correctly but then asked for the
   customer's name and callback number before calling `request_callback`
   — directly violating "never re-ask for info already on file," because
   the older `take_message` instructions elsewhere in the prompt were
   winning out over a prose-only override. Took **two** rounds of
   strengthening (an explicit "ignore that rule for this reply" line,
   then a concrete worked example of the exact turn to produce) before it
   reliably called the tool in the same turn instead of asking a question
   first.

Confirmed live, in order: `fulfilledAt` stamps correctly on a real `PATCH`;
a `followups.php` record is created and scheduled idempotently; a real SMS
sent to a real phone with the correct review link and copy; re-polling
after a send does not double-send; `STOP` opts a number out and is recorded
without ever reaching Claude; a complaint reply gets an apology **and**
`request_callback` in the same turn, recorded as `callbackRequested` +
`staffFollowupRequired` on the dashboard. Not yet exercised against a real
account: outside-sending-hours queueing (logic-verified locally, no live
run outside 9am-8pm needed since it's config-driven) and the retry/backoff
path after repeated failures (nothing to trigger a real failure against —
would need a deliberately invalid number).

### Needs a decision — not made unilaterally

**The 2 pre-existing `Fulfilled` appointments (Kristy, Sunny Lee) have no
`fulfilledAt` and will never auto-trigger a follow-up as-is** — the stamp
only applies going forward, on a fresh transition into `Fulfilled`. Options:
do nothing (they simply don't get a follow-up, cleanest but they're
customers already), or re-save each one's status once (e.g. to `In
Progress` then back to `Fulfilled`) in the Admin UI to backfill it — that
would immediately queue a real review-request text to two real past
customers, so it's Murad's call, not something to do automatically.

### Test data left behind — none

The test appointment, its `followups.php` record, and the temporary
0-hour-delay/all-hours test settings were all created and cleaned up
during this session — `site-settings.json` is back to the real defaults
(24h delay, 9am-8pm Central sending hours) shown above.

Mia, the AI phone receptionist for Twin Wireless. Answers **(318) 723-9666**.
Flask app (`app.py`) on Render web service `srv-da9o8tgn74is738nostg`
(`twin-wireless-phone-agent`, repo `heavenwireless/twin-wireless-phone-agent`),
using `claude-haiku-4-5` for the conversation itself.

Read this before touching anything — it's the full history of a multi-day
debugging thread, so picking this up cold doesn't mean re-deriving it.

## The original complaint

"The phone agent is not forwarding calls right — it only works when the phone
is off, not when a call is unanswered or rejected or no answer."

Two separate systems are involved and had to be debugged independently:
**AT&T's conditional call forwarding** (routes the call to Mia's Twilio number
in the first place) and **the app itself** (what Mia does once the call
arrives). Conflating them wasted time early on — a fix to one looks like it
did nothing if the other is still broken.

## App-side bugs — found and fixed, all deployed and verified live

Three real bugs, found via real forwarded test calls + Twilio call logs, not
guesses:

1. **Silent first turn had zero retries.** Twilio's `<Gather>` does NOT POST to
   its `action` URL on a timeout with no speech captured if there's a next
   verb in the same `<Response>` — it just falls through locally. `/voice`'s
   Gather had no next verb wired to retry, so a caller's first turn getting no
   speech recognized was an instant, silent hangup with no second chance
   (every *later* turn already retried correctly via `/gather`'s own logic).
   Fixed by having `/voice` redirect to `/gather` on that path, reusing the
   existing retry instead of duplicating it. Commit `43fa669`.

2. **6 seconds of total silence before any retry played.** Real test calls
   showed the caller hanging up at exactly greeting-length + 6s — the retry
   was being generated right as they gave up, so they never heard it.
   Shortened the Gather timeout 6→4s. Commit `481fa16`. (This did NOT fix the
   underlying "no audio" symptom — see below — but it's a real improvement on
   its own and stayed in.)

3. **The opening greeting called Claude before saying anything**, adding
   ~1.3s of network latency before Mia spoke at all (measured: 1293ms on a
   real forwarded call that only lasted 4 seconds total). Made the greeting
   fully static/local instead — `opening_greeting()` in `app.py`, no API call
   in `/voice`'s critical path. Local benchmark: 6.8ms. Confirmed live across
   ~8 subsequent real forwarded calls: 113–347ms `/voice` response times.
   Commit `a303f82` — **this is the current deployed state.**

`/gather`'s own empty-speech retry branch (`no_catch` → `no_hearing` →
hangup) was never modified; fix #1 just made `/voice` reach it on the first
turn too.

**Regression check:** when told "it was working and you changed something,"
diffed the very first uploaded commit (`5db979d`, Aug 31) against the latest —
`build_gather()`, `/voice`, `/gather` were byte-for-byt­e identical the whole
time. The app-side behavior for a *normal* call never regressed; only the
retry/timing/latency issues above were real, and all three are fixed.

## The still-open problem: audio is silent on forwarded calls specifically

**A direct call to (318) 723-9666 is completely clear** (confirmed by Murad).
**A call forwarded to it via AT&T's conditional forwarding has the caller
hear only the first word of Mia's greeting, then total silence** — even
though Twilio's own call record proves the app spoke its *entire* script:
greeting → retry → goodbye → app-initiated hangup, with `Who Hung Up: callee`
and the call duration matching that full script exactly. The app is doing
its job; the audio isn't reaching the caller.

Ruled out, with evidence:
- **app.py / Render**: structurally can't cause this. Twilio's own
  infrastructure generates and delivers all voice via Polly TTS,
  independent of the webhook server — the webhook only returns TwiML text.
- **Render itself** (checked 2026-09-03, see below): healthy, stable,
  zero restarts/crashes/errors during real test-call windows.
- **Twilio's number/Voice Configuration**: plain webhook, no codec or media
  settings that differ by call origin.

**Conclusion: this is inside AT&T's network** — their forwarding trunk relays
call signaling correctly (Twilio sees the call, `CalledVia` shows the
forwarding number, `/voice` gets hit, TwiML executes) but does not relay the
RTP media/audio path correctly. Deeper Twilio-side jitter/packet-loss metrics
are gated behind the paid "Voice Insights Advanced Features" add-on and
weren't available to dig further from that side.

**Escalation wording given to Murad for AT&T**, specifically to avoid being
routed back through basic-settings troubleshooting: make clear that
call *routing/signaling* is confirmed working (the call reaches its
destination and the destination app runs to completion), but *audio/media*
specifically fails only on the conditional-call-forwarding path — a direct
call to the same destination number has perfect audio. This isolates the bug
to their forwarding trunk's media handling, not the number, not the app.

**Refined 2026-09-03 evening, after Murad called AT&T again and re-tried the
conditional-forwarding codes — still broken, but with a sharper symptom
breakdown that narrows this further than the framing above:**

- **Unreachable (phone off / airplane mode) → working audio.** Mia answers
  and the caller hears her normally.
- **No answer** and **busy/rejected** → **silent for ~20 seconds, then the
  call ends** — matching the app's own full script duration (greeting →
  retry → goodbye → app-initiated hangup), meaning the app runs to
  completion exactly as before, the caller just never hears any of it.

All three conditions point at the identical destination number, so this
isn't "conditional forwarding is broken" broadly — it's specifically the
**no-answer and busy/rejected forwarding triggers** misbehaving while the
**unreachable** trigger, to that same number, works fine. That's a much
more specific fact for AT&T's network team than what they'd been given
before, and worth leading with on the next contact:

> "Conditional forwarding for unreachable (phone off) has clear, working
> audio to 318-723-9666. Conditional forwarding for no-answer and
> busy/rejected, to that exact same number, is completely silent for about
> 20 seconds before the call ends — even though the call connects and runs
> normally the whole time. Since all three point at the identical
> destination, this isolates the problem to how your network specifically
> handles the no-answer and busy/rejected forwarding triggers, not the
> unreachable one. I need this checked against your provisioning changes,
> not re-tested with the same settings again."

Also confirmed by diff against Render's deploy history: nothing in
`build_gather`/`/voice`/`/gather` (or anything else that could affect call
audio) has changed since Murad's last known-good date, **2026-09-01** —
every commit since then touches only SMS handling, hours-math, or
retry/timeout timing, none of it audio delivery. Combined with Twilio's own
number config being unchanged and plain, the app/Twilio side is provably
constant across the whole "it worked, then it broke" window. Whatever
changed, changed on AT&T's network.

**No further action possible from the app/Render side.** This is blocked on
AT&T actually escalating past front-line support to whoever can see their
own provisioning history and the no-answer/busy-vs-unreachable distinction
above, or on new symptoms/logs from a fresh test call.

## Render check (2026-09-03) — thorough, all clean

Done specifically to rule Render in or out as a contributor, separate from
the AT&T diagnosis above.

- **Events** (full history, all 35): every event is a deploy or the one
  Free→paid compute-plan upgrade (2026-08-30). No crash, no restart, no
  health-check failure, ever.
- **Metrics** (last 12h): memory flat ~15–20% of the 512MB limit, no spikes.
  Instance count flat at 1 the whole time — no autoscale, no crash-restarts.
  On the paid plan since 2026-08-30, so it never spins down/cold-starts.
- **Application logs** (last hour, covering a real test-call window
  3:29–4:22 PM): every `/voice` and `/gather` request returned `200`, all
  served by the *same* instance ID the entire time (no mid-call restart),
  identical response sizes each time, zero errors/exceptions/5xx.

**Render is not a contributor to the silent-audio problem.**

## Other things fixed along the way (not part of the forwarding investigation)

- **DST bug**: `shop_open_status()` used a hardcoded `-5h` UTC offset, correct
  only during Central Daylight Time. From the November DST change to March,
  Mia would have been an hour off on open/closed status. Fixed with
  `ZoneInfo` (stdlib, Python 3.9+). Commit `5ed6a49`.
- **SMS added**: `/sms` route, commit `225707e` — the number's messaging
  webhook previously pointed at Twilio's demo autoreply. Same
  `SYSTEM_PROMPT`/tools as voice; session keyed by phone number, not `CallSid`,
  since a text thread has no hangup event.
- **SMS tone bug**: texts opened with "Thanks for calling..." and a mangled
  "-- and y también hablo español" (voice-prompt artifacts leaking into text
  replies). Fixed with a channel-aware `SMS_CHANNEL_NOTE` (`d633862`) and then
  an exact-template fix for the Spanish line specifically, because a
  prohibition alone ("don't start with and") wasn't strong enough against the
  base prompt's own example (`2056d67`).

## Deployment mechanics for this repo (Windows-specific)

- **Render auto-deploy is OFF.** A GitHub commit does nothing by itself —
  always Manual Deploy → Deploy latest commit from
  `dashboard.render.com/web/srv-da9o8tgn74is738nostg`, then confirm the
  deploys list shows the new commit hash + "Live" badge (~40–50s).
- ✅ **`git push` WORKS — corrected 2026-09-04.** This document previously said
  it hung on an invisible Windows Credential Manager prompt. That is no longer
  true: `git fetch`, `git ls-remote` and `git push origin main` all completed
  non-interactively (commit `3da3fcf` was pushed this way). Prefer git — it is
  far faster than the browser upload and gives an exact, reviewable diff.
  Use `GIT_TERMINAL_PROMPT=0` and a `timeout` so that if credentials ever do
  go stale it fails fast instead of hanging.
- ⚠️ **The local clone falls behind, because past deploys used the web upload
  flow.** Local `HEAD` was `ad4625d` while `origin/main` was `c15e61b` — 5
  commits behind. **Always `git fetch` and reset onto `origin/main` before
  committing**, or you will push a change that silently reverts everything in
  between. Never trust local `git log` for what is deployed; Render → Events
  is authoritative.
- GitHub's inline CodeMirror editor still freezes the tab on a file this size
  typed via simulated keystrokes. If git is ever unavailable, use GitHub's
  **Upload files** page (`.../upload/main`) with the local file — never the
  inline editor.

## Second Twilio number — isolation test, NOT YET REPORTED ON

A second Twilio number, **+1 (318) 515-1633**, was purchased and pointed at
the same `/voice` webhook, to answer one question the AT&T diagnosis can't:
**does the silence follow the destination number, or the forwarding trigger?**
If no-answer/busy forwarding to 515-1633 is *also* silent, the fault is in how
AT&T handles those triggers generally. If it's clear, the problem is somehow
specific to the 723-9666 route, which would be new information.

The test: set `*61*3185151633#` (no-answer) and `*67*3185151633#`
(busy/rejected), then call and let it ring through.

**Configuration VERIFIED in the Twilio console, 2026-09-04:**
- Friendly name: "Twin Wireless - AT&T forwarding isolation test"
- **Voice: enabled**, pointed at `https://twin-wireless-phone-agent.onrender.com/voice`
- Messaging: **disabled — "Complete A2P registration"** (irrelevant to this
  voice test, but it means the number cannot send/receive SMS as-is)
- The account holds exactly **2 numbers** — 723-9666 and 515-1633. No stray
  third number is being billed. Do not buy another.

**Calls to 515-1633 have already happened (2026-09-04, CDT):**

| Time | From | Duration | Call SID |
|---|---|---|---|
| 10:53:07 | +1 318 423-4898 | **20 sec** | `CA40c0f0cda8e6fcae6df4fb47296e69b3` |
| 11:07:02 | +1 502 334-7406 | 44 sec | `CAae1d38a930a520500c49480fccf2c0bc` |
| 12:03:55 | +1 318 423-4898 | 4 sec | `CAe8ae1cde31b87deb97eeb2d3d1b432e0` |

The 10:53 call is **20 seconds — the exact signature of the documented
failure** (full script length, caller hears nothing). `/voice` returned
**200 in 272 ms**, so the app side behaved perfectly, as always.

## ✅ ISOLATION TEST RESULT — confirmed by Murad, 2026-09-04

**The calls to 515-1633 were FORWARDED, and they were SILENT.** Same ~20s of
nothing, then the call ends — identical to 723-9666.

**This is the strongest evidence produced so far.** The full matrix:

| AT&T forwarding trigger | → 723-9666 | → 515-1633 |
|---|---|---|
| Unreachable (phone off/airplane) | ✅ audio works | — |
| **No-answer (CFNRy)** | ❌ silent ~20s | ❌ **silent ~20s** |
| **Busy / rejected (CFB)** | ❌ silent ~20s | ❌ **silent ~20s** |

Changing the destination number changed **nothing**. That conclusively rules
out, as causes:
- the destination number itself (two independent numbers, same failure),
- that number's Twilio configuration (different number, different config),
- any per-number carrier routing or translation.

Meanwhile **unreachable forwarding — same AT&T subscriber, same carrier, same
destination — carries audio perfectly.** So AT&T's network *is* capable of
delivering a working media path to this destination; it only fails to do so on
the no-answer and busy triggers.

**Conclusion: the fault is in AT&T's provisioning of the CFNRy (no-answer) and
CFB (busy) forwarding triggers specifically — the signalling path completes
(the call connects, Twilio answers, the TwiML runs to completion) but the
RTP/media path is not established.** CFNRc (unreachable) is provisioned
correctly on the same line and proves the difference is trigger-specific, not
destination-specific.

This is the fact to lead with at AT&T. It cannot be explained by anything on
the Twilio side or the app side, and it cannot be dismissed as a bad
destination number.

## ✅ Diagnostic REVERTED — deployed 2026-09-04 18:03 CDT

Commit **`3da3fcf`**, deploy `dep-dadksnad0e5s73dbrg50`. `/voice` serves the
live `<Say>` again; no caller hears a recording any more.

Verified by polling the live endpoint, not by the dashboard's status:
- greeting flipped `<Play>` → `<Say>` at **t+20s**
- then **130 seconds** of continuous polling, every response `200`, every
  greeting `<Say>` (the 2+ minute rule from the 9/4 crash incident)
- final TwiML matches `opening_greeting()` **word for word**, so the
  code/recording drift hazard is gone
- follow-up agent survived the restart: `configured: true`, the one pending
  record still future-dated with `attempts: 0` — no duplicate send
- `/audio/greeting-open.wav` still returns 200, so the diagnostic can be
  re-armed from the commented lines in `voice()` if AT&T asks for more proof

Rollback target if ever needed: **`460ec8d`** (the deploy this replaced).

The section below is kept for the record of why it was reverted.

## Why the diagnostic was reverted

`fb2e576` (2026-09-03) changed `/voice` from `<Say>` to `<Play>` with
pre-recorded `.wav` greetings, purely to test whether Twilio's TTS caused the
AT&T silence. **It didn't** — the recording failed identically. The change was
never reverted, and every deploy since sits on top of it, so **every caller is
currently hearing a recording.**

It works (verified 2026-09-04: `/audio/greeting-open.wav` → 200, `audio/x-wav`)
and falls back to `<Say>` for any unrecorded variant. But it carries a silent
hazard: **nothing ties the `.wav` files to `opening_greeting()`'s text.** Edit
the hours or the greeting and Mia keeps speaking the old recording while every
code-level check still passes. The two match today, but `app.py` was last
edited 2h14m after the newest `.wav` was cut.

Either revert to `<Say>` now that the diagnostic has served its purpose, or
keep it and add a check that the recordings match the text. Needs Murad's call
— it's a deploy to the live phone line.

## Picking this back up

0. **Read the two sections directly above first** — the second number and the
   live `<Play>` diagnostic are both open items that this document previously
   failed to mention at all.
1. If Murad has called AT&T since this was written: ask what they said, don't
   re-diagnose from scratch — the app/Render/Twilio-config side is already
   fully cleared.
2. If a *new* symptom shows up (different from "first word then silence"),
   treat it as new evidence, not a sign the old diagnosis was wrong — check
   Twilio call logs for that specific call first (`CalledVia`, `Who Hung Up`,
   duration vs. script length) before touching any code.
3. Don't modify `build_gather()`, `/voice`, or `/gather` timing again without
   a real forwarded test call's logs showing a *specific* new timing problem
   — the three fixes above already closed out everything the logs showed.
