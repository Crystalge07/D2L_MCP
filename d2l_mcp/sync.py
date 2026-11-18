"""the "save my shit as files" half

layout on disk:
  ~/.d2l-mcp/courses/<course code>/<module>/<submodule>/Lecture 5.pdf
                                                       /Lecture 5.pdf.txt   <- extracted text, what search uses
  ~/.d2l-mcp/courses/<course code>/_manifest.json      <- remembers what we already downloaded

re-syncing only downloads topics whose LastModifiedDate changed, so it's fast after the first run
"""
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, unquote_plus

from . import config
from .client import D2LClient
from .extract import extract_text


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


# some profs don't add files as topics at all — they leave the module empty and link the pdf in the
# module's description html, e.g. <a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&type=coursefile&fileId=Notes%2fLec1.pdf">
_COURSEFILE_LINK = re.compile(r"type=coursefile&(?:amp;)?fileId=([^\"'&<>\s]+)")


def _filename(topic: dict) -> str:
    # topic Url is usually like /content/enforced/12345-CS2211/Lecture%205.pdf -> use the real filename.
    # if that's missing, fall back to the topic title
    url = topic.get("Url") or ""
    base = unquote(PurePosixPath(url.split("?")[0]).name) if url else ""
    return safe(base) if "." in base else safe(topic.get("Title", "file"))


async def sync_course(client: D2LClient, course: dict) -> dict:
    cdir = course_dir(course)
    cdir.mkdir(parents=True, exist_ok=True)
    manifest_path = cdir / "_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    toc = await client.toc(course["id"])
    stats = {"downloaded": 0, "unchanged": 0, "links": 0, "broken": 0, "failed": []}

    async def fetch(key: str, title: str, rel: Path, modified: str | None, module: str, download) -> None:
        prev = manifest.get(key)
        # skip if we already have it AND prof hasn't re-uploaded since
        if prev and prev.get("modified") == modified and (cdir / prev["path"]).exists():
            stats["unchanged"] += 1
            return
        dest = cdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(await download())
            text = extract_text(dest)
            if text is not None:
                # sidecar .txt right next to the original, e.g. "Lecture 5.pdf.txt"
                dest.with_name(dest.name + ".txt").write_text(text)
        except Exception as e:  # one broken file shouldn't kill the whole sync
            stats["failed"].append(f"{rel}: {e}")
            return
        manifest[key] = {"title": title, "type": "file", "path": str(rel), "modified": modified, "module": module}
        stats["downloaded"] += 1

    for trail, topic in _walk(toc.get("Modules", [])):
        key = str(topic["TopicId"])

        # "Link" topics (youtube, external sites) have nothing to download — just remember the url
        if topic.get("TypeIdentifier") != "File":
            manifest[key] = {"title": topic.get("Title"), "type": "link", "url": topic.get("Url"), "module": "/".join(trail)}
            stats["links"] += 1
            continue

        # prof deleted/moved the file but left the topic up — owl shows it broken too, nothing to get
        if topic.get("IsBroken"):
            stats["broken"] += 1
            continue

        await fetch(
            key, topic.get("Title"), Path(*trail) / _filename(topic), topic.get("LastModifiedDate"), "/".join(trail),
            lambda t=topic: client.topic_file(course["id"], t["TopicId"]),
        )

    # files linked from module descriptions instead of being topics (see _COURSEFILE_LINK)
    # profs re-link the same notes in "Week 4 (Continued)" etc — keep each file once, under the first module
    seen: set[str] = set()
    for trail, module in _walk_modules(toc.get("Modules", [])):
        prefix = module.get("DefaultPath")  # e.g. /content/enforced/203951-UGRD_1269_11219/
        html = (module.get("Description") or {}).get("Html") or ""
        if not prefix:
            continue
        for file_id in _COURSEFILE_LINK.findall(html):
            remote = unquote_plus(file_id)  # "Notes%2fLec 1.pdf" -> "Notes/Lec 1.pdf"
            if remote in seen:  # also catches link + iframe preview of the same file
                continue
            seen.add(remote)
            name = safe(PurePosixPath(remote).name)
            await fetch(
                f"coursefile:{remote}", name, Path(*trail) / name, module.get("LastModifiedDate"), "/".join(trail),
                lambda url=prefix.rstrip("/") + "/" + quote(remote): client.get_bytes(url),
            )

    manifest_path.write_text(json.dumps(manifest, indent=2))
    stats["folder"] = str(cdir)
    return stats


def _has_preferred_twin(txt: Path) -> bool:
    # profs often post the same lecture as "Lecture16.pdf" AND "Lecture16.pptx" in one folder.
    # searching both = every hit shows up twice. keep the pptx (cleaner text + slide numbers), skip the pdf
    original = txt.with_suffix("")  # "Lecture16.pdf.txt" -> "Lecture16.pdf"
    if original.suffix.lower() != ".pdf":
        return False
    return (original.parent / (original.stem + ".pptx.txt")).exists()


def search(query: str, course_folder: Path | None = None, max_hits: int = 20) -> list[dict]:
    """dumb-but-effective full text search over every extracted .txt (case-insensitive)

    for a few courses' worth of slides this is instant. if it ever gets slow,
    that's when u'd swap in sqlite fts or embeddings
    """
    root = course_folder or config.SYNC_DIR
    q = query.lower()
    hits = []
    for txt in sorted(root.rglob("*.txt")):
        if txt.name.startswith("_") or _has_preferred_twin(txt):
            continue
        text = txt.read_text(errors="ignore")
        low = text.lower()
        start = 0
        while (i := low.find(q, start)) != -1:
            # find the last "--- page N ---" / "--- slide N ---" marker before the match = where it is
            marker = re.findall(r"--- (?:page|slide) \d+ ---", text[:i])
            hits.append({
                "file": str(txt.relative_to(config.SYNC_DIR))[: -len(".txt")],  # show original filename
                "where": marker[-1].strip("- ") if marker else "",
                "snippet": " ".join(text[max(0, i - 150): i + len(q) + 150].split()),  # ±150 chars, whitespace squashed
            })
            if len(hits) >= max_hits:
                return hits
            start = i + len(q) + 150  # skip past this snippet so nearby repeats don't show up twice
    return hits
