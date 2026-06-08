"""
Build a static-HTML version of the Website/ frontend for GitHub Pages.

The Flask backend on the Linux Mint box keeps running and serving the JSON
APIs at api.yourdomain.com. The static frontend lives on GitHub Pages at
yourdomain.com (or your-username.github.io), calls those JSON APIs, and
renders the same pages.

What this script does:
  1. Reads Website/templates/*.html
  2. Strips all Jinja ({{ ... }} and {% ... %}) and replaces url_for() with
     relative file paths so the static site works on GitHub Pages.
  3. Renames snake_case templates to hyphenated filenames and copies
     home.html -> index.html.
  4. Copies Website/static/* into dist-frontend/static/.
  5. Copies Website/graphics/* into dist-frontend/graphics/.
  6. Injects window.BTS_API_BASE at the top of shared.js so the frontend
     knows where the backend lives.
  7. Writes a 404.html that bounces unknown paths back to index.html.

The output is a plain folder (default: dist-frontend/) that can be pushed
to a gh-pages branch on the public frontend repo, or served by any static
host.

Run from the Beyond-the-Stats/ root:
  python scripts/build_static_frontend.py
  python scripts/build_static_frontend.py --api-base https://api.yourdomain.com
  python scripts/build_static_frontend.py --out my-static-build
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "Website"
TEMPLATES = WEBSITE / "templates"
STATIC = WEBSITE / "static"
GRAPHICS = WEBSITE / "graphics"
DEFAULT_OUT = ROOT / "dist-frontend"

TEMPLATE_RENAMES = {
    "home.html": "index.html",
    "upcoming_matches.html": "upcoming-matches.html",
    "head_to_head.html": "head-to-head.html",
    "league_tables.html": "league-tables.html",
    "world_cup.html": "world-cup.html",
}

# Flask route -> static filename mapping for in-page href rewriting.
ROUTE_TO_FILE = {
    "/": "index.html",
    "/upcoming-matches": "upcoming-matches.html",
    "/cups": "cups.html",
    "/head-to-head": "head-to-head.html",
    "/league-tables": "league-tables.html",
    "/world-cup": "world-cup.html",
    "/players": "players.html",
    "/tactics": "tactics.html",
    "/about": "about.html",
}

# Each route appears as href="/foo" in the source HTML; rewrite to the local file.
# The empty alternative handles href="/" (the home link).
HREF_TO_FILE = re.compile(
    r'href="/(' + "|".join(re.escape(k.lstrip("/")) for k in ROUTE_TO_FILE) + r'|)"'
)

# url_for('serve_graphic', filename='X') -> 'graphics/X'
URL_FOR_GRAPHIC = re.compile(
    r"\{\{\s*url_for\(\s*['\"]serve_graphic['\"]\s*,\s*filename\s*=\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}"
)

# url_for('static', filename='X'[, v='Y']) -> 'static/X[?v=Y]'
URL_FOR_STATIC = re.compile(
    r"\{\{\s*url_for\(\s*['\"]static['\"]\s*,\s*filename\s*=\s*['\"]([^'\"]+)['\"](?:\s*,\s*v\s*=\s*['\"]([^'\"]+)['\"])?\s*\)\s*\}\}"
)

# {{ 'active' if active_page == 'X' else '' }} -> '' (active class set by shared.js)
ACTIVE_CLASS = re.compile(
    r"\{\{\s*'active'\s+if\s+active_page\s*==\s*['\"][^'\"]+['\"]\s+else\s+['\"][^'\"]*['\"]\s*\}\}"
)

# {{ something_else if cond else other }} -> ''
GENERIC_JINJA_EXPR = re.compile(r"\{\{[^}]*\}\}")

# {% ... %} block -> ''
JINJA_BLOCK = re.compile(r"\{%[^%]*%\}")


def strip_jinja(html: str) -> str:
    """Strip all Jinja expressions and blocks, replacing url_for() with paths."""
    html = URL_FOR_GRAPHIC.sub(lambda m: f"graphics/{m.group(1)}", html)
    html = URL_FOR_STATIC.sub(lambda m: f"static/{m.group(1)}" + (f"?v={m.group(2)}" if m.group(2) else ""), html)
    html = ACTIVE_CLASS.sub("", html)
    html = JINJA_BLOCK.sub("", html)
    html = GENERIC_JINJA_EXPR.sub("", html)
    # Rewrite in-page navigation hrefs from Flask routes to static filenames.
    def _route_to_file(match: re.Match) -> str:
        # match.group(1) is the path with no leading slash, e.g. "about" or "" (home)
        path = match.group(1)
        route = "/" + path
        return f'href="{ROUTE_TO_FILE[route]}"'
    html = HREF_TO_FILE.sub(_route_to_file, html)
    return html


def inject_api_base_into_shared_js(out_static_dir: Path, api_base: str) -> None:
    """Prepend a window.BTS_API_BASE assignment to shared.js."""
    if not api_base:
        return
    target = out_static_dir / "shared.js"
    if not target.exists():
        print(f"[build] WARNING: {target} not found; skipping API base injection", file=sys.stderr)
        return
    text = target.read_text(encoding="utf-8")
    if "window.BTS_API_BASE" in text:
        return
    injection = f'// Auto-injected by build_static_frontend.py\nwindow.BTS_API_BASE = "{api_base}";\n'
    target.write_text(injection + text, encoding="utf-8")


def write_404(out_dir: Path) -> None:
    """Write a 404.html that soft-redirects to the home page on GitHub Pages."""
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Page not found &middot; Beyond The Stats</title>
  <meta http-equiv="refresh" content="2; url=./index.html">
  <link rel="stylesheet" href="static/styles.css">
</head>
<body class="dark-mode">
  <main class="page">
    <header class="site-header"><div class="site-header-top">
      <a class="brand" href="./index.html">
        <div><div class="brand-name">Beyond The Stats</div></div>
      </a>
    </div></header>
    <section class="card">
      <h3>Page not found</h3>
      <p>Redirecting to the home page&hellip;</p>
      <p><a href="./index.html">Click here</a> if you are not redirected.</p>
    </section>
  </main>
</body>
</html>
"""
    (out_dir / "404.html").write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--api-base",
        default=os.environ.get("BTS_API_BASE", ""),
        help="Backend API base URL injected as window.BTS_API_BASE (default: empty = same-origin)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Output directory (default: {DEFAULT_OUT.name}/)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe the output directory before building",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        # Resolve relative paths against the script's ROOT, not CWD, so the
        # script behaves consistently regardless of where it is invoked from
        # (e.g. Cloudflare Pages runs the build command with a different CWD
        # than the script's location).
        out_dir = (ROOT / out_dir).resolve()
    if out_dir.exists():
        if args.clean:
            shutil.rmtree(out_dir)
        else:
            print(f"[build] {out_dir} already exists; pass --clean to wipe first", file=sys.stderr)
            return 1
    out_dir.mkdir(parents=True)

    out_static = out_dir / "static"
    out_graphics = out_dir / "graphics"
    out_static.mkdir()
    out_graphics.mkdir()

    if not TEMPLATES.is_dir():
        print(f"[build] FATAL: {TEMPLATES} not found", file=sys.stderr)
        return 1
    if not STATIC.is_dir():
        print(f"[build] FATAL: {STATIC} not found", file=sys.stderr)
        return 1
    if not GRAPHICS.is_dir():
        print(f"[build] WARNING: {GRAPHICS} not found; image assets will be missing", file=sys.stderr)

    # 1. Templates
    n_templates = 0
    for src in sorted(TEMPLATES.glob("*.html")):
        target_name = TEMPLATE_RENAMES.get(src.name, src.name)
        target = out_dir / target_name
        html = src.read_text(encoding="utf-8")
        html = strip_jinja(html)
        target.write_text(html, encoding="utf-8")
        n_templates += 1
        print(f"[build] template {src.name} -> {target_name} ({len(html):,} bytes)")
    print(f"[build] wrote {n_templates} HTML files")

    # 2. Static
    if STATIC.is_dir():
        n_files = 0
        for src in STATIC.iterdir():
            if src.is_file():
                shutil.copy2(src, out_static / src.name)
                n_files += 1
        print(f"[build] copied {n_files} static files")

    # 3. Graphics
    if GRAPHICS.is_dir():
        n_files = 0
        for src in GRAPHICS.iterdir():
            if src.is_file():
                shutil.copy2(src, out_graphics / src.name)
                n_files += 1
        print(f"[build] copied {n_files} graphics files")

    # 4. Inject API base URL into shared.js
    if args.api_base:
        inject_api_base_into_shared_js(out_static, args.api_base)
        print(f"[build] injected window.BTS_API_BASE = {args.api_base!r}")
    else:
        print("[build] no --api-base given; shared.js will use same-origin (relative)")

    # 5. 404 page
    write_404(out_dir)
    print(f"[build] wrote 404.html")

    # 6. _redirects for Cloudflare Pages SPA fallback
    redirects = (out_dir / "_redirects")
    if not redirects.exists():
        redirects.write_text(
            "# Cloudflare Pages SPA fallback — existing files take priority\n"
            "# Each app route serves index.html so client-side routing works.\n"
            "/head-to-head  /index.html  200\n"
            "/league-tables  /index.html  200\n"
            "/upcoming-matches  /index.html  200\n"
            "/world-cup  /index.html  200\n"
            "/cups  /index.html  200\n"
            "/tactics  /index.html  200\n"
            "/about  /index.html  200\n",
            encoding="utf-8",
        )
        print("[build] wrote _redirects (SPA fallback)")

    # Summary
    total = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    print(f"[build] DONE: {out_dir} ({total / 1024:.1f} KB total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
