# d2l-mcp

MCP server for D2L Brightspace (Western OWL). Lets Claude check announcements, due dates, grades, and search your actual lecture slides. No more screenshots.

## Setup (once)

Needs [uv](https://docs.astral.sh/uv/) (`brew install uv` or `pip install uv`).

```bash
cd d2l-mcp
uv sync                               # install deps
uv run playwright install chromium    # browser used for login
uv run d2l-mcp-login                  # opens a browser, log in w/ SSO + MFA, cookies get saved
```

### Sanity check before plugging into Claude
While logged into OWL in your normal browser, open:
- `https://westernu.brightspace.com/d2l/api/versions/` should show JSON with `lp` / `le` versions
- `https://westernu.brightspace.com/d2l/api/lp/<lp version>/enrollments/myenrollments/` should show JSON with your courses

If both show JSON, the cookie approach works for Western and the MCP will too.

## Hook it up

**Claude Desktop (normal chats):** edit the config
- mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
- windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "d2l": {
      "command": "uv",
      "args": ["--directory", "/FULL/PATH/TO/d2l-mcp", "run", "d2l-mcp"]
    }
  }
}
```
Then restart Claude Desktop.

**Claude Code:**
```bash
claude mcp add d2l -- uv --directory /FULL/PATH/TO/d2l-mcp run d2l-mcp
```

## Tools

| live (always fresh) | local files (synced) |
|---|---|
| `list_courses` | `sync_course_files` / `sync_all_courses` |
| `get_announcements` | `search_course_files` |
| `get_upcoming_due_dates` | `read_course_file` |
| `get_grades` | `list_synced_files` |
| `get_course_outline` | |

Courses can be referred to loosely ("2211", "data structures").

Synced files land in `~/.d2l-mcp/courses/<course>/<module>/...`, with a `.txt` of the extracted text next to each file (PDF, PPTX, DOCX, HTML and code files). Re-syncing only downloads files that changed.

## When the session expires
Tools will say "session expired". Run `uv run d2l-mcp-login` again. The browser profile is saved, so it's usually quick. There's no need to restart Claude.

## Env vars
- `D2L_BASE_URL` (default `https://westernu.brightspace.com`)
- `D2L_DATA_DIR` (default `~/.d2l-mcp`)

## Tests
`uv run python tests/test_fake_d2l.py` runs every tool against a fake D2L, with no network needed.

## Ideas for v2
- Embeddings search instead of keyword search ("explain the thing about stack frames")
- Calendar events, discussions, rubrics/feedback on submissions
- OCR for scanned PDFs (currently they extract as blank)
- Old `.ppt` files (convert with LibreOffice)
