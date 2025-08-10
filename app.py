# pip install slack-bolt google-api-python-client google-auth google-auth-oauthlib
import os, sqlite3
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build
from dotenv import load_dotenv
from datetime import datetime, timedelta
load_dotenv()

# --- Env vars (safe load + validation) ---
PTO_CHANNEL = os.getenv("PTO_CHANNEL_ID")   # e.g., C0123456789
CAL_ID      = os.getenv("PTO_CAL_ID")       # shared PTO calendar id
SLACK_BOT   = os.getenv("SLACK_BOT_TOKEN")
SIGN_SECRET = os.getenv("SLACK_SIGNING_SECRET")
APP_LEVEL   = os.getenv("SLACK_APP_LEVEL_TOKEN")

missing = [k for k, v in {
    "PTO_CHANNEL_ID": PTO_CHANNEL,
    "PTO_CAL_ID": CAL_ID,
    "SLACK_BOT_TOKEN": SLACK_BOT,
    "SLACK_SIGNING_SECRET": SIGN_SECRET,
    "SLACK_APP_LEVEL_TOKEN": APP_LEVEL
}.items() if not v]
if missing:
    raise SystemExit(f"❌ Missing env var(s): {', '.join(missing)}. Check your .env file.")

# Google
creds = service_account.Credentials.from_service_account_file(
    "google-creds.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
cal = build("calendar", "v3", credentials=creds)

# DB
DB_PATH = "pto.db"
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS pto_links(
          slack_ts TEXT PRIMARY KEY,
          slack_channel TEXT,
          slack_user TEXT,
          target_user TEXT,
          event_id TEXT,
          calendar_id TEXT,
          start_iso TEXT,
          end_iso TEXT,
          note TEXT
        )""")
        # Channel → Calendar mapping
        conn.execute("""CREATE TABLE IF NOT EXISTS channel_settings(
          channel_id TEXT PRIMARY KEY,
          calendar_id TEXT NOT NULL
        )""")
        conn.commit()
        return conn
    except sqlite3.DatabaseError as e:
        # If the file exists but isn't a valid DB, back it up and recreate
        print(f"⚠️ SQLite error: {e}. Backing up and recreating {DB_PATH}…")
        try:
            if os.path.exists(DB_PATH):
                os.rename(DB_PATH, DB_PATH + ".corrupt")
        except Exception:
            pass
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.execute("""CREATE TABLE IF NOT EXISTS pto_links(
          slack_ts TEXT PRIMARY KEY,
          slack_channel TEXT,
          slack_user TEXT,
          target_user TEXT,
          event_id TEXT,
          calendar_id TEXT,
          start_iso TEXT,
          end_iso TEXT,
          note TEXT
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS channel_settings(
          channel_id TEXT PRIMARY KEY,
          calendar_id TEXT NOT NULL
        )""")
        conn.commit()
        return conn

db = init_db()

def get_calendar_for_channel(channel_id: str) -> str:
    row = db.execute("SELECT calendar_id FROM channel_settings WHERE channel_id=?", (channel_id,)).fetchone()
    return row[0] if row else CAL_ID  # fallback to env if not configured

def set_calendar_for_channel(channel_id: str, calendar_id: str):
    db.execute("INSERT OR REPLACE INTO channel_settings(channel_id, calendar_id) VALUES(?,?)", (channel_id, calendar_id))
    db.commit()

print("***** DB ready and env loaded. Starting Slack app…")

app = App(
    token=SLACK_BOT,
    signing_secret=SIGN_SECRET,
)

# 1) Global shortcut to open modal (configure in Slack: "log-pto")
@app.shortcut("log_pto")
def open_modal(ack, body, client):
    ack()
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "pto_submit",
            "title": {"type": "plain_text", "text": "Log PTO"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                # Who
                {
                    "type": "input",
                    "block_id": "user_b",
                    "label": {"type": "plain_text", "text": "Employee"},
                    "element": {"type": "users_select", "action_id": "user_a", "initial_user": body["user"]["id"]}
                },
                {
                    "type": "input",
                    "block_id": "channel_b",
                    "label": {"type": "plain_text", "text": "Post PTO to channel"},
                    "element": {
                        "type": "conversations_select",
                        "action_id": "channel_a",
                        "default_to_current_conversation": True,
                        "filter": {"include": ["public", "private"], "exclude_bot_users": True}
                    }
                },
                # Date
                {
                    "type": "input",
                    "block_id": "date_b",
                    "label": {"type": "plain_text", "text": "Date"},
                    "element": {"type": "datepicker", "action_id": "date_a"}
                },
                # End Date
                {
                    "type": "input",
                    "block_id": "date_end_b",
                    "label": {"type": "plain_text", "text": "End Date"},
                    "element": {"type": "datepicker", "action_id": "date_end_a"}
                },
                # Start
                {
                    "type": "input",
                    "block_id": "start_b",
                    "label": {"type": "plain_text", "text": "Start (HH:MM, 24h)"},
                    "optional": True,
                    "element": {"type": "plain_text_input", "action_id": "start_a", "placeholder": {"type": "plain_text", "text": "09:00"}}
                },
                # End
                {
                    "type": "input",
                    "block_id": "end_b",
                    "label": {"type": "plain_text", "text": "End (HH:MM, 24h)"},
                    "optional": True,
                    "element": {"type": "plain_text_input", "action_id": "end_a", "placeholder": {"type": "plain_text", "text": "17:00"}}
                },
                # Note
                {
                    "type": "input",
                    "optional": True,
                    "block_id": "note_b",
                    "label": {"type": "plain_text", "text": "Reason / Note"},
                    "element": {"type": "plain_text_input", "action_id": "note_a", "multiline": True}
                }
            ]
        }
    )

@app.command("/pto")
def cmd_pto(ack, body, client):
    ack()
    channel_id = body.get("channel_id")
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "pto_submit",
            "title": {"type": "plain_text", "text": "Log PTO"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "private_metadata": channel_id,
            "blocks": [
                {
                    "type": "input",
                    "block_id": "user_b",
                    "label": {"type": "plain_text", "text": "Employee"},
                    "element": {"type": "users_select", "action_id": "user_a", "initial_user": body["user_id"]}
                },
                {
                    "type": "input",
                    "block_id": "date_b",
                    "label": {"type": "plain_text", "text": "Date"},
                    "element": {"type": "datepicker", "action_id": "date_a"}
                },
                {
                    "type": "input",
                    "block_id": "date_end_b",
                    "label": {"type": "plain_text", "text": "End Date"},
                    "element": {"type": "datepicker", "action_id": "date_end_a"}
                },
                {
                    "type": "input",
                    "block_id": "start_b",
                    "label": {"type": "plain_text", "text": "Start (HH:MM, 24h)"},
                    "optional": True,
                    "element": {"type": "plain_text_input", "action_id": "start_a", "placeholder": {"type": "plain_text", "text": "09:00"}}
                },
                {
                    "type": "input",
                    "block_id": "end_b",
                    "label": {"type": "plain_text", "text": "End (HH:MM, 24h)"},
                    "optional": True,
                    "element": {"type": "plain_text_input", "action_id": "end_a", "placeholder": {"type": "plain_text", "text": "17:00"}}
                },
                {
                    "type": "input",
                    "optional": True,
                    "block_id": "note_b",
                    "label": {"type": "plain_text", "text": "Reason / Note"},
                    "element": {"type": "plain_text_input", "action_id": "note_a", "multiline": True}
                }
            ]
        }
    )

# /pto-setup <calendar_id>  — admin command to bind this channel to a Google Calendar
@app.command("/pto-setup")
def pto_setup(ack, respond, body, client, logger):
    ack()
    channel_id = body.get("channel_id")
    text = (body.get("text") or "").strip()
    if not text:
        return respond("Usage: `/pto-setup <calendar_id>` (e.g. abc123@group.calendar.google.com)")
    cal_id = text.split()[0]

    # Optional: validate we can write to this calendar by inserting a tiny event then deleting it
    try:
        test = cal.events().insert(calendarId=cal_id, body={
            "summary": "PTO Bot validation (auto-delete)",
            "start": {"dateTime": datetime.utcnow().isoformat()+"Z"},
            "end": {"dateTime": (datetime.utcnow()+timedelta(minutes=5)).isoformat()+"Z"},
        }).execute()
        cal.events().delete(calendarId=cal_id, eventId=test["id"]).execute()
    except Exception as e:
        logger.error(f"Calendar validation failed for {cal_id}: {e}")
        return respond(f"❌ I can't write to `{cal_id}`. Make sure this calendar is shared with the service account and try again.")

    set_calendar_for_channel(channel_id, cal_id)
    respond(f"✅ This channel is now mapped to calendar:\n`{cal_id}`")

@app.command("/pto-where")
def pto_where(ack, respond, body):
    ack()
    channel_id = body.get("channel_id")
    current = get_calendar_for_channel(channel_id)
    respond(f"📌 This channel writes PTO to:\n`{current}`")

# 2) Handle modal submit → create calendar event → post summary in #pto
@app.view("pto_submit")
def handle_submit(ack, body, client, logger):
    values = body["view"]["state"]["values"]

    # Figure out which channel to post in:
    modal_channel = values.get("channel_b", {}).get("channel_a", {}).get("selected_conversation")
    pm = body.get("view", {}).get("private_metadata")
    post_channel = modal_channel or pm or PTO_CHANNEL

    target_user = values["user_b"]["user_a"]["selected_user"]
    date = values["date_b"]["date_a"]["selected_date"]
    date_end = values.get("date_end_b", {}).get("date_end_a", {}).get("selected_date", date)
    start = (values.get("start_b", {}).get("start_a", {}).get("value") or "").strip()
    end = (values.get("end_b", {}).get("end_a", {}).get("value") or "").strip()
    note = values.get("note_b", {}).get("note_a", {}).get("value", "") or ""

    # baby validation
    errors = {}
    def _valid_time(t):
        try:
            hh, mm = t.split(":")
            return 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except Exception:
            return False

    # times optional
    if start and not _valid_time(start):
        errors["start_b"] = "Use HH:MM (24h), like 09:00"
    if end and not _valid_time(end):
        errors["end_b"] = "Use HH:MM (24h), like 17:00"

    # date range check
    if date_end < date:
        errors["date_end_b"] = "End Date must be on or after Start Date."

    if start and not end:
        errors["end_b"] = "Provide an end time or leave both times empty for all-day."
    if end and not start:
        errors["start_b"] = "Provide a start time or leave both times empty for all-day."

    if errors:
        return ack(response_action="errors", errors=errors)
    ack()  # modal closes

    tz = "America/Los_Angeles"
    # Build event body depending on whether times were provided
    if start and end:
        # timed multi-day supported
        start_iso = f"{date}T{start}:00"
        end_iso = f"{date_end}T{end}:00"
        try:
            if datetime.fromisoformat(start_iso) >= datetime.fromisoformat(end_iso):
                return ack(response_action="errors", errors={"end_b": "End must be after start."})
        except Exception:
            return ack(response_action="errors", errors={"end_b": "Invalid date/time."})
        event_body = {
            "summary": "",  # fill later with display name
            "description": f"Requested via Slack by <@{body['user']['id']}> for <@{target_user}>.\n{note}",
            "start": {"dateTime": start_iso, "timeZone": tz},
            "end": {"dateTime": end_iso, "timeZone": tz},
        }
    else:
        # all-day event (end date exclusive)
        try:
            d0 = datetime.fromisoformat(date).date()
            d1 = datetime.fromisoformat(date_end).date()
        except Exception:
            return ack(response_action="errors", errors={"date_end_b": "Invalid date(s)."})
        end_exclusive = (d1 + timedelta(days=1)).isoformat()
        event_body = {
            "summary": "",  # fill later with display name
            "description": f"Requested via Slack by <@{body['user']['id']}> for <@{target_user}>.\n{note}",
            "start": {"date": d0.isoformat()},
            "end": {"date": end_exclusive},
        }

    # Resolve display name for nicer titles
    try:
        ui = client.users_info(user=target_user)
        # SlackResponse behaves like a dict; support both
        user_obj = ui.get("user") if hasattr(ui, "get") else ui["user"]
        profile = user_obj.get("profile", {})
        display = profile.get("display_name") or profile.get("real_name") or f"<@{target_user}>"
    except Exception:
        display = f"<@{target_user}>"
    event_body["summary"] = f"PTO - {display}"

    cal_id_target = get_calendar_for_channel(post_channel)

    try:
        ev = cal.events().insert(calendarId=cal_id_target, body=event_body).execute()
    except Exception as e:
        logger.error(e)
        client.chat_postMessage(channel=body["user"]["id"], text=f"⚠️ Couldn’t create calendar event: {e}")
        return

    msg = client.chat_postMessage(
        channel=post_channel,
        text=f"PTO booked for {display} ({'<@'+target_user+'>'}) — {date}" +
             (f" → {date_end}" if date_end != date else "") +
             (f" {start}-{end}" if start and end else " (all-day)") +
             (f" — {note}" if note else ""),
        blocks=[
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"*PTO booked* for {display} (<@{target_user}>) — {date}" +
                              (f" → {date_end}" if date_end != date else "") +
                              (f" {start}-{end}" if start and end else " (all-day)") +
                              (f" — {note}" if note else "")}},
            {"type": "actions",
             "elements": [
                 {"type": "button", "action_id": "pto_delete", "style": "danger",
                  "text": {"type": "plain_text", "text": "Delete PTO"},
                  "value": "delete"}
             ]}
        ]
    )

    # Store mapping
    db.execute("INSERT OR REPLACE INTO pto_links VALUES (?,?,?,?,?,?,?,?,?)", (
        msg["ts"], post_channel, body["user"]["id"], target_user,
        ev["id"], cal_id_target,
        event_body["start"].get("dateTime") or event_body["start"].get("date"),
        event_body["end"].get("dateTime") or event_body["end"].get("date"),
        note
    ))
    db.commit()

# 3) If that summary message gets deleted → remove calendar event
@app.event("message")
def on_message_events(event, logger):
    if event.get("subtype") == "message_deleted":
        logger.info(f"🧽 message_deleted event: channel={event.get('channel')} ts={event.get('previous_message', {}).get('ts')}")
        ts = event["previous_message"]["ts"]
        row = db.execute("SELECT event_id, calendar_id FROM pto_links WHERE slack_ts=?", (ts,)).fetchone()
        if row:
            event_id, cal_id = row
            try:
                cal.events().delete(calendarId=cal_id, eventId=event_id).execute()
            except Exception as e:
                logger.error(e)
            db.execute("DELETE FROM pto_links WHERE slack_ts=?", (ts,))
            db.commit()

# Delete button handler for PTO messages
@app.action("pto_delete")
def handle_pto_delete(ack, body, client, logger):
    ack()
    # Determine the channel and ts of the message with the button
    channel_id = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    ts = body.get("message", {}).get("ts")
    if not channel_id or not ts:
        logger.error(f"Delete action missing channel/ts: {body}")
        return
    row = db.execute("SELECT event_id, calendar_id FROM pto_links WHERE slack_ts=?", (ts,)).fetchone()
    if row:
        event_id, cal_id = row
        try:
            cal.events().delete(calendarId=cal_id, eventId=event_id).execute()
        except Exception as e:
            logger.error(e)
        db.execute("DELETE FROM pto_links WHERE slack_ts=?", (ts,))
        db.commit()
    # Remove the Slack message
    try:
        client.chat_delete(channel=channel_id, ts=ts)
    except Exception as e:
        logger.error(e)

if __name__ == "__main__":
    # For local dev, you can run via Socket Mode (no ngrok) if you enable it in Slack
    handler = SocketModeHandler(app, APP_LEVEL)  # xapp- token
    handler.start()