from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

app = FastAPI(title="Teleron AI Receptionist Webhook")

# ── CONFIG ────────────────────────────────────────────────────────────────────
SUPABASE_URL   = "https://fjtngjxvarpboretvrzl.supabase.co"
SUPABASE_KEY   = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZqdG5nanh2YXJwYm9yZXR2cnpsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzkzMTExOTIsImV4cCI6MjA5NDg4NzE5Mn0.UuWxjqPX1YRmhPS6qzSUpX9iaJ0_URC8nk8Yvbps374"
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL     = "llama-3.3-70b-versatile"
GROQ_URL       = "https://api.groq.com/openai/v1/chat/completions"
GMAIL_SENDER   = os.environ.get("GMAIL_SENDER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "")
# ──────────────────────────────────────────────────────────────────────────────


def call_groq(messages: list, system: str = None, max_tokens: int = 800) -> str:
    """Synchronous Groq call using httpx."""
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY environment variable is not set")

    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    for m in messages:
        role    = m.get("role", "user")
        content = m.get("content", "")
        if role in ("user", "assistant") and str(content).strip():
            payload_messages.append({"role": role, "content": str(content)})

    payload = {
        "model":       GROQ_MODEL,
        "max_tokens":  max_tokens,
        "temperature": 0.2,
        "messages":    payload_messages
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json"
    }

    with httpx.Client(timeout=60) as client:
        resp = client.post(GROQ_URL, headers=headers, json=payload)
        if not resp.is_success:
            raise ValueError(f"Groq API error {resp.status_code}: {resp.text}")
        return resp.json()["choices"][0]["message"]["content"].strip()


def analyse_transcript(transcript: str, caller_phone: str) -> dict:
    """Analyse transcript with Groq and return structured summary."""
    system = (
        "You are an expert HVAC and home services BPO dispatcher AI. "
        "Analyse the call transcript and return ONLY valid JSON, no markdown, no extra text."
    )
    prompt = f"""Analyse this customer service call transcript.
Caller phone: {caller_phone}

Transcript:
\"\"\"{transcript}\"\"\"

Return ONLY a valid JSON object with these exact fields:
{{
  "customer_name": "extracted full name or Unknown",
  "phone": "extracted phone or {caller_phone}",
  "address": "extracted full address or Unknown",
  "problem": "one clear sentence describing the main issue",
  "urgency": "one of: Low / Medium / High / Emergency",
  "service_type": "one of: HVAC / Plumbing / Electrical / Appliance Repair / General Home Service / Unknown",
  "tech_skill": "specific skill the technician needs",
  "sentiment": "one of: Calm / Frustrated / Urgent / Angry / Satisfied / Confused",
  "language": "language the customer spoke e.g. English, Urdu, Spanish",
  "follow_up": ["action item 1", "action item 2", "action item 3"],
  "notes": "any other important details"
}}"""

    raw = call_groq([{"role": "user", "content": prompt}], system=system)

    clean = raw.strip()
    if "```" in clean:
        for part in clean.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                clean = part
                break
    start = clean.find("{")
    end   = clean.rfind("}") + 1
    if start != -1 and end > start:
        clean = clean[start:end]

    return json.loads(clean)


def save_to_supabase(summary: dict, transcript: str) -> int | None:
    """Save job to Supabase synchronously."""
    job_data = {
        "customer_name":  summary.get("customer_name", "Unknown"),
        "phone":          summary.get("phone", "Unknown"),
        "transcript":     transcript,
        "status":         "Pending Assignment",
        "scheduled_date": str(datetime.now().date()),
        "assigned_tech":  "Unassigned",
        "timestamp":      datetime.now().isoformat(),
        "keywords":       summary.get("address", ""),
        "ai_summary":     json.dumps(summary)
    }
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation"
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{SUPABASE_URL}/rest/v1/jobs",
            headers=headers,
            json=job_data
        )
        if not resp.is_success:
            raise ValueError(f"Supabase error {resp.status_code}: {resp.text}")
        data = resp.json()
        return data[0]["id"] if data else None


def send_email(summary: dict, job_id: int, transcript: str):
    """Send HTML email summary."""
    if not all([GMAIL_SENDER, GMAIL_PASSWORD, NOTIFY_EMAIL]):
        return

    urgency_colors = {
        "Emergency": "#f87171",
        "High":      "#fb923c",
        "Medium":    "#fbbf24",
        "Low":       "#4ade80"
    }
    urgency       = summary.get("urgency", "Unknown")
    urgency_color = urgency_colors.get(urgency, "#94a3b8")
    follow_ups    = "".join(
        f"<li style='margin-bottom:6px;'>{item}</li>"
        for item in summary.get("follow_up", [])
    )

    html = f"""
    <div style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;background:#f8fafc;border-radius:12px;overflow:hidden;">
      <div style="background:#075E54;padding:24px;text-align:center;">
        <h1 style="color:#fff;margin:0;font-size:20px;">Teleron New AI Call Job #{job_id}</h1>
        <p style="color:#dcf8c6;margin:6px 0 0;">{datetime.now().strftime('%B %d, %Y at %H:%M')}</p>
      </div>
      <div style="padding:24px;">
        <div style="background:#fff;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;color:#64748b;text-transform:uppercase;margin:0 0 12px;">Customer Info</h2>
          <p style="margin:4px 0;"><b>Name:</b> {summary.get('customer_name','Unknown')}</p>
          <p style="margin:4px 0;"><b>Phone:</b> {summary.get('phone','Unknown')}</p>
          <p style="margin:4px 0;"><b>Address:</b> {summary.get('address','Unknown')}</p>
          <p style="margin:4px 0;"><b>Language:</b> {summary.get('language','English')}</p>
        </div>
        <div style="background:#fff;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;color:#64748b;text-transform:uppercase;margin:0 0 8px;">Problem</h2>
          <p style="margin:0;font-size:15px;">{summary.get('problem','Unknown')}</p>
        </div>
        <div style="background:#fff;border-radius:10px;padding:14px;margin-bottom:12px;border:1px solid #e2e8f0;">
          <b>Urgency:</b> <span style="background:{urgency_color};padding:2px 10px;border-radius:999px;font-size:12px;">{urgency}</span>
          &nbsp;&nbsp;<b>Service:</b> {summary.get('service_type','Unknown')}
          &nbsp;&nbsp;<b>Sentiment:</b> {summary.get('sentiment','Unknown')}
        </div>
        <div style="background:#fff;border-radius:10px;padding:18px;margin-bottom:16px;border:1px solid #e2e8f0;">
          <h2 style="font-size:13px;color:#64748b;text-transform:uppercase;margin:0 0 10px;">Follow-up Actions</h2>
          <ul style="margin:0;padding-left:18px;">{follow_ups}</ul>
        </div>
        <div style="text-align:center;padding:14px;background:#075E54;border-radius:10px;">
          <p style="color:#fff;margin:0;font-size:13px;">Job added to your Teleron Dispatch Board</p>
        </div>
      </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[{urgency}] New Call - {summary.get('customer_name','Unknown')} - Job #{job_id}"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_SENDER, NOTIFY_EMAIL, msg.as_string())


@app.get("/")
def root():
    return {"status": "Teleron AI Receptionist Webhook is running"}


@app.get("/health")
def health():
    return {
        "status":       "ok",
        "groq_key_set": bool(GROQ_API_KEY),
        "gmail_set":    bool(GMAIL_SENDER),
        "notify_email": bool(NOTIFY_EMAIL),
        "timestamp":    datetime.now().isoformat()
    }


@app.get("/test-webhook")
def test_webhook():
    if not GROQ_API_KEY:
        return JSONResponse({"status": "error", "message": "GROQ_API_KEY is not set in Railway variables"})

    fake_transcript = """
AI: Thank you for calling Teleron Home Services! How can I help you today?
Customer: Hi yes my AC is not working at all. It is very hot inside and I have two kids at home.
AI: I am so sorry to hear that. Can I get your name please?
Customer: My name is Sarah Johnson.
AI: Thank you Sarah. And what is your service address?
Customer: 4521 Oak Street, Houston, Texas 77001.
AI: Got it. How long has the AC been down?
Customer: Since yesterday evening. It just stopped blowing cold air.
AI: Is there anything else I should know?
Customer: No just please send someone as soon as possible.
AI: Absolutely Sarah. A dispatcher will call you within 15 minutes. Goodbye!
    """.strip()

    try:
        summary = analyse_transcript(fake_transcript, "+1-555-000-1234")
    except Exception as e:
        return JSONResponse({"status": "groq_error", "message": str(e)})

    try:
        job_id = save_to_supabase(summary, fake_transcript)
    except Exception as e:
        return JSONResponse({"status": "supabase_error", "message": str(e), "summary": summary})

    try:
        send_email(summary, job_id or 0, fake_transcript)
    except Exception:
        pass

    return JSONResponse({"status": "success", "job_id": job_id, "summary": summary})


@app.post("/vapi-webhook")
async def vapi_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "Invalid JSON"})

    message      = body.get("message", body)
    msg_type     = message.get("type", "")

    if msg_type != "end-of-call-report":
        return JSONResponse({"status": "ignored", "type": msg_type})

    transcript   = message.get("transcript", "")
    call         = message.get("call", {})
    caller_phone = call.get("customer", {}).get("number", "Unknown")

    if not transcript.strip():
        return JSONResponse({"status": "skipped", "reason": "empty transcript"})

    try:
        summary = analyse_transcript(transcript, caller_phone)
    except Exception as e:
        summary = {
            "customer_name": "Unknown", "phone": caller_phone,
            "address": "Unknown", "problem": f"Analysis failed: {e}",
            "urgency": "Medium", "service_type": "Unknown",
            "tech_skill": "General", "sentiment": "Unknown",
            "language": "Unknown", "follow_up": ["Manual review required"],
            "notes": ""
        }

    try:
        job_id = save_to_supabase(summary, transcript)
    except Exception:
        job_id = None

    try:
        send_email(summary, job_id or 0, transcript)
    except Exception:
        pass

    return JSONResponse({
        "status":   "success",
        "job_id":   job_id,
        "customer": summary.get("customer_name"),
        "urgency":  summary.get("urgency"),
        "language": summary.get("language")
    })