#!/usr/bin/env python3
"""
slack_export.py — Export Slack conversation history (1:1 DMs, public channels,
private channels, MPDMs, Slack Connect) to a clean text file optimised for use
as LLM context.

Usage:
    python slack_export.py --list
    python slack_export.py --list-channels
    python slack_export.py --list-dms                     # deprecated, still works
    python slack_export.py --list-user U0123ABCDEF
    python slack_export.py --list-user "Alice Johnson"
    python slack_export.py --channel C0123ABCDEF --from 01-01-2025 --to 30-06-2025
    python slack_export.py --channel D0123ABCDEF          # defaults to last 30 days
    python slack_export.py --diary --dry-run
    python slack_export.py --diary --from 01-04-2026 --to 24-04-2026 --dry-run
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

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
SNAPSHOTS_DIR = Path("snapshots")

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

# `--diary` without a date: argparse sets this sentinel (today 00:00 UTC → now).
DIARY_ROLLING = "__rolling__"

# Populate after a dry-run by inspecting which user IDs consistently emit bot or
# integration noise (Linear, GitHub, calendar bots, workflows). Expected to grow.
KNOWN_BOT_USER_IDS: set[str] = set()

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

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
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
        self._stream.write("\r" + " " * width + "\r")
        self._stream.flush()
        if final_message:
            print(final_message, file=self._stream)

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
            self._stream.write(f"\r{frame} {msg}")
            self._stream.flush()
            frame_idx += 1
            self._stop_event.wait(_FRAME_INTERVAL)


_spinner = Spinner()
_diary_spinner = Spinner(stream=sys.stderr)
atexit.register(_spinner.stop)
atexit.register(_diary_spinner.stop)

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
# User input resolution (user ID or name → (id, @display))
# ---------------------------------------------------------------------------

# Slack user IDs start with U (regular) or W (Enterprise Grid) followed by an
# uppercase alphanumeric suffix. The 8+ suffix floor avoids false positives on
# short all-caps names like "ULRICH" while matching real 9+ character IDs.
_USER_ID_PATTERN = re.compile(r"^[UW][A-Z0-9]{8,}$")

# users.list is paginated and unchanging within a single invocation; cache the
# full member list so --list-user resolution only pays for it once.
_users_list_cache: list[dict] | None = None

# Cap the number of matches shown when an ambiguous name lookup occurs.
MAX_USER_MATCHES_SHOWN = 10


def _fetch_all_users(client: WebClient) -> list[dict]:
    """Paginate users.list once per process; cached on the module."""
    global _users_list_cache
    if _users_list_cache is not None:
        return _users_list_cache

    members: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"limit": MESSAGES_PER_PAGE}
        if cursor:
            kwargs["cursor"] = cursor
        resp = api_call(client.users_list, **kwargs)
        members.extend(resp.get("members", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(REQUEST_DELAY)

    _users_list_cache = members
    return members


def _user_display_from_member(member: dict) -> str:
    """Pick the best display handle for a users.list / users.info member dict."""
    profile = member.get("profile") or {}
    name = (
        profile.get("display_name")
        or profile.get("real_name")
        or member.get("name")
        or member.get("id", "")
    )
    return f"@{name}"


def resolve_user_input(client: WebClient, user_input: str) -> tuple[str, str]:
    """Return (user_id, @display) for a raw --list-user argument.

    If `user_input` matches a Slack user ID pattern, look it up via users.info.
    Otherwise paginate users.list (cached) and match case-insensitively against
    display_name, real_name, and name. Exits the process with a clear message
    on zero or multiple matches, or on user_not_found from users.info.
    """
    if _USER_ID_PATTERN.match(user_input):
        try:
            resp = api_call(client.users_info, user=user_input)
        except SlackApiError as exc:
            error_code = exc.response.get("error", "")
            if error_code == "user_not_found":
                _spinner.stop()
                print(
                    f"ERROR: No user found with ID '{user_input}'.",
                    file=sys.stderr,
                )
                sys.exit(1)
            raise
        member = resp["user"]
        return member["id"], _user_display_from_member(member)

    _spinner.update(f"Looking up user '{user_input}'...")
    members = _fetch_all_users(client)

    needle = user_input.lstrip("@").casefold()

    matches: list[dict] = []
    for m in members:
        if m.get("deleted") or m.get("is_bot"):
            continue
        profile = m.get("profile") or {}
        candidates = (
            profile.get("display_name") or "",
            profile.get("real_name") or "",
            m.get("name") or "",
        )
        if any(c.casefold() == needle for c in candidates if c):
            matches.append(m)

    if not matches:
        _spinner.stop()
        print(
            f"ERROR: No user found matching '{user_input}'. "
            "Use --list-user with a Slack user ID (U...) for exact match.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(matches) > 1:
        _spinner.stop()
        shown = matches[:MAX_USER_MATCHES_SHOWN]
        formatted = ", ".join(
            f"{_user_display_from_member(m)} ({m.get('id', '')})" for m in shown
        )
        extra = len(matches) - len(shown)
        suffix = f", ... and {extra} more" if extra > 0 else ""
        print(
            f"ERROR: Multiple users match '{user_input}': {formatted}{suffix}. "
            "Please use the exact user ID.",
            file=sys.stderr,
        )
        sys.exit(1)

    member = matches[0]
    return member["id"], _user_display_from_member(member)


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

SLACK_CONNECT_ZERO_SUBTYPES = frozenset({"sh_room_created", "sh_room_shared"})


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


def _fetch_user_channel_rows(
    client: WebClient, user_id: str, spinner_label: str
) -> list[dict]:
    """Paginate users.conversations(user=...) and build rows via _row_for_channel.

    The returned channel dicts have the same shape as conversations.list, so
    `_row_for_channel` handles IMs, MPDMs, public/private channels, Slack
    Connect, and the `(archived)` suffix without modification.
    """
    rows: list[dict] = []
    cursor: str | None = None

    while True:
        kwargs: dict[str, Any] = {
            "user": user_id,
            "types": "im,mpim,public_channel,private_channel",
            "exclude_archived": False,
            "limit": MESSAGES_PER_PAGE,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.users_conversations, **kwargs)
        channels = resp.get("channels", [])

        for ch in channels:
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


def cmd_list_user(client: WebClient, user_input: str) -> None:
    """List every conversation the target user shares with the calling user."""
    _spinner.start(f"Looking up user '{user_input}'...")
    try:
        user_id, display = resolve_user_input(client, user_input)
        label = f"Loading conversations for {display}..."
        _spinner.update(label)
        rows = _fetch_user_channel_rows(client, user_id, spinner_label=label)
        _spinner.stop()
    finally:
        _spinner.stop()

    print(f"Channels for {display} ({user_id}):")
    _print_channel_table(rows, noun="channels")

    if rows:
        print()
        print(
            f"Note: only showing conversations that both you and {display} "
            "are members of."
        )
        print()
        print("To export a conversation, run:")
        print("  python slack_export.py --channel <ID> --from DD-MM-YYYY --to DD-MM-YYYY")


# ---------------------------------------------------------------------------
# Fetch thread replies
# ---------------------------------------------------------------------------


def fetch_replies(
    client: WebClient,
    channel: str,
    parent_ts: str,
    *,
    message_filter: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Fetch all replies for a thread (excludes the parent message at index 0)."""
    pred = message_filter if message_filter is not None else should_include
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
            if pred(msg):
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


def fetch_all_thread_replies(
    client: WebClient,
    channel: str,
    messages: list[dict],
    *,
    message_filter: Callable[[dict], bool] | None = None,
    spinner: Spinner | None = None,
) -> list[dict]:
    """Populate _replies for every threaded message; updates the spinner with i/total progress."""
    spin = spinner if spinner is not None else _spinner
    parents = [m for m in messages if m.get("reply_count", 0) > 0]
    total = len(parents)
    for i, msg in enumerate(parents, 1):
        spin.update(f"Fetching thread replies... {i}/{total} threads")
        msg["_replies"] = fetch_replies(
            client, channel, msg["ts"], message_filter=message_filter
        )
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
    *,
    message_filter: Callable[[dict], bool] | None = None,
    spinner: Spinner | None = None,
    progress_prefix: str | None = None,
) -> list[dict]:
    """
    Fetch all messages in [oldest, latest] from conversations.history.
    Returns a flat list of message dicts; thread replies are embedded under
    each parent as msg["_replies"].
    """
    pred = message_filter if message_filter is not None else should_include
    spin = spinner if spinner is not None else _spinner
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

        included = [m for m in messages if pred(m)]
        for msg in included:
            msg["_replies"] = []
            all_messages.append(msg)

        if progress_prefix:
            spin.update(f"{progress_prefix} — {len(all_messages)} fetched")
        else:
            spin.update(f"Fetching messages... {len(all_messages)} fetched")

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
# Diary dry-run (phase 1)
# ---------------------------------------------------------------------------

DIARY_ALL_MESSAGES: Callable[[dict], bool] = lambda _m: True

_URL_ONLY_RE = re.compile(r"https?://[^\s<>()\[\]{}]+")
_EMOJI_SHORTCODE_RE = re.compile(r"^:[a-z0-9_+-]+:$")


def _dt_iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return s.replace("+00:00", "Z")


def _msg_iso_ts(ts_str: str) -> str:
    return _dt_iso_z(datetime.fromtimestamp(float(ts_str), tz=timezone.utc))


def diary_is_zero_quality(msg: dict) -> bool:
    if msg.get("subtype") == "bot_message" or msg.get("bot_id"):
        return True
    st = msg.get("subtype")
    if st in SKIP_SUBTYPES_SET or st in SLACK_CONNECT_ZERO_SUBTYPES:
        return True
    if st is not None and st not in INCLUDE_SUBTYPES:
        return True
    uid = msg.get("user")
    if uid and uid in KNOWN_BOT_USER_IDS:
        return True
    if msg.get("app_id") and not uid:
        return True
    return False


def _diary_core_text(msg: dict) -> str:
    return (msg.get("text") or "").strip()


def _diary_is_url_only(msg: dict) -> bool:
    s = _diary_core_text(msg)
    if not s:
        return False
    remainder = _URL_ONLY_RE.sub("", s).strip()
    return bool(_URL_ONLY_RE.search(s)) and remainder == ""


def _diary_is_single_emoji_text(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    if _EMOJI_SHORTCODE_RE.fullmatch(s):
        return True
    if any(c.isalnum() for c in s):
        return False
    return 1 <= len(s) <= 8


def _diary_file_share_no_text(msg: dict) -> bool:
    if msg.get("subtype") != "file_share":
        return False
    return len(_diary_core_text(msg)) == 0


def _count_raw_tree(messages: list[dict]) -> int:
    n = 0
    for m in messages:
        n += 1
        n += len(m.get("_replies", []))
    return n


def diary_filter_zero_quality(messages: list[dict]) -> tuple[list[dict], int]:
    """Remove zero-quality messages; returns (tree, num_dropped)."""
    dropped = 0
    out: list[dict] = []
    for msg in messages:
        if diary_is_zero_quality(msg):
            dropped += 1
            continue
        kept_replies: list[dict] = []
        for r in msg.get("_replies", []):
            if diary_is_zero_quality(r):
                dropped += 1
            else:
                kept_replies.append(r)
        msg["_replies"] = kept_replies
        out.append(msg)
    return out, dropped


def diary_classify_message(
    msg: dict,
    *,
    channel_is_dm: bool,
    is_reply: bool,
    parent_filtered_reply_count: int,
) -> str:
    if channel_is_dm:
        return "high"
    if _diary_file_share_no_text(msg):
        return "low"
    if _diary_is_url_only(msg):
        return "low"
    if _diary_is_single_emoji_text(msg.get("text") or ""):
        return "low"
    text_len = len(_diary_core_text(msg))
    if not is_reply:
        if parent_filtered_reply_count >= 3:
            return "high"
        if text_len < 15:
            return "low"
        if text_len <= 100:
            return "medium"
        return "high"
    if parent_filtered_reply_count >= 3:
        if text_len < 15:
            return "low"
        if text_len <= 100:
            return "medium"
        return "high"
    if text_len < 15:
        return "low"
    if text_len <= 100:
        return "medium"
    return "high"


def diary_apply_signal_labels(
    messages: list[dict],
    *,
    channel_is_dm: bool,
) -> None:
    for msg in messages:
        nrep = len(msg.get("_replies", []))
        msg["_signal_quality"] = diary_classify_message(
            msg,
            channel_is_dm=channel_is_dm,
            is_reply=False,
            parent_filtered_reply_count=nrep,
        )
        for reply in msg.get("_replies", []):
            reply["_signal_quality"] = diary_classify_message(
                reply,
                channel_is_dm=channel_is_dm,
                is_reply=True,
                parent_filtered_reply_count=nrep,
            )


def _flatten_signal_counts(
    messages: list[dict],
) -> tuple[int, int, int, int, set[str], bool]:
    """Returns high_ct, medium_ct, low_ct, total, human_authors, has_threads."""
    high_ct = medium_ct = low_ct = 0
    authors: set[str] = set()
    has_threads = False
    total = 0

    def walk(msg: dict) -> None:
        nonlocal high_ct, medium_ct, low_ct, has_threads, total
        total += 1
        uid = msg.get("user")
        if uid:
            authors.add(uid)
        sq = msg.get("_signal_quality", "low")
        if sq == "high":
            high_ct += 1
        elif sq == "medium":
            medium_ct += 1
        else:
            low_ct += 1
        replies = msg.get("_replies", [])
        if replies:
            has_threads = True
        for r in replies:
            walk(r)

    for m in messages:
        walk(m)
    return high_ct, medium_ct, low_ct, total, authors, has_threads


def _deep_thread_reply_max(messages: list[dict]) -> int:
    best = 0
    for msg in messages:
        n = len(msg.get("_replies", []))
        if n > best:
            best = n
    return best


def diary_classify_conversation(
    messages: list[dict],
    *,
    channel_type: str,
    auth_user_id: str,
    dm_peer_id: str | None,
) -> tuple[str, str]:
    """Return (tier, reason) with tier in high|medium|low|skip."""
    if not messages:
        return "skip", "all messages filtered as zero quality"

    high_ct, medium_ct, low_ct, total, authors, has_threads = _flatten_signal_counts(
        messages
    )

    # --- high tier (first match wins) ---
    if channel_type == TYPE_DM and dm_peer_id:
        if auth_user_id in authors and dm_peer_id in authors:
            return "high", "1:1 DM with messages from both participants"
    if len(authors) >= 3:
        return "high", "3+ distinct human authors"
    if _deep_thread_reply_max(messages) >= 3:
        return "high", "thread with 3+ replies"
    if high_ct >= 3:
        return "high", "3+ high-quality messages"

    # --- medium ---
    if high_ct >= 1:
        return "medium", "contains at least one high-quality message"
    if medium_ct >= 5:
        return "medium", "5+ medium-quality messages"
    if channel_type == TYPE_MPDM and len(authors) >= 2:
        return "medium", "MPDM with 2+ active participants"

    # --- low ---
    if low_ct == total:
        return "low", "only low-quality messages"
    if total == 1:
        return "low", "single message in window"
    if len(authors) <= 1:
        return "low", "single author"

    return "low", "default"


def fetch_diary_active_conversations(
    client: WebClient,
    window_start_ts: float,
    spinner: Spinner,
) -> tuple[list[dict], int]:
    """Paginate conversations.list (recent-first) and stop when `updated` < window.

    For 1:1 DMs (`im`), Slack's `updated` can lag behind real message activity.
    If dry-run omits DMs that clearly had traffic in the window, investigate here
    before changing filters or stop rules.
    """
    active: list[dict] = []
    scanned = 0
    cursor: str | None = None
    types = "im,public_channel,private_channel,mpim"

    while True:
        kwargs: dict[str, Any] = {
            "types": types,
            "limit": MESSAGES_PER_PAGE,
            "exclude_archived": True,
        }
        if cursor:
            kwargs["cursor"] = cursor

        resp = api_call(client.conversations_list, **kwargs)
        channels = resp.get("channels", [])

        stop_paging = False
        for ch in channels:
            scanned += 1
            updated_ms = ch.get("updated") or 0
            updated_sec = float(updated_ms) / 1000.0
            if updated_sec < window_start_ts:
                print(
                    f"Stopped scanning: conversation {ch.get('id', '?')} "
                    "has updated timestamp before window start",
                    file=sys.stderr,
                )
                stop_paging = True
                break
            active.append(ch)
            spinner.update(f"Scanning conversations... {len(active)} active")

        if stop_paging:
            break

        next_cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(REQUEST_DELAY)

    return active, scanned


def _diary_first_top_level_ts(messages: list[dict]) -> float:
    if not messages:
        return 0.0
    return min(float(m["ts"]) for m in messages)


def diary_collect_user_ids(messages: list[dict]) -> set[str]:
    ids: set[str] = set()

    def walk(m: dict) -> None:
        uid = m.get("user")
        if uid:
            ids.add(uid)
        for r in m.get("_replies", []):
            walk(r)

    for m in messages:
        walk(m)
    return ids


def diary_serialize_messages(
    messages: list[dict],
    uid_to_display: dict[str, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in sorted(messages, key=lambda m: float(m["ts"])):
        uid = msg.get("user") or ""
        handle = uid_to_display.get(uid, uid or "unknown")
        author = f"@{handle}" if uid else "@unknown"
        entry: dict[str, Any] = {
            "ts": _msg_iso_ts(msg["ts"]),
            "author": author,
            "text": render_text(msg),
            "_signal_quality": msg.get("_signal_quality", "low"),
        }
        replies = msg.get("_replies", [])
        if replies:
            entry["_replies"] = diary_serialize_messages(replies, uid_to_display)
        result.append(entry)
    return result


def cmd_diary_dry_run(
    client: WebClient,
    diary_arg: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> None:
    now_utc = datetime.now(tz=timezone.utc)

    if from_date or to_date:
        if diary_arg != DIARY_ROLLING:
            print(
                "ERROR: cannot combine a positional diary date with "
                "--from/--to. Use one or the other.",
                file=sys.stderr,
            )
            sys.exit(1)

        if to_date:
            end_dt = parse_date(to_date).replace(
                hour=23, minute=59, second=59, microsecond=0
            )
        else:
            end_dt = now_utc

        if from_date:
            start_dt = parse_date(from_date)
        else:
            start_dt = parse_date(to_date)  # --to only

        if from_date and to_date and start_dt > end_dt:
            print("ERROR: --from date must be before --to date.", file=sys.stderr)
            sys.exit(1)

        diary_date_str = (
            f"{start_dt.strftime('%d-%m-%Y')}–{end_dt.strftime('%d-%m-%Y')}"
        )
    elif diary_arg != DIARY_ROLLING:
        end_dt = parse_date(diary_arg).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        start_dt = parse_date(diary_arg)
        diary_date_str = diary_arg
    else:
        end_dt = now_utc
        start_dt = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        diary_date_str = end_dt.strftime("%d-%m-%Y")

    is_today = diary_arg == DIARY_ROLLING and not from_date and not to_date

    oldest = start_dt.timestamp()
    latest = end_dt.timestamp()
    window_start_ts = oldest

    auth_resp = api_call(client.auth_test)
    auth_user_id = str(auth_resp.get("user_id", ""))

    _diary_spinner.start("Scanning conversations...")
    try:
        active_chs, scanned_total = fetch_diary_active_conversations(
            client, window_start_ts, _diary_spinner
        )
        _diary_spinner.stop()
    finally:
        _diary_spinner.stop()

    print(f"Diary scan: {scanned_total} conversation(s) scanned.", file=sys.stderr)

    conv_work: list[dict[str, Any]] = []
    total_messages_raw = 0
    active_with_messages = 0

    n_candidates = len(active_chs)
    for idx, ch in enumerate(active_chs, 1):
        cid = ch["id"]
        info = resolve_channel_info(client, cid)
        label = info["display_name"]
        ch_type = info["type"]
        raw_ch = info.get("raw") or {}
        dm_peer_id = str(raw_ch.get("user") or "") if ch_type == TYPE_DM else None

        prefix = f"Fetching messages... {label} ({idx}/{n_candidates} channels)"
        _diary_spinner.start(prefix)
        try:
            msgs = fetch_history(
                client,
                cid,
                oldest,
                latest,
                message_filter=DIARY_ALL_MESSAGES,
                spinner=_diary_spinner,
                progress_prefix=prefix,
            )
            time.sleep(REQUEST_DELAY)

            if not msgs:
                print(
                    f"Diary: skip {label} — no raw messages in window.",
                    file=sys.stderr,
                )
                continue

            active_with_messages += 1

            fetch_all_thread_replies(
                client,
                cid,
                msgs,
                message_filter=DIARY_ALL_MESSAGES,
                spinner=_diary_spinner,
            )

            raw_ct = _count_raw_tree(msgs)
            total_messages_raw += raw_ct

            filtered, dropped = diary_filter_zero_quality(msgs)

            print(
                f"Diary: {label} — raw messages {raw_ct}, dropped (zero-quality) {dropped}.",
                file=sys.stderr,
            )

            if not filtered:
                print(
                    f"Diary: skip {label} — no messages remain after filtering.",
                    file=sys.stderr,
                )
                continue

            channel_is_dm = ch_type == TYPE_DM
            diary_apply_signal_labels(filtered, channel_is_dm=channel_is_dm)

            tier, reason = diary_classify_conversation(
                filtered,
                channel_type=ch_type,
                auth_user_id=auth_user_id,
                dm_peer_id=dm_peer_id,
            )
            print(
                f"Diary: {label} — conversation tier {tier} ({reason}).",
                file=sys.stderr,
            )

            high_ct, _med_ct, _low_ct, msg_total, authors, has_threads = (
                _flatten_signal_counts(filtered)
            )

            conv_work.append(
                {
                    "_tier": tier,
                    "_sort_ts": _diary_first_top_level_ts(filtered),
                    "channel_id": cid,
                    "channel_name": label,
                    "channel_type": ch_type,
                    "signal_quality": {
                        "tier": tier,
                        "reason": reason,
                        "message_count": msg_total,
                        "human_authors": len(authors),
                        "high_quality_messages": high_ct,
                        "has_threads": has_threads,
                    },
                    "_filtered_messages": filtered,
                }
            )
        finally:
            _diary_spinner.stop()
            time.sleep(REQUEST_DELAY)

    conv_work = [c for c in conv_work if c["_tier"] != "skip"]

    tier_rank = {"high": 0, "medium": 1, "low": 2}
    conv_work.sort(
        key=lambda c: (tier_rank.get(c["_tier"], 9), c["_sort_ts"]),
    )

    all_uids: set[str] = set()
    for c in conv_work:
        all_uids |= diary_collect_user_ids(c["_filtered_messages"])

    uid_to_display: dict[str, str] = {}
    uid_list = sorted(all_uids)
    if uid_list:
        _diary_spinner.start("Resolving usernames...")
        try:
            total_u = len(uid_list)
            for i, uid in enumerate(uid_list, 1):
                _diary_spinner.update(f"Resolving usernames... {i}/{total_u} users")
                disp = resolve_user(client, uid)
                uid_to_display[uid] = disp[1:] if disp.startswith("@") else disp
            _diary_spinner.stop()
        finally:
            _diary_spinner.stop()

    conv_payloads: list[dict[str, Any]] = []
    total_messages_filtered = 0

    for c in conv_work:
        authors = diary_collect_user_ids(c["_filtered_messages"])
        participant_labels = sorted(
            f"@{uid_to_display.get(u, u)}" for u in authors
        )
        messages_json = diary_serialize_messages(c["_filtered_messages"], uid_to_display)
        conv_payloads.append(
            {
                "channel_id": c["channel_id"],
                "channel_name": c["channel_name"],
                "channel_type": c["channel_type"],
                "signal_quality": c["signal_quality"],
                "participants": participant_labels,
                "messages": messages_json,
            }
        )
        total_messages_filtered += c["signal_quality"]["message_count"]

    high_convos = sum(1 for c in conv_payloads if c["signal_quality"]["tier"] == "high")
    med_convos = sum(1 for c in conv_payloads if c["signal_quality"]["tier"] == "medium")
    low_convos = sum(1 for c in conv_payloads if c["signal_quality"]["tier"] == "low")

    payload = {
        "diary_date": diary_date_str,
        "window": {
            "start": _dt_iso_z(start_dt),
            "end": _dt_iso_z(end_dt),
        },
        "conversations_scanned": scanned_total,
        "conversations_with_activity": active_with_messages,
        "conversations_after_filtering": len(conv_payloads),
        "total_messages_raw": total_messages_raw,
        "total_messages_after_filtering": total_messages_filtered,
        "conversations": conv_payloads,
    }

    # Phase 2: align diaries/diary_{date}.md naming with single-day vs range (match snapshot stem).
    if is_today or start_dt.date() == end_dt.date():
        snapshot_stem = end_dt.strftime("%Y-%m-%d")
        snapshot_path = SNAPSHOTS_DIR / f"diary_raw_{snapshot_stem}.json"
    else:
        snapshot_path = SNAPSHOTS_DIR / (
            f"diary_raw_{start_dt.strftime('%Y-%m-%d')}_"
            f"{end_dt.strftime('%Y-%m-%d')}.json"
        )

    json_text = json.dumps(payload, indent=2) + "\n"
    SNAPSHOTS_DIR.mkdir(exist_ok=True)
    snapshot_path.write_text(json_text, encoding="utf-8")
    sys.stdout.write(json_text)

    print("", file=sys.stderr)
    print(f"Written: {snapshot_path}", file=sys.stderr)
    print("Diary dry run complete.", file=sys.stderr)
    print(f"  Date:                {diary_date_str}", file=sys.stderr)
    print(f"  Conversations scanned: {scanned_total}", file=sys.stderr)
    print(f"  Active (with messages): {active_with_messages}", file=sys.stderr)
    print(f"  After filtering:       {len(conv_payloads)}", file=sys.stderr)
    print(f"  Total messages (raw):  {total_messages_raw}", file=sys.stderr)
    print(f"  After filtering:       {total_messages_filtered}", file=sys.stderr)
    print(f"  High quality convos:   {high_convos}", file=sys.stderr)
    print(f"  Medium quality convos: {med_convos}", file=sys.stderr)
    print(f"  Low quality convos:    {low_convos}", file=sys.stderr)


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
            "  python slack_export.py --list-user U0123ABCDEF\n"
            "  python slack_export.py --list-user \"Alice Johnson\"\n"
            "  python slack_export.py --channel C0123ABCDEF --from 01-01-2025 --to 30-06-2025\n"
            "  python slack_export.py --channel G0999XYZABC            # MPDM, last 30 days\n"
            "  python slack_export.py --channel D0123ABCDEF\n"
            "  python slack_export.py --diary --dry-run\n"
            "  python slack_export.py --diary 24-04-2026 --dry-run   # snapshots/diary_raw_YYYY-MM-DD.json\n"
            "  python slack_export.py --diary --from 01-04-2026 --to 24-04-2026 --dry-run\n"
            "  python slack_export.py --diary --from 01-04-2026 --dry-run\n"
            "  python slack_export.py --diary --from 24-04-2026 --to 24-04-2026 --dry-run  # same as --diary 24-04-2026\n"
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
        "--list-user",
        dest="list_user",
        metavar="USER",
        help=(
            "List every conversation a specific user shares with you. "
            "USER is a Slack user ID (U...) or a name/@handle "
            "(matched case-insensitively against display_name, real_name, name)."
        ),
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
    parser.add_argument(
        "--diary",
        nargs="?",
        const=DIARY_ROLLING,
        default=None,
        metavar="DD-MM-YYYY",
        help=(
            "Dry-run diary JSON to stdout and snapshots/diary_raw_*.json (requires --dry-run). "
            "Optional calendar day DD-MM-YYYY (UTC full day); omit value for today 00:00 UTC through now. "
            "Use --from / --to for ranges (not with a positional diary date)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "With --diary: write classified payload JSON to stdout and snapshots/ "
            "(phase 1; no LLM)."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    diary_mode = args.diary is not None

    list_flags = [args.list_all, args.list_channels, args.list_dms, bool(args.list_user)]
    if sum(1 for f in list_flags if f) > 1:
        print(
            "ERROR: Pass only one of --list, --list-channels, --list-dms, --list-user.",
            file=sys.stderr,
        )
        sys.exit(2)

    if diary_mode:
        if args.channel:
            print(
                "ERROR: --diary cannot be combined with --channel.",
                file=sys.stderr,
            )
            sys.exit(2)
        if any(list_flags):
            print(
                "ERROR: --diary cannot be combined with --list, --list-channels, "
                "--list-dms, or --list-user.",
                file=sys.stderr,
            )
            sys.exit(2)
        if args.type_filter:
            print(
                "ERROR: --type only applies to --list or --list-channels.",
                file=sys.stderr,
            )
            sys.exit(2)
        if not args.dry_run:
            print(
                "ERROR: Diary generation requires --dry-run in this phase; "
                "LLM integration is not built yet.",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.dry_run and not diary_mode:
        print(
            "ERROR: --dry-run is only supported with --diary in this phase.",
            file=sys.stderr,
        )
        sys.exit(2)

    if not any(list_flags) and not args.channel and not diary_mode:
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
        if diary_mode:
            cmd_diary_dry_run(
                client,
                args.diary,
                args.from_date,
                args.to_date,
            )
            return
        if args.list_all:
            cmd_list(client, type_filter=args.type_filter)
            return
        if args.list_channels:
            cmd_list_channels(client, type_filter=args.type_filter)
            return
        if args.list_dms:
            cmd_list_dms(client)
            return
        if args.list_user:
            cmd_list_user(client, args.list_user)
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
        _diary_spinner.stop()
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
