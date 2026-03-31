const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const PUBLIC = path.join(ROOT, "frontend", "public");

function walk(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(p);
    else if (entry.isFile() && p.endsWith(".html")) {
      let c = fs.readFileSync(p, "utf8");
      const before = c;
      // Remove Jinja control blocks {% ... %}
      c = c.replace(/\{%[\s\S]*?%\}/g, "");
      // Remove Jinja expressions {{ ... }}
      c = c.replace(/\{\{[\s\S]*?\}\}/g, "");
      if (c !== before) {
        fs.writeFileSync(p, c, "utf8");
        console.log("Cleaned Jinja from", p);
      }
    }
  }
}

try {
  if (fs.existsSync(PUBLIC)) {
    walk(PUBLIC);
    console.log("strip_jinja_public: done");
  } else {
    console.log("strip_jinja_public: frontend/public not found, skipping");
  }
} catch (err) {
  console.error("strip_jinja_public failed:", err && err.message);
  process.exit(1);
}
