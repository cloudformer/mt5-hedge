"""Stage the database server for Lambda: pip install <spec> (e.g. "s3rds[server]==0.3.4") as Linux x86_64 wheels
into ./.build/db. Terraform runs it at plan time (data "external"); prints {"dir": ...}. Cached by spec."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".build", "db")
PLATFORM = ["--platform", "manylinux_2_28_x86_64", "--implementation", "cp", "--python-version", "3.12",
            "--only-binary=:all:"]


def main():
    spec = sys.argv[1]
    stamp, want = OUT + ".stamp", hashlib.sha256(spec.encode()).hexdigest()
    if os.path.isdir(OUT) and os.path.isfile(stamp) and open(stamp, encoding="utf-8").read() == want:
        print(json.dumps({"dir": OUT}))
        return
    shutil.rmtree(OUT, ignore_errors=True)
    r = subprocess.run([sys.executable, "-m", "pip", "-q", "--disable-pip-version-check", "install",
                        "--target", OUT, "--no-compile", *PLATFORM, spec], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"build_server: pip failed: {r.stderr.strip()[-500:]}")
    shutil.rmtree(os.path.join(OUT, "bin"), ignore_errors=True)
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write(want)
    print(json.dumps({"dir": OUT}))


if __name__ == "__main__":
    main()
