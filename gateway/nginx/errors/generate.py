#!/usr/bin/env python3
"""
Writes the gateway's error pages, one file per status code.

These replace the pages Nginx generates itself -- a backend that cannot be
reached, a rate limit, a body too large -- which otherwise name Nginx in their
footer. They are deliberately static and self-contained: no fonts, scripts or
images fetched from anywhere, because the moment they are shown is often the
moment something else is down.

The request ID and host are filled in by Nginx's SSI at serve time, so the
page a user screenshots carries the same ID as the X-Request-Id header and the
access log line.

Run it after editing PAGES or the template, and commit the output:

    python3 gateway/nginx/errors/generate.py           # write
    python3 gateway/nginx/errors/generate.py --check   # CI: fail if stale
"""
import sys
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent

# code -> (status shown, title, explanation). 494 is Nginx's internal code for
# oversized request headers; it is answered as a 400 (see the snippet), but
# keeps its own wording because "bad request" says nothing useful about it.
PAGES = {
    400: (400, "Bad request",
          "The request could not be understood. Check the address and try again."),
    403: (403, "Access denied",
          "You do not have permission to reach this page from where you are."),
    404: (404, "Page not found",
          "There is nothing at this address. Check it for typos, or start from the home page."),
    405: (405, "Method not allowed",
          "This address does not accept that kind of request."),
    408: (408, "Request timed out",
          "The request took too long to arrive. Check your connection and try again."),
    413: (413, "Request too large",
          "What was sent is larger than this site accepts."),
    414: (414, "Address too long",
          "The address requested is longer than this site accepts."),
    429: (429, "Too many requests",
          "Too many requests arrived from your address. Wait a moment, then try again."),
    494: (400, "Request headers too large",
          "The request carried more header data than this site accepts. "
          "Clearing this site's cookies usually fixes it."),
    500: (500, "Something went wrong",
          "The gateway could not complete this request. Try again in a moment."),
    502: (502, "Site unreachable",
          "The site's server is not responding correctly. This is usually temporary; "
          "try again in a moment."),
    503: (503, "Temporarily unavailable",
          "The site cannot take this request right now. Try again in a moment."),
    504: (504, "Site took too long",
          "The site's server did not answer in time. This is usually temporary; "
          "try again in a moment."),
}

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{status} {title}</title>
<style>
:root {{
  --bg: #f6f7f9; --card: #ffffff; --ink: #14181f; --dim: #5b6472;
  --faint: #8a93a1; --line: #e3e6eb; --accent: #2f6fed;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0e1116; --card: #161a21; --ink: #e8ebf0; --dim: #a3abb8;
    --faint: #6c7584; --line: #262c36; --accent: #6e9bff;
  }}
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; height: 100%; }}
body {{
  background: var(--bg); color: var(--ink);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  display: flex; align-items: center; justify-content: center; padding: 24px 16px;
}}
main {{
  width: 100%; max-width: 480px; background: var(--card);
  border: 1px solid var(--line); border-radius: 14px; padding: 32px 28px;
}}
.code {{
  font: 600 0.8rem/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--accent); letter-spacing: 0.08em;
}}
h1 {{ font-size: 1.45rem; letter-spacing: -0.02em; margin: 10px 0 8px; }}
p {{ margin: 0; color: var(--dim); }}
dl {{
  margin: 24px 0 0; padding-top: 16px; border-top: 1px solid var(--line);
  display: grid; grid-template-columns: auto 1fr; gap: 4px 14px;
  font-size: 0.8rem; color: var(--faint);
}}
dd {{
  margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  overflow-wrap: anywhere;
}}
footer {{ margin-top: 20px; font-size: 0.75rem; color: var(--faint); }}
</style>
</head>
<body>
<main>
  <div class="code">ERROR {status}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <dl>
    <dt>Host</dt><dd><!--# echo var="host" default="" --></dd>
    <dt>Request ID</dt><dd><!--# echo var="request_id" default="" --></dd>
  </dl>
  <footer>Served by CloudBalancer</footer>
</main>
</body>
</html>
"""


def render() -> dict[str, str]:
    return {
        f"{code}.html": TEMPLATE.format(
            status=status, title=escape(title), message=escape(message)
        )
        for code, (status, title, message) in PAGES.items()
    }


def main() -> int:
    pages = render()
    check = "--check" in sys.argv[1:]
    stale = []

    for name, content in pages.items():
        path = HERE / name
        if path.exists() and path.read_text() == content:
            continue
        if check:
            stale.append(name)
        else:
            path.write_text(content)

    orphans = sorted(
        p.name for p in HERE.glob("*.html") if p.name not in pages
    )
    if check and (stale or orphans):
        for name in stale:
            print(f"stale or missing: {name}")
        for name in orphans:
            print(f"not generated by this script: {name}")
        print("Run: python3 gateway/nginx/errors/generate.py")
        return 1
    if not check:
        for name in orphans:
            (HERE / name).unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
