"""Stage mt5-hedge for the log Lambda: a released mt5-hedge from PyPI, the package only (--no-deps: the Lambda uses
the standard library; fastapi / MetaTrader5 are the Windows side's).

    python3 build.py "mt5-hedge==0.2.9" <out dir>

Terraform runs it at plan time (main.tf). Prints {"dir": "<out dir>"} for Terraform's external data source.
"""
import json
import os
import shutil
import subprocess
import sys


def main():
    spec, out = sys.argv[1], os.path.abspath(sys.argv[2])
    stamp = out + ".stamp"
    # same spec as last time -> reuse the directory as it is (a reinstall gives new file times, so a new zip
    # hash and a Lambda update on every plan)
    if os.path.isdir(out) and os.path.isfile(stamp) and open(stamp, encoding="utf-8").read() == spec:
        print(json.dumps({"dir": out}))
        return
    shutil.rmtree(out, ignore_errors=True)
    # no .pyc: they embed timestamps, the zip hash would change every plan; --ignore-requires-python: staged for the Lambda's
    # python3.12, whatever python3 runs Terraform here
    r = subprocess.run([sys.executable, "-m", "pip", "-q", "--disable-pip-version-check", "install", "--no-cache-dir",
                        "--target", out, "--no-compile", "--no-deps",
                        "--ignore-requires-python", spec], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"build.py: pip failed: {r.stderr.strip()[-500:]}")
    shutil.rmtree(os.path.join(out, "bin"), ignore_errors=True)
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write(spec)
    print(json.dumps({"dir": out}))


if __name__ == "__main__":
    main()
