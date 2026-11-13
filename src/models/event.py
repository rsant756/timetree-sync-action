from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo


@dataclass
class Event:
    SYNC_SOURCE: ClassVar[str] = "timetree-sync-action"

    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    location: str | None = None
    description: str | None = None

    @classmethod
    def from_timetree(cls, event: dict):
        start_tz = ZoneInfo(event["start_timezone"])
        end_tz = ZoneInfo(event["end_timezone"])

        return cls(
            id=event["id"],
            title=event["title"],
            start=datetime.fromtimestamp(
                event["start_at"] / 1000,
                tz=start_tz,
            ),
            end=datetime.fromtimestamp(
                event["end_at"] / 1000,
                tz=end_tz,
            ),
            all_day=event["all_day"],
            location=event.get("location") or None,
            description=event.get("note") or None,
        )

    def to_google(self, calendar_code: str) -> dict:
        event = {
            "summary": self.title,
            "extendedProperties": {
                "private": {
                    "sync_source": self.SYNC_SOURCE,
                    "timetree_calendar_code": calendar_code,
                    "timetree_id": self.id,
                }
            },
        }

        if self.all_day:
            event["start"] = {
                "date": self.start.strftime("%Y-%m-%d"),
            }
            event["end"] = {
                # TimeTree includes the last day; Google uses an exclusive end date.
                "date": (self.end.date() + timedelta(days=1)).isoformat(),
            }
        else:
            event["start"] = {
                "dateTime": self.start.isoformat(),
                "timeZone": "Asia/Tokyo",
            }
            event["end"] = {
                "dateTime": self.end.isoformat(),
                "timeZone": "Asia/Tokyo",
            }

        if self.location:
            event["location"] = self.location

        if self.description:
            event["description"] = self.description

        return event

    def equals_google(self, google_event: dict, calendar_code: str) -> bool:
        google = self.to_google(calendar_code)

        return (
            google.get("summary") == google_event.get("summary")
            and google.get("location") == google_event.get("location")
            and google.get("description") == google_event.get("description")
            and google.get("start") == google_event.get("start")
            and google.get("end") == google_event.get("end")
        )
