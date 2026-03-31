/**
 * Node replacement for scripts/extract_frontend.py
 * Copies app/static to frontend/public/static and copies app/templates/*.html
 * Places sw.js at frontend/public/sw.js
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const APP_DIR = path.join(ROOT, "app");
const STATIC_SRC = path.join(APP_DIR, "static");
const TEMPLATES_SRC = path.join(APP_DIR, "templates");
const DEST = path.join(ROOT, "frontend", "public");

function rmdirRecursive(dir) {
  if (fs.existsSync(dir)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function copyRecursive(src, dst) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dst, { recursive: true });
  const entries = fs.readdirSync(src, { withFileTypes: true });
  for (const entry of entries) {
    const srcPath = path.join(src, entry.name);
    const dstPath = path.join(dst, entry.name);
    if (entry.isDirectory()) {
      copyRecursive(srcPath, dstPath);
    } else if (entry.isFile()) {
      fs.copyFileSync(srcPath, dstPath);
    }
  }
}

function main() {
  // Clean destination
  rmdirRecursive(DEST);
  fs.mkdirSync(DEST, { recursive: true });

  // Copy static
  if (fs.existsSync(STATIC_SRC)) {
    console.log("Copying static assets...");
    copyRecursive(STATIC_SRC, path.join(DEST, "static"));
  } else {
    console.log("No static assets found to copy.");
  }

  // Copy templates
  if (fs.existsSync(TEMPLATES_SRC)) {
    console.log("Copying templates (as static placeholders)...");
    const walk = (dir) => {
      const entries = fs.readdirSync(dir, { withFileTypes: true });
      for (const entry of entries) {
        const srcPath = path.join(dir, entry.name);
        const rel = path.relative(TEMPLATES_SRC, srcPath);
        const outPath = path.join(DEST, rel);
        if (entry.isDirectory()) {
          fs.mkdirSync(outPath, { recursive: true });
          walk(srcPath);
        } else if (entry.isFile() && srcPath.endsWith(".html")) {
          fs.mkdirSync(path.dirname(outPath), { recursive: true });
          fs.copyFileSync(srcPath, outPath);
        }
      }
    };
    walk(TEMPLATES_SRC);
  }

  // Ensure service worker at root
  const swSrc = path.join(STATIC_SRC, "sw.js");
  if (fs.existsSync(swSrc)) {
    console.log("Copying service worker to root...");
    fs.copyFileSync(swSrc, path.join(DEST, "sw.js"));
  }

  console.log(
    "Frontend extraction complete. Publish the contents of frontend/public on Netlify.",
  );
}

main();

// Post-process copied HTML to remove server-side Jinja tags so static site doesn't
// display raw template syntax when served from Netlify. This is a pragmatic
// fallback: dynamic values will be empty and should be populated client-side
// by calling the API (recommended future work).
function postProcessHtml(dir) {
  const walk = (d) => {
    const entries = fs.readdirSync(d, { withFileTypes: true });
    for (const entry of entries) {
      const p = path.join(d, entry.name);
      if (entry.isDirectory()) {
        walk(p);
      } else if (entry.isFile() && p.endsWith('.html')) {
        let content = fs.readFileSync(p, 'utf8');
        // Remove Jinja control blocks like {% ... %}
        content = content.replace(/\{%[\s\S]*?%\}/g, '');
        // Remove Jinja variable expressions like {{ ... }}
        content = content.replace(/\{\{[\s\S]*?\}\}/g, '');
        // Write back cleaned file
        fs.writeFileSync(p, content, 'utf8');
      }
    }
  };
  if (fs.existsSync(dir)) walk(dir);
}

try {
  postProcessHtml(DEST);
  console.log('Post-processing HTML: removed Jinja tags from static files.');
} catch (err) {
  console.warn('Post-processing step failed:', err.message);
}
