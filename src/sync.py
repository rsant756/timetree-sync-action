"""Sync TimeTree events from this week onward; preview deletions only."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import Config
from gcalendar import GoogleCalendarClient
from logger import logger
from models import Event
from timetree import TimeTree


def _private_properties(google_event: dict) -> dict:
    return google_event.get("extendedProperties", {}).get("private", {})


def _is_managed_google_event(google_event: dict, calendar_code: str) -> bool:
    props = _private_properties(google_event)
    timetree_id = props.get("timetree_id")
    if not timetree_id:
        return False
    sync_source = props.get("sync_source")
    source_calendar_code = props.get("timetree_calendar_code")
    if sync_source is None and source_calendar_code is None:
        return True
    return sync_source == Event.SYNC_SOURCE and source_calendar_code == calendar_code


def _google_event_end(google_event: dict):
    data = google_event.get("end") or {}
    value = data.get("dateTime") or data.get("date")
    if not value:
        return None
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return result


def sync():
    client = TimeTree()
    client.login(Config.TIMETREE_EMAIL, Config.TIMETREE_PASSWORD)
    calendar = client.get_calendar(Config.TIMETREE_CALENDAR_CODE)
    logger.info("Selected TimeTree calendar")

    raw_events = client.get_events(calendar)
    if not raw_events:
        raise RuntimeError("TimeTree returned zero events; aborting as a safety precaution")

    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    events = [
        event for raw in raw_events
        if (event := Event.from_timetree(raw)).end > week_start
    ]
    timetree_ids = {event.id for event in events}
    if len(timetree_ids) != len(events):
        raise RuntimeError("Duplicate TimeTree event IDs detected; aborting")
    logger.info("Filtered events: %d -> %d (from %s)",
                len(raw_events), len(events), week_start.date())

    google = GoogleCalendarClient(Config.GOOGLE_SERVICE_ACCOUNT_JSON)
    logger.info("Connected to Google Calendar")
    google_events = google.list_events(Config.GOOGLE_CALENDAR_ID)
    google_event_map: dict[str, list[dict]] = {}
    for google_event in google_events:
        if not _is_managed_google_event(google_event, Config.TIMETREE_CALENDAR_CODE):
            continue
        timetree_id = _private_properties(google_event)["timetree_id"]
        google_event_map.setdefault(timetree_id, []).append(google_event)

    created_count = updated_count = skipped_count = 0
    deletion_candidates = []
    for event in events:
        matches = google_event_map.get(event.id, [])
        if not matches:
            google.create_event(
                Config.GOOGLE_CALENDAR_ID,
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            created_count += 1
            continue

        google_event = matches[0]
        props = _private_properties(google_event)
        needs_marker_migration = (
            props.get("sync_source") != Event.SYNC_SOURCE
            or props.get("timetree_calendar_code") != Config.TIMETREE_CALENDAR_CODE
        )
        if needs_marker_migration or not event.equals_google(
            google_event, Config.TIMETREE_CALENDAR_CODE
        ):
            google.update_event(
                Config.GOOGLE_CALENDAR_ID,
                google_event["id"],
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            updated_count += 1
        else:
            skipped_count += 1

        # Dry-run: never delete duplicates until explicitly reviewed.
        for duplicate in matches[1:]:
            deletion_candidates.append(("duplicate", duplicate))

    # Dry-run: events absent from TimeTree are candidates only if their
    # Google end date falls within the sync window.
    for timetree_id, matches in google_event_map.items():
        if timetree_id in timetree_ids:
            continue
        for google_event in matches:
            google_end = _google_event_end(google_event)
            if google_end is not None and google_end > week_start:
                deletion_candidates.append(("missing_from_source", google_event))

    for reason, candidate in deletion_candidates:
        logger.info("DRY RUN - would delete Google event: reason=%s id=%s",
                    reason, candidate["id"])

    logger.info(
        "Sync complete: created=%d updated=%d deleted=0 skipped=%d deletion_candidates=%d",
        created_count, updated_count, skipped_count, len(deletion_candidates),
    )
