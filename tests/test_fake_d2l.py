"""end-to-end test against a FAKE d2l (no real network). run: python tests/test_fake_d2l.py

fakes the exact json shapes brightspace returns so u can test changes without hitting owl
"""
import asyncio
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# point everything at a temp folder BEFORE importing the package (config reads env at import)
tmp = Path(tempfile.mkdtemp())
os.environ["D2L_DATA_DIR"] = str(tmp)
os.environ["D2L_BASE_URL"] = "https://fake.brightspace.com"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from docx import Document

from d2l_mcp import config, server
from d2l_mcp.client import D2LClient

config.DATA_DIR.mkdir(parents=True, exist_ok=True)
config.COOKIE_FILE.write_text(json.dumps({"d2lSessionVal": "x", "d2lSecureSessionVal": "y"}))

# a real .docx so extraction gets exercised
buf = io.BytesIO()
d = Document(); d.add_paragraph("Lecture 7: pointers and malloc. Always free what you malloc."); d.save(buf)
DOCX = buf.getvalue()

soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
expired = {"on": False, "mode": "redirect"}  # flip to simulate session dying


def fake(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if expired["on"]:
        if expired["mode"] == "403":  # what real OWL does for api calls w/ a dead session
            return httpx.Response(403, text="Authentication required")
        if expired["mode"] == "forbidden":  # what real OWL does w/ cookies missing/cleared
            return httpx.Response(403, text='{ Errors: [ {Message: "Forbidden"} ] }')
        return httpx.Response(302, headers={"location": "https://fake.brightspace.com/d2l/login"})
    if p == "/d2l/api/lp/1.0/users/whoami":
        return httpx.Response(200, json={"Identifier": "1"})
    assert req.headers.get("cookie", "").count("d2lSessionVal") == 1, "cookies not sent!"
    if p == "/d2l/api/versions/":
        return httpx.Response(200, json=[{"ProductCode": "lp", "LatestVersion": "1.47"}, {"ProductCode": "le", "LatestVersion": "1.80"}])
    if p == "/d2l/api/lp/1.47/enrollments/myenrollments/":
        # 2 pages to test bookmark paging
        if "bookmark" not in req.url.params:
            return httpx.Response(200, json={"Items": [{"OrgUnit": {"Id": 101, "Name": "Software Tools and Systems Programming", "Code": "COMPSCI-2211A"}, "Access": {"IsActive": True, "CanAccess": True}}], "PagingInfo": {"Bookmark": "b1", "HasMoreItems": True}})
        return httpx.Response(200, json={"Items": [{"OrgUnit": {"Id": 102, "Name": "Data Structures", "Code": "COMPSCI-2210A"}, "Access": {"IsActive": True, "CanAccess": True}}], "PagingInfo": {"Bookmark": None, "HasMoreItems": False}})
    if p == "/d2l/api/le/1.80/101/news/":
        return httpx.Response(200, json=[{"Title": "Midterm room change", "StartDate": "2026-11-10T14:00:00.000Z", "Body": {"Html": "<p>Midterm is in <b>NS 145</b> now.</p>", "Text": ""}}])
    if p == "/d2l/api/le/1.80/101/dropbox/folders/":
        return httpx.Response(200, json=[{"Name": "Assignment 4", "DueDate": soon}, {"Name": "Old A1", "DueDate": "2026-09-01T04:00:00.000Z"}])
    if p == "/d2l/api/le/1.80/101/quizzes/":
        return httpx.Response(200, json={"Objects": [{"Name": "Quiz 3", "DueDate": soon}], "Next": None})
    if p.startswith("/d2l/api/le/1.80/102/"):
        return httpx.Response(403)  # 2210 has these tools off -> should be skipped gracefully
    if p == "/d2l/api/le/1.80/101/grades/values/myGradeValues/":
        return httpx.Response(200, json=[{"GradeObjectName": "Assignment 1", "DisplayedGrade": "92 %"}])
    if p == "/d2l/api/le/1.80/101/content/toc":
        return httpx.Response(200, json={"Modules": [{"Title": "Week 7", "Topics": [
            {"TopicId": 9001, "Title": "Lecture 7", "TypeIdentifier": "File", "Url": "/content/enforced/101/Lecture%207.docx", "LastModifiedDate": "2026-11-01T00:00:00.000Z"},
            {"TopicId": 9002, "Title": "Valgrind video", "TypeIdentifier": "Link", "Url": "https://youtube.com/x"},
            {"TopicId": 9003, "Title": "Deleted slides", "TypeIdentifier": "File", "Url": None, "IsBroken": True},
        ], "Modules": [
            # real owl: some profs leave modules empty + link the file in the description (link + iframe = same file twice)
            {"Title": "Week 8", "Topics": [], "Modules": [], "DefaultPath": "/content/enforced/101-X/",
             "LastModifiedDate": "2026-11-02T00:00:00.000Z",
             "Description": {"Html": '<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=101&amp;type=coursefile&amp;fileId=Notes%2fLecture+8.docx">L8</a>'
                                     '<iframe src="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=101&amp;type=coursefile&amp;fileId=Notes%2fLecture+8.docx"></iframe>'}},
        ]}]})
    if p == "/content/enforced/101-X/Notes/Lecture 8.docx":
        return httpx.Response(200, content=DOCX)
    if p == "/d2l/api/le/1.80/101/content/topics/9001/file":
        return httpx.Response(200, content=DOCX)
    return httpx.Response(404)


async def main():
    server.client = D2LClient(transport=httpx.MockTransport(fake))

    print(await server.list_courses(), "\n")
    print(await server.get_announcements("2211"), "\n")
    print(await server.get_upcoming_due_dates(days=7), "\n")
    print(await server.get_grades("2211"), "\n")
    print(await server.get_course_outline("2211"), "\n")
    print(await server.sync_course_files("2211"), "\n")
    print(await server.sync_course_files("2211"), "  <- 2nd sync should be all unchanged\n")
    print(await server.search_course_files("malloc"), "\n")
    print(await server.list_synced_files("2211"), "\n")
    print(await server.read_course_file("COMPSCI-2211A/Week 7/Lecture 7.docx"), "\n")
    print(await server.read_course_file("../../etc/passwd"), "  <- path escape blocked\n")
    print(await server.get_grades("compsci"), "  <- ambiguous\n")
    expired["on"] = True
    print(await server.get_grades("2211"), "  <- expired session (redirect)\n")
    expired["mode"] = "403"
    print(await server.get_upcoming_due_dates(), "  <- expired session (403 'Authentication required') must NOT say nothing due\n")
    expired["mode"] = "forbidden"
    print(await server.get_upcoming_due_dates(), "  <- expired session (plain 403 'Forbidden') must NOT say nothing due\n")

    # tools registered w/ proper schemas?
    tools = await server.mcp.list_tools()
    print("tools:", [t.name for t in tools])
    print("schema sample:", tools[1].input_schema)


asyncio.run(main())
