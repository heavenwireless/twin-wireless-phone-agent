"""Does REVIEW_REQUIRE_WRITTEN_CONSENT actually do what its name says?

Murad, 2026-09-16: "go and fix everything to the previous eidition where it was
working and sending messages last." The previous edition texted any completed
repair; the written-consent gate added on 2026-09-15 is what stopped it. This
suite proves the override relaxes THAT and nothing else.

The point is the "nothing else". Relaxing a consent check by hand is exactly the
kind of edit that quietly takes a neighbouring guard with it, so every case below
runs with the override ON and asserts that opt-out suppression, duplicate
protection and the backlog cutoff still refuse the send.

Fully isolated: no network, no Twilio account, no production data. The admin API
is faked, and the consent endpoint deliberately returns NOTHING -- a POS walk-in
who never filled in a form.

    python test_consent_override.py
"""
import datetime
import importlib
import os
import types

# ---------------------------------------------------------------- environment
os.environ.update({
    "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_FROM_NUMBER": "+13187239666",
    "OWNER_PHONE": "+17735550000",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "ADMIN_API_USER": "test-user",
    "ADMIN_API_PASS": "test-pass",
    "POS_REVIEW_SECRET": "test-secret",
    "REVIEW_SMS_PAUSED": "0",
})

SENT = []


class _FakeMessages:
    def create(self, **kwargs):
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
LEDGER = {}
OPTED_OUT = set()
BLOCKING = ("claimed", "sent", "uncertain")


def _fake_post(path, payload):
    if not path.endswith("review-sends.php"):
        raise AssertionError(f"unexpected POST to {path}")
    key = payload["key"]
    existing = LEDGER.get(key)
    if existing and existing["status"] in BLOCKING:
        return {"ok": True, "claimed": False, "reason": "already " + existing["status"],
                "record": existing}
    LEDGER[key] = {"key": key, "phone": payload["phone"], "status": "claimed",
                   "claimedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "sentAt": None}
    return {"ok": True, "claimed": True, "record": LEDGER[key]}


def _fake_patch(path, payload):
    key = payload["key"]
    if key not in LEDGER:
        return {"ok": False, "updated": False}
    LEDGER[key]["status"] = payload["status"]
    if payload["status"] == "sent":
        LEDGER[key]["sentAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {"ok": True, "updated": True}


def _fake_get(path, params=None):
    if path.endswith("review-sends.php"):
        phone = (params or {}).get("phone")
        return {"ok": True, "records": {k: v for k, v in LEDGER.items() if v["phone"] == phone}}
    if path.endswith("sms-consent.php"):
        # The whole point: this walk-in has NO consent record.
        return {"ok": True, "consents": []}
    if path.endswith("followups.php"):
        # Opt-out is read from here by _is_opted_out.
        phone = (params or {}).get("phone")
        if phone in OPTED_OUT:
            return {"ok": True, "followups": [{"phone": phone, "optedOut": True}]}
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
    OPTED_OUT.clear()


now = datetime.datetime.now(datetime.timezone.utc)
RECENT = (now - datetime.timedelta(days=1)).isoformat()
ANCIENT = (now - datetime.timedelta(days=400)).isoformat()

client = app.app.test_client()


def post_pos_review(phone, repair_id, completed_at=RECENT, name="Walkin"):
    return client.post(
        "/send-pos-review",
        headers={"X-Webhook-Secret": "test-secret"},
        json={"phone": phone, "name": name, "repairId": repair_id,
              "completedAt": completed_at},
    )


print("\nWRITTEN-CONSENT OVERRIDE\n")

# ------------------------------------------- 1. the flag parses the way it says
print("1. the environment variable is read fail-safe")


def parse(value):
    """The exact expression app.py uses, so this tests the real rule."""
    return (value if value is not None else "1").strip().lower() not in ("0", "false", "no")


check("unset means REQUIRED", parse(None) is True)
check("empty means REQUIRED", parse("") is True)
check("'1' means REQUIRED", parse("1") is True)
check("'0' relaxes it", parse("0") is False)
check("'false' relaxes it", parse("false") is False)
check("'no' relaxes it", parse("no") is False)
check("' 0 ' relaxes it (whitespace tolerated)", parse(" 0 ") is False)
check("a typo stays REQUIRED", parse("offf") is True)
check("shipped default is REQUIRED", app.REVIEW_REQUIRE_WRITTEN_CONSENT is True,
      f"got {app.REVIEW_REQUIRE_WRITTEN_CONSENT!r}")

# ------------------------------------------- 2. required: the walk-in is held
print("\n2. with the requirement ON, a no-consent walk-in is refused")
reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = True
r = post_pos_review("+13185550142", "701")
check("refused", r.get_json().get("skipped") == "no-written-consent", f"got {r.get_json()}")
check("nothing sent", len(SENT) == 0, f"got {len(SENT)}")
check("no ledger claim left behind", LEDGER == {}, f"got {LEDGER}")

# ------------------------------------------- 3. relaxed: the walk-in goes out
print("\n3. with the requirement OFF, the same walk-in sends")
reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = False
r = post_pos_review("+13185550142", "701")
check("sent", r.get_json().get("ok") is True, f"got {r.get_json()}")
check("exactly one message", len(SENT) == 1, f"got {len(SENT)}")
check("it is the real review wording",
      "g.page/r/CdNI_z0bef6qEBM/review" in SENT[0].get("body", ""),
      f"got {SENT[0].get('body')!r}")
check("the opt-out line is present", "STOP" in SENT[0].get("body", "").upper(),
      f"got {SENT[0].get('body')!r}")

# --------------------------- 4. the other guards are NOT relaxed along with it
print("\n4. relaxing consent relaxes NOTHING else")

reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = False
OPTED_OUT.add("+13185550143")
r = post_pos_review("+13185550143", "702")
check("an opted-out customer is still refused",
      r.get_json().get("skipped") == "opted-out", f"got {r.get_json()}")
check("nothing sent to them", len(SENT) == 0, f"got {len(SENT)}")

reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = False
post_pos_review("+13185550144", "703")
first = len(SENT)
r = post_pos_review("+13185550144", "703")
check("the same repair is still never texted twice", len(SENT) == first == 1,
      f"got {len(SENT)} after {first}")

reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = False
r = post_pos_review("+13185550145", "704", completed_at=ANCIENT)
check("a 400-day-old repair is still held by the backlog cutoff",
      r.get_json().get("skipped") == "backlog-cutoff", f"got {r.get_json()}")
check("no historical blast", len(SENT) == 0, f"got {len(SENT)}")

reset()
app.REVIEW_REQUIRE_WRITTEN_CONSENT = False
app.REVIEW_SMS_PAUSED = True
r = post_pos_review("+13185550146", "705")
check("the pause still outranks the override",
      r.get_json().get("skipped") == "review-sms-paused", f"got {r.get_json()}")
check("nothing sent while paused", len(SENT) == 0, f"got {len(SENT)}")
app.REVIEW_SMS_PAUSED = False

# ------------------------------------------- 5. the gate itself is not damaged
print("\n5. the consent function itself is untouched")
app.REVIEW_REQUIRE_WRITTEN_CONSENT = True
check("still reports no consent when there is none",
      app._has_written_consent("+13185550142") is False)


def _consenting_get(path, params=None):
    if path.endswith("sms-consent.php"):
        return {"ok": True, "consents": [{"type": "written_electronic", "consent": True}]}
    return _fake_get(path, params)


app._admin_api_get = _consenting_get
check("still recognises a real written consent record",
      app._has_written_consent("+13185550142") is True)
app._admin_api_get = _fake_get

print(f"\nRESULT: {PASS} passed, {FAIL} failed\n")
raise SystemExit(1 if FAIL else 0)
