"""the mcp server — this is what claude actually talks to

every tool returns plain text (not json) on purpose: claude reads text better and it keeps
token usage low. docstrings matter a LOT here — they're what claude reads to decide which tool to call
"""
import functools
from datetime import datetime, timedelta

import httpx
from mcp.server.mcpserver import MCPServer  # mcp sdk 2.x name (was FastMCP in 1.x)

from . import config
from .auth import NotLoggedIn
from .client import AuthExpired, D2LClient, parse_date
from .extract import strip_html
from .sync import course_dir, search, sync_course

mcp = MCPServer(
    "d2l",
    # instructions = extra context claude gets about this server as a whole
    instructions=(
        "Tools for the user's D2L Brightspace (Western OWL) account. When the user mentions a course, "
        "lecture, assignment, announcement or grade, use these instead of asking for screenshots. "
        "For questions about course material, search_course_files first; if empty, sync_course_files then search again."
    ),
)
client = D2LClient()  # one shared client = cookies + api versions + course list cached across calls


def friendly_errors(fn):
    # wraps every tool so auth problems come back as a readable msg instead of a stack trace.
    # claude will relay "run d2l-mcp-login" to u instead of being confused
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


@mcp.tool()
@friendly_errors
async def get_upcoming_due_dates(days: int = 14, course: str | None = None) -> str:
    """Assignments + quizzes due in the next `days` days, across all current courses (or one course)."""
    courses = [await resolve_course(course)] if course else await client.my_courses()
    now = datetime.now().astimezone()
    cutoff = now + timedelta(days=days)
    rows = []

    for c in courses:
        # assignments and quizzes are separate tools in d2l, check both.
        # wrapped in try bc some courses just don't have one of them turned on (403/404)
        for kind, fetch, name_key, due_key in (
            ("assignment", client.assignments, "Name", "DueDate"),
            ("quiz", client.quizzes, "Name", "DueDate"),
        ):
            try:
                items = await fetch(c["id"])
            except httpx.HTTPStatusError:
                continue
            for it in items:
                due = parse_date(it.get(due_key))
                if due and now <= due <= cutoff:
                    rows.append((due, c["code"] or c["name"], kind, it.get(name_key)))

    rows.sort()  # tuples sort by due date first
    if not rows:
        return f"nothing due in the next {days} days 🎉"
    return "\n".join(f"- {d:%a %b %d %I:%M %p} | {code} | {kind}: {name}" for d, code, kind, name in rows)


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
                lines.append("  " * (depth + 1) + f"{icon} {t.get('Title')}")
            walk(m.get("Modules", []), depth + 1)

    walk(toc.get("Modules", []))
    return f"# {c['name']}\n" + "\n".join(lines)


# =============== SYNC + SEARCH (local copies of course files) ===============

@mcp.tool()
@friendly_errors
async def sync_course_files(course: str) -> str:
    """Download a course's content files (slides, PDFs, docs) to the user's computer and extract their text
    so they can be searched. Only re-downloads files that changed. Run this before searching a course
    for the first time, or when the user says new material was posted."""
    c = await resolve_course(course)
    s = await sync_course(client, c)
    msg = f"synced {c['name']}: {s['downloaded']} downloaded, {s['unchanged']} unchanged, {s['links']} links skipped, {s['broken']} broken on OWL.\nfolder: {s['folder']}"
    if s["failed"]:
        msg += "\nfailed:\n" + "\n".join(f"- {f}" for f in s["failed"])
    return msg


@mcp.tool()
@friendly_errors
async def sync_all_courses() -> str:
    """Sync content files for every current course. Slow the first time, quick after."""
    out = []
    for c in await client.my_courses():
        s = await sync_course(client, c)
        out.append(f"- {c['code'] or c['name']}: {s['downloaded']} new, {s['unchanged']} unchanged, {len(s['failed'])} failed")
    return "\n".join(out) or "no current courses"


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
