import os
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
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

SYSTEM_PROMPT = """You are the phone assistant for Twin Wireless, a device repair shop
at 2328 Line Ave, Shreveport, LA 71104, phone (318) 670-3938, website twin-wireless.com.
Hours: Monday-Saturday 9AM-8PM, Sunday 11AM-5PM.

You are answering a live phone call. Your replies are spoken aloud by text-to-speech, so
keep them short, natural, and conversational -- a sentence or two per turn, never a long
paragraph. This is the start of the call if the user message is exactly "[CALL STARTED]" --
in that case, greet the caller and ask what they need, using the correct greeting for
whether the shop is currently open or closed (you will be told the current status below).

TONE: Talking like the tech at the counter, not a call center. Casual, quick, confident --
contractions, plain language ("your screen," "the charging port"), no corporate script-speak
("I understand your concern," "at this time," "representative," "please hold"). Never use a
personal name when talking about who handles things -- say "our team," "one of our techs,"
or "someone from the shop," never an individual's name.

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
  takes about 15 minutes; online setup about 30 minutes. Works with eSIM or physical SIM
  depending on the device.
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

WHEN TO TAKE A MESSAGE (use the take_message tool): the caller doesn't want to come in for a
free diagnosis and needs a callback instead, a repair status check, the caller wants to speak
to a person, the caller seems upset, or anything else you can't confidently resolve yourself.
Get their name and callback number first by asking in conversation, then call the tool once
you have both.

WHEN TO END THE CALL (use the end_call tool): once the caller's question is fully answered
and there's nothing else they need, or right after taking a message. Say a brief goodbye in
your spoken reply first, then call the tool.
"""

sessions = {}


def call_claude(call_sid, user_text, is_open, next_open_text):
    history = sessions.setdefault(call_sid, [])
    status_note = (
        f"[Current status: shop is OPEN right now.]"
        if is_open
        else f"[Current status: shop is CLOSED right now. Next open: {next_open_text}.]"
    )
    history.append({"role": "user", "content": f"{status_note}\n{user_text}"})

    response = claude.messages.create(
        model=MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
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
    # Mon-Sat 9AM-8PM, Sun 11AM-5PM, America/Chicago. Render's default TZ is UTC,
    # so this converts using a fixed offset -- good enough for CST/CDT most of the
    # year; revisit if DST edge cases matter.
    import datetime

    now_utc = datetime.datetime.utcnow()
    now_central = now_utc - datetime.timedelta(hours=5)  # approx CDT offset
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


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid")
    sessions[call_sid] = []

    is_open, next_open = shop_open_status()
    spoken, _ = call_claude(call_sid, "[CALL STARTED]", is_open, next_open)

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action="/gather",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    gather.say(spoken, voice="Polly.Matthew")
    vr.append(gather)
    vr.say("Sorry, I didn't catch that. Please call back and try again.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = request.form.get("CallSid")
    speech = request.form.get("SpeechResult", "")
    vr = VoiceResponse()

    if not speech:
        gather = Gather(
            input="speech",
            action="/gather",
            method="POST",
            speech_timeout="auto",
            timeout=6,
        )
        gather.say("Sorry, I didn't catch that -- could you say that again?", voice="Polly.Matthew")
        vr.append(gather)
        vr.say("Sorry, I'm having trouble hearing you. Please call back. Goodbye.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    is_open, next_open = shop_open_status()
    spoken, tool_call = call_claude(call_sid, speech, is_open, next_open)

    if spoken:
        vr.say(spoken, voice="Polly.Matthew")

    if tool_call and tool_call.name == "take_message":
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

    gather = Gather(
        input="speech",
        action="/gather",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    vr.append(gather)
    vr.say("Thanks for calling Twin Wireless. Goodbye.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


@app.route("/", methods=["GET"])
def health():
    return "Twin Wireless phone agent is running."
