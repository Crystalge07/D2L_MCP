# d2l-mcp

MCP server for D2L Brightspace (Western OWL). Lets Claude check announcements, due dates (and whether you already submitted), assignment instructions, grades, and search your actual lecture slides. No more screenshots.

**Read-only by design.** It only ever sends GET requests to OWL. It can't submit, post, or change anything, and downloading files doesn't mark them as read in OWL.

## Setup (once)

Needs [uv](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
cd d2l-mcp
uv sync                               # install deps
uv run playwright install chromium    # browser used for login
uv run d2l-mcp-login                  # optional: log in now (or later from Claude with login_to_owl)
```

## Hook it up

> **Only works in apps on your computer**: the Claude desktop app and Claude Code. claude.ai in a web browser can't reach a server running on your laptop.

**Claude desktop app:** add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows: `%APPDATA%\Claude\claude_desktop_config.json`), then fully quit (⌘Q) and reopen the app.

```json
{
  "mcpServers": {
    "d2l": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/FULL/PATH/TO/d2l-mcp", "run", "d2l-mcp"]
    }
  }
}
```
Use the **full path** to uv (find it with `which uv`). The desktop app doesn't see your shell's PATH, so `"command": "uv"` fails.

**Claude Code** (available in every folder):
```bash
claude mcp add --scope user d2l -- uv --directory /FULL/PATH/TO/d2l-mcp run d2l-mcp
```

## Logging in
Just ask Claude to "log me into OWL" (the `login_to_owl` tool). A browser window opens, you sign in with Microsoft + MFA, and it closes by itself. Claude also offers this whenever a tool says the session expired. There's no need to restart anything afterwards.

From a terminal instead: `uv run d2l-mcp-login` in this folder.

## Tools

| live from OWL | local copies (synced) |
|---|---|
| `login_to_owl` | `sync_course_files` / `sync_all_courses` |
| `list_courses` | `search_course_files` |
| `get_announcements` | `read_course_file` |
| `get_upcoming_due_dates` (marks ✅ submitted) | `list_synced_files` |
| `get_assignment_details` (instructions, files, status) | |
| `get_grades` | |
| `get_course_outline` | |

Courses can be referred to loosely ("2211", "data structures"). "Current" courses are figured out from OWL's start dates: half courses (2211A, 2111F…) for one term, full-year courses for two.

### What sync grabs
Into `~/.d2l-mcp/courses/<course>/<module>/...`, with a `.txt` of the extracted text next to each file:
- content files (PDF, PPTX, DOCX, HTML, code, and code inside `.zip`s)
- files linked from module descriptions and OWL pages (some profs never post files as topics)
- assignment instructions + attachments, under `Assignment instructions/`

It skips Keynote, video and audio files (nothing to search), and decorative images in pages. Re-syncing only downloads what changed, and deletes local copies of files the prof removed. A first sync can take a few minutes, so it runs in the background: if Claude says "still syncing", it just asks again.

Search ignores line breaks, shows at most 3 hits per file, and hides a lecture's PDF when the same-named PPTX exists.

## Env vars
- `D2L_BASE_URL` (default `https://westernu.brightspace.com`)
- `D2L_DATA_DIR` (default `~/.d2l-mcp`)

## Tests
`uv run python tests/test_fake_d2l.py` runs every tool against a fake D2L, with no network needed.

## Ideas for v2
- Embeddings search instead of keyword search ("explain the thing about stack frames")
- Calendar events, discussions, rubric/feedback on submissions
- Quiz attempt status (OWL doesn't expose it to students the way it does assignments)
- OCR for scanned PDFs (currently they extract as blank)
- Old `.ppt` files (convert with LibreOffice)
