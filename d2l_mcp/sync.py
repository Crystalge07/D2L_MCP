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
from urllib.parse import unquote

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
    stats = {"downloaded": 0, "unchanged": 0, "links": 0, "failed": []}

    for trail, topic in _walk(toc.get("Modules", [])):
        key = str(topic["TopicId"])

        # "Link" topics (youtube, external sites) have nothing to download — just remember the url
        if topic.get("TypeIdentifier") != "File":
            manifest[key] = {"title": topic.get("Title"), "type": "link", "url": topic.get("Url"), "module": "/".join(trail)}
            stats["links"] += 1
            continue

        modified = topic.get("LastModifiedDate")
        prev = manifest.get(key)
        # skip if we already have it AND prof hasn't re-uploaded since
        if prev and prev.get("modified") == modified and (cdir / prev["path"]).exists():
            stats["unchanged"] += 1
            continue

        rel = Path(*trail) / _filename(topic)
        dest = cdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            dest.write_bytes(await client.topic_file(course["id"], topic["TopicId"]))
            text = extract_text(dest)
            if text is not None:
                # sidecar .txt right next to the original, e.g. "Lecture 5.pdf.txt"
                dest.with_name(dest.name + ".txt").write_text(text)
        except Exception as e:  # one broken file shouldn't kill the whole sync
            stats["failed"].append(f"{rel}: {e}")
            continue

        manifest[key] = {"title": topic.get("Title"), "type": "file", "path": str(rel), "modified": modified, "module": "/".join(trail)}
        stats["downloaded"] += 1

    manifest_path.write_text(json.dumps(manifest, indent=2))
    stats["folder"] = str(cdir)
    return stats


def search(query: str, course_folder: Path | None = None, max_hits: int = 20) -> list[dict]:
    """dumb-but-effective full text search over every extracted .txt (case-insensitive)

    for a few courses' worth of slides this is instant. if it ever gets slow,
    that's when u'd swap in sqlite fts or embeddings
    """
    root = course_folder or config.SYNC_DIR
    q = query.lower()
    hits = []
    for txt in sorted(root.rglob("*.txt")):
        if txt.name.startswith("_"):
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
