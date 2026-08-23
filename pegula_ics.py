#!/usr/bin/env python3
"""Build a subscribable .ics from the Pegula Ice Arena rink calendar.

The schedule page embeds the whole event list as JSON in a `_onlineScheduleList`
variable, so there is no API call to reverse-engineer and no login needed. One
request returns roughly five weeks of events for both rinks.

    python pegula_ics.py                    # write public_skate.ics
    python pegula_ics.py --list-types       # see every event type available
    python pegula_ics.py --print --all      # dump everything without filtering
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    RINK_TZ = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - missing tzdata
    RINK_TZ = timezone(timedelta(hours=-4))

SCHEDULE_URL = "https://pegula.finnlyconnect.com/schedule/411"
USER_AGENT = "pegula-ics/1.0 (personal calendar feed)"

# Matched case-insensitively against EventTypeName.
EVENT_TYPES = ["Public Skating", "Adult Open Skate", "Family Skate Night"]

CALENDAR_NAME = "Pegula Public Skate"
CALENDAR_DESC = "Public skate sessions at Pegula Ice Arena"
DEFAULT_OUT = "public_skate.ics"

# Events that ended more than this long ago are dropped.
PAST_WINDOW = timedelta(hours=12)

_EVENT_LIST = re.compile(r"_onlineScheduleList\s*=\s*(\[.*?\])\s*;", re.DOTALL)


def fetch_page(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
        return response.read().decode("utf-8", "replace")


def extract_records(html: str) -> list[dict]:
    match = _EVENT_LIST.search(html)
    if not match:
        raise SystemExit(
            "Could not find _onlineScheduleList in the page. The site markup probably changed."
        )
    return json.loads(match.group(1))


def local(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=RINK_TZ)


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > 75:
            parts.append(current.decode("utf-8"))
            current = b" "
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n".join(parts)


def build_ics(records: list[dict]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//pegula-ics//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape(CALENDAR_NAME)}",
        f"X-WR-CALDESC:{escape(CALENDAR_DESC)}",
        "X-WR-TIMEZONE:America/New_York",
        "X-PUBLISHED-TTL:PT6H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
    ]
    for record in records:
        start = utc(local(record["EventStartTime"]))
        end = utc(local(record["EventEndTime"]))
        rink = record.get("FacilityName") or ""
        summary = record.get("EventTypeName") or "Skate"
        if rink:
            summary = f"{summary} - {rink}"

        notes = [
            record.get("Description") or "",
            record.get("ScheduleNotes") or "",
            record.get("AccountName") or "",
        ]
        description = " | ".join(n.strip() for n in notes if n.strip())

        lines += [
            "BEGIN:VEVENT",
            f"UID:finnly-{record['EventId']}@pegula.finnlyconnect.com",
            # Deterministic DTSTAMP keeps the file byte-stable so the daily
            # commit only fires when the schedule actually changed.
            f"DTSTAMP:{start}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{escape(summary)}",
        ]
        if rink:
            lines.append(f"LOCATION:{escape('Pegula Ice Arena - ' + rink)}")
        if description:
            lines.append(f"DESCRIPTION:{escape(description)}")
        lines += ["STATUS:CONFIRMED", "TRANSP:TRANSPARENT", "END:VEVENT"]

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output path (default {DEFAULT_OUT})")
    parser.add_argument("--html-file", help="parse a saved page instead of fetching")
    parser.add_argument("--types", help="comma-separated event types, overriding EVENT_TYPES")
    parser.add_argument("--all", action="store_true", help="keep every event type")
    parser.add_argument("--rink", help="only keep this rink, e.g. Community")
    parser.add_argument("--list-types", action="store_true", help="print event types with counts and exit")
    parser.add_argument("--print", dest="do_print", action="store_true", help="print the events being written")
    parser.add_argument("--no-write", action="store_true", help="parse only")
    args = parser.parse_args(argv)

    if args.html_file:
        with open(args.html_file, "r", encoding="utf-8-sig", errors="replace") as handle:
            html = handle.read()
    else:
        html = fetch_page(SCHEDULE_URL)

    records = extract_records(html)
    print(f"Fetched {len(records)} events.", file=sys.stderr)

    if args.list_types:
        for name, count in Counter(r["EventTypeName"] for r in records).most_common():
            print(f"{count:5d}  {name}")
        return 0

    if not args.all:
        wanted = [t.strip().lower() for t in (args.types.split(",") if args.types else EVENT_TYPES) if t.strip()]
        records = [r for r in records if (r.get("EventTypeName") or "").strip().lower() in wanted]
        if not records:
            print("Nothing matched. Run --list-types to see what's available.", file=sys.stderr)
            return 1

    if args.rink:
        needle = args.rink.lower()
        records = [r for r in records if needle in (r.get("FacilityName") or "").lower()]

    cutoff = datetime.now(timezone.utc) - PAST_WINDOW
    records = [r for r in records if local(r["EventEndTime"]) >= cutoff]
    records.sort(key=lambda r: (r["EventStartTime"], r.get("FacilityName") or ""))

    if args.do_print:
        for record in records:
            start = local(record["EventStartTime"])
            end = local(record["EventEndTime"])
            print(
                f"{start:%a %b %d} {start:%I:%M %p}-{end:%I:%M %p}  "
                f"{record['EventTypeName']}  [{record.get('FacilityName')}]"
            )

    if args.no_write:
        return 0

    ics = build_ics(records)
    try:
        with open(args.out, "r", encoding="utf-8", newline="") as handle:
            changed = handle.read() != ics
    except OSError:
        changed = True

    with open(args.out, "w", encoding="utf-8", newline="") as handle:
        handle.write(ics)

    print(
        f"Wrote {len(records)} events to {args.out} ({'updated' if changed else 'unchanged'}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
