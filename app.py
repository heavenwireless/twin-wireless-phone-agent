import os
import re
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


def call_claude(call_sid, user_text, is_open, next_open_text, channel="voice"):
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

    response = claude.messages.create(
        model=MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT + (SMS_CHANNEL_NOTE if channel == "sms" else ""),
        tools=[
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
        ],
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


def build_gather(language):
    return Gather(
        input="speech",
        action="/gather",
        method="POST",
        speech_timeout="auto",
        timeout=6,
        language=LANGUAGES[language]["gather_language"],
    )


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid")
    sessions[call_sid] = {"history": [], "language": DEFAULT_LANGUAGE}

    is_open, next_open = shop_open_status()
    spoken, _ = call_claude(call_sid, "[CALL STARTED]", is_open, next_open)

    language = sessions[call_sid]["language"]
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

    session_key = f"sms:{from_number}"
    session = sessions.setdefault(session_key, {"history": [], "language": DEFAULT_LANGUAGE})
    session["language"] = detect_language(body, session["language"])

    is_open, next_open = shop_open_status()
    try:
        spoken, tool_call = call_claude(session_key, body, is_open, next_open, channel="sms")
    except Exception as exc:  # noqa: BLE001
        # Never leave a texter with silence. Falling back to the shop's real
        # contact details is always safe and never wrong.
        print(f"SMS: Claude call failed: {exc}")
        resp.message(
            "Sorry, I'm having trouble right now. Please call (318) 670-3938 "
            "or come by 2328 Line Ave, Shreveport."
        )
        return Response(str(resp), mimetype="text/xml")

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
