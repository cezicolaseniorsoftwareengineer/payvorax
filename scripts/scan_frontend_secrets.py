from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / 'frontend' / 'public'
PATTERN = re.compile(r"ASAAS|ASAAS_API_KEY|SECRET_KEY|DATABASE_URL|API_KEY|ACCESS_TOKEN|PRIVATE_KEY|SECRET", re.IGNORECASE)

matches = []
if PUBLIC.exists():
    for p in PUBLIC.rglob('*'):
        if p.is_file():
            try:
                text = p.read_text(errors='ignore')
            except Exception:
                continue
            for m in PATTERN.finditer(text):
                # capture the line containing the match
                for i, line in enumerate(text.splitlines(), start=1):
                    if m.group(0).lower() in line.lower():
                        matches.append((str(p), i, line.strip()))
                        break

if matches:
    for path, lineno, line in matches:
        print(f"MATCH: {path} :{lineno}: {line}")
    raise SystemExit(2)
else:
    print('No secret-like strings found in frontend/public')
