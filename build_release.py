#!/usr/bin/env python3
"""Build Stash2Plex.zip and refresh index.yml.

Mirrors the zip contents and index fields produced by .github/workflows/release.yml
so a release can be cut without relying on GitHub Actions.

Usage: python build_release.py <version>
"""
import hashlib
import os
import re
import sys
import time
import zipfile

ROOT_FILES = ["Stash2Plex.py", "Stash2Plex.yml", "poster_source.py", "requirements.txt"]
DIRS = ["worker", "sync_queue", "plex", "validation", "shared", "shared_lib", "hooks", "reconciliation"]


def excluded(path):
    parts = path.replace("\\", "/").split("/")
    return (
        any(p == "__pycache__" or p.startswith(".pytest") for p in parts)
        or path.endswith(".pyc")
        or any(os.path.basename(p).startswith("test") for p in parts)
    )


def main():
    version = sys.argv[1]
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)

    # Keep the plugin manifest's version in step with the index.
    manifest = open("Stash2Plex.yml", encoding="utf-8").read()
    manifest = re.sub(r"^version: .*$", f"version: {version}", manifest, count=1, flags=re.M)
    open("Stash2Plex.yml", "w", encoding="utf-8", newline="\n").write(manifest)

    members = list(ROOT_FILES)
    for d in DIRS:
        for dirpath, _, filenames in os.walk(d):
            for fn in filenames:
                p = os.path.join(dirpath, fn).replace("\\", "/")
                if not excluded(p):
                    members.append(p)
    members.sort()

    with zipfile.ZipFile("Stash2Plex.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for m in members:
            z.write(m, m)

    sha = hashlib.sha256(open("Stash2Plex.zip", "rb").read()).hexdigest()
    date = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    index = open("index.yml", encoding="utf-8").read()
    index = re.sub(r"^  version: .*$", f"  version: {version}", index, count=1, flags=re.M)
    index = re.sub(r"^  date: .*$", f'  date: "{date}"', index, count=1, flags=re.M)
    index = re.sub(r"^  path: .*$", "  path: Stash2Plex.zip", index, count=1, flags=re.M)
    index = re.sub(r"^  sha256: .*$", f"  sha256: {sha}", index, count=1, flags=re.M)
    open("index.yml", "w", encoding="utf-8", newline="\n").write(index)

    print(f"version {version}  files {len(members)}  sha256 {sha}")


if __name__ == "__main__":
    main()
