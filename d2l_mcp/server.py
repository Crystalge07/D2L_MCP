"""the mcp server — this is what claude actually talks to

every tool returns plain text (not json) on purpose: claude reads text better and it keeps
token usage low. docstrings matter a LOT here — they're what claude reads to decide which tool to call
"""
import asyncio
import functools
from datetime import datetime, timedelta
from pathlib import PurePosixPath

import httpx
from mcp.server.mcpserver import MCPServer  # mcp sdk 2.x name (was FastMCP in 1.x)

from . import auth, config
from .auth import NotLoggedIn
from .client import AuthExpired, D2LClient, parse_date
from .extract import strip_html
from .sync import INSTRUCTIONS_DIR, course_dir, linked_files, search, sync_course

mcp = MCPServer(
    "d2l",
    # instructions = extra context claude gets about this server as a whole
    instructions=(
        "Tools for the user's D2L Brightspace (Western OWL) account. When the user mentions a course, "
        "lecture, assignment, announcement or grade, use these instead of asking for screenshots. "
        "For questions about course material, search_course_files first; if empty, sync_course_files then search again. "
        "If a tool says the user isn't logged in or the session expired, call login_to_owl."
    ),
)
client = D2LClient()  # one shared client = cookies + api versions + course list cached across calls


def friendly_errors(fn):
    # wraps every tool so auth problems come back as a readable msg instead of a stack trace.
    # the msg tells claude about login_to_owl, so it can offer/call it instead of being confused
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (NotLoggedIn, AuthExpired) as e:
            return f"⚠️ {e}"
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 403:
                return "D2L said 403 (no permission) — that tool might be turned off for this course."
            if code == 404:
                return "D2L said 404 — that course doesn't use this tool (or the item's gone)."
            return f"D2L error {code}: {e.request.url}"
        except ValueError as e:  # our own "couldn't find course" errors
            return str(e)
    return wrapper


async def resolve_course(query: str) -> dict:
    # lets u say "2211" or "data structures" instead of the org unit id
    courses = await client.my_courses(include_past=True)
    q = query.lower().strip()
    hits = [
        c for c in courses
        if q in c["code"].lower() or q in c["name"].lower() or q == c.get("raw_code", "").lower() or q == str(c["id"])
    ]
    # if a course matches in both current + past terms, prefer the current one
    if len(hits) > 1:
        active = [c for c in hits if c["active"]]
        hits = active or hits
    if len(hits) == 1:
        return hits[0]
    names = "\n".join(f"- {c['code']} | {c['name']}" for c in (hits or courses))
    if not hits:
        raise ValueError(f"no course matching '{query}'. ur courses:\n{names}")
    raise ValueError(f"'{query}' matches multiple courses, be more specific:\n{names}")


# =============== LOGIN ===============

_login_task: asyncio.Task | None = None  # the browser login running in the background, if any


@mcp.tool()
async def login_to_owl() -> str:
    """Log the user in to OWL (D2L). Opens a browser window on their computer where they sign in with
    Microsoft SSO + MFA themselves. Call this when another tool says the user isn't logged in or the
    session expired. Returns quickly: if they're still signing in, tell them to finish in the window and
    then ask again — the other tools pick up the new session automatically."""
    global _login_task
    if _login_task is not None and not _login_task.done():
        return "the OWL login window is already open — finish signing in there, then ask again."
    _login_task = asyncio.create_task(auth.login())
    try:
        # microsoft usually remembers the browser, so this often finishes in a few secs w/ no clicks
        await asyncio.wait_for(asyncio.shield(_login_task), timeout=25)
    except asyncio.TimeoutError:
        return ("opened a browser window for OWL — sign in there (Microsoft + MFA). it'll close by itself "
                "once ur in (5 min limit). then ask again.")
    except auth.LoginFailed as e:
        return f"login didn't finish: {e}"
    except Exception as e:  # e.g. playwright's browser not installed
        return f"couldn't open the login window: {e}"
    return "✅ logged in to OWL. go ahead and ask again."


# =============== LIVE TOOLS (always fresh from d2l) ===============

@mcp.tool()
@friendly_errors
async def list_courses(include_past: bool = False) -> str:
    """List the user's D2L (OWL Brightspace) courses. Set include_past=True for previous terms."""
    courses = await client.my_courses(include_past)
    return "\n".join(f"- {c['code']} | {c['name']} (id {c['id']})" for c in courses) or "no courses found"


@mcp.tool()
@friendly_errors
async def get_announcements(course: str, limit: int = 5) -> str:
    """Get recent announcements from a course. `course` can be a code fragment like '2211' or part of the name."""
    c = await resolve_course(course)
    items = await client.announcements(c["id"])
    # newest first. StartDate = when it was posted/visible
    items.sort(key=lambda n: n.get("StartDate") or "", reverse=True)
    out = []
    for n in items[:limit]:
        when = parse_date(n.get("StartDate"))
        body = n.get("Body") or {}
        # prefer html (more complete) and strip it, fall back to plain Text
        text = strip_html(body.get("Html") or "") or body.get("Text") or ""
        out.append(f"## {n.get('Title')}  ({when:%a %b %d, %I:%M %p})\n{text}" if when else f"## {n.get('Title')}\n{text}")
    return f"# {c['name']}\n\n" + "\n\n".join(out) if out else f"no announcements in {c['name']}"


async def _submitted(org_id: int, folder_id) -> str:
    # " ✅ submitted" if u've turned anything in to this assignment, else "". unknown -> "" (don't guess)
    if folder_id is None:
        return ""
    try:
        subs = await client.my_submissions(org_id, folder_id)
    except httpx.HTTPStatusError:
        return ""
    dates = [s.get("SubmissionDate") for e in subs for s in e.get("Submissions", [])]
    if not dates:
        return ""
    last = parse_date(max(d for d in dates if d)) if any(dates) else None
    return f" ✅ submitted{f' {last:%b %d %I:%M %p}' if last else ''}"


@mcp.tool()
@friendly_errors
async def get_upcoming_due_dates(days: int = 14, course: str | None = None) -> str:
    """Assignments + quizzes due in the next `days` days, across all current courses (or one course).
    Assignments the user already submitted are marked ✅ submitted — don't tell them to do those."""
    courses = [await resolve_course(course)] if course else await client.my_courses()
    now = datetime.now().astimezone()
    cutoff = now + timedelta(days=days)
    rows = []

    for c in courses:
        # assignments and quizzes are separate tools in d2l, check both.
        # wrapped in try bc some courses just don't have one of them turned on (403/404)
        for kind, fetch in (("assignment", client.assignments), ("quiz", client.quizzes)):
            try:
                items = await fetch(c["id"])
            except httpx.HTTPStatusError:
                continue
            for it in items:
                due = parse_date(it.get("DueDate"))
                if due and now <= due <= cutoff:
                    # only assignments have a student-readable "my submissions" endpoint
                    status = await _submitted(c["id"], it.get("Id")) if kind == "assignment" else ""
                    rows.append((due, c["code"] or c["name"], kind, it.get("Name"), status))

    rows.sort(key=lambda r: r[0])
    if not rows:
        return f"nothing due in the next {days} days 🎉"
    return "\n".join(f"- {d:%a %b %d %I:%M %p} | {code} | {kind}: {name}{st}" for d, code, kind, name, st in rows)


@mcp.tool()
@friendly_errors
async def get_assignment_details(course: str, assignment: str) -> str:
    """Full details for one assignment/lab: instructions, due date, attached files, and whether the user
    submitted it. `assignment` is part of its name, e.g. 'lab 7' or 'assignment 2'."""
    c = await resolve_course(course)
    folders = await client.assignments(c["id"])
    q = " ".join(assignment.lower().replace("#", " ").split())
    hits = [f for f in folders if q in " ".join((f.get("Name") or "").lower().replace("#", " ").split())]
    if len(hits) != 1:
        names = "\n".join(f"- {f.get('Name')}" for f in folders) or "(none)"
        what = "no assignment" if not hits else "more than one assignment"
        return f"{what} matching '{assignment}' in {c['code']}. assignments:\n{names}"
    f = hits[0]
    due = parse_date(f.get("DueDate"))
    end = parse_date((f.get("Availability") or {}).get("EndDate"))
    lines = [f"# {f.get('Name')} ({c['code']})"]
    lines.append(f"due: {due:%a %b %d %I:%M %p}" if due else "due: (no due date set)")
    if end and end != due:
        lines.append(f"closes (late submissions until): {end:%a %b %d %I:%M %p}")
    lines.append(f"status:{await _submitted(c['id'], f.get('Id')) or ' not submitted yet'}")
    html = (f.get("CustomInstructions") or {}).get("Html") or ""
    text = strip_html(html) or (f.get("CustomInstructions") or {}).get("Text") or ""
    lines.append("\n## instructions\n" + (text or "(no instructions written on the assignment page)"))
    files = [a.get("FileName") for a in f.get("Attachments") or []]
    files += [PurePosixPath(p).name for p in linked_files(html, f"/content/enforced/{c['id']}-{c.get('raw_code')}/")]
    if files:
        lines.append("\n## files\n" + "\n".join(f"- {n}" for n in files))
        lines.append(f"(sync_course_files saves these under '{INSTRUCTIONS_DIR}' so they can be searched/read)")
    return "\n".join(lines)


@mcp.tool()
@friendly_errors
async def get_grades(course: str) -> str:
    """The user's released grades for a course."""
    c = await resolve_course(course)
    grades = await client.grades(c["id"])
    lines = []
    for g in grades:
        # DisplayedGrade is whatever d2l shows u (e.g. "85 %" or "17/20"), use that when available
        shown = g.get("DisplayedGrade") or (
            f"{g.get('PointsNumerator')}/{g.get('PointsDenominator')}" if g.get("PointsDenominator") else "—"
        )
        # GradeObjectType 9 = category total (e.g. "Assignments" = all assignments combined), not a single item
        label = " (category total)" if g.get("GradeObjectType") == 9 else ""
        lines.append(f"- {g.get('GradeObjectName')}{label}: {shown}")
    return f"# {c['name']} grades\n" + ("\n".join(lines) or "no released grades yet")


@mcp.tool()
@friendly_errors
async def get_course_outline(course: str) -> str:
    """The course's content tree (modules + topic titles) straight from D2L. Good for 'what's in week 7' questions."""
    c = await resolve_course(course)
    toc = await client.toc(c["id"])
    lines: list[str] = []

    def walk(mods, depth=0):
        for m in mods:
            lines.append("  " * depth + f"📁 {m.get('Title')}")
            for t in m.get("Topics", []):
                icon = "📄" if t.get("TypeIdentifier") == "File" else "🔗"
                broken = " (broken link on OWL)" if t.get("IsBroken") else ""
                lines.append("  " * (depth + 1) + f"{icon} {t.get('Title')}{broken}")
            # some profs leave modules empty + link the files in the module description instead
            html = (m.get("Description") or {}).get("Html") or ""
            for remote in linked_files(html, m.get("DefaultPath") or ""):
                lines.append("  " * (depth + 1) + f"📄 {PurePosixPath(remote).name}")
            walk(m.get("Modules", []), depth + 1)

    walk(toc.get("Modules", []))
    return f"# {c['name']}\n" + "\n".join(lines)


# =============== SYNC + SEARCH (local copies of course files) ===============

# a first sync can take minutes, longer than claude apps wait for one tool call. so syncs run as
# background tasks: the tool waits up to SYNC_WAIT secs, and if it's not done says "still going, ask again".
# asking again picks up the SAME task (never starts a 2nd one), and returns its result once finished
SYNC_WAIT = 40
_sync_tasks: dict[str, asyncio.Task] = {}
_sync_lock = asyncio.Lock()  # one sync at a time, so 2 syncs never write the same course folder at once


async def _in_background(key: str, make) -> str | None:
    task = _sync_tasks.get(key)
    if task is None:
        async def locked():
            async with _sync_lock:
                return await make()
        task = _sync_tasks[key] = asyncio.create_task(locked())
    try:
        result = await asyncio.wait_for(asyncio.shield(task), SYNC_WAIT)
    except asyncio.TimeoutError:
        return None  # still running
    except asyncio.CancelledError:
        raise  # claude gave up on this call — task keeps going, next call picks it up
    except Exception:
        _sync_tasks.pop(key, None)  # failed (e.g. session expired) -> next call starts fresh
        raise
    _sync_tasks.pop(key, None)
    return result


def _sync_summary(s: dict) -> str:
    parts = [f"{s['downloaded']} downloaded", f"{s['unchanged']} unchanged"]
    for k, label in (("removed", "removed (gone from OWL)"), ("skipped", "skipped (video/keynote)"),
                     ("broken", "broken on OWL"), ("links", "web links")):
        if s.get(k):
            parts.append(f"{s[k]} {label}")
    return ", ".join(parts)


STILL_SYNCING = ("still syncing in the background (the first sync of a course can take a few minutes). "
                 "call this tool again in a minute to get the result.")


@mcp.tool()
@friendly_errors
async def sync_course_files(course: str) -> str:
    """Download a course's content files (slides, PDFs, docs, assignment handouts) to the user's computer and
    extract their text so they can be searched. Only re-downloads files that changed. Run this before searching
    a course for the first time, or when the user says new material was posted. If it says it's still syncing,
    call it again after a bit to get the result."""
    c = await resolve_course(course)
    s = await _in_background(f"course:{c['id']}", lambda: sync_course(client, c))
    if s is None:
        return STILL_SYNCING
    msg = f"synced {c['name']}: {_sync_summary(s)}.\nfolder: {s['folder']}"
    if s["failed"]:
        msg += "\nfailed:\n" + "\n".join(f"- {f}" for f in s["failed"])
    return msg


@mcp.tool()
@friendly_errors
async def sync_all_courses() -> str:
    """Sync content files for every current course. Slow the first time, quick after. If it says it's still
    syncing, call it again after a bit to get the result."""
    async def run():
        out = []
        for c in await client.my_courses():
            s = await sync_course(client, c)
            fails = f", {len(s['failed'])} failed" if s["failed"] else ""
            out.append(f"- {c['code'] or c['name']}: {_sync_summary(s)}{fails}")
        return "\n".join(out) or "no current courses"
    result = await _in_background("all", run)
    return STILL_SYNCING if result is None else result


@mcp.tool()
@friendly_errors
async def search_course_files(query: str, course: str | None = None) -> str:
    """Full-text search over synced course files (lecture slides, PDFs, labs). Returns file, page/slide, and a snippet.
    Use short specific queries (e.g. 'linked list', 'malloc'). If nothing's found the course may not be synced yet."""
    folder = course_dir(await resolve_course(course)) if course else None
    hits = search(query, folder)
    if not hits:
        return f"no matches for '{query}'. if this course hasn't been synced, run sync_course_files first."
    return "\n\n".join(f"**{h['file']}** {('(' + h['where'] + ')') if h['where'] else ''}\n…{h['snippet']}…" for h in hits)


@mcp.tool()
@friendly_errors
async def read_course_file(file: str, offset: int = 0, max_chars: int = 30000) -> str:
    """Read the extracted text of a synced file. `file` is the path shown by search_course_files or
    list_synced_files. Long files are returned in chunks — call again with the given offset to continue."""
    # resolve inside SYNC_DIR only — don't let a weird path read random files on ur laptop
    target = (config.SYNC_DIR / (file + ".txt")).resolve()
    if config.SYNC_DIR.resolve() not in target.parents or not target.exists():
        return f"couldn't find '{file}'. use list_synced_files or search_course_files to get the exact path."
    text = target.read_text(errors="ignore")
    chunk = text[offset: offset + max_chars]
    more = offset + max_chars < len(text)
    return chunk + (f"\n\n[... {len(text) - offset - max_chars} more chars — call again with offset={offset + max_chars}]" if more else "")


@mcp.tool()
@friendly_errors
async def list_synced_files(course: str) -> str:
    """List all locally synced files for a course (only ones with readable text)."""
    cdir = course_dir(await resolve_course(course))
    files = sorted(p for p in cdir.rglob("*.txt") if not p.name.startswith("_"))
    if not files:
        return "nothing synced for this course yet — run sync_course_files first."
    return "\n".join(str(p.relative_to(config.SYNC_DIR))[: -len(".txt")] for p in files)


def main() -> None:
    # entry point for the `d2l-mcp` command. stdio = claude desktop / claude code launch this as a subprocess
    mcp.run()


if __name__ == "__main__":
    main()
