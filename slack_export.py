#!/usr/bin/env python3
"""
slack_export.py — Export Slack 1:1 DM conversation history to a clean text file
optimised for use as LLM context.

Usage:
    python slack_export.py --list-dms
    python slack_export.py --channel D0123ABCDEF --from 01-01-2025 --to 30-06-2025
    python slack_export.py --channel D0123ABCDEF          # defaults to last 30 days
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MESSAGES_PER_PAGE = 200
REQUEST_DELAY = 0.5  # seconds between paginated requests

SKIP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "group_topic",
    "group_purpose",
    "group_archive",
    "group_unarchive",
    "pinned_item",
    "unpinned_item",
    "ekm_access_denied",
    "channel_name",
    "thread_broadcast",  # "also send to channel" copies — replies handle these
}

INCLUDE_SUBTYPES = {None, "bot_message", "file_share"}

EXPORT_DIR = Path("export")

# ---------------------------------------------------------------------------
# Auth / client setup
# ---------------------------------------------------------------------------


def load_client() -> WebClient:
    load_dotenv()
    token = os.getenv("SLACK_USER_TOKEN", "").strip()
    if not token or not token.startswith("xoxp-"):
        print(
            "ERROR: SLACK_USER_TOKEN is missing or invalid.\n"
            "Add a valid xoxp-... token to your .env file.\n"
            "See README.md for instructions.",
            file=sys.stderr,
        )
        sys.exit(1)
    return WebClient(token=token)


# ---------------------------------------------------------------------------
# Rate-limit-aware API wrapper
# ---------------------------------------------------------------------------


def api_call(fn, **kwargs) -> Any:
    """Call a Slack SDK method, retrying once on rate-limit (HTTP 429)."""
    while True:
        try:
            return fn(**kwargs)
        except SlackApiError as exc:
            error_code = exc.response.get("error", "")
            status = exc.response.status_code if hasattr(exc.response, "status_code") else None

            if status == 429 or error_code == "ratelimited":
                retry_after = int(exc.response.headers.get("Retry-After", 5))
                print(f"  Rate limited — waiting {retry_after}s before retrying…")
                time.sleep(retry_after)
                continue

            if error_code in ("invalid_auth", "not_authed", "token_revoked", "token_expired"):
                print(
                    f"ERROR: Authentication failed ({error_code}).\n"
                    "Check that SLACK_USER_TOKEN in .env is correct and has not expired.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if error_code == "missing_scope":
                print(
                    f"ERROR: Missing OAuth scope.\n"
                    "Ensure your token has the scopes: im:history, im:read, users:read.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if error_code == "channel_not_found":
                print(
                    "ERROR: Channel not found. Use --list-dms to find valid channel IDs.",
                    file=sys.stderr,
                )
                sys.exit(1)

            raise


# ---------------------------------------------------------------------------
# User ID → display name cache
# ---------------------------------------------------------------------------


_user_cache: dict[str, str] = {}


def resolve_user(client: WebClient, user_id: str) -> str:
    """Return @displayname for a Slack user ID, with caching."""
    if user_id in _user_cache:
        return _user_cache[user_id]

    try:
        resp = api_call(client.users_info, user=user_id)
        profile = resp["user"]["profile"]
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or resp["user"].get("name")
            or user_id
        )
    except (SlackApiError, KeyError):
        name = user_id

    display = f"@{name}"
    _user_cache[user_id] = display
    return display


# ---------------------------------------------------------------------------
# Date / timestamp helpers
# ---------------------------------------------------------------------------


def parse_date(date_str: str) -> datetime:
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        print(
            f"ERROR: Invalid date '{date_str}'. Expected format: DD-MM-YYYY",
            file=sys.stderr,
        )
        sys.exit(1)


def format_ts(unix_ts: float) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM UTC'."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def ts_to_date_str(unix_ts: float) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD' for --list-dms display."""
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Message content rendering
# ---------------------------------------------------------------------------

SKIP_SUBTYPES_SET = frozenset(SKIP_SUBTYPES)


def should_include(msg: dict) -> bool:
    subtype = msg.get("subtype")
    if subtype in SKIP_SUBTYPES_SET:
        return False
    if subtype not in INCLUDE_SUBTYPES:
        # Unknown subtype — skip to be safe
        return False
    return True


def render_text(msg: dict) -> str:
    """Build the text portion of a message, including file/image placeholders."""
    parts: list[str] = []

    raw_text = (msg.get("text") or "").strip()
    if raw_text:
        parts.append(raw_text)

    # Files attached to this message
    for f in msg.get("files", []):
        name = f.get("name") or f.get("title") or "unknown"
        mimetype = f.get("mimetype", "")
        if mimetype.startswith("image/"):
            parts.append(f"[image: {name}]")
        else:
            parts.append(f"[file: {name}]")

    # Legacy attachments (file_share subtype uses this sometimes)
    for att in msg.get("attachments", []):
        if att.get("is_share"):
            continue  # already represented in text via unfurl
        fname = att.get("filename") or att.get("title")
        if fname:
            image_url = att.get("image_url") or att.get("thumb_url")
            if image_url:
                parts.append(f"[image: {fname}]")
            else:
                parts.append(f"[file: {fname}]")

    text = " ".join(parts) if parts else "(empty message)"

    if msg.get("edited"):
        text += " (edited)"

    return text


def format_message(msg: dict, client: WebClient, prefix: str = "") -> str:
    """Render a single message as a text line."""
    ts = float(msg["ts"])
    timestamp = format_ts(ts)

    user_id = msg.get("user") or msg.get("bot_id") or "unknown"
    username = resolve_user(client, user_id) if user_id != "unknown" else "@unknown"

    text = render_text(msg)
    return f"{prefix}[{timestamp}] {username}: {text}"


# ---------------------------------------------------------------------------
# --list-dms
# ---------------------------------------------------------------------------


def cmd_list_dms(client: WebClient) -> None:
    print("Fetching DM conversations…\n")
    cursor = None
    rows: list[tuple[str, str, str]] = []

    while True:
        kwargs: dict[str, Any] = {"types": "im", "limit": MESSAGES_PER_PAGE}
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.conversations_list, **kwargs)
        channels = resp.get("channels", [])

        for ch in channels:
            ch_id = ch["id"]
            other_user_id = ch.get("user", "")
            if not other_user_id:
                continue

            display_name = resolve_user(client, other_user_id)

            last_ts = ch.get("last_read") or ch.get("updated")
            if last_ts:
                try:
                    last_date = ts_to_date_str(float(last_ts))
                except (ValueError, TypeError):
                    last_date = "unknown"
            else:
                last_date = "unknown"

            rows.append((ch_id, display_name, last_date))

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_DELAY)

    if not rows:
        print("No 1:1 DM conversations found.")
        return

    # Align columns
    id_width = max(len(r[0]) for r in rows)
    name_width = max(len(r[1]) for r in rows)

    print(f"{'Channel ID':<{id_width}}  {'Participant':<{name_width}}  Last message")
    print("-" * (id_width + name_width + 20))
    for ch_id, name, last_date in rows:
        print(f"{ch_id:<{id_width}}  {name:<{name_width}}  {last_date}")


# ---------------------------------------------------------------------------
# Fetch thread replies
# ---------------------------------------------------------------------------


def fetch_replies(client: WebClient, channel: str, parent_ts: str) -> list[dict]:
    """Fetch all replies for a thread (excludes the parent message at index 0)."""
    replies: list[dict] = []
    cursor = None

    while True:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "ts": parent_ts,
            "limit": MESSAGES_PER_PAGE,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.conversations_replies, **kwargs)
        messages = resp.get("messages", [])

        # Index 0 is the parent message — skip it
        for msg in messages[1:]:
            if should_include(msg):
                replies.append(msg)

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_DELAY)

    return replies


# ---------------------------------------------------------------------------
# Fetch conversation history
# ---------------------------------------------------------------------------


def fetch_history(
    client: WebClient,
    channel: str,
    oldest: float,
    latest: float,
) -> list[dict]:
    """
    Fetch all messages in [oldest, latest] from conversations.history.
    Returns a flat list of message dicts; thread replies are embedded under
    each parent as msg["_replies"].
    """
    all_messages: list[dict] = []
    cursor = None
    page = 1

    while True:
        kwargs: dict[str, Any] = {
            "channel": channel,
            "oldest": str(oldest),
            "latest": str(latest),
            "limit": MESSAGES_PER_PAGE,
            "inclusive": True,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.conversations_history, **kwargs)
        messages = resp.get("messages", [])

        included = [m for m in messages if should_include(m)]
        print(f"  Fetched page {page}… {len(messages)} messages ({len(included)} included)")

        for msg in included:
            if msg.get("reply_count", 0) > 0:
                msg["_replies"] = fetch_replies(client, channel, msg["ts"])
                time.sleep(REQUEST_DELAY)
            else:
                msg["_replies"] = []
            all_messages.append(msg)

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        page += 1
        time.sleep(REQUEST_DELAY)

    # conversations.history returns newest-first; reverse to chronological order
    all_messages.reverse()
    return all_messages


# ---------------------------------------------------------------------------
# Format and write output
# ---------------------------------------------------------------------------


def build_output(
    messages: list[dict],
    client: WebClient,
    participants: list[str],
    from_str: str,
    to_str: str,
) -> tuple[str, int]:
    """
    Build the full export text. Returns (text, total_message_count).
    Total count includes thread replies.
    """
    lines: list[str] = []
    total = 0

    for msg in messages:
        line = format_message(msg, client)
        lines.append(line)
        total += 1

        for reply in msg.get("_replies", []):
            reply_line = format_message(reply, client, prefix="  [thread] ")
            lines.append(reply_line)
            total += 1

    participant_str = ", ".join(sorted(set(participants)))
    header = (
        "=== Slack DM Export ===\n"
        f"Participants: {participant_str}\n"
        f"Period: {from_str} to {to_str}\n"
        f"Total messages: {total}\n"
    )

    body = "\n".join(lines)
    return header + "\n" + body + "\n", total


def write_export(
    client: WebClient,
    channel: str,
    from_dt: datetime,
    to_dt: datetime,
    from_str: str,
    to_str: str,
) -> None:
    oldest = from_dt.timestamp()
    latest = to_dt.timestamp()

    print(f"\nFetching messages from {from_str} to {to_str}…")
    messages = fetch_history(client, channel, oldest, latest)

    if not messages:
        print("No messages found in the specified date range.")
        return

    # Collect unique participant IDs from top-level messages and replies
    participant_ids: set[str] = set()
    for msg in messages:
        uid = msg.get("user") or msg.get("bot_id")
        if uid:
            participant_ids.add(uid)
        for reply in msg.get("_replies", []):
            uid = reply.get("user") or reply.get("bot_id")
            if uid:
                participant_ids.add(uid)

    participants = [resolve_user(client, uid) for uid in participant_ids]

    print("\nFormatting output…")
    text, total = build_output(messages, client, participants, from_str, to_str)

    EXPORT_DIR.mkdir(exist_ok=True)
    filename = f"{channel}_{from_str}_{to_str}.txt"
    output_path = EXPORT_DIR / filename

    output_path.write_text(text, encoding="utf-8")

    participant_str = ", ".join(sorted(set(participants)))
    print(
        f"\nExport complete.\n"
        f"  File:           {output_path}\n"
        f"  Participants:   {participant_str}\n"
        f"  Date range:     {from_str} → {to_str}\n"
        f"  Total messages: {total}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Slack 1:1 DM history to a text file optimised for LLM context.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python slack_export.py --list-dms\n"
            "  python slack_export.py --channel D0123ABCDEF --from 01-01-2025 --to 30-06-2025\n"
            "  python slack_export.py --channel D0123ABCDEF\n"
        ),
    )
    parser.add_argument(
        "--list-dms",
        action="store_true",
        help="List all 1:1 DM conversations with participant names and last message date.",
    )
    parser.add_argument(
        "--channel",
        metavar="CHANNEL_ID",
        help="Slack DM channel ID to export (e.g. D0123ABCDEF).",
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        metavar="DD-MM-YYYY",
        help="Start date (inclusive). Defaults to 30 days ago.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        metavar="DD-MM-YYYY",
        help="End date (inclusive). Defaults to today.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.list_dms and not args.channel:
        parser.print_help()
        sys.exit(0)

    client = load_client()

    if args.list_dms:
        cmd_list_dms(client)
        return

    # Export mode
    now_utc = datetime.now(tz=timezone.utc)

    if args.from_date:
        from_dt = parse_date(args.from_date)
        from_str = args.from_date
    else:
        from_dt = (now_utc - timedelta(days=30)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        from_str = from_dt.strftime("%d-%m-%Y")

    if args.to_date:
        to_dt = parse_date(args.to_date).replace(hour=23, minute=59, second=59)
        to_str = args.to_date
    else:
        to_dt = now_utc.replace(hour=23, minute=59, second=59, microsecond=0)
        to_str = now_utc.strftime("%d-%m-%Y")

    if from_dt > to_dt:
        print("ERROR: --from date must be before --to date.", file=sys.stderr)
        sys.exit(1)

    write_export(client, args.channel, from_dt, to_dt, from_str, to_str)


if __name__ == "__main__":
    main()
