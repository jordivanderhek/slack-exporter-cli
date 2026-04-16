# slack-exporter

A single-file Python CLI that exports Slack conversation history — 1:1 DMs, public channels, private channels, multi-party DMs (MPDMs), and Slack Connect channels — to a clean text file optimised for use as LLM context.

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
2. Give it a name (e.g. `slack-exporter`) and select your workspace.
3. In the left sidebar, go to **OAuth & Permissions**.
4. Under **User Token Scopes**, add the following scopes:

   | Scope | Purpose |
   |---|---|
   | `im:history` | Read messages from 1:1 DMs |
   | `im:read` | List 1:1 DMs |
   | `channels:history` | Read messages from public channels |
   | `channels:read` | List public channels |
   | `groups:history` | Read messages from private channels and MPDMs |
   | `groups:read` | List private channels and MPDMs |
   | `mpim:history` | Read messages from multi-party DMs |
   | `mpim:read` | List multi-party DMs |
   | `users:read` | Resolve user IDs to display names |

5. Click **Install to Workspace** and authorise.
6. Copy the **User OAuth Token** (`xoxp-...`) and paste it into your `.env` file.

> **Already installed the app?** After adding new scopes, go back to **OAuth & Permissions** and click **Reinstall to Workspace** to pick up the new scopes. Then copy the updated `xoxp-...` token into `.env` — the previous token will not have the new permissions.

---

## Usage

### List all conversations

```bash
python slack_export.py --list
```

Output:

```
Found 23 conversations.

ID              Name                        Type     Members  Last active
C0123ABCDEF     #product-updates            public   34       2025-06-12
C0456GHIJKL     #acme-client-x              connect  8        2025-06-10
G0789MNOPQR     private-leadership          private  5        2025-06-11
G0999XYZABC     alice, bob, charlie         mpdm     3        2025-05-28
D0123ABCDEF     @alice                      dm       2        2025-06-12
```

The `Type` column is one of `dm`, `mpdm`, `public`, `private`, or `connect`. Slack Connect channels (shared with external workspaces) are tagged `connect` regardless of whether they are technically public or private on your side. Archived channels appear with an `(archived)` suffix on the name.

### Other listing commands

```bash
# Public, private, and multi-party DM channels (excludes 1:1 DMs)
python slack_export.py --list-channels

# [Deprecated] 1:1 DMs only — use --list instead
python slack_export.py --list-dms
```

### Filtering by type

Workspaces with hundreds of channels and MPDMs quickly fill the screen. Use `--type` (comma-separated) to narrow `--list` or `--list-channels` to specific types.

Valid values: `dm`, `public`, `private`, `mpdm`, `connect`.

```bash
# Just public + private channels (no MPDMs, no DMs)
python slack_export.py --list --type public,private

# Just Slack Connect channels
python slack_export.py --list-channels --type connect

# Just MPDMs
python slack_export.py --list-channels --type mpdm

# Just DMs (equivalent to the legacy --list-dms but in the new format)
python slack_export.py --list --type dm
```

`--type` also makes the Slack API call smaller: only the requested channel types are requested from `conversations.list` (with the exception of `connect`, which has no dedicated API type and is filtered in post).

### Export a conversation

```bash
# Public channel
python slack_export.py --channel C0123ABCDEF --from 01-01-2025 --to 30-06-2025

# Slack Connect channel
python slack_export.py --channel C0456GHIJKL --from 01-03-2025 --to 30-06-2025

# Private channel
python slack_export.py --channel G0789MNOPQR --from 01-01-2025 --to 30-06-2025

# Multi-party DM
python slack_export.py --channel G0999XYZABC --from 01-01-2025 --to 30-06-2025

# 1:1 DM
python slack_export.py --channel D0123ABCDEF --from 01-01-2025 --to 30-06-2025

# Default: last 30 days
python slack_export.py --channel C0123ABCDEF
```

The exported file is saved to `export/{channel_id}_{from}_{to}.txt`.

A summary is printed to the console:

```
Export complete.
  File:           export/C0123ABCDEF_01-01-2025_30-06-2025.txt
  Channel:        #product-updates (public)
  Participants:   @alice, @bob, @charlie, ... and 31 others
  Date range:     01-01-2025 → 30-06-2025
  Total messages: 1,204
```

### CLI reference

| Flag | Value | Description |
|---|---|---|
| `--list` | — | List every conversation you belong to (DMs, channels, MPDMs, Slack Connect). |
| `--list-channels` | — | List public, private, MPDM, and Slack Connect channels (excludes 1:1 DMs). |
| `--list-dms` | — | **Deprecated.** Legacy 1:1-DM listing, kept for backwards compatibility. Prefer `--list` or `--list --type dm`. |
| `--type` | `dm,public,private,mpdm,connect` (comma-separated) | Restrict `--list` / `--list-channels` to a subset of types. Only valid with those flags. Narrows the `conversations.list` API call where possible. |
| `--channel` | `CHANNEL_ID` | Slack conversation ID to export. Accepts `D...` (DM), `C...` (public/private), `G...` (private/MPDM), including Slack Connect channels. |
| `--from` | `DD-MM-YYYY` | Start date (inclusive). Defaults to 30 days ago. |
| `--to` | `DD-MM-YYYY` | End date (inclusive). Defaults to today. |

Only one of `--list`, `--list-channels`, `--list-dms` may be passed at a time. `--type` may only be combined with `--list` or `--list-channels`.

---

## Output format

The export file is a plain text document structured for efficient LLM consumption.

### 1:1 DM

```
=== Slack Export ===
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

### Channel (public / private / MPDM / Slack Connect)

```
=== Slack Export ===
Channel: #product-updates (public)
Participants: @alice, @bob, @charlie, ... and 31 others
Period: 01-01-2025 to 30-06-2025
Total messages: 1,204

[2025-01-03 09:14 UTC] @alice: Kicking off the Q1 roadmap thread.
  [thread] [2025-01-03 09:30 UTC] @bob: +1, I'll add API items.
[2025-01-03 10:22 UTC] @charlie: Deploy is green.
```

- For channels and MPDMs, a `Channel:` line identifies the channel and its type.
- For 1:1 DMs, the `Channel:` line is omitted.
- When a conversation has more than 20 participants, the extras are summarised as `, ... and N others`.

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
| Missing OAuth scope | Prints the required scopes (including the needed one when Slack reports it) and exits |
| Rate limiting (HTTP 429) | Reads `Retry-After` header and retries automatically |
| Channel not found | Prints a message pointing to `--list` and exits |
| Not a member of channel (`not_in_channel`) | Prints a message asking you to join the channel in Slack and exits |
| Archived channel | Prints a warning but proceeds with the export — archived history is still readable |

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
