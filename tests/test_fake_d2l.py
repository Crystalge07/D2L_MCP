"""end-to-end test against a FAKE d2l (no real network). run: python tests/test_fake_d2l.py

fakes the exact json shapes brightspace returns so u can test changes without hitting owl
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import zipfile
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
lecture7_deleted = {"on": False}  # flip to simulate prof removing a file from owl

# zipped lecture code (search should see inside it, even w/ a phrase split across lines)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("code/bst.c", "/* a binary\n   search tree */\nint main(void) { return 0; }")
    z.writestr("__MACOSX/._bst.c", "junk")
ZIP = buf.getvalue()

# an owl html page whose only job is linking the real file (like 2210's "File for Assignment 1")
PAGE = '<p>File for A3: <a href="https://fake.brightspace.com/content/enforced/101-X/Starter.java">Starter.java</a></p>'

DROPBOX = [
    {"Id": 501, "Name": "Assignment 4", "DueDate": soon,
     "CustomInstructions": {"Html": '<p>Implement a linked list. Handout: <a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=101&amp;type=coursefile&amp;fileId=Labs%2flab4.c">lab4.c</a></p>'},
     "Attachments": [{"FileId": 77, "FileName": "a4-handout.docx", "Size": 123}]},
    {"Id": 502, "Name": "Assignment 5", "DueDate": soon, "CustomInstructions": {"Html": ""}, "Attachments": []},
    {"Id": 500, "Name": "Old A1", "DueDate": "2026-09-01T04:00:00.000Z"},
]


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
        return httpx.Response(200, json=DROPBOX)
    if p == "/d2l/api/le/1.80/101/dropbox/folders/501/submissions/mysubmissions/":
        return httpx.Response(200, json=[{"Submissions": [{"SubmissionDate": "2026-11-17T20:00:00.000Z"}]}])
    if p == "/d2l/api/le/1.80/101/dropbox/folders/502/submissions/mysubmissions/":
        return httpx.Response(200, json=[])
    if p == "/d2l/api/le/1.80/101/dropbox/folders/501/attachments/77":
        return httpx.Response(200, content=DOCX)
    if p == "/content/enforced/101-X/Labs/lab4.c":
        return httpx.Response(200, content=b"// lab 4 starter: linked list\nstruct node { int v; struct node *next; };")
    if p == "/content/enforced/101-X/Starter.java":
        return httpx.Response(200, content=b"public class Starter { /* hash table */ }")
    if p == "/d2l/api/le/1.80/101/content/topics/9004/file":
        return httpx.Response(200, text=PAGE)
    if p == "/d2l/api/le/1.80/101/content/topics/9006/file":
        return httpx.Response(200, content=ZIP)
    if p == "/d2l/api/le/1.80/101/quizzes/":
        # 2 pages; real owl's Next is a full absolute url
        if "bookmark" not in req.url.params:
            return httpx.Response(200, json={"Objects": [{"Name": "Quiz 3", "DueDate": soon}], "Next": "https://fake.brightspace.com/d2l/api/le/1.80/101/quizzes/?bookmark=2"})
        return httpx.Response(200, json={"Objects": [{"Name": "Quiz 4 (page 2)", "DueDate": soon}], "Next": None})
    if p.startswith("/d2l/api/le/1.80/102/"):
        return httpx.Response(403)  # 2210 has these tools off -> should be skipped gracefully
    if p == "/d2l/api/le/1.80/101/grades/values/myGradeValues/":
        return httpx.Response(200, json=[{"GradeObjectName": "Assignment 1", "DisplayedGrade": "92 %"}])
    if p == "/d2l/api/le/1.80/101/content/toc":
        topics = [
            {"TopicId": 9001, "Title": "Lecture 7", "TypeIdentifier": "File", "Url": "/content/enforced/101-X/Lecture%207.docx", "LastModifiedDate": "2026-11-01T00:00:00.000Z"},
            {"TopicId": 9002, "Title": "Valgrind video", "TypeIdentifier": "Link", "Url": "https://youtube.com/x"},
            {"TopicId": 9003, "Title": "Deleted slides", "TypeIdentifier": "File", "Url": None, "IsBroken": True},
            {"TopicId": 9004, "Title": "File for A3", "TypeIdentifier": "File", "Url": "/content/enforced/101-X/a3page.html", "LastModifiedDate": "2026-11-01T00:00:00.000Z"},
            {"TopicId": 9005, "Title": "Lecture 7 (Keynote)", "TypeIdentifier": "File", "Url": "/content/enforced/101-X/Lecture7.key", "LastModifiedDate": "2026-11-01T00:00:00.000Z"},
            {"TopicId": 9006, "Title": "Lecture 7 code", "TypeIdentifier": "File", "Url": "/content/enforced/101-X/lec7code.zip", "LastModifiedDate": "2026-11-01T00:00:00.000Z"},
        ]
        if lecture7_deleted["on"]:
            topics = topics[1:]
        return httpx.Response(200, json={"Modules": [{"Title": "Week 7", "Topics": topics, "DefaultPath": "/content/enforced/101-X/", "Modules": [
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
    due = await server.get_upcoming_due_dates(days=7)
    assert "Assignment 4 ✅ submitted" in due and "Assignment 5\n" in due + "\n", due
    print(await server.get_assignment_details("2211", "assignment 4"), "\n")
    outline = await server.get_course_outline("2211")
    assert "Lecture 8.docx" in outline and "broken link on OWL" in outline, outline

    s1 = await server.sync_course_files("2211"); print(s1, "\n")
    assert "7 downloaded" in s1 and "1 skipped" in s1, s1  # L7, page, zip, L8, A4 handout, lab4.c, Starter.java; .key skipped
    s2 = await server.sync_course_files("2211"); print(s2, "  <- 2nd sync should be all unchanged\n")
    assert "0 downloaded" in s2, s2
    for q, want in (("linked list", "lab4.c"), ("hash table", "Starter.java"), ("binary search", "file code/bst.c"),
                    ("implement a linked", "Instructions.html")):
        r = await server.search_course_files(q)
        assert want in r, (q, r)
    print(await server.search_course_files("binary search"), "  <- inside zip, phrase split across lines\n")

    lecture7_deleted["on"] = True
    s3 = await server.sync_course_files("2211"); print(s3, "  <- prof deleted Lecture 7\n")
    assert "1 removed" in s3 and not (config.SYNC_DIR / "COMPSCI-2211A/Week 7/Lecture 7.docx").exists(), s3
    lecture7_deleted["on"] = False
    await server.sync_course_files("2211")  # put it back for the read test below
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

    # "current course" rule, w/ real owl names + start dates, checked at different points in the year
    from d2l_mcp.client import _is_current
    fall = {"IsActive": True, "StartDate": "2026-08-09T04:00:00.000Z"}
    winter = {"IsActive": True, "StartDate": "2026-12-05T05:00:00.000Z"}
    last_winter = {"IsActive": True, "StartDate": "2025-12-05T05:00:00.000Z"}
    half, full = "COMPSCI 2211A 001 LEC FW26: X", "BUSINESS 2257 001 LEC FW26: X"
    at = lambda s: datetime.fromisoformat(s).astimezone()
    cases = [
        (fall, half, "2026-11-18", True), (last_winter, "MATH 1600B 001 LEC FW25: X", "2026-11-18", False),
        (winter, "COMPSCI 2212B 001 LEC FW26: X", "2026-11-18", True),   # opens within a month
        (fall, half, "2026-12-20", True),                                 # exams
        (fall, half, "2027-01-15", False), (fall, full, "2027-01-15", True),
        (winter, "COMPSCI 2212B 001 LEC FW26: X", "2027-03-01", True),
        (fall, full, "2027-07-01", False),
    ]
    for access, name, when, want in cases:
        assert _is_current(access, at(when), name) == want, (name, when, want)
    print("current-course rule: all", len(cases), "cases ok")

    # tools registered w/ proper schemas?
    tools = await server.mcp.list_tools()
    print("tools:", [t.name for t in tools])
    print("schema sample:", tools[1].input_schema)


asyncio.run(main())
