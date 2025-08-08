# Slack PTO Bot (Modal → Google Calendar)

## Features
- Global Shortcut opens a PTO modal
- Logs PTO in a shared Google Calendar
- Posts a message in #pto
- Deleting the message removes the calendar event

## Setup

1. **Create a Slack App**
   - Enable Socket Mode
   - Enable Global Shortcuts (`log_pto`)
   - Enable `message.channels` event
   - Scopes:
     - `channels:history`
     - `channels:read`
     - `chat:write`
     - `commands`
     - `users:read`
     - `users:read.email`

2. **Google Calendar**
   - Create a service account in Google Cloud Console
   - Download JSON key → save as `google-creds.json`
   - Share your PTO calendar with the service account email (make changes allowed)

3. **Clone & Install**
   ```bash
   git clone <repo-url>
   cd slack-pto-bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env# PTOAutomation
