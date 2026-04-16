# slack-exporter

A single-file Python CLI that exports Slack 1:1 DM conversation history to a clean text file, optimised for use as LLM context.

---

## Requirements

- Python 3.10+
- A Slack **User Token** (`xoxp-...`) with the scopes below

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy the env template and add your token
cp .env.example .env
# Edit .env and set SLACK_USER_TOKEN=xoxp-...
```

### Creating a Slack User Token

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App → From scratch**.
2. Give it a name (e.g. `dm-exporter`) and select your workspace.
3. In the left sidebar, go to **OAuth & Permissions**.
4. Under **User Token Scopes**, add the following scopes:

   | Scope | Purpose |
   |---|---|
   | `im:history` | Read messages from DM conversations |
   | `im:read` | List DM conversations |
   | `users:read` | Resolve user IDs to display names |

5. Click **Install to Workspace** and authorise.
6. Copy the **User OAuth Token** (`xoxp-...`) and paste it into your `.env` file.

---

## Usage

### List all DM conversations

```bash
python slack_export.py --list-dms
```

Output:

```
Channel ID     Participant   Last message
----------------------------------------------
D0123ABCDEF    @alice        2025-06-12
D0456GHIJKL    @bob          2025-05-30
```

Use this to find the channel ID for the conversation you want to export.

### Export a conversation

```bash
# Specify a date range
python slack_export.py --channel D0123ABCDEF --from 01-01-2025 --to 30-06-2025

# Default: last 30 days
python slack_export.py --channel D0123ABCDEF
```

The exported file is saved to `export/{channel_id}_{from}_{to}.txt`.

A summary is printed to the console:

```
Export complete.
  File:           export/D0123ABCDEF_01-01-2025_30-06-2025.txt
  Participants:   @alice, @bob
  Date range:     01-01-2025 → 30-06-2025
  Total messages: 342
```

---

## Output format

The export file is a plain text document structured for efficient LLM consumption.

```
=== Slack DM Export ===
Participants: @alice, @bob
Period: 01-01-2025 to 30-06-2025
Total messages: 342

[2025-01-03 09:14 UTC] @alice: Hey, did you see the proposal?
[2025-01-03 09:16 UTC] @bob: Yes, looks good. A few notes though.
  [thread] [2025-01-03 09:18 UTC] @alice: Sure, go ahead.
  [thread] [2025-01-03 09:22 UTC] @bob: Section 3 needs rework on pricing.
[2025-01-03 10:01 UTC] @alice: Updated version is in the drive now. (edited)
[2025-01-03 10:45 UTC] @alice: [file: proposal-v2.pdf]
[2025-01-03 11:02 UTC] @bob: [image: screenshot.png]
```

### Format rules

| Element | Behaviour |
|---|---|
| Timestamps | Always UTC (`YYYY-MM-DD HH:MM UTC`) |
| Thread replies | Indented with `  [thread]` prefix, inline under parent |
| Edited messages | Appended with ` (edited)` |
| File attachments | Rendered as `[file: filename.ext]` |
| Images | Rendered as `[image: filename.ext]` |
| Reactions | Skipped entirely |
| System messages | Skipped (join/leave/topic changes/etc.) |

### Included message subtypes

| Subtype | Included |
|---|---|
| Normal messages (no subtype) | Yes |
| `bot_message` | Yes |
| `file_share` | Yes |
| `channel_join`, `channel_leave`, `channel_topic`, `channel_purpose`, and other administrative subtypes | No |

---

## Error handling

| Error | Behaviour |
|---|---|
| Missing/invalid token | Prints a clear message and exits |
| Missing OAuth scope | Prints the required scopes and exits |
| Rate limiting (HTTP 429) | Reads `Retry-After` header and retries automatically |
| Channel not found | Prints a message pointing to `--list-dms` and exits |

---

## Project structure

```
slack-exporter/
├── slack_export.py     # all logic in one file
├── .env                # your token (not committed)
├── .env.example        # template
├── requirements.txt
├── README.md
└── export/             # output files land here (gitignored)
```
