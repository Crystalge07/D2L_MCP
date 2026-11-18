"""login flow: open a real browser, u log in (sso + mfa), we steal (borrow) the session cookies

why a real browser: western uses microsoft sso + mfa, so there's no way to log in with just
a username/password from code. we let u do the login like normal, then save the cookies
d2l gave the browser. the mcp server sends those cookies w/ every api request = acts as u.
"""
import asyncio
import json

from . import config


class NotLoggedIn(Exception):
    """no cookie file yet, or the session expired — either way u gotta run the login cmd"""


def load_cookies() -> dict[str, str]:
    # cookies.json is just {name: value}, written by login() below
    if not config.COOKIE_FILE.exists():
        raise NotLoggedIn("no saved D2L session. run `d2l-mcp-login` in a terminal first.")
    return json.loads(config.COOKIE_FILE.read_text())


async def login() -> None:
    # import here so the server itself doesn't need playwright loaded just to run
    from playwright.async_api import async_playwright

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # persistent context = browser profile saved to disk, so microsoft remembers u
        # and next login is often just one click (or zero) instead of the full mfa dance
        ctx = await p.chromium.launch_persistent_context(
            str(config.BROWSER_PROFILE),
            headless=False,  # u need to SEE it to log in lol
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        await page.goto(f"{config.BASE_URL}/d2l/home")
        print("👉 log in in the browser window that just opened (sso + mfa).")
        print("   i'll grab the session automatically once u land on the D2L homepage.")

        # can't just wait for the url to be /d2l/home: when logged out, d2l serves a 200 page AT
        # /d2l/home that js-redirects to /d2l/login, so the url check would pass before u log in.
        # instead poll an api call that only succeeds once the session is real. 5 min for slow mfa
        whoami = f"{config.BASE_URL}/d2l/api/lp/1.0/users/whoami"
        for _ in range(5 * 60 // 2):
            r = await ctx.request.get(whoami, max_redirects=0)
            if r.ok:
                break
            await asyncio.sleep(2)
        else:
            await ctx.close()
            raise SystemExit("❌ timed out waiting for login (5 min). run d2l-mcp-login again.")

        # only cookies that apply to the brightspace domain (skip microsoft's etc)
        cookies = await ctx.cookies(config.BASE_URL)
        await ctx.close()

    jar = {c["name"]: c["value"] for c in cookies}

    # sanity check — d2lSessionVal + d2lSecureSessionVal are THE session cookies
    if "d2lSessionVal" not in jar:
        print("⚠️  didn't see d2lSessionVal — login might not have worked. saving anyway.")

    config.COOKIE_FILE.write_text(json.dumps(jar, indent=2))
    config.COOKIE_FILE.chmod(0o600)  # cookies = ur login. only u should be able to read this file
    print(f"✅ saved session to {config.COOKIE_FILE}")


def main() -> None:
    # entry point for the `d2l-mcp-login` command (see pyproject.toml)
    asyncio.run(login())


if __name__ == "__main__":
    main()
