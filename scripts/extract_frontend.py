"""Extract current static assets and templates into frontend/public for Netlify deploy.

Usage:
    python scripts/extract_frontend.py

This script copies `app/static/` to `frontend/public/static/`, copies Jinja templates
to `frontend/public/` (as static HTML placeholders) and places `sw.js` at the root of
`frontend/public/` so Netlify can register the service worker.
"""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / 'app'
STATIC_SRC = APP_DIR / 'static'
TEMPLATES_SRC = APP_DIR / 'templates'
DEST = ROOT / 'frontend' / 'public'


def rmtree(path: Path):
    if path.exists():
        shutil.rmtree(path)


def copy_tree(src: Path, dst: Path):
    if not src.exists():
        print(f"Source not found: {src}")
        return
    shutil.copytree(src, dst)


def main():
    # Clean destination
    if DEST.exists():
        print('Cleaning existing frontend/public...')
        rmtree(DEST)
    DEST.mkdir(parents=True, exist_ok=True)

    # Copy static directory
    if STATIC_SRC.exists():
        print('Copying static assets...')
        copy_tree(STATIC_SRC, DEST / 'static')
    else:
        print('No static assets found to copy.')

    # Copy templates as-is (they may contain Jinja placeholders; this is a scaffold step)
    if TEMPLATES_SRC.exists():
        print('Copying templates (as static placeholders)...')
        for tpl in TEMPLATES_SRC.rglob('*.html'):
            rel = tpl.relative_to(TEMPLATES_SRC)
            out = DEST / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tpl, out)

    # Ensure service worker is at root
    sw_src = STATIC_SRC / 'sw.js'
    if sw_src.exists():
        print('Copying service worker to root...')
        shutil.copy2(sw_src, DEST / 'sw.js')

    print('Frontend extraction complete. Publish the contents of frontend/public on Netlify.')


if __name__ == '__main__':
    main()
