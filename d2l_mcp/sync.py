"""the "save my shit as files" half

layout on disk:
  ~/.d2l-mcp/courses/<course code>/<module>/<submodule>/Lecture 5.pdf
                                                       /Lecture 5.pdf.txt   <- extracted text, what search uses
  ~/.d2l-mcp/courses/<course code>/Assignment instructions/<assignment>/Instructions.html (+ attachments)
  ~/.d2l-mcp/courses/<course code>/_manifest.json      <- remembers what we already downloaded

re-syncing only downloads topics whose LastModifiedDate changed, so it's fast after the first run.
files the prof removes from owl get removed here too, so search never shows stale stuff
"""
import json
import re
from html import unescape
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, unquote_plus, urlparse

import httpx

from . import config
from .client import D2LClient
from .extract import extract_text

# downloaded for nothing: can't extract text from them + they're big. keynote = mac-only dupe of the pptx/pdf
SKIP_EXTS = {".key", ".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mp3", ".m4a", ".wav"}
# images embedded in pages/descriptions are decoration (banners, posters) — don't follow those links
SKIP_LINKED_EXTS = SKIP_EXTS | {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}

INSTRUCTIONS_DIR = "Assignment instructions"


def safe(name: str) -> str:
    # strip chars that windows/mac hate in filenames, collapse whitespace, cap length
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return re.sub(r"\s+", " ", name).strip(" .")[:120] or "untitled"


def course_dir(course: dict) -> Path:
    # folder per course, named by code (falls back to name if code is blank)
    return config.SYNC_DIR / safe(course["code"] or course["name"])


def _walk(modules: list[dict], trail: tuple[str, ...] = ()):
    # recursively yield (folder trail, topic) for every topic in the content tree
    for m in modules:
        here = trail + (safe(m.get("Title", "module")),)
        for t in m.get("Topics", []):
            yield here, t
        yield from _walk(m.get("Modules", []), here)


def _walk_modules(modules: list[dict], trail: tuple[str, ...] = ()):
    # recursively yield (folder trail, module) for every module in the content tree
    for m in modules:
        here = trail + (safe(m.get("Title", "module")),)
        yield here, m
        yield from _walk_modules(m.get("Modules", []), here)


# profs link course files from html in 2 ways:
# 1. owl's "quicklink" picker: /d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&type=coursefile&fileId=Notes%2fLec1.pdf
#    (module descriptions in 2209, lab instructions in 2211)
# 2. plain links to the file itself: https://westernu.brightspace.com/content/enforced/201439-UGRD_1269_9220/Search.java
_COURSEFILE_LINK = re.compile(r"type=coursefile&(?:amp;)?fileId=([^\"'&<>\s]+)")
_ENFORCED_LINK = re.compile(
    r"""(?:href|src)=["'](?:https?://""" + re.escape(urlparse(config.BASE_URL).netloc) + r""")?/content/enforced/([^/"']+)/([^"'?#]+)"""
)


def linked_files(html: str, root: str) -> list[str]:
    """course-file paths (relative to the course's files root) linked from some html, deduped, in order.
    root = the course's DefaultPath, e.g. /content/enforced/203951-UGRD_1269_11219/"""
    root_name = root.strip("/").split("/")[-1]
    # links sit inside html attributes, so "&" shows up as "&amp;" etc — unescape before url-decoding
    out = [unquote_plus(unescape(f)) for f in _COURSEFILE_LINK.findall(html)]  # quicklink fileIds use + for spaces
    out += [unquote(unescape(rest)) for course_part, rest in _ENFORCED_LINK.findall(html) if course_part == root_name]
    return list(dict.fromkeys(out))


def _filename(topic: dict) -> str:
    # topic Url is usually like /content/enforced/12345-CS2211/Lecture%205.pdf -> use the real filename.
    # if that's missing, fall back to the topic title
    url = topic.get("Url") or ""
    base = unquote(PurePosixPath(url.split("?")[0]).name) if url else ""
    return safe(base) if "." in base else safe(topic.get("Title", "file"))


def _course_root(toc: dict, course: dict) -> str:
    # where this course's files live on owl. every module has it as DefaultPath; build it if there are no modules
    for _, m in _walk_modules(toc.get("Modules", [])):
        if m.get("DefaultPath"):
            return m["DefaultPath"].rstrip("/") + "/"
    return f"/content/enforced/{course['id']}-{course.get('raw_code') or course['code']}/"


async def sync_course(client: D2LClient, course: dict) -> dict:
    cdir = course_dir(course)
    cdir.mkdir(parents=True, exist_ok=True)
    manifest_path = cdir / "_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    toc = await client.toc(course["id"])
    root = _course_root(toc, course)
    stats = {"downloaded": 0, "unchanged": 0, "links": 0, "broken": 0, "skipped": 0, "removed": 0, "failed": []}
    alive: set[str] = set()      # manifest keys still on owl this sync — anything else gets pruned at the end
    have_remote: set[str] = set()  # course-file paths already saved, so links to them don't download twice
    to_follow: list[tuple[tuple[str, ...], str, str | None]] = []  # (folder trail, remote path, modified)

    async def fetch(key: str, title: str, rel: Path, modified: str | None, module: str, download) -> None:
        alive.add(key)
        prev = manifest.get(key)
        # skip if we already have it AND prof hasn't re-uploaded since
        if prev and prev.get("modified") == modified and (cdir / prev["path"]).exists():
            stats["unchanged"] += 1
            return
        dest = cdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(await download())
            _write_sidecar(dest)
        except Exception as e:  # one broken file shouldn't kill the whole sync
            stats["failed"].append(f"{rel}: {e}")
            return
        manifest[key] = {"title": title, "type": "file", "path": str(rel), "modified": modified, "module": module}
        stats["downloaded"] += 1

    # ---- 1. content topics (the normal way profs post files) ----
    for trail, topic in _walk(toc.get("Modules", [])):
        key = str(topic["TopicId"])

        # "Link" topics (youtube, external sites) have nothing to download — just remember the url
        if topic.get("TypeIdentifier") != "File":
            manifest[key] = {"title": topic.get("Title"), "type": "link", "url": topic.get("Url"), "module": "/".join(trail)}
            alive.add(key)
            stats["links"] += 1
            continue

        # prof deleted/moved the file but left the topic up — owl shows it broken too, nothing to get
        if topic.get("IsBroken"):
            stats["broken"] += 1
            continue

        name = _filename(topic)
        if PurePosixPath(name).suffix.lower() in SKIP_EXTS:
            stats["skipped"] += 1
            continue

        url = unquote((topic.get("Url") or "").split("?")[0])
        if url.startswith(root):
            have_remote.add(url[len(root):])

        await fetch(
            key, topic.get("Title"), Path(*trail) / name, topic.get("LastModifiedDate"), "/".join(trail),
            lambda t=topic: client.topic_file(course["id"], t["TopicId"]),
        )

        # html pages often just link to the real file (e.g. 2210's "File for Assignment 1" -> Search.java)
        saved = cdir / (manifest.get(key) or {}).get("path", "")
        if saved.suffix.lower() in (".html", ".htm") and saved.exists():
            for remote in linked_files(saved.read_text(errors="ignore"), root):
                to_follow.append((trail, remote, topic.get("LastModifiedDate")))

    # ---- 2. files linked in module descriptions (some profs leave modules empty + link the pdf there) ----
    for trail, module in _walk_modules(toc.get("Modules", [])):
        html = (module.get("Description") or {}).get("Html") or ""
        for remote in linked_files(html, root):
            to_follow.append((trail, remote, module.get("LastModifiedDate")))

    # ---- 3. assignment instructions + attachments (lab handouts often live ONLY here) ----
    try:
        folders = await client.assignments(course["id"])
    except httpx.HTTPStatusError:  # assignments tool off in this course -> keep whatever we had
        folders = None
        alive.update(k for k in manifest if k.startswith("dropbox:"))
    for f in folders or []:
        fid = f.get("Id")
        trail = (INSTRUCTIONS_DIR, safe(f.get("Name") or f"assignment {fid}"))
        html = (f.get("CustomInstructions") or {}).get("Html") or ""
        if html.strip():
            key = f"dropbox:{fid}:instructions"
            rel = Path(*trail) / "Instructions.html"
            dest = cdir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"<h1>{f.get('Name')}</h1>\n{html}")  # local only, no download needed
            _write_sidecar(dest)
            manifest[key] = {"title": f"{f.get('Name')} instructions", "type": "file", "path": str(rel), "module": "/".join(trail)}
            alive.add(key)
            for remote in linked_files(html, root):
                to_follow.append((trail, remote, None))
        for a in f.get("Attachments") or []:
            name = safe(a.get("FileName") or str(a.get("FileId")))
            await fetch(
                f"dropbox:{fid}:att:{a.get('FileId')}", name, Path(*trail) / name, str(a.get("Size")), "/".join(trail),
                lambda fid=fid, a=a: client.dropbox_attachment(course["id"], fid, a["FileId"]),
            )

    # ---- 4. download everything linked from 1-3, once each ----
    # profs re-link the same notes in "Week 4 (Continued)" etc — keep each file once, under the first place it's linked
    for trail, remote, modified in to_follow:
        name = safe(PurePosixPath(remote).name)
        if remote in have_remote:
            continue
        have_remote.add(remote)
        if PurePosixPath(name).suffix.lower() in SKIP_LINKED_EXTS:
            continue  # not counted as "skipped (video/keynote)" — embedded images are just page decoration
        await fetch(
            f"coursefile:{remote}", name, Path(*trail) / name, modified, "/".join(trail),
            lambda url=root + quote(remote): client.get_bytes(url),
        )

    # ---- 5. prune stuff that's gone from owl (prof deleted it, or it's a type we now skip) ----
    for key in [k for k in manifest if k not in alive]:
        entry = manifest.pop(key)
        if entry.get("type") == "file":
            target = (cdir / entry["path"]).resolve()
            if cdir.resolve() in target.parents:  # never delete outside this course's folder
                target.unlink(missing_ok=True)
                target.with_name(target.name + ".txt").unlink(missing_ok=True)
                stats["removed"] += 1

    manifest_path.write_text(json.dumps(manifest, indent=2))
    stats["folder"] = str(cdir)
    return stats


def _write_sidecar(dest: Path) -> None:
    text = extract_text(dest)
    if text is not None:
        # sidecar .txt right next to the original, e.g. "Lecture 5.pdf.txt"
        dest.with_name(dest.name + ".txt").write_text(text)


def _has_preferred_twin(txt: Path) -> bool:
    # profs often post the same lecture as "Lecture16.pdf" AND "Lecture16.pptx" in one folder.
    # searching both = every hit shows up twice. keep the pptx (cleaner text + slide numbers), skip the pdf
    original = txt.with_suffix("")  # "Lecture16.pdf.txt" -> "Lecture16.pdf"
    if original.suffix.lower() != ".pdf":
        return False
    return (original.parent / (original.stem + ".pptx.txt")).exists()


_MARKER = re.compile(r"--- (?:(?:page|slide) \d+|file .+?) ---")


def search(query: str, course_folder: Path | None = None, max_hits: int = 20, per_file: int = 3) -> list[dict]:
    """dumb-but-effective full text search over every extracted .txt (case-insensitive)

    for a few courses' worth of slides this is instant. if it ever gets slow,
    that's when u'd swap in sqlite fts or embeddings
    """
    root = course_folder or config.SYNC_DIR
    # squash all whitespace (in query AND text) so a phrase split across lines in a pdf still matches
    q = " ".join(query.lower().split())
    if not q:
        return []
    hits = []
    for txt in sorted(root.rglob("*.txt")):
        if txt.name.startswith("_") or _has_preferred_twin(txt):
            continue
        text = " ".join(txt.read_text(errors="ignore").split())
        low = text.lower()
        start = n = 0
        # cap per file so one huge pdf can't eat every result slot
        while n < per_file and (i := low.find(q, start)) != -1:
            # last "--- page N ---" / "--- slide N ---" / "--- file x.c ---" marker before the match = where it is
            marker = _MARKER.findall(text[:i])
            hits.append({
                "file": str(txt.relative_to(config.SYNC_DIR))[: -len(".txt")],  # show original filename
                "where": marker[-1].strip("- ") if marker else "",
                "snippet": text[max(0, i - 150): i + len(q) + 150],  # ±150 chars
            })
            n += 1
            if len(hits) >= max_hits:
                return hits
            start = i + len(q) + 150  # skip past this snippet so nearby repeats don't show up twice
    return hits
