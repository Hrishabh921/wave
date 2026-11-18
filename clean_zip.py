"""
clean_zip.py
Makes wave_project_clean.zip containing only git-tracked-or-allowed files
(skips wave_env/, tessdata/, *.exe, *.dll, .env, logs, db, screenshots -
anything listed in .gitignore) using your CURRENT files, not just the
last commit.

Run it by double-clicking clean_zip.py, or from cmd:  python clean_zip.py
Needs only the Python that's already installed for this project.
"""
import subprocess
import sys
import os
import zipfile


def run_git(args, cwd=None):
    return subprocess.run(
        ["git"] + args, cwd=cwd, text=True,
        capture_output=True
    )


def main():
    here = os.path.dirname(os.path.abspath(__file__))

    # Find repo root
    res = run_git(["rev-parse", "--show-toplevel"], cwd=here)

    # Handle "dubious ownership" by trusting this exact folder, then retry.
    if res.returncode != 0 and "dubious ownership" in (res.stderr or ""):
        run_git(["config", "--global", "--add", "safe.directory", here.replace("\\", "/")])
        res = run_git(["rev-parse", "--show-toplevel"], cwd=here)

    if res.returncode != 0:
        print("ERROR: this doesn't look like a git repo, or git isn't installed.")
        print(res.stderr)
        print("Run this script from inside the wave_project folder.")
        input("Press Enter to close...")
        sys.exit(1)

    root = res.stdout.strip()
    os.chdir(root)

    # Tracked + untracked-but-not-ignored files
    res = run_git(["ls-files", "-co", "--exclude-standard"], cwd=root)
    if res.returncode != 0:
        print("ERROR running git ls-files:")
        print(res.stderr)
        input("Press Enter to close...")
        sys.exit(1)

    files = [f for f in res.stdout.splitlines() if f and os.path.isfile(os.path.join(root, f))]
    # Exclude large voice models from the source ZIP - except Wave's own
    # small wake-word model (Tier 11), which is part of the product.
    excluded_extensions = (".onnx",)
    keep_prefixes = ("voice_models/wakewords/",)

    files = [
        f for f in files
        if not f.lower().endswith(excluded_extensions)
        or f.replace("\\", "/").startswith(keep_prefixes)
    ]
    # Safety net - refuse if anything secret/huge slipped through
    
    forbidden = []
    for f in files:
        low = f.replace("\\", "/").lower()
        if low == ".env" or low.startswith("wave_env/") or \
           low.endswith((".exe", ".dll", ".db")) or low.startswith("tessdata/"):
            forbidden.append(f)

    if forbidden:
        print("ABORTED - these should not be packaged:")
        for f in forbidden[:10]:
            print("  ", f)
        input("Press Enter to close...")
        sys.exit(1)

    if not files:
        print("ERROR: no files found to zip. Something is wrong with git ls-files.")
        input("Press Enter to close...")
        sys.exit(1)

    zip_path = os.path.join(root, "wave_project_clean.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(os.path.join(root, f), f)

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    print()
    print(f"Done: wave_project_clean.zip created in {root}")
    print(f"Size: {size_mb:.2f} MB   Files: {len(files)}")
    print("Contains only source files - no venv, no Tesseract binaries,")
    print("no .env, no personal data.")
    print("Reminder: .env.example IS included, your real .env is NOT.")
    input("Press Enter to close...")


if __name__ == "__main__":
    main()