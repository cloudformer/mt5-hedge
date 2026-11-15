"""Write this deployment's values into the one-line Windows installer: $PKG_VERSION and $LOG_URL. Only those two
lines change; commit and push the file for the installer on GitHub to serve them.
    python3 render.py <installers/install.ps1> <mt5_hedge_version> <log_url>
Terraform runs it whenever either value changes."""
import re
import sys

path, version, url = sys.argv[1:4]
with open(path, encoding="utf-8") as f:
    text = f.read()
for name, value in (("PKG_VERSION", version), ("LOG_URL", url)):
    text, n = re.subn(rf'^(\${name}\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{value}"', text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"{path}: no ${name} = \"...\" line")
with open(path, "w", encoding="utf-8", newline="") as f:
    f.write(text)
print(f"{path}: $PKG_VERSION = {version}, $LOG_URL = {url}")
