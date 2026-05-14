#!/usr/bin/env python3
"""
OCGT static prerender — generates one HTML file per route from the master
SPA file, baking the correct <title>, <meta description>, canonical URL,
og:url and active-page CSS class into each variant.

This solves the "search-engine sees 23 competing H1s on /" SEO problem
without changing the source HTML or the SPA runtime: the SPA still works
identically client-side, but Google crawls each route's static prerender
and indexes it as a unique page.

Usage:
    python3 build-prerender.py                  # outputs to ./dist
    python3 build-prerender.py --out _site      # custom output dir

Workflow:
    1. Edit OCGT_website.html as before
    2. Run this script before deploy
    3. Upload contents of dist/ to the server (preserves /geotechnik, etc.)
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

# ── Source-of-truth route map. Mirror of ROUTES in OCGT_website.html. ──
# ── R2 / CDN config ────────────────────────────────────────────────────
# R2_BASE_URL is read from the environment (set in Cloudflare Pages/Workers
# dashboard → Variables and Secrets) so you can change the CDN host without
# touching code. Local builds fall back to the hardcoded default.
# Set R2_BASE_URL='' to bundle media into dist/ instead of rewriting to R2.
R2_BASE_URL   = os.environ.get('R2_BASE_URL', 'https://assets.ocgt.de').rstrip('/')
USE_R2        = bool(R2_BASE_URL)
R2_DIRS       = ['Images', 'logos', 'icons', 'company_logos', 'marketing', 'Videos']

# Turnstile site key — injected at build time from env. Falls back to
# Cloudflare's "always passes" test key if unset, so dev builds still render.
TURNSTILE_SITE_KEY = os.environ.get('TURNSTILE_SITE_KEY', '1x00000000000000000000AA')

ROUTES = {
    '':                        'home',
    'geotechnik':              'geotechnik',
    'bauueberwachung':         'a1',
    'vergabe':                 'a2',
    'grundwasser':             'a3',
    'beratung':                'a4',
    'reality-capture':         'rc',
    '3d-vermessung':           'b1',
    'baustellendokumentation': 'b2',
    'inspektionen':            'b3',
    'digitale-zwillinge':      'b4',
    'thermografie':            'b5',
    'multispektral':           'b6',
    'video-film':              'b7',
    'technologie':             'tech',
    'ueber-uns':               'about',
    'referenzen':              'refs',
    'kontakt':                 'contact',
    'impressum':               'impressum',
    'datenschutz':             'datenschutz',
}


def extract_meta_map(html: str) -> dict:
    """Pull the per-page META object out of OCGT_website.html so we can
    bake the right title/description into each prerendered file."""
    m = re.search(r'const META\s*=\s*\{(.*?)\n\};', html, re.DOTALL)
    if not m:
        raise SystemExit('Could not locate META = { ... } in source HTML')
    body = m.group(1)
    out = {}
    # Each entry looks like:  pageId: { de:{t:'...',d:'...'}, en:{t:'...',d:'...'} },
    pattern = re.compile(
        r"(\w+)\s*:\s*\{\s*de:\{t:'([^']*)',d:'([^']*)'\}\s*,\s*"
        r"en:\{t:'([^']*)',d:'([^']*)'\}\s*\}",
        re.DOTALL,
    )
    for pid, dt, dd, et, ed in pattern.findall(body):
        out[pid] = {'de_t': dt, 'de_d': dd, 'en_t': et, 'en_d': ed}
    return out


def html_escape(s: str) -> str:
    return (s.replace('&', '&amp;')
             .replace('"', '&quot;')
             .replace('<', '&lt;')
             .replace('>', '&gt;'))


def prerender_one(html: str, page_id: str, slug: str, meta: dict) -> str:
    """Return a copy of html with the requested page set as active and
    its title / description / canonical baked in."""
    info = meta.get(page_id, meta['home'])
    title = info['de_t']
    desc = html_escape(info['de_d'])
    canonical = 'https://ocgt.de/' + slug if slug else 'https://ocgt.de/'

    # 1) replace <title>
    html = re.sub(
        r'<title id="pg-title">.*?</title>',
        f'<title id="pg-title">{html_escape(title)}</title>',
        html, count=1)

    # 2) replace meta description
    html = re.sub(
        r'<meta id="pg-desc" name="description" content="[^"]*">',
        f'<meta id="pg-desc" name="description" content="{desc}">',
        html, count=1)

    # 3) replace canonical
    html = re.sub(
        r'<link rel="canonical" href="[^"]*">',
        f'<link rel="canonical" href="{canonical}">',
        html, count=1)

    # 4) replace og:url
    html = re.sub(
        r'<meta property="og:url" content="[^"]*">',
        f'<meta property="og:url" content="{canonical}">',
        html, count=1)

    # 5) replace og:title + twitter:title
    og_title = html_escape(title)
    html = re.sub(
        r'<meta property="og:title" content="[^"]*">',
        f'<meta property="og:title" content="{og_title}">',
        html, count=1)
    html = re.sub(
        r'<meta name="twitter:title" content="[^"]*">',
        f'<meta name="twitter:title" content="{og_title}">',
        html, count=1)

    # 6) replace og:description + twitter:description
    html = re.sub(
        r'<meta property="og:description" content="[^"]*">',
        f'<meta property="og:description" content="{desc}">',
        html, count=1)
    html = re.sub(
        r'<meta name="twitter:description" content="[^"]*">',
        f'<meta name="twitter:description" content="{desc}">',
        html, count=1)

    # 7) Mark the target page as active (.on) so it's visible without JS.
    #    First strip any existing .on class from `<div id="p-*" class="page on">`
    html = re.sub(
        r'(<div id="p-[a-z0-9-]+"\s+class="page) on(")',
        r'\1\2',
        html)
    #    Then add .on to the requested page
    html = re.sub(
        rf'(<div id="p-{re.escape(page_id)}"\s+class="page)("[ >])',
        r'\1 on\2',
        html, count=1)

    # 8) Single-H1 SEO: keep only the H1 inside the active page section.
    #    All other <h1> tags (in inactive page sections AND the global
    #    sr-only H1) are demoted to <h2> so Googlebot sees exactly one H1
    #    per prerendered route. Visual styling is class-based so no CSS
    #    change is needed; this is a semantic-only swap.
    PAGE_DIV_RE = re.compile(
        r'(<div id="p-([a-z0-9-]+)"\s+class="page[^"]*">)(.*?)(?=<div id="p-[a-z0-9-]+"\s+class="page|</main>)',
        re.DOTALL,
    )
    def _demote_inactive(match):
        opener, pid, body = match.group(1), match.group(2), match.group(3)
        if pid == page_id:
            return opener + body
        return opener + re.sub(r'<(/?)h1(\b)', r'<\1h2\2', body)
    html = PAGE_DIV_RE.sub(_demote_inactive, html)

    # Also demote the global sr-only H1s outside the page sections when the
    # active route isn't home (they describe the home page).
    if page_id != 'home':
        html = re.sub(
            r'(<h1 class="sr-only[^"]*"[^>]*>.*?)</h1>',
            r'\1</h2>',
            html, flags=re.DOTALL,
        )
        html = re.sub(
            r'<h1(\s+class="sr-only)',
            r'<h2\1',
            html,
        )

    # 9) Strip inactive page sections so each prerendered route ships only
    #    its own content. Drops HTML from ~1180 KB → ~250 KB per route,
    #    fixes the crawl/html-size error and reduces DOM size.
    #    Inactive sections are replaced with a stub <div id="p-X" class="page"
    #    data-route-stub> so the client router can detect them and lazy-fetch
    #    the full content when the user navigates.
    DIV_OPEN = re.compile(r'<div')
    DIV_CLOSE = re.compile(r'</div>')

    def find_section_bounds(text, start_idx):
        """Walk forward from start_idx (pointing at <div id="p-...) counting
        nested divs until depth hits 0. Returns the index just past the
        matching </div>."""
        depth = 0
        i = start_idx
        n = len(text)
        while i < n:
            o = text.find('<div', i)
            c = text.find('</div>', i)
            if c == -1:
                return -1
            if o != -1 and o < c:
                depth += 1
                i = o + 4
            else:
                depth -= 1
                i = c + 6
                if depth == 0:
                    return i
        return -1

    PAGE_OPEN_RE = re.compile(r'<div id="p-([a-z0-9-]+)"\s+class="page[^"]*">')
    out_parts = []
    cursor = 0
    for m in PAGE_OPEN_RE.finditer(html):
        pid = m.group(1)
        start = m.start()
        end = find_section_bounds(html, start)
        if end == -1:
            continue  # malformed; skip stripping for safety
        out_parts.append(html[cursor:start])
        if pid == page_id:
            out_parts.append(html[start:end])
        else:
            # Stub keeps the slot in DOM but empty so the router can recognise
            # an unloaded route and fetch it lazily on first navigation.
            out_parts.append(f'<div id="p-{pid}" class="page" data-route-stub></div>')
        cursor = end
    if out_parts:
        out_parts.append(html[cursor:])
        html = ''.join(out_parts)

    return html


def main():
    parser = argparse.ArgumentParser(description='Prerender OCGT SPA into per-route static HTML.')
    parser.add_argument('--src', default='OCGT_website.html',
                        help='Source SPA HTML file (default: OCGT_website.html)')
    parser.add_argument('--out', default='dist',
                        help='Output directory (default: dist)')
    parser.add_argument('--copy-assets', action='store_true', default=True,
                        help='Copy static assets (images, css, js, etc.) to output dir')
    args = parser.parse_args()

    root = Path(__file__).parent.resolve()
    src_path = root / args.src
    out_path = root / args.out

    if not src_path.exists():
        sys.exit(f'Source file not found: {src_path}')

    print(f'OCGT static prerender')
    print(f'  source:  {src_path.name}')
    print(f'  output:  {out_path.name}/')
    print()

    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True)

    html = src_path.read_text(encoding='utf-8')
    meta = extract_meta_map(html)
    print(f'Loaded {len(meta)} META entries\n')

    # Generate one HTML file per route
    for slug, page_id in ROUTES.items():
        rendered = prerender_one(html, page_id, slug, meta)
        if slug == '':
            target = out_path / 'index.html'
        else:
            (out_path / slug).mkdir(parents=True, exist_ok=True)
            target = out_path / slug / 'index.html'
        target.write_text(rendered, encoding='utf-8')
        size_kb = target.stat().st_size / 1024
        print(f'  ✓ /{slug or "(home)":<28} → {target.relative_to(root)}  ({size_kb:.1f} KB)')

    # ── Extract inline <style> + <script> into shared external assets ──
    # The source file is a single editable monolith. The build splits CSS/JS
    # into shared external files so the browser caches them once across all
    # 20 prerendered pages instead of re-downloading 437 KB of CSS + 104 KB
    # of JS on every navigation.
    print('\nExtracting inline CSS/JS into shared assets...')
    assets_dir = out_path / 'assets'
    assets_dir.mkdir(parents=True, exist_ok=True)

    STYLE_RE = re.compile(r'<style[^>]*>(.*?)</style>', re.DOTALL)
    # Inline <script> tags, no src=, not JSON-LD, not the theme bootstrap.
    SCRIPT_RE = re.compile(r'<script(\b[^>]*)>(.*?)</script>', re.DOTALL)

    css_chunks = []
    js_chunks = []

    def is_extractable_script(attrs: str, body: str) -> bool:
        if 'src=' in attrs:
            return False
        if 'application/ld+json' in attrs:
            return False
        # Theme bootstrap IIFE must run synchronously before paint to prevent
        # flash-of-wrong-theme. Identified by being a short script that touches
        # localStorage('ocgt_theme'); the main app JS also references this key
        # in its toggle UI but is far longer.
        if len(body) < 500 and "ocgt_theme" in body:
            return False
        return True

    # Use the source HTML as the reference (all 20 prerendered files share
    # identical style/script blocks; only meta/title/active-class differ).
    src_html = html

    for m in STYLE_RE.finditer(src_html):
        css_chunks.append(m.group(1))
    for m in SCRIPT_RE.finditer(src_html):
        if is_extractable_script(m.group(1), m.group(2)):
            js_chunks.append(m.group(2))

    css_text = '\n\n/* ── boundary ── */\n\n'.join(c.strip() for c in css_chunks)
    js_text  = '\n\n/* ── boundary ── */\n\n'.join(j.strip() for j in js_chunks)
    # CSS lives at /assets/site.css — rewrite relative url(Images/...) refs
    # to absolute /Images/... so they resolve correctly from any route.
    _ASSET_DIRS_CSS = ['Images', 'logos', 'icons', 'company_logos', 'marketing', 'Videos']
    for d in _ASSET_DIRS_CSS:
        css_text = re.sub(
            rf'(url\(["\']?)({re.escape(d)}/)',
            rf'\g<1>/\g<2>',
            css_text,
        )

    # ── Production CSS minification (safe transforms only) ──────────────
    # Drops comments, collapses whitespace, trims redundant separators.
    # Saves ~50-65 KB on top of the merged 400+ KB stylesheet.
    css_pre_size = len(css_text)
    css_text = re.sub(r'/\*.*?\*/', '', css_text, flags=re.DOTALL)    # comments
    css_text = re.sub(r'\s+', ' ', css_text)                          # whitespace
    css_text = re.sub(r'\s*([{}:;,>+~])\s*', r'\1', css_text)         # around punctuation
    css_text = re.sub(r';}', '}', css_text)                           # trailing ;
    css_text = css_text.strip()
    print(f'  ✓ CSS minified: {css_pre_size/1024:.1f} KB → {len(css_text)/1024:.1f} KB')
    # ── Content-hash filenames for cache-busting ─────────────────────
    # Filenames embed the first 8 hex chars of the content's SHA-256.
    # When you change CSS/JS, the filename changes, so the immutable
    # 1-year browser cache automatically becomes a fresh request.
    import hashlib
    css_hash = hashlib.sha256(css_text.encode('utf-8')).hexdigest()[:8]
    js_hash  = hashlib.sha256(js_text.encode('utf-8')).hexdigest()[:8]
    css_name = f'site.{css_hash}.css'
    js_name  = f'site.{js_hash}.js'
    (assets_dir / css_name).write_text(css_text, encoding='utf-8')
    (assets_dir / js_name).write_text(js_text, encoding='utf-8')
    print(f'  ✓ assets/{css_name} ({len(css_text)/1024:.1f} KB, {len(css_chunks)} blocks merged)')
    print(f'  ✓ assets/{js_name}  ({len(js_text)/1024:.1f} KB, {len(js_chunks)} blocks merged)')

    CSS_LINK = f'<link rel="stylesheet" href="/assets/{css_name}">'
    JS_TAG   = f'<script src="/assets/{js_name}" defer></script>'

    def rewrite_html_for_external_assets(text: str) -> str:
        # Strip every extracted <style>; replace the first one with the link.
        first = {'done': False}
        def style_repl(_m):
            if not first['done']:
                first['done'] = True
                return CSS_LINK
            return ''
        text = STYLE_RE.sub(style_repl, text)

        # Strip every extractable <script>; the last one becomes the external tag.
        positions = []
        for m in SCRIPT_RE.finditer(text):
            if is_extractable_script(m.group(1), m.group(2)):
                positions.append((m.start(), m.end()))
        # Iterate in reverse so earlier indices remain valid as we mutate.
        for i, (start, end) in enumerate(reversed(positions)):
            if i == 0:
                # Document-last extractable script → replace with external tag.
                text = text[:start] + JS_TAG + text[end:]
            else:
                # Earlier extractable scripts → remove (content moved to site.js).
                text = text[:start] + text[end:]
        return text

    # ── Make asset paths absolute so they work on deep routes ────────────
    # Source HTML uses relative paths like src="Images/foo.png". These
    # resolve correctly on the home page (/Images/foo.png) but break on
    # deep routes like /kontakt/ where they resolve to /kontakt/Images/foo.png
    # → SPA fallback serves index.html → browser tries to render HTML as
    # an image and fails. Rewrite each known asset folder to be absolute.
    ASSET_DIRS = ['Images', 'logos', 'icons', 'company_logos', 'marketing', 'Videos', 'assets']
    def absolutize_assets(text: str) -> str:
        for d in ASSET_DIRS:
            # src="Foo/..." or href="Foo/..." (but not already absolute, hash, or full URL)
            text = re.sub(
                rf'((?:src|href|poster|content)=")({re.escape(d)}/)',
                rf'\g<1>/\g<2>',
                text,
            )
        # Also fix the SVG logo at the project root (single file, special name)
        text = text.replace('href="250129_Logos für Kalle.svg"',
                            'href="/250129_Logos für Kalle.svg"')
        text = text.replace('src="250129_Logos für Kalle.svg"',
                            'src="/250129_Logos für Kalle.svg"')
        return text

    # ── Wrap <img> in <picture> with AVIF + WebP sources ─────────────────
    # Browser picks the smallest format it supports. JPG/PNG kept as fallback.
    # Self-closing and existing <picture> wrappers are left alone.
    #
    # SKIP cross-origin images we don't control (YouTube thumbs, etc.) —
    # YouTube's i.ytimg.com only serves .jpg/.webp, NOT .avif, so requesting
    # `.../hqdefault.avif` would 404 on every page load. Same for any other
    # external host we don't own.
    IMG_WRAP_RE = re.compile(
        r'<img\b(?P<pre>[^>]*?)\bsrc="(?P<src>[^"]+\.(?:jpe?g|png))"(?P<post>[^>]*?)>',
        re.IGNORECASE,
    )
    # Hosts where we can't guarantee .avif / .webp variants exist
    EXTERNAL_HOSTS_SKIP = ('ytimg.com', 'youtube.com', 'googleusercontent.com')
    def wrap_pictures(text: str) -> str:
        # Skip rewriting if an <img> is already inside a <picture> (idempotent).
        def repl(m):
            start = m.start()
            window = text[max(0, start - 200):start].lower()
            if '<picture' in window and '</picture' not in window:
                return m.group(0)
            src = m.group('src')
            # Skip external hosts that don't serve .avif/.webp variants
            if any(host in src for host in EXTERNAL_HOSTS_SKIP):
                return m.group(0)
            base = src.rsplit('.', 1)[0]
            return (
                f'<picture>'
                f'<source srcset="{base}.avif" type="image/avif">'
                f'<source srcset="{base}.webp" type="image/webp">'
                f'<img{m.group("pre")} src="{src}"{m.group("post")}>'
                f'</picture>'
            )
        return IMG_WRAP_RE.sub(repl, text)

    # ── Rewrite media URLs to point at R2 ───────────────────────────────
    # Only touches the configured asset directories. Catches both absolute
    # (/Images/...) and relative (Images/...) forms in src/href/poster/
    # content/srcset attributes.
    def rewrite_to_r2(text: str) -> str:
        if not USE_R2:
            return text
        for d in R2_DIRS:
            text = re.sub(
                rf'((?:src|href|poster|content|srcset)=")/?{re.escape(d)}/',
                rf'\g<1>{R2_BASE_URL}/{d}/',
                text,
            )
            # Inline CSS url(...) references (no quotes, single, or double quoted)
            text = re.sub(
                rf'url\(\s*([\'"]?)/?{re.escape(d)}/',
                rf'url(\g<1>{R2_BASE_URL}/{d}/',
                text,
            )
        return text

    for html_file in out_path.rglob('*.html'):
        original = html_file.read_text(encoding='utf-8')
        rewritten = rewrite_html_for_external_assets(original)
        rewritten = absolutize_assets(rewritten)
        rewritten = wrap_pictures(rewritten)
        rewritten = rewrite_to_r2(rewritten)
        rewritten = rewritten.replace('REPLACE_WITH_TURNSTILE_SITE_KEY', TURNSTILE_SITE_KEY)
        html_file.write_text(rewritten, encoding='utf-8')
    print(f'  ✓ Rewrote {len(list(out_path.rglob("*.html")))} HTML files (external assets, absolute paths, <picture>, R2 URLs)')

    # Also rewrite CSS bundle that was just written (background-image URLs etc.)
    css_path = assets_dir / css_name
    if css_path.exists() and USE_R2:
        css_body = css_path.read_text(encoding='utf-8')
        css_body = rewrite_to_r2(css_body)
        css_path.write_text(css_body, encoding='utf-8')
        print(f'  ✓ Rewrote CSS bundle for R2')

    # Copy critical assets so the prerendered site is self-contained
    if args.copy_assets:
        print('\nCopying static assets...')
        if USE_R2:
            asset_paths = [
                '250129_Logos für Kalle.svg',
                'sitemap.xml', 'robots.txt', 'manifest.webmanifest',
                'og-image.jpg', 'og-image-1200x630.jpg',
            ]
            print(f'  (skipping {", ".join(R2_DIRS)} — served from {R2_BASE_URL})')
        else:
            asset_paths = [
                'Images', 'logos', 'icons', 'company_logos', 'marketing',
                'Videos', '250129_Logos für Kalle.svg',
                'sitemap.xml', 'robots.txt', 'manifest.webmanifest',
                'og-image.jpg', 'og-image-1200x630.jpg',
            ]
        for rel in asset_paths:
            src = root / rel
            if not src.exists():
                continue
            dst = out_path / rel
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                print(f'  ✓ {rel}/')
            else:
                shutil.copy2(src, dst)
                print(f'  ✓ {rel}')

        # ── Image weight optimizer ─────────────────────────────────────
        # Two thresholds:
        #   • >24 MiB (hard) → must compress (Cloudflare Pages 25 MiB/file cap)
        #   • >4 MiB (soft)  → should compress for mobile performance / SEO
        # Both target 2560-pixel max dimension at JPEG quality 88. 2560 is
        # bigger than every common laptop/4K display, so visual quality is
        # unaffected; payload typically drops by 70-90%.
        # Uses macOS `sips` (preinstalled, no dependencies).
        import subprocess
        HARD_LIMIT = 24 * 1024 * 1024  # 24 MiB — Cloudflare upload cap
        SOFT_LIMIT = 4  * 1024 * 1024  #  4 MiB — performance target
        TARGET_DIM = 2560
        oversized = []
        for f in out_path.rglob('*'):
            if f.is_file() and f.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                if f.stat().st_size > SOFT_LIMIT:
                    oversized.append(f)
        if oversized:
            saved = 0
            print(f'\nOptimizing {len(oversized)} oversized image(s) (>4 MiB)...')
            for f in oversized:
                before = f.stat().st_size
                subprocess.run([
                    'sips', '--resampleHeightWidthMax', str(TARGET_DIM),
                    '-s', 'formatOptions', '88',
                    str(f), '--out', str(f),
                ], check=False, capture_output=True)
                after = f.stat().st_size
                saved += (before - after)
                print(f'  ✓ {f.relative_to(out_path)}: {before/1024/1024:.1f} → {after/1024/1024:.1f} MiB')
            print(f'  ✓ Total saved: {saved/1024/1024:.1f} MiB')
        # Sanity: anything still >24 MiB after optimization is a problem
        leftover = [f for f in out_path.rglob('*') if f.is_file() and f.stat().st_size > HARD_LIMIT]
        for f in leftover:
            print(f'  ⚠ {f.relative_to(out_path)} is {f.stat().st_size/1024/1024:.1f} MiB '
                  f'and exceeds Cloudflare 25 MiB limit — manual fix needed')

        # Copy service worker if present
        sw_src = root / 'sw.js'
        if sw_src.exists():
            shutil.copy2(sw_src, out_path / 'sw.js')
            print('  ✓ sw.js (service worker)')

        # ── Per-HTML optimizations: lazy-load images, preload hero, register SW ──
        # Heuristic: the FIRST <img> on each page is the LCP candidate → never
        # lazy-load it (would push it out of the initial render). All subsequent
        # <img> tags get loading="lazy" decoding="async".
        IMG_RE = re.compile(r'<img\b([^>]*?)>', re.IGNORECASE)
        # SW registration: install ONLY the self-destructing /sw.js if a previous
        # SW is already registered. This guarantees:
        #   - Existing visitors with the old buggy v1 SW get their SW unregistered
        #     and caches wiped on next visit.
        #   - Fresh visitors never register a SW at all → no risk of regressions.
        SW_REG = (
            "<script>"
            "if('serviceWorker' in navigator){"
            "navigator.serviceWorker.getRegistrations().then(function(rs){"
            "if(rs && rs.length){"
            "navigator.serviceWorker.register('/sw.js').catch(function(){});"
            "}});"
            "}"
            "</script>"
        )
        # Hero preload — first image actually referenced on the home page.
        # Generic enough to be safe across routes; harmless if asset is absent.
        HERO_PRELOAD = (
            '<link rel="preload" as="image" href="/Images/hero.avif" '
            'fetchpriority="high" type="image/avif" '
            'onerror="this.remove()">'
        )

        def optimize_images(html: str) -> str:
            seen = {'first': False}
            def repl(m):
                attrs = m.group(1)
                low = attrs.lower()
                # Skip if already has loading or fetchpriority set explicitly
                if 'loading=' in low or 'fetchpriority=' in low:
                    return m.group(0)
                if not seen['first']:
                    seen['first'] = True
                    # LCP candidate: eager + high priority
                    if 'decoding=' not in low:
                        attrs += ' decoding="async"'
                    attrs += ' fetchpriority="high"'
                    return f'<img{attrs}>'
                # All other images: lazy
                add = ' loading="lazy"'
                if 'decoding=' not in low:
                    add += ' decoding="async"'
                return f'<img{attrs}{add}>'
            return IMG_RE.sub(repl, html)

        for html_file in out_path.rglob('*.html'):
            txt = html_file.read_text(encoding='utf-8')
            new_txt = optimize_images(txt)
            # Always inject SW registration if not already present.
            # Skip generic preload — source HTML already preloads hero video/poster.
            if 'serviceWorker.register' not in new_txt:
                new_txt = new_txt.replace('</head>', f'  {SW_REG}\n</head>', 1)
            if new_txt != txt:
                html_file.write_text(new_txt, encoding='utf-8')
        print('  ✓ Images lazy-loaded, SW registered (hero already preloaded by source)')

        # ── Cloudflare Pages deployment files ───────────────────────────
        # _headers — security + cache rules (replaces .htaccess on Pages)
        # Content-Security-Policy — hardened policy that whitelists every
        # third-party origin actually used by the site. Update this if you
        # add new external scripts/fonts/iframes.
        #
        # 'unsafe-inline' is granted to script-src and style-src because the
        # source HTML uses a few short inline scripts (theme bootstrap, SW
        # registration) and many inline style="..." attributes. Using nonces
        # would be stricter but requires per-request rendering, which this
        # static-prerendered site doesn't do.
        # Whitelist the R2 host for images and video. The wildcard r2.dev
        # entry future-proofs token rotation; the explicit R2_BASE_URL is
        # included for precision when a custom domain is configured.
        cdn_hosts = "https://*.r2.dev"
        if R2_BASE_URL and R2_BASE_URL not in ('https://pub-.r2.dev', ''):
            cdn_hosts = f"{R2_BASE_URL} https://*.r2.dev"

        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' "
                "https://challenges.cloudflare.com "
                "https://code.iconify.design "
                "https://www.youtube.com "
                "https://www.youtube-nocookie.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.bunny.net; "
            "font-src 'self' data: https://fonts.bunny.net; "
            "img-src 'self' data: blob: "
                f"{cdn_hosts} "
                "https://i.ytimg.com "
                "https://*.ytimg.com "
                "https://*.cloudflare.com; "
            f"media-src 'self' blob: {cdn_hosts}; "
            "frame-src 'self' "
                "https://challenges.cloudflare.com "
                "https://cloud.pix4d.com "
                "https://www.youtube.com "
                "https://www.youtube-nocookie.com; "
            "connect-src 'self' "
                "https://api.iconify.design "
                "https://api.simplesvg.com "
                "https://api.unisvg.com "
                "https://challenges.cloudflare.com; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'self'; "
            "upgrade-insecure-requests"
        )
        headers_txt = (
            "/*\n"
            f"  Content-Security-Policy: {csp}\n"
            "  X-Frame-Options: SAMEORIGIN\n"
            "  X-Content-Type-Options: nosniff\n"
            "  Referrer-Policy: strict-origin-when-cross-origin\n"
            "  Permissions-Policy: geolocation=(), microphone=(), camera=()\n"
            "  Strict-Transport-Security: max-age=31536000; includeSubDomains; preload\n"
            "\n"
            "/assets/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/Images/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/logos/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/icons/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/marketing/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/company_logos/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "/04 Videos/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n"
            "\n"
            "/*.html\n"
            "  Cache-Control: public, max-age=300, s-maxage=86400, must-revalidate\n"
            "\n"
            "# Pretty URLs (no extension) — prerendered route folders\n"
            "# like /3d-vermessung/, /kontakt/, /referenzen/, etc.\n"
            "/\n"
            "  Cache-Control: public, max-age=300, s-maxage=86400, must-revalidate\n"
            "/*/\n"
            "  Cache-Control: public, max-age=300, s-maxage=86400, must-revalidate\n"
            "\n"
            "/sw.js\n"
            "  Cache-Control: public, max-age=0, must-revalidate\n"
            "  Service-Worker-Allowed: /\n"
        )
        (out_path / '_headers').write_text(headers_txt, encoding='utf-8')
        print('  ✓ _headers (Cloudflare Pages)')

        # _redirects — not used on Workers Static Assets:
        #   • SPA fallback is handled by `not_found_handling = "single-page-application"` in wrangler.toml
        #   • www → apex must be done via Cloudflare Redirect Rules (Workers _redirects rejects absolute URLs)
        # Remove any stale file from a previous Pages build.
        stale_redirects = out_path / '_redirects'
        if stale_redirects.exists():
            stale_redirects.unlink()

        # functions/ is no longer copied — the Worker (worker/index.js) handles /api/contact.

        # Wire the form to the Pages Function endpoint (source HTML untouched)
        for html_file in out_path.rglob('*.html'):
            txt = html_file.read_text(encoding='utf-8')
            patched = txt.replace(
                "const FORMSPREE_ENDPOINT = '';",
                "const FORMSPREE_ENDPOINT = '/api/contact';"
            )
            if patched != txt:
                html_file.write_text(patched, encoding='utf-8')
        print('  ✓ Form endpoint rewritten → /api/contact')

        # Write a deployment-tuned .htaccess for dist:
        # - DirectoryIndex prefers the prerendered index.html (not the SPA file)
        # - SPA fallback rule still routes any unknown URL to the home prerender
        # - All security headers / caching / file blocks from the original
        src_ht = root / '.htaccess'
        if src_ht.exists():
            ht = src_ht.read_text(encoding='utf-8')
            # Prefer index.html first so each route's prerendered file wins
            ht = ht.replace(
                'DirectoryIndex OCGT_website.html index.html',
                'DirectoryIndex index.html OCGT_website.html')
            # Fall back to the home prerender, not the SPA master
            ht = ht.replace(
                'RewriteRule ^(.*)$ /OCGT_website.html [L]',
                'RewriteRule ^(.*)$ /index.html [L]')
            (out_path / '.htaccess').write_text(ht, encoding='utf-8')
            print('  ✓ .htaccess (deployment-tuned)')

    print(f'\nDone. {len(ROUTES)} prerendered routes written to {out_path.name}/')
    print(f'Deploy by uploading the contents of {out_path.name}/ to the web root.')


if __name__ == '__main__':
    main()
