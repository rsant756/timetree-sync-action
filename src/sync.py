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
    """Return True only for events owned by this TimeTree sync.

    Events created by older versions are also accepted when they contain a
    timetree_id but do not yet have the newer ownership markers.
    """
    props = _private_properties(google_event)
    timetree_id = props.get("timetree_id")

    if not timetree_id:
        return False

    sync_source = props.get("sync_source")
    source_calendar_code = props.get("timetree_calendar_code")

    # Backward compatibility for events created by older versions.
    if sync_source is None and source_calendar_code is None:
        return True

    return sync_source == Event.SYNC_SOURCE and source_calendar_code == calendar_code


def sync():
    client = TimeTree()
    client.login(
        Config.TIMETREE_EMAIL,
        Config.TIMETREE_PASSWORD,
    )
    calendar = client.get_calendar(
        Config.TIMETREE_CALENDAR_CODE,
    )
    logger.info("Selected TimeTree calendar")

    raw_events = client.get_events(calendar)

    # 日本時間で今週の月曜日 00:00 を計算
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    
    # 今週以降の予定のみ対象
    events = [
        event
        for raw in raw_events
        if (event := Event.from_timetree(raw)).end > week_start
    ]
    
    timetree_ids = {event.id for event in events}
    
    logger.info(
        "Filtered events: %d -> %d (from %s)",
        len(raw_events),
        len(events),
        week_start.date(),
    )

    google = GoogleCalendarClient(
        Config.GOOGLE_SERVICE_ACCOUNT_JSON,
    )
    logger.info("Connected to Google Calendar")

    google_events = google.list_events(
        Config.GOOGLE_CALENDAR_ID,
    )

    # Keep all Google copies for each TimeTree ID. This lets us clean up
    # duplicates left by older versions or interrupted sync runs.
    google_event_map: dict[str, list[dict]] = {}

    for google_event in google_events:
        if not _is_managed_google_event(
            google_event,
            Config.TIMETREE_CALENDAR_CODE,
        ):
            # Native Google Calendar events and events managed by another
            # calendar/workflow are intentionally ignored.
            continue

        timetree_id = _private_properties(google_event)["timetree_id"]
        google_event_map.setdefault(timetree_id, []).append(google_event)

    created_count = 0
    updated_count = 0
    deleted_count = 0
    skipped_count = 0

    for event in events:
        matches = google_event_map.get(event.id, [])
        if not matches:
            google.create_event(
                Config.GOOGLE_CALENDAR_ID,
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            created_count += 1
            continue

        # Keep one canonical Google event for this TimeTree event.
        google_event = matches[0]
        props = _private_properties(google_event)
        needs_marker_migration = (
            props.get("sync_source") != Event.SYNC_SOURCE
            or props.get("timetree_calendar_code") != Config.TIMETREE_CALENDAR_CODE
        )

        if needs_marker_migration or not event.equals_google(
            google_event,
            Config.TIMETREE_CALENDAR_CODE,
        ):
            google.update_event(
                Config.GOOGLE_CALENDAR_ID,
                google_event["id"],
                event.to_google(Config.TIMETREE_CALENDAR_CODE),
            )
            updated_count += 1
        else:
            skipped_count += 1

        # Remove duplicate Google copies carrying the same TimeTree ID.
        for duplicate in matches[1:]:
            google.delete_event(
                Config.GOOGLE_CALENDAR_ID,
                duplicate["id"],
            )
            deleted_count += 1

    # Delete Google events owned by this sync when the source TimeTree event
    # no longer exists. Native Google Calendar events are never included in
    # google_event_map, so they are left untouched.
    for timetree_id, matches in google_event_map.items():
        if timetree_id in timetree_ids:
            continue
    
        for google_event in matches:
            end_data = google_event.get("end", {})
            end_value = end_data.get("dateTime") or end_data.get("date")
    
            if not end_value:
                continue
    
            google_end = datetime.fromisoformat(
                end_value.replace("Z", "+00:00")
            )
    
            if google_end.tzinfo is None:
                google_end = google_end.replace(
                    tzinfo=ZoneInfo("Asia/Tokyo")
                )
    
            if google_end <= week_start:
                continue
    
            # 初回テスト中は削除を無効化
            logger.info(
                "Skipping deletion during initial test: %s",
                google_event["id"],
            )

    logger.info(
        "Sync complete: created=%d updated=%d deleted=%d skipped=%d",
        created_count,
        updated_count,
        deleted_count,
        skipped_count,
    )
