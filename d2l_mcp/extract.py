"""turn downloaded files into plain text so claude can search + read them

page/slide markers ("--- page 4 ---") are on purpose: lets claude say
"this is on slide 12 of lecture 5" instead of just vibes
"""
import html
import logging
import re
from pathlib import Path

# pypdf spams "Ignoring wrong pointing object" etc for lots of prof-made pdfs. harmless, hide it
logging.getLogger("pypdf").setLevel(logging.ERROR)

# file types we just read as-is (code, notes, etc)
PLAIN = {".txt", ".md", ".c", ".h", ".cpp", ".hpp", ".py", ".java", ".sh", ".csv", ".json", ".sql"}


def strip_html(s: str) -> str:
    # good-enough html -> text. announcements + html topics are simple, no need for bs4
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)  # drop js/css blocks entirely
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)  # keep line breaks where they matter
    s = re.sub(r"<[^>]+>", "", s)                         # nuke all remaining tags
    s = html.unescape(s)                                  # &amp; -> &
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_text(path: Path) -> str | None:
    """returns text, or None if we don't know how to read this file type"""
    ext = path.suffix.lower()

    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        # extract_text() can return None on image-only pages (scanned stuff) -> ""
        return "\n\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages, 1))

    if ext == ".pptx":
        from pptx import Presentation
        out = []
        for i, slide in enumerate(Presentation(str(path)).slides, 1):
            # every shape that has text (title, body, text boxes). tables/images skipped
            texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.strip()]
            # speaker notes often have the actual explanation profs say out loud — grab those too
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                texts.append("[notes] " + slide.notes_slide.notes_text_frame.text)
            out.append(f"--- slide {i} ---\n" + "\n".join(texts))
        return "\n\n".join(out)

    if ext == ".docx":
        from docx import Document
        return "\n".join(p.text for p in Document(str(path)).paragraphs)

    if ext in (".html", ".htm"):
        return strip_html(path.read_text(errors="ignore"))

    if ext in PLAIN:
        return path.read_text(errors="ignore")

    if ext == ".zip":
        # lecture code usually comes zipped. pull out the text/code files inside (skip mac junk + huge files)
        import zipfile
        out = []
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                name = info.filename
                if info.is_dir() or "__MACOSX" in name or Path(name).name.startswith("."):
                    continue
                if Path(name).suffix.lower() in PLAIN | {".html", ".htm"} and info.file_size <= 2_000_000:
                    out.append(f"--- file {name} ---\n" + z.read(info).decode("utf-8", errors="ignore"))
        return "\n\n".join(out) or None

    return None  # images, .ppt (old format) etc — saved but not searchable
