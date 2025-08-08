# pip install slack-bolt google-api-python-client google-auth google-auth-oauthlib
import os, sqlite3
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from google.oauth2 import service_account
from googleapiclient.discovery import build

PTO_CHANNEL = os.environ["PTO_CHANNEL_ID"]   # e.g. C0123456789
CAL_ID = os.environ["PTO_CAL_ID"]            # shared PTO calendar id

# Google
creds = service_account.Credentials.from_service_account_file(
    "google-creds.json",
    scopes=["https://www.googleapis.com/auth/calendar"],
)
cal = build("calendar", "v3", credentials=creds)

# DB
db = sqlite3.connect("pto.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS pto_links(
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

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
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
                # Date
                {
                    "type": "input",
                    "block_id": "date_b",
                    "label": {"type": "plain_text", "text": "Date"},
                    "element": {"type": "datepicker", "action_id": "date_a"}
                },
                # Start
                {
                    "type": "input",
                    "block_id": "start_b",
                    "label": {"type": "plain_text", "text": "Start (HH:MM, 24h)"},
                    "element": {"type": "plain_text_input", "action_id": "start_a", "placeholder": {"type": "plain_text", "text": "09:00"}}
                },
                # End
                {
                    "type": "input",
                    "block_id": "end_b",
                    "label": {"type": "plain_text", "text": "End (HH:MM, 24h)"},
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

# 2) Handle modal submit → create calendar event → post summary in #pto
@app.view("pto_submit")
def handle_submit(ack, body, client, logger):
    values = body["view"]["state"]["values"]
    target_user = values["user_b"]["user_a"]["selected_user"]
    date = values["date_b"]["date_a"]["selected_date"]
    start = values["start_b"]["start_a"]["value"].strip()
    end = values["end_b"]["end_a"]["value"].strip()
    note = values.get("note_b", {}).get("note_a", {}).get("value", "") or ""
    errors = {}

    # baby validation
    def _valid_time(t):
        try:
            hh, mm = t.split(":")
            return 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        except Exception:
            return False
    if not _valid_time(start): errors["start_b"] = "Use HH:MM (24h), like 09:00"
    if not _valid_time(end): errors["end_b"] = "Use HH:MM (24h), like 17:00"
    if errors:
        return ack(response_action="errors", errors=errors)
    ack()  # modal closes

    start_iso = f"{date}T{start}:00"
    end_iso = f"{date}T{end}:00"

    # Create Google Calendar event
    try:
        ev = cal.events().insert(calendarId=CAL_ID, body={
            "summary": f"PTO - Slack {target_user}",
            "description": f"Requested via Slack by <@{body['user']['id']}> for <@{target_user}>.\n{note}",
            "start": {"dateTime": start_iso},
            "end": {"dateTime": end_iso},
        }).execute()
    except Exception as e:
        logger.error(e)
        # DM the submitter if Calendar fails
        client.chat_postMessage(channel=body["user"]["id"], text=f"⚠️ Couldn’t create calendar event: {e}")
        return

    # Post summary in #pto (this is the thing they can delete to remove)
    msg = client.chat_postMessage(
        channel=PTO_CHANNEL,
        text=f"PTO booked for <@{target_user}> on {date} {start}-{end}. {('— ' + note) if note else ''}\n_Delete this message to remove from calendar._"
    )

    # Store mapping
    db.execute("INSERT OR REPLACE INTO pto_links VALUES (?,?,?,?,?,?,?,?,?)", (
        msg["ts"], PTO_CHANNEL, body["user"]["id"], target_user, ev["id"], CAL_ID, start_iso, end_iso, note
    ))
    db.commit()

# 3) If that summary message gets deleted → remove calendar event
@app.event("message")
def on_message_events(event, logger):
    if event.get("subtype") == "message_deleted":
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

if __name__ == "__main__":
    # For local dev, you can run via Socket Mode (no ngrok) if you enable it in Slack
    handler = SocketModeHandler(app, os.environ["SLACK_APP_LEVEL_TOKEN"])  # xapp- token
    handler.start()