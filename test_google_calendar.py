# test_google_calendar.py
# Quick sanity check: can the service account create & delete an event?

import os
import datetime as dt
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()  # loads PTO_CAL_ID from .env

CAL_ID = os.getenv("PTO_CAL_ID")
if not CAL_ID:
    raise SystemExit("❌ Missing PTO_CAL_ID in .env")

# Path to your downloaded service account key:
CREDS_PATH = "google-creds.json"

# Build client
creds = service_account.Credentials.from_service_account_file(
    CREDS_PATH,
    scopes=["https://www.googleapis.com/auth/calendar"],
)
cal = build("calendar", "v3", credentials=creds)

# Make a quick 30-min event starting 2 minutes from now
now = dt.datetime.now(dt.timezone.utc)
start = now + dt.timedelta(minutes=2)
end = start + dt.timedelta(minutes=30)

event_body = {
    "summary": "PTO Bot Test (safe to delete)",
    "description": "Created by test_google_calendar.py",
    "start": {"dateTime": start.isoformat()},
    "end": {"dateTime": end.isoformat()},
}

print("➕ Creating event...")
ev = cal.events().insert(calendarId=CAL_ID, body=event_body).execute()
print("✅ Created:", ev["id"])

print("🗑️ Deleting event...")
cal.events().delete(calendarId=CAL_ID, eventId=ev["id"]).execute()
print("✅ Deleted")