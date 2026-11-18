"""thin wrapper around brightspace's rest api (aka "valence")

the official way needs an app key from the school (not happening). but the d2l web ui
itself calls these same /d2l/api/... routes using ur browser cookies — so we do the same.
we only ever do GETs (read-only), so no xsrf token needed.
"""
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from . import auth, config


class AuthExpired(Exception):
    """d2l kicked us to the login page = session cookies died"""


class D2LClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport  # only used by tests to fake d2l. real use = None
        self._http: httpx.AsyncClient | None = None
        self._cookie_mtime: float | None = None  # so we notice when u re-login
        self._versions: dict[str, str] = {}      # e.g. {"lp": "1.47", "le": "1.80"}
        self._courses: list[dict] | None = None  # cached for the session, courses don't change mid-day

    # ---------- plumbing ----------

    async def _client(self) -> httpx.AsyncClient:
        # if cookies.json changed since we built the client (u ran login again),
        # rebuild it w/ the fresh cookies. means no need to restart claude after re-login
        mtime = config.COOKIE_FILE.stat().st_mtime if config.COOKIE_FILE.exists() else None
        if self._http is None or mtime != self._cookie_mtime:
            if self._http is not None:
                await self._http.aclose()
            self._http = httpx.AsyncClient(
                base_url=config.BASE_URL,
                cookies=auth.load_cookies(),  # raises NotLoggedIn if never logged in
                follow_redirects=False,       # we follow manually in _request() to catch login redirects
                timeout=60,
                transport=self._transport,
            )
            self._cookie_mtime = mtime
        return self._http

    async def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        http = await self._client()
        expected_host = urlparse(config.BASE_URL).netloc

        # follow redirects by hand (max 5) so we can check WHERE each one goes.
        # dead session = d2l redirects to /d2l/login or off to microsoft sso.
        # doing it manually also means a login redirect loop can't hang/crash us
        r = await http.get(path, params=params)
        for _ in range(5):
            if not r.is_redirect:
                break
            nxt = r.next_request
            if nxt is None or "/d2l/login" in str(nxt.url) or nxt.url.host != expected_host:
                raise AuthExpired(f"OWL session expired. {auth.LOGIN_HINT}")
            r = await http.send(nxt)
        else:
            raise AuthExpired(f"OWL kept redirecting (session probably expired). {auth.LOGIN_HINT}")

        # real brightspace answers api calls w/ a dead/missing session as 403 + body "Authentication required"
        # (not a redirect or 401). must catch it here, otherwise it looks like "tool turned off for this course"
        # and get_upcoming_due_dates would silently skip every course
        # BUT with no/cleared cookies it's a plain 403 "Forbidden" — same as a tool that's turned off. so on any
        # other 403, ask whoami (always allowed for a logged-in user) to tell the two apart
        if r.status_code == 401 or (
            r.status_code == 403 and ("authentication required" in r.text.lower() or not await self._session_alive())
        ):
            raise AuthExpired(f"OWL session expired. {auth.LOGIN_HINT}")

        r.raise_for_status()  # 403/404/500 bubble up as httpx.HTTPStatusError
        return r

    async def _session_alive(self) -> bool:
        http = await self._client()
        r = await http.get("/d2l/api/lp/1.0/users/whoami")  # GET, read-only
        return r.status_code == 200

    async def get(self, path: str, params: dict | None = None) -> Any:
        return (await self._request(path, params)).json()

    async def get_bytes(self, path: str) -> bytes:
        return (await self._request(path)).content

    async def ver(self, product: str) -> str:
        # api urls have a version in them (/d2l/api/le/1.80/...) and it differs per school.
        # instead of hardcoding, ask d2l once what it supports and use the newest
        if not self._versions:
            data = await self.get("/d2l/api/versions/")
            self._versions = {v["ProductCode"]: v["LatestVersion"] for v in data}
        return self._versions[product]

    async def paged(self, path: str, params: dict | None = None) -> list[dict]:
        # d2l's "PagedResultSet": {Items: [...], PagingInfo: {Bookmark, HasMoreItems}}
        # keep asking w/ the bookmark until it says no more
        params = dict(params or {})
        items: list[dict] = []
        while True:
            data = await self.get(path, params)
            items.extend(data.get("Items", []))
            paging = data.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                return items
            params["bookmark"] = paging["Bookmark"]

    # ---------- actual d2l stuff ----------

    async def my_courses(self, include_past: bool = False) -> list[dict]:
        if self._courses is None:
            lp = await self.ver("lp")
            # orgUnitTypeId=3 = "course offering" (filters out dept/semester org units)
            raw = await self.paged(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", {"orgUnitTypeId": 3})
            now = datetime.now().astimezone()
            self._courses = [
                {
                    "id": e["OrgUnit"]["Id"],
                    "name": e["OrgUnit"]["Name"],
                    "code": _short_code(e["OrgUnit"]["Name"], e["OrgUnit"].get("Code") or ""),
                    "raw_code": e["OrgUnit"].get("Code") or "",  # owl's is internal, e.g. UGRD_1269_9222
                    "active": _is_current(e.get("Access") or {}, now, e["OrgUnit"]["Name"]),
                    "can_access": bool(e.get("Access", {}).get("CanAccess", True)),
                }
                for e in raw
            ]
        courses = [c for c in self._courses if c["can_access"]]
        return courses if include_past else [c for c in courses if c["active"]]

    async def announcements(self, org_id: int) -> list[dict]:
        le = await self.ver("le")
        return await self.get(f"/d2l/api/le/{le}/{org_id}/news/")

    async def assignments(self, org_id: int) -> list[dict]:
        # "dropbox folders" = assignments in d2l-speak
        le = await self.ver("le")
        return await self.get(f"/d2l/api/le/{le}/{org_id}/dropbox/folders/")

    async def my_submissions(self, org_id: int, folder_id: int) -> list[dict]:
        # what u turned in to one assignment: [{Submitter, Submissions: [{SubmissionDate, Files...}]}]
        le = await self.ver("le")
        return await self.get(f"/d2l/api/le/{le}/{org_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/")

    async def dropbox_attachment(self, org_id: int, folder_id: int, file_id: int) -> bytes:
        # files the prof attached to an assignment (handouts, starter code)
        le = await self.ver("le")
        return await self.get_bytes(f"/d2l/api/le/{le}/{org_id}/dropbox/folders/{folder_id}/attachments/{file_id}")

    async def quizzes(self, org_id: int) -> list[dict]:
        # quizzes use a diff paging style: {Objects: [...], Next: "full url or null"}
        le = await self.ver("le")
        data = await self.get(f"/d2l/api/le/{le}/{org_id}/quizzes/")
        out = list(data.get("Objects", []))
        while data.get("Next"):
            data = await self.get(data["Next"])
            out.extend(data.get("Objects", []))
        return out

    async def grades(self, org_id: int) -> list[dict]:
        le = await self.ver("le")
        return await self.get(f"/d2l/api/le/{le}/{org_id}/grades/values/myGradeValues/")

    async def toc(self, org_id: int) -> dict:
        # the whole content tree: Modules -> (sub)Modules + Topics. topics are the actual files/links
        le = await self.ver("le")
        return await self.get(f"/d2l/api/le/{le}/{org_id}/content/toc")

    async def topic_file(self, org_id: int, topic_id: int) -> bytes:
        le = await self.ver("le")
        return await self.get_bytes(f"/d2l/api/le/{le}/{org_id}/content/topics/{topic_id}/file")


_COURSE_CODE = re.compile(r"^([A-Z]+ \d{4}[A-Z]?)\b")


def _short_code(name: str, fallback: str) -> str:
    # owl names look like "COMPSCI 2211A 001 LEC FW26: SOFTWARE TOOLS ..." -> "COMPSCI 2211A"
    m = _COURSE_CODE.match(name)
    return m.group(1) if m else fallback


def _is_half_course(name: str) -> bool:
    # western suffixes: letter (A/B/F/G/...) = half course, one term. no letter = full year
    m = _COURSE_CODE.match(name)
    return bool(m and m.group(1)[-1].isalpha())


def _is_current(access: dict, now: datetime, name: str = "") -> bool:
    # owl leaves IsActive=true on old courses for years (FW25 courses end in 2027) and EndDate is
    # just as useless, so go by StartDate: owl opens fall courses ~aug 9 and winter ones ~dec 5.
    # half course = one term, so ~5 months (aug 9 -> early jan, dec 5 -> early may, past exams).
    # full-year/unknown = ~10 months. also show courses opening within the next month
    if not access.get("IsActive"):
        return False
    start = parse_date(access.get("StartDate"))
    if start is None:
        return True
    span = timedelta(days=150 if _is_half_course(name) else 300)
    return now - span <= start <= now + timedelta(days=30)


def parse_date(s: str | None) -> datetime | None:
    # d2l gives utc like "2026-11-20T04:59:00.000Z" -> convert to ur laptop's local time
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
