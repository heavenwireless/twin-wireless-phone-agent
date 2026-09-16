"""Isolated behavioural tests for ONE review text per completed repair.

Murad, 2026-09-15:
    "Implement one review request per completed repair, with duplicate
     protection across every sending path. Do not resend because the customer
     did not reply, click, or leave a review. An uncertain send must be
     reconciled before any retry."

Every assertion below runs the REAL send path with Twilio and the admin API
replaced by in-process fakes. The fake ledger implements the same rule the PHP
does -- insert-if-absent, and only `failed` releases a claim -- so a test that
passes here is testing the contract, not a mock that agrees with itself. The
PHP's own atomicity is proven separately by a 20-way concurrent claim test
against an isolated store (1 granted, 19 refused).

Fully isolated: no network, no Twilio account, no production data.

    python test_review_once.py
"""
import datetime
import os
import sys
import types
import importlib

# ---------------------------------------------------------------- environment
os.environ.update({
    "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_FROM_NUMBER": "+13187239666",
    "OWNER_PHONE": "+17735550000",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    # Unpaused: this suite is about duplicate protection, and a paused send
    # never reaches it. The pause itself is test_review_pause.py's job.
    "REVIEW_SMS_PAUSED": "0",
    "ADMIN_API_USER": "test-user",
    "ADMIN_API_PASS": "test-pass",
})

SENT = []
FAIL_NEXT = {"exc": None}


class _FakeMessages:
    def create(self, **kwargs):
        if FAIL_NEXT["exc"] is not None:
            exc = FAIL_NEXT["exc"]
            FAIL_NEXT["exc"] = None
            raise exc
        SENT.append(kwargs)
        return types.SimpleNamespace(sid="SMtest", status="queued")


class _FakeTwilio:
    def __init__(self, *a, **k):
        self.messages = _FakeMessages()


class _FakeAnthropic:
    def __init__(self, *a, **k):
        self.messages = types.SimpleNamespace(create=lambda **k: None)


import twilio.rest
import anthropic
import apscheduler.schedulers.background as _bg
twilio.rest.Client = _FakeTwilio
anthropic.Anthropic = _FakeAnthropic
_bg.BackgroundScheduler = lambda *a, **k: types.SimpleNamespace(
    add_job=lambda *a, **k: None, start=lambda: None
)

app = importlib.import_module("app")

# ------------------------------------------------------------- the fake ledger
# Mirrors admin-api/review-sends.php: POST is insert-if-absent, and a record in
# claimed / sent / uncertain blocks. Only `failed` may be taken over.
LEDGER = {}
LEDGER_DOWN = {"yes": False}
BLOCKING = ("claimed", "sent", "uncertain")


def _fake_post(path, payload):
    if LEDGER_DOWN["yes"]:
        raise RuntimeError("ledger unreachable")
    if not path.endswith("review-sends.php"):
        raise AssertionError(f"unexpected POST to {path}")
    key = payload["key"]
    existing = LEDGER.get(key)
    if existing and existing["status"] in BLOCKING:
        return {"ok": True, "claimed": False, "reason": "already " + existing["status"],
                "record": existing}
    LEDGER[key] = {
        "key": key,
        "phone": payload["phone"],
        "status": "claimed",
        "claimedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sentAt": None,
    }
    return {"ok": True, "claimed": True, "record": LEDGER[key]}


def _fake_patch(path, payload):
    if LEDGER_DOWN["yes"]:
        raise RuntimeError("ledger unreachable")
    key = payload["key"]
    if key not in LEDGER:
        return {"ok": False, "updated": False}
    LEDGER[key]["status"] = payload["status"]
    if payload["status"] == "sent":
        LEDGER[key]["sentAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"ok": True, "updated": True}


def _fake_get(path, params=None):
    if LEDGER_DOWN["yes"]:
        raise RuntimeError("ledger unreachable")
    if path.endswith("review-sends.php"):
        phone = (params or {}).get("phone")
        return {"ok": True, "records": {k: v for k, v in LEDGER.items() if v["phone"] == phone}}
    if path.endswith("sms-consent.php"):
        return {"ok": True, "consents": [{"type": "written_electronic", "consent": True}]}
    if path.endswith("followups.php"):
        return {"ok": True, "followups": []}
    raise AssertionError(f"unexpected GET to {path}")


app._admin_api_post = _fake_post
app._admin_api_patch = _fake_patch
app._admin_api_get = _fake_get

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok:   {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}" + (f"\n        {detail}" if detail else ""))


def reset():
    SENT.clear()
    LEDGER.clear()
    LEDGER_DOWN["yes"] = False
    FAIL_NEXT["exc"] = None


CUSTOMER = "+13185550142"
OTHER = "+13185550199"
now = datetime.datetime.now(datetime.timezone.utc)
recently = (now - datetime.timedelta(days=2)).isoformat()
long_ago = (now - datetime.timedelta(days=60)).isoformat()

print("\nONE REVIEW TEXT PER COMPLETED REPAIR\n")

# ------------------------------------------------------- 1. the core rule
print("1. the same repair is never texted twice")
reset()
sid, _, err = app.send_sms(CUSTOMER, "first", category="review", dedupe_key="pos-500")
check("first send goes out", err is None and sid == "SMtest", f"got {sid=} {err=}")
check("exactly one message", len(SENT) == 1, f"got {len(SENT)}")
check("ledger says sent", LEDGER["pos-500"]["status"] == "sent", f"got {LEDGER['pos-500']}")

sid, _, err = app.send_sms(CUSTOMER, "second", category="review", dedupe_key="pos-500")
check("second send is refused", sid is None and err is not None, f"got {sid=} {err=}")
check("still exactly one message", len(SENT) == 1, f"got {len(SENT)}")
check("refusal names the duplicate", "duplicate review send blocked" in (err or ""), f"got {err!r}")

# "Do not resend because the customer did not reply, click, or leave a review."
# There is no reply/click/review input anywhere in the send path, so the only
# way to prove this is that repeated attempts -- whatever prompted them -- never
# produce a second message.
for attempt in range(5):
    app.send_sms(CUSTOMER, "nudge", category="review", dedupe_key="pos-500")
check("five further attempts send nothing", len(SENT) == 1, f"got {len(SENT)}")

# ------------------------------------------------ 2. across every sending path
print("\n2. the guard holds across different sending paths")
reset()
# Website cycle claims it first...
ok, err = app._send_followup_sms(
    {"id": "1789073707785", "phone": "3185550142", "firstName": "Q"},
    {"googleReviewUrl": app.REVIEW_LINK},
)
check("website follow-up sends", ok is True, f"got {ok=} {err=}")
# ...then the POS path tries the same repair id. Different code, same ledger.
sid, _, err = app.send_sms(CUSTOMER, "pos copy", category="review",
                           dedupe_key="appt-1789073707785")
check("POS path refused for the same repair", sid is None, f"got {sid=}")
check("still one message total", len(SENT) == 1, f"got {len(SENT)}")

# ------------------------------------------- 3. same person, different repairs
print("\n3. same customer, a second repair, inside the quiet window")
reset()
app.send_sms(CUSTOMER, "repair one", category="review", dedupe_key="pos-600")
check("first repair texted", len(SENT) == 1, f"got {len(SENT)}")
check("quiet window now blocks this number", app._recently_sent_review(CUSTOMER) is True)
check("a different number is unaffected", app._recently_sent_review(OTHER) is False)

# ----------------------------------------------------- 4. uncertain vs failed
print("\n4. an uncertain send is not retried; a refused one may be")


class _TwilioRefusal(Exception):
    status = 400


reset()
FAIL_NEXT["exc"] = TimeoutError("connection reset after create")
sid, _, err = app.send_sms(CUSTOMER, "maybe sent", category="review", dedupe_key="pos-700")
check("uncertain send reports an error", sid is None and err is not None, f"got {sid=} {err=}")
check("ledger records it as uncertain", LEDGER["pos-700"]["status"] == "uncertain",
      f"got {LEDGER['pos-700']['status']}")
sid, _, err = app.send_sms(CUSTOMER, "retry", category="review", dedupe_key="pos-700")
check("retry after uncertain is REFUSED", sid is None, f"got {sid=}")
check("nothing sent on the retry", len(SENT) == 0, f"got {len(SENT)}")

reset()
FAIL_NEXT["exc"] = _TwilioRefusal("21211 invalid 'To' number")
sid, _, err = app.send_sms(CUSTOMER, "bad number", category="review", dedupe_key="pos-701")
check("provider refusal reports an error", sid is None and err is not None)
check("ledger records it as failed", LEDGER["pos-701"]["status"] == "failed",
      f"got {LEDGER['pos-701']['status']}")
sid, _, err = app.send_sms(CUSTOMER, "corrected", category="review", dedupe_key="pos-701")
check("retry after a definite refusal is ALLOWED", sid == "SMtest", f"got {sid=} {err=}")

check("timeout classifies uncertain", app._classify_send_failure(TimeoutError("x")) == "uncertain")
check("4xx classifies failed", app._classify_send_failure(_TwilioRefusal("x")) == "failed")


class _ServerError(Exception):
    status = 503


check("5xx classifies uncertain", app._classify_send_failure(_ServerError("x")) == "uncertain")

# ------------------------------------------------------------ 5. fails closed
print("\n5. an unreachable ledger stops the send")
reset()
LEDGER_DOWN["yes"] = True
sid, _, err = app.send_sms(CUSTOMER, "hello", category="review", dedupe_key="pos-800")
check("send refused when ledger is down", sid is None and err is not None, f"got {sid=} {err=}")
check("nothing left the process", SENT == [], f"got {SENT}")

reset()
saved_user = app.ADMIN_API_USER
app.ADMIN_API_USER = ""
won, why = app._claim_review_send("pos-801", CUSTOMER, claimed_by="test")
check("unconfigured admin API fails closed", won is False, f"got {won=} {why=}")
app.ADMIN_API_USER = saved_user

# ------------------------------------------------------ 6. no missing-key path
print("\n6. a review send without a repair key is impossible")
reset()
try:
    app.send_sms(CUSTOMER, "unkeyed", category="review")
    check("missing dedupe_key raises", False, "no ValueError")
except ValueError as exc:
    check("missing dedupe_key raises", True)
    check("the error says why", "duplicate protection" in str(exc), f"got {exc}")
check("nothing sent", SENT == [], f"got {SENT}")

# --------------------------------------------------------- 7. backlog cutoff
print("\n7. no historical backlog")
settings_default = {}
check("a repair finished 60 days ago is backlog",
      app._is_backlog(long_ago, settings_default) is True)
check("a repair finished 2 days ago is not",
      app._is_backlog(recently, settings_default) is False)
check("an explicit cutoff excludes anything before it",
      app._is_backlog(recently, {"reviewBacklogCutoff": now.isoformat()}) is True)
check("an unknown completion date is treated as backlog",
      app._is_backlog("", settings_default) is True)
check("an unparseable completion date is treated as backlog",
      app._is_backlog("last tuesday", settings_default) is True)

# -------------------------------------------------------- 8. the message body
print("\n8. the message is the one Murad wrote")
body = app.build_review_message("Qwandairus")
expected = (
    "Hi Qwandairus, thank you for choosing Twin Wireless. If you'd like to "
    "share your experience, here's our Google review link: "
    "https://g.page/r/CdNI_z0bef6qEBM/review Reply STOP to opt out."
)
check("wording matches exactly", body == expected, f"got {body!r}")
check("no social handles", not any(h in body.lower() for h in ("facebook", "instagram", "tiktok")))
check("no offers or upsell", not any(w in body.lower() for w in ("deal", "offer", "we also do", "% off")))
check("one STOP disclosure, not two", body.count("Reply STOP to opt out.") == 1)
check("missing name degrades safely", app.build_review_message("").startswith("Hi there,"))
check("every path builds this same body",
      app._build_followup_message({"firstName": "Qwandairus"}, {}) == expected)

# -------------------------------------------------- 9. owner test stays clean
print("\n9. the owner test writes no ledger row")
reset()
sid, _, err = app.send_sms(os.environ["OWNER_PHONE"], app.build_review_message("Murad"),
                           category="review", owner_test_override=True)
check("owner test sends", sid == "SMtest", f"got {sid=} {err=}")
check("no claim written", LEDGER == {}, f"got {LEDGER}")
check("no dedupe_key needed for the owner test", err is None)



# ===========================================================================
# 10-13: Murad's 2026-09-16 confirmations, each as a behavioural assertion
# rather than a claim about the code.
# ===========================================================================

print("\n10. dedupe is per REPAIR, not a permanent block on the person")
reset()
# Historical send, exactly as the seeder records it: this repair is done.
LEDGER["pos-900"] = {"key": "pos-900", "phone": CUSTOMER, "status": "sent",
                     "claimedAt": long_ago, "sentAt": long_ago}
sid, _, err = app.send_sms(CUSTOMER, "again", category="review", dedupe_key="pos-900")
check("the SAME repair stays blocked forever", sid is None, f"got {sid=}")
# A different repair for the same person, long after the quiet window.
sid, _, err = app.send_sms(CUSTOMER, "new repair", category="review", dedupe_key="pos-901")
check("a LATER separate repair is NOT blocked", sid == "SMtest", f"got {sid=} {err=}")
check("both repairs now in the ledger", set(LEDGER) == {"pos-900", "pos-901"}, f"got {set(LEDGER)}")

print("\n11. the quiet window is time-bounded and switchable, never permanent")
reset()
LEDGER["pos-910"] = {"key": "pos-910", "phone": CUSTOMER, "status": "sent",
                     "claimedAt": recently, "sentAt": recently}
check("a send 2 days ago blocks within a 30-day window",
      app._recently_sent_review(CUSTOMER, {"reviewQuietDays": 30}) is True)
check("the same send does NOT block a 1-day window",
      app._recently_sent_review(CUSTOMER, {"reviewQuietDays": 1}) is False)
check("reviewQuietDays=0 disables it entirely",
      app._recently_sent_review(CUSTOMER, {"reviewQuietDays": 0}) is False)
LEDGER.clear()
LEDGER["pos-911"] = {"key": "pos-911", "phone": CUSTOMER, "status": "sent",
                     "claimedAt": long_ago, "sentAt": long_ago}
check("a 60-day-old send does not block a 30-day window",
      app._recently_sent_review(CUSTOMER, {"reviewQuietDays": 30}) is False)

print("\n12. no-show / unfinished / opted-out / already-messaged stay excluded")


def cycle_with(appointments, followups, consent=True, quiet=0):
    """Run the real run_followup_cycle against fixed data. Returns what was sent."""
    SENT.clear()
    patched = []

    def fake_get(path, params=None):
        if path.endswith("site-settings.php"):
            return {"followUps": {"enabled": True, "delayHours": 0,
                                  "sendingHoursStart": 0, "sendingHoursEnd": 24,
                                  "reviewQuietDays": quiet, "reviewMaxAgeDays": 3650}}
        if path.endswith("appointments.php"):
            return {"appointments": appointments}
        if path.endswith("followups.php"):
            return {"followups": followups}
        if path.endswith("sms-consent.php"):
            return {"consents": [{"type": "written_electronic", "consent": True}] if consent else []}
        if path.endswith("review-sends.php"):
            phone = (params or {}).get("phone")
            return {"records": {k: v for k, v in LEDGER.items() if v["phone"] == phone}}
        raise AssertionError(f"unexpected GET {path}")

    app._admin_api_get = fake_get
    app._admin_api_post = lambda path, payload: (
        _fake_post(path, payload) if path.endswith("review-sends.php")
        else patched.append(payload) or {"ok": True})
    app._admin_api_patch = lambda path, payload: (
        _fake_patch(path, payload) if path.endswith("review-sends.php")
        else patched.append(payload) or {"ok": True})
    app.run_followup_cycle()
    app._admin_api_get, app._admin_api_post, app._admin_api_patch = _fake_get, _fake_post, _fake_patch
    return list(SENT)


base_appt = {"phone": "3185550142", "firstName": "Test", "fulfilledAt": recently}

reset()
sent = cycle_with(
    [dict(base_appt, id="A1", status="Fulfilled")],
    [{"appointmentId": "A1", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}])
check("a fulfilled, consented, unsent repair DOES send", len(sent) == 1, f"got {len(sent)}")

reset()
sent = cycle_with(
    [dict(base_appt, id="A2", status="No Show")],
    [{"appointmentId": "A2", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}])
check("a NO SHOW sends nothing", sent == [], f"got {sent}")

reset()
sent = cycle_with(
    [dict(base_appt, id="A3", status="In Progress")],
    [{"appointmentId": "A3", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}])
check("an UNFINISHED repair sends nothing", sent == [], f"got {sent}")

reset()
sent = cycle_with(
    [dict(base_appt, id="A4", status="Fulfilled")],
    [{"appointmentId": "A4", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending", "optedOut": True}])
check("an OPTED-OUT customer sends nothing", sent == [], f"got {sent}")

reset()
sent = cycle_with(
    [dict(base_appt, id="A5", status="Fulfilled")],
    [{"appointmentId": "A5", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "sent"}])
check("an ALREADY-MESSAGED record sends nothing", sent == [], f"got {sent}")

reset()
LEDGER["appt-A6"] = {"key": "appt-A6", "phone": CUSTOMER, "status": "sent",
                     "claimedAt": recently, "sentAt": recently}
sent = cycle_with(
    [dict(base_appt, id="A6", status="Fulfilled")],
    [{"appointmentId": "A6", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}])
check("a record the LEDGER already claims sends nothing", sent == [], f"got {sent}")

reset()
sent = cycle_with(
    [dict(base_appt, id="A7", status="Fulfilled")],
    [{"appointmentId": "A7", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}],
    consent=False)
check("NO CONSENT sends nothing", sent == [], f"got {sent}")

print("\n13. an opt-out on one repair protects the person on every other")
reset()
sent = cycle_with(
    [dict(base_appt, id="B1", status="Fulfilled"), dict(base_appt, id="B2", status="Fulfilled")],
    [{"appointmentId": "B1", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "sent", "optedOut": True},
     {"appointmentId": "B2", "phone": CUSTOMER, "fulfilledAt": recently,
      "followupScheduledAt": long_ago, "followUpStatus": "pending"}])
check("a second repair for an opted-out number sends nothing", sent == [], f"got {sent}")

print(f"\nFINAL: {PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
