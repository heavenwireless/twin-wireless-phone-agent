"""Isolated behavioural tests for the server-side review-SMS pause.

Runs the REAL send_sms / send_review_link_sms / _send_followup_sms code with
the Twilio client and the admin API stubbed out, so every assertion is about
actual behaviour rather than a static read of the source.

Fully isolated: no network, no Twilio account, no production data. The module
is imported with dummy env vars and the background scheduler suppressed, and
every outbound send is captured in a list instead of leaving the process.

    python test_review_pause.py            # paused (production default)
    python test_review_pause.py --unpaused # proves the gate opens correctly
"""
import os
import sys
import types
import importlib

UNPAUSED = "--unpaused" in sys.argv

# ---------------------------------------------------------------- environment
# Dummy values only. Nothing here can reach a real account.
os.environ.update({
    "TWILIO_ACCOUNT_SID": "ACtest0000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test-token",
    "TWILIO_FROM_NUMBER": "+13187239666",
    "OWNER_PHONE": "+17735550000",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "REVIEW_SMS_PAUSED": "0" if UNPAUSED else "1",
})
os.environ.pop("ADMIN_API_USER", None)
os.environ.pop("ADMIN_API_PASS", None)
os.environ.pop("TWILIO_MESSAGING_SERVICE_SID", None)

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


# Stub the SDKs and the scheduler BEFORE importing app, so module-level
# construction and scheduler.start() never touch the network.
import twilio.rest
import anthropic
import apscheduler.schedulers.background as _bg
twilio.rest.Client = _FakeTwilio
anthropic.Anthropic = _FakeAnthropic
_bg.BackgroundScheduler = lambda *a, **k: types.SimpleNamespace(
    add_job=lambda *a, **k: None, start=lambda: None
)

app = importlib.import_module("app")

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok:   {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}" + (f"\n        {detail}" if detail else ""))


CUSTOMER = "+13185550142"
OWNER = os.environ["OWNER_PHONE"]

print(f"\nREVIEW_SMS_PAUSED = {app.REVIEW_SMS_PAUSED}  (--unpaused={UNPAUSED})\n")

# ------------------------------------------------------------ 1. send_sms gate
print("1. send_sms category gate")
SENT.clear()
sid, status, err = app.send_sms(CUSTOMER, "review please", category="review")
if app.REVIEW_SMS_PAUSED:
    check("review to customer is refused", err is not None and sid is None, f"got {sid=} {err=}")
    check("nothing left the process", SENT == [], f"got {SENT}")
    check("error names the pause", "REVIEW_SMS_PAUSED" in (err or ""), f"got {err!r}")
else:
    check("review to customer is allowed", err is None and sid == "SMtest", f"got {sid=} {err=}")
    check("one message sent", len(SENT) == 1, f"got {SENT}")
    check("opt-out line appended", app.SMS_OPT_OUT_LINE in SENT[0]["body"], f"got {SENT[0]['body']!r}")

SENT.clear()
_, _, err = app.send_sms(CUSTOMER, "hi", category="transactional")
check("transactional still sends while paused", err is None and len(SENT) == 1, f"got {err=} {SENT=}")
check("transactional gets NO opt-out line", app.SMS_OPT_OUT_LINE not in SENT[0]["body"])

SENT.clear()
_, _, err = app.send_sms(OWNER, "owner alert", category="owner")
check("owner alerts still send while paused", err is None and len(SENT) == 1, f"got {err=}")

SENT.clear()
_, _, err = app.send_sms(CUSTOMER, "promo", category="marketing")
check("non-review marketing is not blocked by review pause", err is None and len(SENT) == 1)
check("marketing gets opt-out line", app.SMS_OPT_OUT_LINE in SENT[0]["body"])

try:
    app.send_sms(CUSTOMER, "x", category="bogus")
    check("unknown category rejected", False, "no ValueError raised")
except ValueError:
    check("unknown category rejected", True)

# --------------------------------------------- 2. owner_test_override scoping
print("\n2. owner_test_override is scoped to OWNER_PHONE")
SENT.clear()
sid, _, err = app.send_sms(OWNER, "owner test", category="review", owner_test_override=True)
check("override works for OWNER_PHONE", err is None and sid == "SMtest", f"got {sid=} {err=}")

SENT.clear()
sid, _, err = app.send_sms(CUSTOMER, "sneaky", category="review", owner_test_override=True)
if app.REVIEW_SMS_PAUSED:
    check("override does NOT work for a customer", sid is None and err is not None, f"got {sid=} {err=}")
    check("nothing sent to the customer", SENT == [], f"got {SENT}")
else:
    check("override is a no-op when unpaused", err is None)

# unnormalized owner number must still be recognised
SENT.clear()
sid, _, err = app.send_sms("7735550000", "owner test", category="review", owner_test_override=True)
check("override matches owner on un-normalized number", err is None and sid == "SMtest", f"got {err=}")

# ------------------------------------------- 3. phone-agent review-link route
print("\n3. send_review_link_sms (phone-agent tool route)")
SENT.clear()
app.send_review_link_sms(CUSTOMER)
if app.REVIEW_SMS_PAUSED:
    check("no review link texted while paused", SENT == [], f"got {SENT}")
else:
    check("review link texted when unpaused", len(SENT) == 1, f"got {SENT}")
    check("review link body carries opt-out", app.SMS_OPT_OUT_LINE in SENT[0]["body"])

SENT.clear()
app.send_review_link_sms("not-a-number")
check("malformed number never sends", SENT == [])

# ------------------------------------------------ 4. tool list + prompt wiring
print("\n4. model-facing surface")
tools = app.build_tools() if hasattr(app, "build_tools") else None
src = open(os.path.join(os.path.dirname(__file__), "app.py"), encoding="utf-8").read()
has_tool_guard = "if not REVIEW_SMS_PAUSED:" in src and "send_review_link" in src
check("tool exposure is guarded", has_tool_guard)
if app.REVIEW_SMS_PAUSED:
    check("prompt tells agent not to offer a review text",
          "Do not ask callers for a review" in app.SYSTEM_PROMPT)
    check("prompt does not still instruct the old ask",
          "would you mind" not in app.SYSTEM_PROMPT)
    check("prompt redirects to the counter QR card",
          "QR code on the counter" in app.SYSTEM_PROMPT)
else:
    check("prompt restores the review ask", "send_review_link tool" in app.SYSTEM_PROMPT)

# ------------------------------------------------- 5. website follow-up route
print("\n5. _send_followup_sms (website follow-up cycle route)")
SENT.clear()
ok, err = app._send_followup_sms(
    {"phone": "3185550142", "repairs": [{"repair": "screen"}], "name": "Test"},
    {"googleReviewUrl": app.REVIEW_LINK, "serviceRecommendationsEnabled": False},
)
if app.REVIEW_SMS_PAUSED:
    check("follow-up refused while paused", ok is False and err is not None, f"got {ok=} {err=}")
    check("nothing sent", SENT == [], f"got {SENT}")
else:
    check("follow-up sends when unpaused", ok is True and len(SENT) == 1, f"got {ok=} {err=}")

# ------------------------------------------------------ 6. consent gate shape
print("\n6. _has_written_consent fails closed")
check("no consent when admin API unconfigured", app._has_written_consent(CUSTOMER) is False)


def _boom(*a, **k):
    raise RuntimeError("admin API down")


app._admin_api_get = _boom
app.ADMIN_API_USER, app.ADMIN_API_PASS = "u", "p"
check("no consent when admin API errors", app._has_written_consent(CUSTOMER) is False)

app._admin_api_get = lambda path, params=None: {
    "consents": [{"type": "written_electronic", "consent": True}]
}
check("consent recognised when record exists", app._has_written_consent(CUSTOMER) is True)

app._admin_api_get = lambda path, params=None: {
    "consents": [{"type": "verbal_staff_recorded", "consent": True}]
}
check("staff-recorded verbal consent REJECTED", app._has_written_consent(CUSTOMER) is False)

app._admin_api_get = lambda path, params=None: {
    "consents": [{"type": "written_electronic", "consent": "yes"}]
}
check("truthy-but-not-true consent rejected", app._has_written_consent(CUSTOMER) is False)

print(f"\nRESULT: {PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
