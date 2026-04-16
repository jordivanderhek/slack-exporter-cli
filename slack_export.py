#!/usr/bin/env python3
"""
slack_export.py — Export Slack conversation history (1:1 DMs, public channels,
private channels, MPDMs, Slack Connect) to a clean text file optimised for use
as LLM context.

Usage:
    python slack_export.py --list
    python slack_export.py --list-channels
    python slack_export.py --list-dms                     # deprecated, still works
    python slack_export.py --channel C0123ABCDEF --from 01-01-2025 --to 30-06-2025
    python slack_export.py --channel D0123ABCDEF          # defaults to last 30 days
"""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import sys
import threading
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

# Channel type labels (used in listings and export headers)
TYPE_DM = "dm"
TYPE_MPDM = "mpdm"
TYPE_PUBLIC = "public"
TYPE_PRIVATE = "private"
TYPE_CONNECT = "connect"

# At most this many participant names are printed in the export header;
# the rest are summarised as "... and N others".
MAX_PARTICIPANTS_SHOWN = 20

# Full OAuth scope list (shown to the user when missing_scope fires).
REQUIRED_SCOPES = (
    "im:history, im:read, channels:history, channels:read, "
    "groups:history, groups:read, mpim:history, mpim:read, users:read"
)

# Cap the rendered Name column width in the --list / --list-channels table.
MAX_NAME_COLUMN = 40

# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

_BOUNCING_BAR_FRAMES = [
    "[    =     ]", "[   =      ]", "[  =       ]", "[ =        ]",
    "[=         ]", "[=         ]", "[ =        ]", "[  =       ]",
    "[   =      ]", "[    =     ]", "[     =    ]", "[      =   ]",
    "[       =  ]", "[        = ]", "[         =]", "[         =]",
    "[        = ]", "[       =  ]", "[      =   ]", "[     =    ]",
]
_FRAME_INTERVAL = 0.08  # seconds


class Spinner:
    """Bouncing-bar progress indicator that runs on a background thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._message = ""
        self._lock = threading.Lock()
        self._active = False

    def start(self, message: str = "") -> None:
        self._stop_event.clear()
        with self._lock:
            self._message = message
        self._active = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, message: str) -> None:
        with self._lock:
            self._message = message

    def stop(self, final_message: str = "") -> None:
        if not self._active:
            return
        self._active = False
        self._stop_event.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        sys.stdout.write("\r" + " " * width + "\r")
        sys.stdout.flush()
        if final_message:
            print(final_message)

    def _spin(self) -> None:
        frame_idx = 0
        while not self._stop_event.is_set():
            frame = _BOUNCING_BAR_FRAMES[frame_idx % len(_BOUNCING_BAR_FRAMES)]
            with self._lock:
                msg = self._message
            width = shutil.get_terminal_size(fallback=(80, 24)).columns
            # frame is 12 chars, space separator is 1 char
            available = max(0, width - len(frame) - 1)
            if len(msg) > available:
                msg = msg[: max(0, available - 3)] + "..."
            sys.stdout.write(f"\r{frame} {msg}")
            sys.stdout.flush()
            frame_idx += 1
            self._stop_event.wait(_FRAME_INTERVAL)


_spinner = Spinner()
atexit.register(_spinner.stop)

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
                _spinner.update(f"Rate limited — waiting {retry_after}s before retrying...")
                time.sleep(retry_after)
                continue

            if error_code in ("invalid_auth", "not_authed", "token_revoked", "token_expired"):
                _spinner.stop()
                print(
                    f"ERROR: Authentication failed ({error_code}).\n"
                    "Check that SLACK_USER_TOKEN in .env is correct and has not expired.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if error_code == "missing_scope":
                _spinner.stop()
                needed = exc.response.get("needed") or ""
                hint = f" (needed: {needed})" if needed else ""
                print(
                    f"ERROR: Missing OAuth scope{hint}.\n"
                    f"Ensure your token has the scopes: {REQUIRED_SCOPES}.\n"
                    "After adding scopes, click 'Reinstall to Workspace' in the Slack app\n"
                    "settings and copy the new xoxp- token into .env.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if error_code == "channel_not_found":
                _spinner.stop()
                print(
                    "ERROR: Channel not found. Use --list to find valid channel IDs.",
                    file=sys.stderr,
                )
                sys.exit(1)

            if error_code == "not_in_channel":
                _spinner.stop()
                print(
                    "ERROR: Your user is not a member of this channel.\n"
                    "Join it in Slack and retry.",
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
# Channel classification / resolution helpers
# ---------------------------------------------------------------------------


def classify_channel(ch: dict) -> str:
    """Return the display-level type label for a channel dict from the Slack API.

    Slack Connect takes precedence over public/private so these channels are
    identifiable at a glance in listings.
    """
    if ch.get("is_im"):
        return TYPE_DM
    if ch.get("is_mpim"):
        return TYPE_MPDM
    if ch.get("is_ext_shared") or ch.get("is_shared"):
        return TYPE_CONNECT
    if ch.get("is_private"):
        return TYPE_PRIVATE
    return TYPE_PUBLIC


def fetch_channel_members(client: WebClient, channel_id: str) -> list[str]:
    """Return all member user IDs for a channel, paginating as needed."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": MESSAGES_PER_PAGE}
        if cursor:
            kwargs["cursor"] = cursor
        resp = api_call(client.conversations_members, **kwargs)
        ids.extend(resp.get("members", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(REQUEST_DELAY)
    return ids


def resolve_channel_info(client: WebClient, channel_id: str) -> dict:
    """Fetch and classify a single channel for the export path.

    Returns a dict: {id, type, display_name, is_archived, raw}.
    - DM    -> display_name = @otheruser
    - MPDM  -> display_name = "Group DM" (members are listed in the export header)
    - public/private/connect -> display_name = #channelname
    """
    resp = api_call(client.conversations_info, channel=channel_id)
    ch = resp["channel"]
    ch_type = classify_channel(ch)
    is_archived = bool(ch.get("is_archived"))

    if ch_type == TYPE_DM:
        other_id = ch.get("user", "")
        display = resolve_user(client, other_id) if other_id else channel_id
    elif ch_type == TYPE_MPDM:
        display = "Group DM"
    else:
        display = f"#{ch.get('name') or channel_id}"

    return {
        "id": channel_id,
        "type": ch_type,
        "display_name": display,
        "is_archived": is_archived,
        "raw": ch,
    }


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
# List commands (--list-dms / --list-channels / --list)
# ---------------------------------------------------------------------------


def _last_active_from_channel(
    client: WebClient, ch: dict, ch_type: str
) -> tuple[str, float]:
    """Derive ('YYYY-MM-DD', sort_ts) for the Last active column.

    Uses the `updated` field (milliseconds epoch) already present in the
    conversations.list response for non-DM channels. Falls back to a single
    conversations.history(limit=1) call when `updated` is missing/zero, or for
    DMs (where `updated` reflects metadata changes rather than last message).
    """
    updated_ms = ch.get("updated")
    if ch_type != TYPE_DM and updated_ms:
        last_ts = float(updated_ms) / 1000.0
        return ts_to_date_str(last_ts), last_ts

    ch_id = ch["id"]
    try:
        hist = api_call(client.conversations_history, channel=ch_id, limit=1)
        msgs = hist.get("messages", [])
        if msgs:
            last_ts = float(msgs[0]["ts"])
            return ts_to_date_str(last_ts), last_ts
    except (SlackApiError, KeyError, ValueError, TypeError):
        pass
    finally:
        time.sleep(REQUEST_DELAY)
    return "unknown", 0.0


def _row_for_channel(client: WebClient, ch: dict) -> dict | None:
    """Build a normalised row dict for one channel from conversations.list.

    Returns None for DM entries with no other user (should not happen in
    practice, but defensive).
    """
    ch_id = ch["id"]
    ch_type = classify_channel(ch)
    is_archived = bool(ch.get("is_archived"))

    if ch_type == TYPE_DM:
        other_id = ch.get("user", "")
        if not other_id:
            return None
        name = resolve_user(client, other_id)
        members = 2
    elif ch_type == TYPE_MPDM:
        member_ids = fetch_channel_members(client, ch_id)
        names = [resolve_user(client, uid).lstrip("@") for uid in member_ids]
        name = ", ".join(names)
        members = ch.get("num_members", len(member_ids))
    else:
        base_name = ch.get("name") or ch_id
        name = f"#{base_name}"
        if is_archived:
            name = f"{name} (archived)"
        members = ch.get("num_members", 0)

    last_date, last_ts = _last_active_from_channel(client, ch, ch_type)

    return {
        "id": ch_id,
        "name": name,
        "type": ch_type,
        "members": members,
        "last_date": last_date,
        "last_ts": last_ts,
    }


# User-facing type label -> Slack API `types` token.
# `connect` is not a real API type: Slack Connect channels come through as
# public_channel or private_channel with is_ext_shared/is_shared set, so we
# request both and post-filter.
_TYPE_LABEL_TO_API: dict[str, tuple[str, ...]] = {
    TYPE_DM: ("im",),
    TYPE_PUBLIC: ("public_channel",),
    TYPE_PRIVATE: ("private_channel",),
    TYPE_MPDM: ("mpim",),
    TYPE_CONNECT: ("public_channel", "private_channel"),
}

_VALID_TYPE_LABELS = tuple(_TYPE_LABEL_TO_API.keys())


def _resolve_type_filter(
    spec: str | None, default_labels: set[str]
) -> tuple[str, set[str]]:
    """Parse a --type spec into (api_types_string, allowed_label_set).

    `spec` is a comma-separated user-facing string ("public,mpdm"). Unknown
    tokens exit the process with a clear error. Returns the API `types`
    parameter (comma-joined, deduplicated) plus the set of labels used for
    post-filtering.
    """
    if not spec:
        labels = set(default_labels)
    else:
        raw = [t.strip().lower() for t in spec.split(",") if t.strip()]
        unknown = [t for t in raw if t not in _TYPE_LABEL_TO_API]
        if unknown:
            print(
                f"ERROR: Unknown type(s): {', '.join(unknown)}.\n"
                f"Valid values: {', '.join(_VALID_TYPE_LABELS)}.",
                file=sys.stderr,
            )
            sys.exit(2)
        labels = set(raw)
        # Every --type value must be a subset of what the caller allows (e.g.
        # --list-channels can't surface DMs).
        disallowed = labels - default_labels
        if disallowed:
            print(
                f"ERROR: Type(s) {', '.join(sorted(disallowed))} are not "
                "available for this command.\n"
                f"Allowed here: {', '.join(sorted(default_labels))}.",
                file=sys.stderr,
            )
            sys.exit(2)

    api_types: list[str] = []
    for label in labels:
        for api_token in _TYPE_LABEL_TO_API[label]:
            if api_token not in api_types:
                api_types.append(api_token)

    return ",".join(api_types), labels


def _fetch_channel_rows(
    client: WebClient,
    types: str,
    spinner_label: str,
    allowed_labels: set[str] | None = None,
) -> list[dict]:
    """Paginate conversations.list and build normalised rows for all channels.

    When `allowed_labels` is provided, rows whose classified type is not in
    the set are skipped (used by the --type filter, and by `connect` which
    must be post-filtered since Slack has no dedicated API type for it).
    """
    rows: list[dict] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "types": types,
            "limit": MESSAGES_PER_PAGE,
            "exclude_archived": False,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.conversations_list, **kwargs)
        channels = resp.get("channels", [])

        for ch in channels:
            if allowed_labels is not None and classify_channel(ch) not in allowed_labels:
                continue
            row = _row_for_channel(client, ch)
            if row is None:
                continue
            rows.append(row)
            _spinner.update(f"{spinner_label} found {len(rows)}")

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_DELAY)

    return rows


def _truncate(text: str, width: int) -> str:
    """Truncate `text` to `width` characters, appending '...' if shortened."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def cmd_list_dms(client: WebClient) -> None:
    """Legacy listing — 1:1 DMs only, in the original two-column format."""
    print("Note: --list-dms is deprecated; use --list instead.")

    _spinner.start("Loading DMs...")
    try:
        rows = _fetch_channel_rows(client, types="im", spinner_label="Loading DMs...")
        _spinner.stop(f"Found {len(rows)} DM conversation(s).")
    finally:
        _spinner.stop()

    if not rows:
        print("No 1:1 DM conversations found.")
        return

    rows.sort(key=lambda r: r["last_ts"], reverse=True)

    id_width = max(len(r["id"]) for r in rows)
    name_width = max(len(r["name"]) for r in rows)

    print(f"{'Channel ID':<{id_width}}  {'Participant':<{name_width}}  Last message")
    print("-" * (id_width + name_width + 20))
    for r in rows:
        print(
            f"{r['id']:<{id_width}}  {r['name']:<{name_width}}  {r['last_date']}"
        )


def _print_channel_table(rows: list[dict], noun: str) -> None:
    """Render rows with the unified ID / Name / Type / Members / Last active columns."""
    if not rows:
        print(f"No {noun} found.")
        return

    rows.sort(key=lambda r: r["last_ts"], reverse=True)

    # Clamp the Name column for long MPDM member lists.
    for r in rows:
        r["_name_display"] = _truncate(r["name"], MAX_NAME_COLUMN)

    id_width = max(len("ID"), max(len(r["id"]) for r in rows))
    name_width = max(len("Name"), max(len(r["_name_display"]) for r in rows))
    type_width = max(len("Type"), max(len(r["type"]) for r in rows))
    members_width = max(len("Members"), max(len(str(r["members"])) for r in rows))

    print(f"Found {len(rows)} {noun}.\n")
    header = (
        f"{'ID':<{id_width}}  {'Name':<{name_width}}  "
        f"{'Type':<{type_width}}  {'Members':<{members_width}}  Last active"
    )
    print(header)
    for r in rows:
        print(
            f"{r['id']:<{id_width}}  {r['_name_display']:<{name_width}}  "
            f"{r['type']:<{type_width}}  {str(r['members']):<{members_width}}  "
            f"{r['last_date']}"
        )


def cmd_list_channels(client: WebClient, type_filter: str | None = None) -> None:
    """List public, private, and multi-party DM channels (no 1:1 DMs).

    `type_filter` is an optional comma-separated subset of
    {public, private, mpdm, connect}.
    """
    defaults = {TYPE_PUBLIC, TYPE_PRIVATE, TYPE_MPDM, TYPE_CONNECT}
    api_types, allowed = _resolve_type_filter(type_filter, defaults)

    _spinner.start("Loading channels...")
    try:
        rows = _fetch_channel_rows(
            client,
            types=api_types,
            spinner_label="Loading channels...",
            allowed_labels=allowed,
        )
        _spinner.stop()
    finally:
        _spinner.stop()

    _print_channel_table(rows, noun="channels")


def cmd_list(client: WebClient, type_filter: str | None = None) -> None:
    """List every conversation the user belongs to (DMs + channels + MPDMs).

    `type_filter` is an optional comma-separated subset of
    {dm, public, private, mpdm, connect}.
    """
    defaults = {TYPE_DM, TYPE_PUBLIC, TYPE_PRIVATE, TYPE_MPDM, TYPE_CONNECT}
    api_types, allowed = _resolve_type_filter(type_filter, defaults)

    _spinner.start("Loading conversations...")
    try:
        rows = _fetch_channel_rows(
            client,
            types=api_types,
            spinner_label="Loading conversations...",
            allowed_labels=allowed,
        )
        _spinner.stop()
    finally:
        _spinner.stop()

    _print_channel_table(rows, noun="conversations")


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
# Fetch thread replies for all threaded messages (two-pass approach)
# ---------------------------------------------------------------------------


def fetch_all_thread_replies(client: WebClient, channel: str, messages: list[dict]) -> list[dict]:
    """Populate _replies for every threaded message; updates the spinner with i/total progress."""
    parents = [m for m in messages if m.get("reply_count", 0) > 0]
    total = len(parents)
    for i, msg in enumerate(parents, 1):
        _spinner.update(f"Fetching thread replies... {i}/{total} threads")
        msg["_replies"] = fetch_replies(client, channel, msg["ts"])
        time.sleep(REQUEST_DELAY)
    return messages


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
        for msg in included:
            msg["_replies"] = []
            all_messages.append(msg)

        _spinner.update(f"Fetching messages... {len(all_messages)} fetched")

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


def _format_participants(participants: list[str]) -> str:
    """Render the Participants line, capping at MAX_PARTICIPANTS_SHOWN names."""
    sorted_p = sorted(set(participants))
    if len(sorted_p) > MAX_PARTICIPANTS_SHOWN:
        shown = ", ".join(sorted_p[:MAX_PARTICIPANTS_SHOWN])
        others = len(sorted_p) - MAX_PARTICIPANTS_SHOWN
        return f"{shown}, ... and {others} others"
    return ", ".join(sorted_p)


def build_output(
    messages: list[dict],
    client: WebClient,
    participants: list[str],
    from_str: str,
    to_str: str,
    channel_type: str,
    channel_display_name: str,
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

    header_lines = ["=== Slack Export ==="]
    if channel_type != TYPE_DM:
        header_lines.append(
            f"Channel: {channel_display_name} ({channel_type})"
        )
    header_lines += [
        f"Participants: {_format_participants(participants)}",
        f"Period: {from_str} to {to_str}",
        f"Total messages: {total:,}",
    ]
    header = "\n".join(header_lines) + "\n"

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

    # Resolve channel metadata up-front so we know the type / archived state.
    _spinner.start(f"Resolving channel {channel}...")
    try:
        info = resolve_channel_info(client, channel)
        _spinner.stop()
    finally:
        _spinner.stop()

    if info["is_archived"]:
        print("Note: this channel is archived. Proceeding with export.")

    channel_type = info["type"]
    channel_display_name = info["display_name"]

    _spinner.start(f"Fetching messages from {from_str} to {to_str}...")
    try:
        messages = fetch_history(client, channel, oldest, latest)

        if not messages:
            _spinner.stop("No messages found in the specified date range.")
            return

        fetch_all_thread_replies(client, channel, messages)

        # Participant IDs:
        # - DMs: derived from message authors (matches existing behaviour).
        # - Channels / MPDMs: full member list from conversations.members so the
        #   header reflects the channel roster, not just active authors.
        participant_ids: set[str] = set()
        if channel_type == TYPE_DM:
            for msg in messages:
                uid = msg.get("user") or msg.get("bot_id")
                if uid:
                    participant_ids.add(uid)
                for reply in msg.get("_replies", []):
                    uid = reply.get("user") or reply.get("bot_id")
                    if uid:
                        participant_ids.add(uid)
        else:
            _spinner.update("Fetching channel members...")
            participant_ids.update(fetch_channel_members(client, channel))

        participant_ids_list = list(participant_ids)
        total_users = len(participant_ids_list)
        participants: list[str] = []
        for i, uid in enumerate(participant_ids_list, 1):
            _spinner.update(f"Resolving usernames... {i}/{total_users} users")
            participants.append(resolve_user(client, uid))

        _spinner.stop()
    finally:
        _spinner.stop()  # no-op if already stopped cleanly; catches KeyboardInterrupt

    text, total = build_output(
        messages,
        client,
        participants,
        from_str,
        to_str,
        channel_type,
        channel_display_name,
    )

    EXPORT_DIR.mkdir(exist_ok=True)
    filename = f"{channel}_{from_str}_{to_str}.txt"
    output_path = EXPORT_DIR / filename

    output_path.write_text(text, encoding="utf-8")

    summary_channel = (
        channel_display_name if channel_type == TYPE_DM
        else f"{channel_display_name} ({channel_type})"
    )
    print(
        f"\nExport complete.\n"
        f"  File:           {output_path}\n"
        f"  Channel:        {summary_channel}\n"
        f"  Participants:   {_format_participants(participants)}\n"
        f"  Date range:     {from_str} → {to_str}\n"
        f"  Total messages: {total:,}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export Slack conversation history (DMs, public/private channels, "
            "MPDMs, Slack Connect) to a text file optimised for LLM context."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python slack_export.py --list\n"
            "  python slack_export.py --list --type public,private\n"
            "  python slack_export.py --list-channels --type connect\n"
            "  python slack_export.py --list-channels --type mpdm\n"
            "  python slack_export.py --list-dms                       # deprecated\n"
            "  python slack_export.py --channel C0123ABCDEF --from 01-01-2025 --to 30-06-2025\n"
            "  python slack_export.py --channel G0999XYZABC            # MPDM, last 30 days\n"
            "  python slack_export.py --channel D0123ABCDEF\n"
        ),
    )
    parser.add_argument(
        "--list",
        dest="list_all",
        action="store_true",
        help="List every conversation you belong to (DMs, channels, MPDMs, Slack Connect).",
    )
    parser.add_argument(
        "--list-channels",
        action="store_true",
        help="List public, private, and multi-party DM channels (no 1:1 DMs).",
    )
    parser.add_argument(
        "--list-dms",
        action="store_true",
        help="[Deprecated] List only 1:1 DMs. Use --list instead.",
    )
    parser.add_argument(
        "--type",
        dest="type_filter",
        metavar="TYPES",
        help=(
            "Comma-separated filter for --list / --list-channels. "
            "Values: dm, public, private, mpdm, connect "
            "(e.g. --type public,connect). "
            "Defaults: --list shows all types; --list-channels shows all non-DM types."
        ),
    )
    parser.add_argument(
        "--channel",
        metavar="CHANNEL_ID",
        help=(
            "Slack conversation ID to export (D.../C.../G...). "
            "Accepts DMs, public/private channels, MPDMs, and Slack Connect channels."
        ),
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

    list_flags = [args.list_all, args.list_channels, args.list_dms]
    if sum(1 for f in list_flags if f) > 1:
        print(
            "ERROR: Pass only one of --list, --list-channels, --list-dms.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not any(list_flags) and not args.channel:
        parser.print_help()
        sys.exit(0)

    if args.type_filter and not (args.list_all or args.list_channels):
        print(
            "ERROR: --type only applies to --list or --list-channels.",
            file=sys.stderr,
        )
        sys.exit(2)

    client = load_client()

    try:
        if args.list_all:
            cmd_list(client, type_filter=args.type_filter)
            return
        if args.list_channels:
            cmd_list_channels(client, type_filter=args.type_filter)
            return
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

    except KeyboardInterrupt:
        _spinner.stop()
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
