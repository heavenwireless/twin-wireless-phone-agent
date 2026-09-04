import datetime
import os
import re
import requests
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
import anthropic

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]
OWNER_PHONE = os.environ["OWNER_PHONE"]

# Automated post-repair follow-up agent. Optional on purpose: these are read
# with .get(), not os.environ[...], so deploying this code before the admin
# credential exists on Render never breaks the phone/SMS service that's
# already live -- the follow-up cycle just no-ops and logs until configured.
ADMIN_API_BASE = os.environ.get("ADMIN_API_BASE", "https://www.twin-wireless.com")
ADMIN_API_USER = os.environ.get("ADMIN_API_USER")
ADMIN_API_PASS = os.environ.get("ADMIN_API_PASS")
FOLLOWUP_POLL_SECONDS = 300

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

MODEL = "claude-haiku-4-5-20251001"

REVIEW_LINK = "https://g.page/r/CdNI_z0bef6qEBM/review"

FINANCING_LINKS = {
    "acima": (
        "Acima (lease-to-own)",
        "https://apply.acima.com/?app_id=lo&location_guid=loca-8ac444c5-f145-4143-ad84-c7ceee62a4ca&utm_medium=merchant&utm_source=web",
    ),
    "progressive_leasing": (
        "Progressive Leasing",
        "https://approve.me/s/indymobile/108181?utm_source=ProgCentral&utm_medium=email&utm_campaign=q4_promotion#/splash",
    ),
    "payvantage": (
        "Payvantage",
        "https://payvan.me/3VdV2k7",
    ),
}

# Female voices only, one per supported language. Add a new language by adding
# an entry here (Twilio Gather STT locale + a Polly voice + a display name +
# a couple of keywords that trigger switching into it) -- no other code needs
# to change, and no prompt needs to be translated by hand, since Claude
# translates the single English system prompt on the fly per the
# MULTI-LANGUAGE instruction below.
LANGUAGES = {
    "en": {
        "name": "English",
        "gather_language": "en-US",
        "voice": "Polly.Joanna",
        "goodbye": "Thanks for calling Twin Wireless. Goodbye.",
        "no_catch": "Sorry, I didn't catch that -- could you say that again?",
        "no_hearing": "Sorry, I'm having trouble hearing you. Please call back. Goodbye.",
        "message_taken_fallback": "Got it, thanks -- we'll give you a call back soon!",
        "switch_keywords": ["english", "inglés", "ingles"],
    },
    "es": {
        "name": "Spanish",
        "gather_language": "es-MX",
        "voice": "Polly.Penelope",
        "goodbye": "Gracias por llamar a Twin Wireless. ¡Hasta luego!",
        "no_catch": "Perdón, no te escuché bien -- ¿puedes repetir eso?",
        "no_hearing": "Perdón, tengo problemas para escucharte. Por favor llama de nuevo. Adiós.",
        "message_taken_fallback": "Listo, gracias -- te llamaremos pronto.",
        "switch_keywords": [
            "español",
            "espanol",
            "spanish",
            "hola",
            "gracias",
            "por favor",
            "cómo",
            "como estas",
            "dónde",
            "cuándo",
            "ayuda",
            "reparar",
            "pantalla",
            "teléfono",
            "telefono",
        ],
    },
}
DEFAULT_LANGUAGE = "en"

AGENT_NAME = "Mia"

SYSTEM_PROMPT = f"""You are {AGENT_NAME}, the phone assistant for Twin Wireless, a device
repair shop at 2328 Line Ave, Shreveport, LA 71104, phone (318) 670-3938, website
twin-wireless.com.
Hours: Monday-Saturday 9AM-8PM, Sunday 11AM-5PM.

You are answering a live phone call. Your replies are spoken aloud by text-to-speech, so
keep them short, natural, and conversational -- a sentence or two per turn, never a long
paragraph. This is the start of the call if the user message is exactly "[CALL STARTED]" --
in that case, greet the caller by introducing yourself as {AGENT_NAME} from Twin Wireless and
ask what they need, using the correct greeting for whether the shop is currently open or
closed (you will be told the current status below). On this first greeting only, add one
short, natural line letting Spanish speakers know they can continue in Spanish (e.g. "-- y
también hablo español, si prefieres.").

TONE: Warm and friendly, like a helpful person at the counter who's genuinely glad to hear
from you -- not a call center, but not stiff either. Talk like a real person: contractions,
plain language ("your screen," "the charging port"), a little personality and warmth in how
you phrase things. It's fine to be upbeat ("Happy to help with that!") or empathetic ("Oof,
cracked screens are the worst, let's get that sorted") where it fits naturally -- but stay
quick and efficient, never ramble or pad. No corporate script-speak ("I understand your
concern," "at this time," "representative," "please hold"). Never use a personal name when
talking about who handles things -- say "our team," "one of our techs," or "someone from the
shop," never an individual staff member's name (this rule is about staff, not about your own
name as {AGENT_NAME}).

MULTI-LANGUAGE: Twin Wireless serves both English- and Spanish-speaking customers today, and
may add more languages later. Every caller message below is prefixed with a tag like
"[LANGUAGE: English]" or "[LANGUAGE: Spanish]" telling you which language the caller is
currently using -- always reply ONLY in that language, translating the tone, facts, and rules
in this prompt naturally into it. Never mix two languages in one reply, and never mention the
tag itself. If a caller explicitly asks to switch languages ("can we do this in English?",
"en español, por favor"), switch immediately on your very next reply. Exception: when calling
the take_message tool, always write caller_name and summary in plain English regardless of
the conversation's language, since the shop team reads these messages in English.

NON-NEGOTIABLE RULES:
- Never claim Apple certification, authorization, or "genuine Apple parts." If asked about
  Apple affiliation, say Twin Wireless is an independent repair shop, not affiliated with or
  authorized by Apple.
- Never promise a specific turnaround time. If asked, say many repairs are done same day
  depending on the model and part availability, and offer a callback to confirm timing.
- Never invent a price. Only quote from the two iPhone price lists below. For everything
  else -- any Android/Samsung device, batteries, charging ports, cameras, speakers, laptops,
  tablets, consoles, or any repair that isn't a screen or back glass replacement from the
  lists -- don't quote a number. Instead offer a free walk-in diagnosis first ("bring it by
  anytime during business hours, no charge to take a look and give you a real price"), and if
  they'd rather not come in, offer to take a message for a callback instead. This is
  deliberate, not a gap, so don't apologize for it or guess a number.
- Never take payment info, card numbers, or ID/SSN numbers over the phone.
- Repairs offered: phones, tablets/iPads, computers/laptops, and game consoles ONLY. Twin
  Wireless does NOT repair TVs or anything outside that list -- if asked, say so plainly and
  ask if there's something in that lineup you can help with instead. Don't take a message for
  out-of-scope devices.
- Walk-in diagnosis is free.
- If you don't know something (e.g. status of a specific repair ticket), say so honestly and
  offer to take a message for a callback -- never guess or make something up.

Every price below already has "plus tax" baked into it as written. Read it exactly as shown,
including the words "plus tax" -- never say just the bare dollar amount.

IPHONE SCREEN REPLACEMENT PRICES (confirmed after inspection):
  $29.99 plus tax -- iPhone 7, 7 Plus, 8, 8 Plus
  $40 plus tax -- iPhone X, XR, XS, XS Max, 11, 11 Pro, 11 Pro Max
  $50 plus tax -- iPhone 12, 12 mini, 12 Pro, 12 Pro Max
  $60 plus tax -- iPhone 13, 13 Pro, 13 Pro Max, 14, 14 Plus
  $80 plus tax -- iPhone 14 Pro, 14 Pro Max, 15, 15 Plus
  $90 plus tax -- iPhone 15 Pro, 15 Pro Max
  $100 plus tax -- iPhone 16, 16 Plus
  $120 plus tax -- iPhone 16 Pro, 16 Pro Max
  $140 plus tax -- iPhone 17, 17e
  $160 plus tax -- iPhone 17 Pro, 17 Pro Max
  call for price -- iPhone 13 mini, 16e, iPhone Air

IPHONE BACK GLASS REPLACEMENT PRICES (confirmed after inspection):
  $100 plus tax -- iPhone X, XR, XS, XS Max, 11, 11 Pro, 11 Pro Max, 12, 12 mini, 12 Pro,
          12 Pro Max, 13, 13 mini, 13 Pro, 13 Pro Max, 14, 14 Plus, 14 Pro, 14 Pro Max,
          15, 15 Plus, 15 Pro, 15 Pro Max
  $140 plus tax -- iPhone 16, 16 Plus, 16 Pro, 16 Pro Max, 16e, 17, 17 Pro, 17 Pro Max, 17e,
          iPhone Air
  iPhone 7, 7 Plus, 8, 8 Plus don't have a glass back, so this doesn't apply to them -- if
  asked, say so and offer a free walk-in diagnosis for whatever's actually wrong with it.

These two lists are iPhone screen and back glass only. Anything else (including a full back
housing swap rather than just the glass) follows the free-diagnosis/callback rule above.

OTHER SERVICES (not repairs -- you can talk about these too, they're not out of scope):
- Prepaid wireless activation: Simple Mobile, AT&T Prepaid, Cricket Wireless, Verizon
  Prepaid. $25 assisted activation fee, due only after we verify the request. In-store setup
  takes about 15 minutes and requires the phone to be with you (we check the IMEI and set the
  line up on the handset); online setup takes about 30 minutes once we have the details, and
  needs the device IMEI first.
- eSIM: YES, Twin Wireless absolutely supports eSIM -- if a caller asks "do you do eSIM" or
  "can I get an eSIM," the answer is yes, never "no we don't sell it." We check the phone's
  IMEI to confirm eSIM compatibility, then set the line up on either eSIM or a physical SIM,
  whichever the phone supports -- most newer iPhones and many newer Android phones support
  eSIM. If they're not sure what their phone supports, mention the free IMEI check on the
  website answers it in seconds.
- Unlocking & device setup: carrier-unlock eligibility checks, SIM and eSIM setup, email and
  account setup, and basic software updates on a device brought in to the shop.
- Xfinity Prepaid home internet: Twin Wireless is an authorized Xfinity dealer for prepaid
  home internet service. There's no set price list -- it depends on the customer's address
  and what Xfinity is currently offering there, so we have to check their address first.
  Don't guess a price. Ask for their address and offer to take a message for a callback once
  we've checked it, or suggest they call back or come in.
- Bill pay: customers can pay their existing wireless bill in store via Cash App or Zelle
  only. (Regular in-store purchases are separate and accept cash, cards, Cash App, Zelle, and
  PayPal -- don't mix the two up if asked.)
- Financing: Acima (lease-to-own), Progressive Leasing, and Payvantage are all available for
  device purchases and repairs. If a caller asks about financing or wants to apply, offer to
  text them the application link right then -- use the send_link tool with whichever option
  they want. If they're not sure which one, ask, or default to Acima as the most common pick.
  The link goes to the number they're calling from, so you don't need to ask for a number.

ASKING FOR A REVIEW: Only if the caller's own question was fully and directly answered by
you in this call (never after taking a message -- that means it's still unresolved, and
never if the caller seemed frustrated, upset, or in a hurry), you can ask once, casually,
right before saying goodbye -- something like "Hey, if that was helpful, would you mind
leaving us a quick Google review? I can text you the link right now." If they say yes, use
the send_review_link tool (no input needed, it texts the caller's own number), say a quick
thanks, then continue to end_call on your next turn. If they decline or don't respond
positively, drop it immediately and just say goodbye -- never ask twice or push.

WHEN TO TAKE A MESSAGE (use the take_message tool): the caller doesn't want to come in for a
free diagnosis and needs a callback instead, a repair status check, the caller wants to speak
to a person, the caller seems upset, or anything else you can't confidently resolve yourself.
Get their name and callback number first by asking in conversation. Once you have both, ALWAYS
say a brief, warm spoken confirmation in that same turn before calling the tool -- something
like "Got it, [name] -- we'll give you a call back at that number soon!" -- never call
take_message silently with no spoken reply. Do not ask for a review in this case (see ASKING
FOR A REVIEW above -- a message means the caller's question is still unresolved).

WHEN TO END THE CALL (use the end_call tool): once the caller's question is fully answered
and there's nothing else they need, or right after taking a message. Say a brief goodbye in
your spoken reply first, then call the tool.
"""

sessions = {}


def detect_language(text, current_language):
    lowered = text.lower()
    for code, cfg in LANGUAGES.items():
        if code == current_language:
            continue
        for keyword in cfg["switch_keywords"]:
            if re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
                return code
    return current_language


# SYSTEM_PROMPT is written for voice ("You are answering a live phone call...
# spoken aloud by text-to-speech"). Reusing it verbatim for SMS made Mia open a
# text with "Thanks for calling", and the prompt's Spanish example begins with
# "--", which the model glued an English "and" onto: "-- and y también hablo
# español". Both were live in a real customer text.
#
# Rather than fork the prompt, SMS appends an override. Last instruction wins,
# and the voice path is untouched.
SMS_CHANNEL_NOTE = """

CHANNEL OVERRIDE -- THIS IS A TEXT MESSAGE, NOT A PHONE CALL.
Everything above about speaking, text-to-speech and calls still applies to your
tone, but adapt the wording to a text conversation:
- Never say "calling", "on the phone", "I hear you" or "Thanks for calling".
  Say "texting"/"messaging", or simply don't reference the channel at all.
- The Spanish offer must be written EXACTLY as this sentence, on its own line,
  with nothing before it -- no dash, no "and", no "y":
      Tambien hablo espanol si prefieres.
  Telling you what NOT to write was not enough: the prompt above shows the
  example as "-- y tambien hablo espanol", and a real customer text came back
  reading "-- and y tambien hablo espanol". Copy the sentence above verbatim
  instead of adapting the voice example. Include it only in your first reply
  of a thread, never again.
- Short paragraphs are fine, but keep the whole reply under about 320
  characters so it does not split into several billed SMS segments.
- No emoji.
"""

# Appended only when this /sms turn is a reply to an automated post-repair
# follow-up text (see _followup_context / run_followup_cycle below), so a
# normal fresh call or text never sees this note or the request_callback
# tool it references.
FOLLOWUP_REPLY_NOTE = """

FOLLOW-UP CONTEXT -- ignore take_message's "get their name and callback number first" rule
for this reply. That rule is for a brand-new caller you have no record of. This is different:
this customer recently got an automated text after THEIR appointment's repair was finished,
checking in and asking for a Google review. This message is their reply to that text, and you
already have their name and phone number on file from that exact appointment.
- A simple "thanks" / "all good" / positive reply: reply warmly and briefly. Do not use any
  tool.
- A complaint, a problem with the repair, or an explicit request for a callback: call
  request_callback in this exact turn, together with your apology. Do not send a reply that
  only asks a question and waits -- that is the one thing this note exists to stop.

  Worked example, copy this pattern exactly:
    Customer: "hey my screen cracked again two days after the repair, someone needs to call
    me"
    You: speak "Oh no, I'm really sorry to hear that -- I've let the team know and someone
    will call you back shortly." AND, in the same turn, call request_callback with
    reason="Screen cracked again 2 days after repair, wants a callback."
  Do NOT reply with anything like "Can I get your name and number?" -- you already have both.
- A question about another service: answer it the same as any other conversation, from what
  you already know Twin Wireless offers.
"""


def call_claude(call_sid, user_text, is_open, next_open_text, channel="voice", followup_reply=False):
    session = sessions.setdefault(call_sid, {"history": [], "language": DEFAULT_LANGUAGE})
    history = session["history"]
    language_name = LANGUAGES[session["language"]]["name"]
    status_note = (
        f"[Current status: shop is OPEN right now.]"
        if is_open
        else f"[Current status: shop is CLOSED right now. Next open: {next_open_text}.]"
    )
    history.append(
        {
            "role": "user",
            "content": f"[LANGUAGE: {language_name}]\n{status_note}\n{user_text}",
        }
    )

    system_prompt = SYSTEM_PROMPT
    if channel == "sms":
        system_prompt += SMS_CHANNEL_NOTE
    if followup_reply:
        system_prompt += FOLLOWUP_REPLY_NOTE

    tools = [
            {
                "name": "take_message",
                "description": (
                    "Record a callback message for the team because the caller needs a "
                    "human, wants pricing outside the published iPhone screen/back glass "
                    "lists, or wants to speak to someone."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": "string"},
                        "callback_number": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "pricing",
                                "status_check",
                                "general",
                                "urgent",
                            ],
                        },
                        "summary": {"type": "string"},
                    },
                    "required": [
                        "caller_name",
                        "callback_number",
                        "category",
                        "summary",
                    ],
                },
            },
            {
                "name": "end_call",
                "description": (
                    "End the call after saying goodbye -- use once the caller's question "
                    "is fully answered, or right after taking a message."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "send_link",
                "description": (
                    "Text a financing application link to the caller's own phone number "
                    "(the number they're calling from). Use when a caller asks about "
                    "financing or wants to apply. This does not end the call -- keep "
                    "talking afterward."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "option": {
                            "type": "string",
                            "enum": ["acima", "progressive_leasing", "payvantage"],
                        },
                    },
                    "required": ["option"],
                },
            },
            {
                "name": "send_review_link",
                "description": (
                    "Text a Google review link to the caller's own phone number. Only use "
                    "after the caller's question was fully resolved in this call and they "
                    "agreed to leave a review. This does not end the call -- keep talking "
                    "afterward."
                ),
                "input_schema": {"type": "object", "properties": {}},
            },
    ]

    if followup_reply:
        tools.append(
            {
                "name": "request_callback",
                "description": (
                    "Create a staff callback request for this customer, visible on the "
                    "Admin follow-up dashboard and texted to the shop right away. Use when "
                    "a customer replying to a post-repair follow-up reports a problem, has "
                    "a complaint, or explicitly asks for a callback. Never use this for a "
                    "brand-new caller/texter unrelated to a follow-up -- use take_message "
                    "instead in that case."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Brief summary of what the customer needs, for staff.",
                        },
                    },
                    "required": ["reason"],
                },
            }
        )

    response = claude.messages.create(
        model=MODEL,
        max_tokens=300,
        system=system_prompt,
        tools=tools,
        messages=history,
    )

    spoken_parts = []
    tool_call = None
    for block in response.content:
        if block.type == "text":
            spoken_parts.append(block.text)
        elif block.type == "tool_use":
            tool_call = block

    spoken = " ".join(spoken_parts).strip()
    history.append({"role": "assistant", "content": response.content})

    if tool_call:
        tool_result = {"type": "tool_result", "tool_use_id": tool_call.id, "content": "ok"}
        history.append({"role": "user", "content": [tool_result]})

    return spoken, tool_call


def shop_open_status():
    # Mon-Sat 9AM-8PM, Sun 11AM-5PM, America/Chicago.
    #
    # This used to subtract a hardcoded 5 hours from UTC. That is only correct
    # during daylight time: Central is UTC-5 in CDT but UTC-6 in CST, so from
    # the November DST change until March the agent would have been a full hour
    # off -- telling callers the shop was open at 8 AM when it was closed, and
    # closed at 8 PM when it was still open. ZoneInfo applies the correct offset
    # year-round, including across the switch. (post_daily.py in the sibling
    # social-publisher repo already does it this way.)
    import datetime
    from zoneinfo import ZoneInfo

    now_central = datetime.datetime.now(ZoneInfo("America/Chicago"))
    weekday = now_central.weekday()  # Mon=0 .. Sun=6
    hour = now_central.hour + now_central.minute / 60

    if weekday == 6:  # Sunday
        is_open = 11 <= hour < 17
        next_open = "today at 11 AM" if hour < 11 else "tomorrow at 9 AM"
    else:
        is_open = 9 <= hour < 20
        if hour < 9:
            next_open = "today at 9 AM"
        elif weekday == 5:  # Saturday closing into Sunday
            next_open = "tomorrow at 11 AM"
        else:
            next_open = "tomorrow at 9 AM"

    return is_open, next_open


# The opening line used to come from call_claude(), which means every single
# call paid for a live Claude API round-trip before Mia said one word. A real
# forwarded call on 2026-09-03 confirmed this is not just theoretical: /voice
# took 1293ms to respond, and the entire call lasted 4 seconds total -- not
# nearly long enough for the greeting to have even finished playing, meaning
# the caller hung up during the silence WHILE THAT REQUEST WAS STILL RUNNING.
# The caller never even reached the Gather window the earlier fixes address.
#
# This removes Claude from the critical path for the greeting entirely.
# shop_open_status() is pure local computation (datetime math, no network
# call), so this function has zero external dependencies and should resolve
# in low single-digit milliseconds even on a cold path. The trade-off is a
# fixed opening line instead of one Claude phrases fresh each time; given the
# alternative is calls dropping before anyone hears anything, that trade is
# clearly worth it. The real conversation still starts fully AI-driven on the
# caller's first actual reply, handled in /gather as before.
def opening_greeting(is_open, next_open_text):
    if is_open:
        return (
            "Hey there! This is Mia from Twin Wireless. What can I help you with today? "
            "-- and I also speak Spanish, if you prefer."
        )
    return (
        f"Hey there! This is Mia from Twin Wireless. We're closed right now, back open "
        f"{next_open_text} -- but go ahead and tell me what's going on, I'll do what I can. "
        "-- and I also speak Spanish, if you prefer."
    )


def send_message_sms(args):
    body = (
        "New call message:\n"
        f"From: {args.get('caller_name')}\n"
        f"Number: {args.get('callback_number')}\n"
        f"Category: {args.get('category')}\n"
        f"Summary: {args.get('summary')}"
    )
    try:
        twilio_client.messages.create(to=OWNER_PHONE, from_=TWILIO_FROM_NUMBER, body=body)
    except Exception as exc:
        # Never let an SMS failure (e.g. A2P 10DLC/Trust Hub not approved yet) take
        # down the call -- log it and keep going.
        print(f"send_message_sms failed: {exc}")


def send_financing_link_sms(args, caller_number):
    if not caller_number or not caller_number.startswith("+"):
        return
    name, link = FINANCING_LINKS.get(args.get("option"), FINANCING_LINKS["acima"])
    body = f"Twin Wireless financing -- {name}: {link}"
    try:
        twilio_client.messages.create(to=caller_number, from_=TWILIO_FROM_NUMBER, body=body)
    except Exception as exc:
        print(f"send_financing_link_sms failed: {exc}")


def send_review_link_sms(caller_number):
    if not caller_number or not caller_number.startswith("+"):
        return
    body = f"Thanks for calling Twin Wireless! Mind leaving us a quick review? {REVIEW_LINK}"
    try:
        twilio_client.messages.create(to=caller_number, from_=TWILIO_FROM_NUMBER, body=body)
    except Exception as exc:
        print(f"send_review_link_sms failed: {exc}")


def send_callback_request_sms(reason, phone, appointment_id):
    body = (
        "Follow-up callback requested:\n"
        f"Number: {phone}\n"
        f"Appointment: {appointment_id}\n"
        f"Reason: {reason}"
    )
    try:
        twilio_client.messages.create(to=OWNER_PHONE, from_=TWILIO_FROM_NUMBER, body=body)
    except Exception as exc:
        print(f"send_callback_request_sms failed: {exc}")


# ---------------------------------------------------------------------------
# Automated post-repair follow-up agent.
#
# The admin panel and its data (appointments, follow-up tracking, settings)
# live on the twin-wireless.com server, not here -- this app polls that API
# on a schedule, decides what's due, sends the text (reusing the same
# twilio_client already set up above), and writes the result back so state
# survives a restart/redeploy of THIS service. See
# twin-wireless.com/app/admin/follow-ups/FollowUpManager.jsx for the
# matching admin dashboard, and STATUS.md for the full design writeup.
# ---------------------------------------------------------------------------

FOLLOWUP_DEFAULT_SETTINGS = {
    "enabled": True,
    "delayHours": 24,
    "sendingHoursStart": 9,
    "sendingHoursEnd": 20,
    "googleReviewUrl": REVIEW_LINK,
    "websiteUrl": "https://www.twin-wireless.com",
    "maxRetries": 3,
    "serviceRecommendationsEnabled": True,
}

# Checked before anything else in /sms for a customer replying to a follow-up
# -- unconditional and keyword-based on purpose (reliability over nuance) so
# an opt-out is never missed because Claude interpreted the reply oddly.
OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "cancel", "quit", "end", "stopall"}


def _admin_api_request(method, path, params=None, json_body=None, auth=True):
    url = f"{ADMIN_API_BASE}{path}"
    creds = (ADMIN_API_USER, ADMIN_API_PASS) if auth else None
    response = requests.request(method, url, params=params, json=json_body, auth=creds, timeout=15)
    response.raise_for_status()
    return response.json()


def _admin_api_get(path, params=None, auth=True):
    return _admin_api_request("GET", path, params=params, auth=auth)


def _admin_api_post(path, payload, auth=True):
    return _admin_api_request("POST", path, json_body=payload, auth=auth)


def _admin_api_patch(path, payload, auth=True):
    return _admin_api_request("PATCH", path, json_body=payload, auth=auth)


def get_followup_settings():
    # /admin-api/site-settings.php requires auth for every method (unlike
    # the public mirror at /api/site-settings.php), so this must NOT pass
    # auth=False -- a live test caught this returning a silent 401 and
    # falling back to defaults instead of the real configured delay.
    try:
        data = _admin_api_get("/admin-api/site-settings.php")
    except Exception as exc:
        print(f"Follow-up agent: could not read site-settings, using defaults: {exc}")
        return dict(FOLLOWUP_DEFAULT_SETTINGS)
    settings = dict(FOLLOWUP_DEFAULT_SETTINGS)
    settings.update(data.get("followUps") or {})
    return settings


def _service_recommendation(appointment):
    # Pulled live from the real catalog every time -- never a hardcoded
    # list, so this can never recommend a service Twin Wireless doesn't
    # actually currently offer (or has taken down).
    # /api/catalog.php is also Basic-Auth-protected (part of the "Private
    # Shop Phase 2C" restriction), so this needs auth=True too -- same bug
    # shape as get_followup_settings() above, caught by the same live test.
    try:
        catalog = _admin_api_get("/api/catalog.php").get("items", [])
    except Exception as exc:
        print(f"Follow-up agent: could not read catalog for recommendation: {exc}")
        return None

    repaired = {(r.get("repair") or "").strip().lower() for r in appointment.get("repairs", [])}
    candidates = [
        item.get("name") for item in catalog
        if item.get("name") and item.get("name").strip().lower() not in repaired
    ]
    if not candidates:
        return None
    return f"Also, if it's ever useful, we also do {candidates[0]} -- just ask next time you're in."


def _build_followup_message(appointment, settings):
    first_name = (appointment.get("firstName") or "").strip() or "there"
    review_url = settings.get("googleReviewUrl") or REVIEW_LINK
    website_url = settings.get("websiteUrl") or "https://www.twin-wireless.com"

    parts = [
        f"Hi {first_name}! This is Twin Wireless. We wanted to check in and make sure "
        "everything is working great after your recent visit. We really appreciate your "
        "business!",
        "If you had a great experience, we'd really appreciate an honest Google review "
        f"about it: {review_url}",
    ]

    if settings.get("serviceRecommendationsEnabled", True):
        recommendation = _service_recommendation(appointment)
        if recommendation:
            parts.append(recommendation)

    parts.append(
        "And if you ever need phone repair, accessories, upgrades, activation, or anything "
        f"else we offer, everything's here: {website_url}"
    )

    return " ".join(parts)[:1400]


def _send_followup_sms(appointment, settings):
    phone = appointment.get("phone", "")
    if not phone.startswith("+"):
        return False, "appointment has no usable phone number"
    body = _build_followup_message(appointment, settings)
    try:
        twilio_client.messages.create(to=phone, from_=TWILIO_FROM_NUMBER, body=body)
        return True, None
    except Exception as exc:
        return False, str(exc)


def run_followup_cycle():
    if not (ADMIN_API_USER and ADMIN_API_PASS):
        # Not configured yet -- see STATUS.md for how to set
        # ADMIN_API_USER/ADMIN_API_PASS. Never fatal: the phone/SMS service
        # this scheduler lives inside must keep working either way.
        return

    settings = get_followup_settings()
    if not settings.get("enabled", True):
        return

    try:
        appointments = _admin_api_get("/admin-api/appointments.php").get("appointments", [])
    except Exception as exc:
        print(f"Follow-up cycle: could not fetch appointments: {exc}")
        return

    fulfilled = [a for a in appointments if a.get("status") == "Fulfilled" and a.get("fulfilledAt")]

    # Idempotent: POST only creates a tracking record if one doesn't already
    # exist for this appointment id, so re-polling or restarting never
    # double-schedules or double-sends.
    for appointment in fulfilled:
        try:
            _admin_api_post(
                "/admin-api/followups.php",
                {
                    "appointmentId": appointment["id"],
                    "phone": appointment.get("phone", ""),
                    "fulfilledAt": appointment["fulfilledAt"],
                },
            )
        except Exception as exc:
            print(f"Follow-up cycle: could not ensure record for {appointment.get('id')}: {exc}")

    try:
        records = _admin_api_get("/admin-api/followups.php").get("followups", [])
    except Exception as exc:
        print(f"Follow-up cycle: could not fetch followups: {exc}")
        return

    appointment_by_id = {str(a.get("id")): a for a in appointments}
    now = datetime.datetime.now(ZoneInfo("America/Chicago"))
    delay_hours = settings.get("delayHours", 24)
    start_hour = settings.get("sendingHoursStart", 9)
    end_hour = settings.get("sendingHoursEnd", 20)
    max_retries = settings.get("maxRetries", 3)

    for record in records:
        if record.get("optedOut") or record.get("followUpStatus") in ("sent", "failed"):
            continue

        appointment = appointment_by_id.get(str(record.get("appointmentId")))
        if not appointment or appointment.get("status") != "Fulfilled":
            # Re-verification per spec: status changed since it was recorded
            # fulfilled (e.g. reopened) -- don't send.
            continue

        scheduled_at = record.get("followupScheduledAt")
        if not scheduled_at:
            try:
                fulfilled_at = datetime.datetime.fromisoformat(
                    record["fulfilledAt"].replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue
            scheduled_dt = fulfilled_at + datetime.timedelta(hours=delay_hours)
            try:
                _admin_api_patch(
                    "/admin-api/followups.php",
                    {"appointmentId": record["appointmentId"], "followupScheduledAt": scheduled_dt.isoformat()},
                )
            except Exception as exc:
                print(f"Follow-up cycle: could not set schedule for {record.get('appointmentId')}: {exc}")
            continue

        try:
            scheduled_dt = datetime.datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now < scheduled_dt:
            continue  # not due yet

        if not (start_hour <= now.hour < end_hour):
            # Outside sending hours -- stays queued, the next poll inside
            # the window picks it up. Nothing to do this cycle.
            continue

        if record.get("attempts", 0) >= max_retries:
            continue  # already exhausted retries, sitting in Needs Staff Attention

        sent_ok, error = _send_followup_sms(appointment, settings)
        appointment_id = record["appointmentId"]
        if sent_ok:
            try:
                _admin_api_patch(
                    "/admin-api/followups.php",
                    {
                        "appointmentId": appointment_id,
                        "followupSentAt": now.isoformat(),
                        "followUpStatus": "sent",
                        "reviewRequestSent": True,
                    },
                )
            except Exception as exc:
                print(f"Follow-up cycle: sent but could not record it for {appointment_id}: {exc}")
        else:
            attempts = record.get("attempts", 0) + 1
            patch = {"appointmentId": appointment_id, "attempts": attempts, "lastError": str(error)[:500]}
            if attempts >= max_retries:
                patch["followUpStatus"] = "failed"
                patch["staffFollowupRequired"] = True
            try:
                _admin_api_patch("/admin-api/followups.php", patch)
            except Exception as exc:
                print(f"Follow-up cycle: send failed AND could not record failure for {appointment_id}: {exc}")


def _followup_context(phone):
    """The most recent SENT follow-up record for this phone, if any -- used
    by /sms to decide whether an inbound text is a reply to a follow-up
    (opt-out handling, request_callback tool, FOLLOWUP_REPLY_NOTE) or just a
    normal fresh conversation."""
    if not (ADMIN_API_USER and ADMIN_API_PASS) or not phone:
        return None
    try:
        records = _admin_api_get("/admin-api/followups.php", params={"phone": phone}).get("followups", [])
    except Exception as exc:
        print(f"_followup_context: could not fetch followups for {phone}: {exc}")
        return None
    sent = [r for r in records if r.get("followUpStatus") == "sent"]
    return sent[-1] if sent else None


def build_gather(language):
    return Gather(
        input="speech",
        action="/gather",
        method="POST",
        speech_timeout="auto",
        # Was 6. Two real forwarded test calls both ended at EXACTLY
        # greeting-length + 6s, matching the retry-generation timestamp in
        # Twilio's own logs to the second -- the caller sat through 6 full
        # seconds of total silence after the greeting, with no audio at all,
        # and hung up right as the retry ("Sorry, I didn't catch that...")
        # was being fetched, never hearing it. The retry logic itself is
        # correct (confirmed in the TwiML Twilio actually returned); the
        # problem is 6s of dead air reads as a dropped call before it ever
        # gets a chance to speak. Shortened so a quiet moment resolves into
        # audible feedback faster than a caller's patience runs out.
        timeout=4,
        language=LANGUAGES[language]["gather_language"],
    )


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid")
    sessions[call_sid] = {"history": [], "language": DEFAULT_LANGUAGE}

    is_open, next_open = shop_open_status()
    spoken = opening_greeting(is_open, next_open)

    language = DEFAULT_LANGUAGE
    vr = VoiceResponse()
    gather = build_gather(language)
    gather.say(spoken, voice=LANGUAGES[language]["voice"])
    vr.append(gather)
    # A silent Gather (nothing recognized) does not POST to /gather -- Twilio
    # just falls through to the next verb in THIS response. Ending here would
    # give the caller's very first turn zero retries, unlike every later turn
    # (see /gather's own empty-speech branch below), so one bad connection on
    # the opening line was an instant hangup with no second chance. Redirecting
    # to /gather with no SpeechResult reuses that existing retry instead of
    # duplicating it.
    vr.redirect("/gather", method="POST")
    return Response(str(vr), mimetype="text/xml")


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = request.form.get("CallSid")
    speech = request.form.get("SpeechResult", "")
    session = sessions.setdefault(call_sid, {"history": [], "language": DEFAULT_LANGUAGE})
    vr = VoiceResponse()

    if not speech:
        language = session["language"]
        gather = build_gather(language)
        gather.say(LANGUAGES[language]["no_catch"], voice=LANGUAGES[language]["voice"])
        vr.append(gather)
        vr.say(LANGUAGES[language]["no_hearing"], voice=LANGUAGES[language]["voice"])
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    session["language"] = detect_language(speech, session["language"])

    is_open, next_open = shop_open_status()
    spoken, tool_call = call_claude(call_sid, speech, is_open, next_open)
    language = session["language"]

    if spoken:
        vr.say(spoken, voice=LANGUAGES[language]["voice"])

    if tool_call and tool_call.name == "take_message":
        if not spoken:
            vr.say(LANGUAGES[language]["message_taken_fallback"], voice=LANGUAGES[language]["voice"])
        send_message_sms(tool_call.input)
        vr.hangup()
        sessions.pop(call_sid, None)
        return Response(str(vr), mimetype="text/xml")

    if tool_call and tool_call.name == "end_call":
        vr.hangup()
        sessions.pop(call_sid, None)
        return Response(str(vr), mimetype="text/xml")

    if tool_call and tool_call.name == "send_link":
        send_financing_link_sms(tool_call.input, request.form.get("From"))

    if tool_call and tool_call.name == "send_review_link":
        send_review_link_sms(request.form.get("From"))

    gather = build_gather(language)
    vr.append(gather)
    vr.say(LANGUAGES[language]["goodbye"], voice=LANGUAGES[language]["voice"])
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/sms", methods=["POST"])
def sms():
    # Texts to the shop number, answered by the same Mia that answers calls:
    # same SYSTEM_PROMPT, same brand facts, same hours logic, same tools.
    #
    # Before this existed the number's messaging webhook still pointed at
    # Twilio's demo endpoint, so a customer texting the shop got a canned
    # Twilio autoreply.
    #
    # Session key is the CALLER'S NUMBER, not a CallSid. A call is one
    # continuous session that ends on hangup; a text conversation has no
    # hangup, so history is keyed per person and reused across messages. That
    # is what lets someone ask a follow-up ("what about the 13?") and be
    # understood.
    from_number = request.form.get("From", "")
    body = (request.form.get("Body") or "").strip()

    resp = MessagingResponse()
    if not body:
        return Response(str(resp), mimetype="text/xml")

    # A reply to an automated post-repair follow-up text is handled
    # differently from a fresh customer message -- see FOLLOWUP_REPLY_NOTE
    # and run_followup_cycle() above. followup_record is None for any
    # ordinary call/text, so none of this changes existing behavior.
    followup_record = _followup_context(from_number)
    if followup_record:
        lowered = re.sub(r"[^a-z ]", "", body.lower()).strip()
        if lowered in OPT_OUT_KEYWORDS:
            try:
                _admin_api_patch(
                    "/admin-api/followups.php",
                    {
                        "appointmentId": followup_record["appointmentId"],
                        "optedOut": True,
                        "customerResponse": body[:500],
                    },
                )
            except Exception as exc:
                print(f"opt-out record failed: {exc}")
            # Unconditional and independent of Claude on purpose -- an
            # opt-out must never be missed because a reply was ambiguous.
            resp.message("You're unsubscribed from Twin Wireless follow-up texts. Text us any time if you need us again.")
            return Response(str(resp), mimetype="text/xml")

    session_key = f"sms:{from_number}"
    session = sessions.setdefault(session_key, {"history": [], "language": DEFAULT_LANGUAGE})
    session["language"] = detect_language(body, session["language"])

    is_open, next_open = shop_open_status()
    try:
        spoken, tool_call = call_claude(
            session_key, body, is_open, next_open, channel="sms", followup_reply=bool(followup_record)
        )
    except Exception as exc:  # noqa: BLE001
        # Never leave a texter with silence. Falling back to the shop's real
        # contact details is always safe and never wrong.
        print(f"SMS: Claude call failed: {exc}")
        resp.message(
            "Sorry, I'm having trouble right now. Please call (318) 670-3938 "
            "or come by 2328 Line Ave, Shreveport."
        )
        return Response(str(resp), mimetype="text/xml")

    if followup_record:
        try:
            _admin_api_patch(
                "/admin-api/followups.php",
                {"appointmentId": followup_record["appointmentId"], "customerResponse": body[:500]},
            )
        except Exception as exc:
            print(f"record customerResponse failed: {exc}")

    # take_message on SMS means the texter wants a human. The message still
    # goes to the owner, but there is no call to hang up -- the thread simply
    # continues, so the session is kept rather than popped.
    if tool_call and tool_call.name == "take_message":
        send_message_sms(tool_call.input)
        if not spoken:
            spoken = LANGUAGES[session["language"]]["message_taken_fallback"]

    if tool_call and tool_call.name == "send_link":
        send_financing_link_sms(tool_call.input, from_number)

    if tool_call and tool_call.name == "send_review_link":
        send_review_link_sms(from_number)

    if tool_call and tool_call.name == "request_callback" and followup_record:
        reason = tool_call.input.get("reason", "")
        send_callback_request_sms(reason, from_number, followup_record["appointmentId"])
        try:
            _admin_api_patch(
                "/admin-api/followups.php",
                {
                    "appointmentId": followup_record["appointmentId"],
                    "callbackRequested": True,
                    "staffFollowupRequired": True,
                },
            )
        except Exception as exc:
            print(f"callback record failed: {exc}")
        if not spoken:
            spoken = "Got it -- we'll have someone from the team reach out to you soon."

    # end_call has no meaning in a text thread; the reply is just sent.
    if spoken:
        # SMS segments bill per 160 chars, and Claude is capped at 300 tokens
        # anyway, but trim defensively so one long reply cannot fan out into
        # many billable segments.
        resp.message(spoken[:1200])

    return Response(str(resp), mimetype="text/xml")


@app.route("/", methods=["GET"])
def health():
    return "Twin Wireless phone agent is running."


@app.route("/followups/status", methods=["GET"])
def followups_status():
    # Cheap health check for the follow-up agent specifically, separate from
    # "/" -- used by the Routines check-in so a broken poller (bad admin-api
    # credentials, site-settings unreachable, etc.) shows up on its own
    # instead of hiding behind an otherwise-healthy phone service.
    if not (ADMIN_API_USER and ADMIN_API_PASS):
        return {"configured": False}
    try:
        records = _admin_api_get("/admin-api/followups.php").get("followups", [])
    except Exception as exc:
        return {"configured": True, "error": str(exc)}, 502
    return {
        "configured": True,
        "total": len(records),
        "pending": sum(1 for r in records if r.get("followUpStatus") == "pending" and not r.get("optedOut")),
        "sent": sum(1 for r in records if r.get("followUpStatus") == "sent"),
        "needsStaffAttention": sum(1 for r in records if r.get("staffFollowupRequired")),
        "optedOut": sum(1 for r in records if r.get("optedOut")),
    }


# Single gunicorn worker (see README's Start command: `gunicorn app:app`, no
# --workers flag), so this runs exactly once per deploy, not once per
# worker. A failure here must never take the whole app down with it --
# that's already the phone/SMS service customers are calling right now.
try:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_followup_cycle, "interval", seconds=FOLLOWUP_POLL_SECONDS, next_run_time=datetime.datetime.now())
    scheduler.start()
except Exception as exc:
    print(f"Follow-up agent: scheduler failed to start: {exc}")
