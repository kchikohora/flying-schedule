#!/usr/bin/env python3
"""
parse_roster.py - Converts Ryanair eCrew roster PDF to Kudzi's ICS flying schedule.

Triggered by GitHub Actions when Print Roster.pdf is pushed to the repo.
Uses Claude API to intelligently parse the roster and generate family-friendly events.
"""

import os
import re
import sys
from datetime import date

import anthropic
import pdfplumber

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ICS_FILE = "Kudzi-Flying-Schedule.ics"
PDF_FILE = "Print Roster.pdf"

CALENDAR_HEADER = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Kudzi Ryanair Roster//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
X-WR-CALNAME:Kudzi's Flying Schedule
X-WR-CALDESC:Kudzi's Ryanair flying schedule"""

# Fixed bank holidays — rebuilt fresh every run so they're always correct
BANK_HOLIDAYS_2026 = [
    ("20260101", "20260102", "🇬🇧 New Year's Day"),
    ("20260403", "20260404", "🇬🇧 Good Friday"),
    ("20260406", "20260407", "🇬🇧 Easter Monday"),
    ("20260504", "20260505", "🇬🇧 Early May Bank Holiday"),
    ("20260525", "20260526", "🇬🇧 Spring Bank Holiday"),
    ("20260831", "20260901", "🇬🇧 Summer Bank Holiday"),
    ("20261225", "20261226", "🇬🇧 Christmas Day"),
    ("20261228", "20261229", "🇬🇧 Boxing Day (substitute)"),
    # 2027
    ("20270101", "20270102", "🇬🇧 New Year's Day"),
]

# Annual leave — confirmed dates, always included
ANNUAL_LEAVE = [
    ("20260907", "20260912", "🏖️ Annual Leave", "20260907-leave@kudzi-roster"),
    ("20261225", "20261226", "🏖️ Annual Leave", "20261225-leave@kudzi-roster"),
    ("20261228", "20270101", "🏖️ Annual Leave", "20261228-leave@kudzi-roster"),
]

# ── PDF Extraction ─────────────────────────────────────────────────────────────
def extract_pdf_text(pdf_path: str) -> str:
    text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    return text


# ── ICS Parsing ────────────────────────────────────────────────────────────────
def parse_events_from_ics(ics_content: str) -> dict:
    """Returns dict of {uid: full_event_text}"""
    events = {}
    for match in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_content, re.DOTALL):
        body = match.group(1)
        uid_match = re.search(r"UID:(.+)", body)
        if uid_match:
            uid = uid_match.group(1).strip()
            events[uid] = "BEGIN:VEVENT" + body + "END:VEVENT"
    return events


def get_event_start_date(event_text: str) -> date | None:
    m = re.search(r"DTSTART(?:;VALUE=DATE)?:(\d{8})", event_text)
    if m:
        s = m.group(1)
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None


def is_duty_uid(uid: str) -> bool:
    """True if this is a duty event (not a bank holiday or annual leave)"""
    return (
        uid.endswith("@kudzi-roster")
        and not uid.endswith("-bh@kudzi-roster")
        and not uid.endswith("-leave@kudzi-roster")
    )


# ── Claude API ─────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You convert a Ryanair eCrew roster into ICS VEVENT blocks for a family calendar.

Captain Kudzi Chikohora, base Newcastle (NCL). All roster times are Zulu (UTC).
BST = UTC+1 (late March to late October). DTSTART/DTEND use UTC with Z suffix.

OUTPUT: Raw VEVENT blocks only. No calendar wrapper, no markdown, no explanation.
Separate each event with a blank line.

═══ RULES BY DUTY TYPE ═══

OFF days → skip entirely. No event.

FLYING DAYS:
- SUMMARY (1-2 sectors): ✈️ FR{out}/{in} — {Destination}
- SUMMARY (3-4 sectors): ✈️ {N} Sectors — {Dest1} & {Dest2}
- DESCRIPTION:
  • "🌅 Up at ~HH:MM BST" if check-in before 07:00 BST
  • "Leave home: ~HH:MM BST" (1hr 45min before first dep)
  • "Check-in: HH:MM BST"
  • Each leg: "FR{NUM} {DEP} → {ARR}\\nDep: HH:MM BST  |  Arr: HH:MM BST\\nTrack: https://www.flightradar24.com/data/flights/fr{num-lowercase}"
  • "Home around: ~HH:MM BST" — operating = last NCL arrival +1hr; dead heading = NCL landing +40min
  • "💰 Worked day off" if IWOFF
  • "{N} sectors today" if 2+ sectors
  • "⚠️ Iris nursery drop-off needed (~08:30) — Kudzi not available" if Monday OR Tuesday AND (check-in before 08:30 OR away on night stop)
- URL: FR24 link for first flight
- UID: YYYYMMDD-flying@kudzi-roster

DEAD HEAD (DH positioning):
- Include in description, note "(positioning)" after flight number
- Still include FR24 link
- Home around = NCL landing +40min
- UID: YYYYMMDD-flying@kudzi-roster

NIGHT STOPS:
- Add "🌙 Night stop in {City}" to description
- Add "Returns home {date} — home around ~HH:MM BST"
- If 2+ consecutive nights away, ALSO create an all-day banner spanning the full trip:
  SUMMARY: ✈️ Away: {DayFrom>–{DayTo}, home around {returnDay} ~HH:MM BST
  DTSTART;VALUE=DATE / DTEND;VALUE=DATE
  UID: YYYYMMDD-trip@kudzi-roster

SD (Special Duty / Office):
- SUMMARY: 📋 Office Day — Newcastle (or Dublin if after DH to Dublin)
- DESCRIPTION: Newcastle\\n09:00–17:00 BST
- DTSTART: {date}T080000Z, DTEND: {date}T160000Z
- UID: YYYYMMDD-office@kudzi-roster

HSBY (Home Standby):
- SUMMARY: 🏠 Home Standby — on call until HH:MM BST
- DESCRIPTION: On call from home — available until HH:MM BST
- UID: YYYYMMDD-hsby@kudzi-roster

JURY DUTY:
- SUMMARY: ⚖️ Jury Duty
- All-day event
- UID: YYYYMMDD-juryduty@kudzi-roster

═══ GENERAL NOTES ═══
- Flight numbers in SUMMARY: uppercase (FR460). In FR24 URLs: lowercase (fr460).
- Never add "subject to change" or planning caveats.
- Days off (OFF) produce no event whatsoever.
"""


def generate_duty_events(roster_text: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                "Convert this Ryanair eCrew roster to ICS VEVENT blocks. "
                "Skip OFF days entirely.\n\n"
                f"{roster_text}"
            ),
        }],
    )
    return response.content[0].text


# ── Fixed Events ───────────────────────────────────────────────────────────────
def make_bank_holiday_events() -> dict:
    events = {}
    for start, end, name in BANK_HOLIDAYS_2026:
        uid = f"{start}-bh@kudzi-roster"
        events[uid] = (
            f"BEGIN:VEVENT\n"
            f"UID:{uid}\n"
            f"DTSTART;VALUE=DATE:{start}\n"
            f"DTEND;VALUE=DATE:{end}\n"
            f"SUMMARY:{name}\n"
            f"END:VEVENT"
        )
    return events


def make_annual_leave_events() -> dict:
    events = {}
    for start, end, name, uid in ANNUAL_LEAVE:
        events[uid] = (
            f"BEGIN:VEVENT\n"
            f"UID:{uid}\n"
            f"DTSTART;VALUE=DATE:{start}\n"
            f"DTEND;VALUE=DATE:{end}\n"
            f"SUMMARY:{name}\n"
            f"END:VEVENT"
        )
    return events


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set")
        sys.exit(1)

    if not os.path.exists(PDF_FILE):
        print(f"ERROR: {PDF_FILE} not found in repo root")
        sys.exit(1)

    # 1. Extract roster text
    print(f"📄 Extracting text from {PDF_FILE}...")
    roster_text = extract_pdf_text(PDF_FILE)
    print(f"   → {len(roster_text):,} chars extracted")

    # 2. Load historical events from existing ICS (past duties only)
    historical_events = {}
    if os.path.exists(ICS_FILE):
        with open(ICS_FILE, "r", encoding="utf-8") as f:
            existing_ics = f.read()
        all_existing = parse_events_from_ics(existing_ics)
        today = date.today()
        for uid, event in all_existing.items():
            if is_duty_uid(uid):
                event_date = get_event_start_date(event)
                if event_date and event_date < today:
                    historical_events[uid] = event
        print(f"📚 Preserved {len(historical_events)} historical duty events")

    # 3. Generate new duty events via Claude
    print("🤖 Calling Claude API to generate calendar events...")
    new_events_text = generate_duty_events(roster_text)
    new_duty_events = parse_events_from_ics(new_events_text)
    print(f"   → {len(new_duty_events)} duty events generated")

    # 4. Build fixed events
    bank_holidays = make_bank_holiday_events()
    annual_leave = make_annual_leave_events()

    # 5. Merge all events (new takes precedence over historical for same UID)
    all_events = {
        **historical_events,
        **new_duty_events,
        **bank_holidays,
        **annual_leave,
    }

    # 6. Sort by start date
    def sort_key(item):
        d = get_event_start_date(item[1])
        return d if d else date.max

    sorted_events = sorted(all_events.items(), key=sort_key)

    # 7. Write ICS
    lines = [CALENDAR_HEADER, ""]
    for _, event in sorted_events:
        lines.append(event)
        lines.append("")
    lines.append("END:VCALENDAR")

    ics_content = "\n".join(lines)
    with open(ICS_FILE, "w", encoding="utf-8") as f:
        f.write(ics_content)

    print(f"✅ Written {ICS_FILE} — {len(all_events)} total events")
    print(f"   ({len(historical_events)} historical + {len(new_duty_events)} new duties + "
          f"{len(bank_holidays)} bank holidays + {len(annual_leave)} annual leave)")


if __name__ == "__main__":
    main()
