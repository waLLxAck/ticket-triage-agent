"""Build build/lambda.zip: our code plus Linux wheels of every runtime dependency.

Lambda runs Amazon Linux on x86_64, so the wheels must be Linux ones even though we build on
Windows. `uv pip install --python-platform x86_64-manylinux_2_28 --only-binary :all:` downloads
exactly those, so no Docker is needed. Both functions share one zip; only the handler differs.

    uv run python scripts/build_lambda.py
"""

import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
PKG = BUILD / "pkg"
SKIP_DIRS = {"__pycache__", "tests"}


def main() -> None:
    shutil.rmtree(PKG, ignore_errors=True)
    PKG.mkdir(parents=True)
    reqs = BUILD / "requirements.txt"
    reqs.write_text(
        subprocess.check_output(
            ["uv", "export", "--no-dev", "--no-hashes", "--no-emit-project", "-q"],
            cwd=ROOT,
            text=True,
        ),
        encoding="utf-8",
    )
    subprocess.check_call(
        [
            "uv", "pip", "install", "-q", "--target", str(PKG),
            "--python-platform", "x86_64-manylinux_2_28", "--python-version", "3.13",
            "--only-binary", ":all:", "-r", str(reqs),
        ]
    )  # fmt: skip
    shutil.copytree(ROOT / "src" / "triage", PKG / "triage")
    shutil.copytree(ROOT / "data", PKG / "data")  # help-centre articles

    out = BUILD / "lambda.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(PKG.rglob("*")):
            if path.is_file() and not SKIP_DIRS & set(path.relative_to(PKG).parts):
                info = zipfile.ZipInfo(str(path.relative_to(PKG)).replace("\\", "/"))
                info.date_time = (2026, 1, 1, 0, 0, 0)  # fixed timestamps: same input, same zip
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    print(f"{out} {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
