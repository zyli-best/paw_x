"""Check installed distribution versions against the original Windows venv."""
import argparse
from importlib.metadata import distributions
from pathlib import Path
import re
import runpy
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--allow-extra-packages",
    action="store_true",
    help="Allow additional packages while checking all locked dependency versions.",
)
args = parser.parse_args()

root = Path(__file__).resolve().parents[1]
required_python = (root / ".python-version").read_text(encoding="utf-8").strip()
actual_python = ".".join(map(str, sys.version_info[:3]))
if actual_python != required_python:
    raise SystemExit(f"Python {required_python} required, found {actual_python}")

def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

expected = {}
for line in (root / "requirements-windows.lock.txt").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.startswith("#"):
        continue
    name, version = line.split("==", 1)
    expected[canonical(name)] = version
# qwenpaw is installed from this checkout, with the same version as the source.
expected["qwenpaw"] = runpy.run_path(str(root / "src/qwenpaw/__version__.py"))["__version__"]
actual = {canonical(item.metadata["Name"]): item.version for item in distributions()}
differences = [f"{name}: expected {version}, found {actual.get(name, 'missing')}"
               for name, version in expected.items() if actual.get(name) != version]
extra_packages = sorted(actual.keys() - expected.keys())
if not args.allow_extra_packages:
    differences += [f"{name}: unexpected installed package {actual[name]}"
                    for name in extra_packages]
if differences:
    raise SystemExit("Environment differs from the original. Run scripts/init-windows.ps1 -RecreateVenv.\n"
                     + "\n".join(differences))
if args.allow_extra_packages and extra_packages:
    print("Additional packages allowed: "
          + ", ".join(f"{name}=={actual[name]}" for name in extra_packages))
print(f"PASS Python {actual_python}; all {len(expected)} required distribution versions match the original")
