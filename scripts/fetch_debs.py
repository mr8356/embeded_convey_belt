#!/usr/bin/env python3
"""
Download .deb packages and their dependencies for offline Raspberry Pi install.
Runs on the Mac (which has internet). Outputs downloaded file paths to stdout.

Usage:
    python3 fetch_debs.py --arch arm64 --codename bookworm \
        --packages mosquitto mosquitto-clients --outdir ./deb_cache
"""
import argparse
import gzip
import os
import re
import sys
import urllib.request

# Packages almost certainly present on a base Raspberry Pi OS install.
# We skip resolving/downloading these to keep the bundle small.
_BASE_PKGS = {
    "adduser", "base-files", "base-passwd", "bash", "coreutils", "dash",
    "debconf", "debianutils", "diffutils", "dpkg", "e2fsprogs", "findutils",
    "gcc-12-base", "grep", "gzip", "hostname", "init-system-helpers",
    "libapt-pkg6.0", "libattr1", "libaudit1", "libaudit-common",
    "libblkid1", "libbz2-1.0", "libc6", "libcap-ng0", "libcap2",
    "libcap2-bin", "libcom-err2", "libcrypt1", "libdb5.3",
    "libdbus-1-3", "libext2fs2", "libffi8", "libgcc-s1",
    "libgcrypt20", "libgmp10", "libgnutls30", "libgpg-error0",
    "libhogweed6", "libidn2-0", "liblz4-1", "liblzma5", "libmount1",
    "libnettle8", "libp11-kit0", "libpam-modules", "libpam-modules-bin",
    "libpam-runtime", "libpam0g", "libpcre2-8-0", "libpcre3",
    "libseccomp2", "libselinux1", "libsepol2", "libsmartcols1",
    "libss2", "libssl3", "libssl1.1", "libssl1.0.2", "libstdc++6", "libsystemd0", "libtasn1-6",
    "libtinfo6", "libudev1", "libunistring2", "libuuid1", "libxxhash0",
    "libzstd1", "login", "logsave", "lsb-base", "mawk", "mount",
    "ncurses-base", "ncurses-bin", "openssl", "passwd", "perl-base",
    "procps", "sed", "sensible-utils", "sysvinit-utils",
    "tar", "tzdata", "util-linux", "util-linux-extra", "zlib1g",
}

_MIRRORS = {
    # (arch, codename) → (base_url, suite, component)
    ("arm64",  "bookworm"): ("http://deb.debian.org/debian/",             "bookworm", "main"),
    ("arm64",  "bullseye"): ("http://deb.debian.org/debian/",             "bullseye", "main"),
    ("arm64",  "buster"):   ("http://deb.debian.org/debian/",             "buster",   "main"),
    ("armhf",  "bookworm"): ("http://deb.debian.org/debian/",             "bookworm", "main"),
    ("armhf",  "bullseye"): ("http://deb.debian.org/debian/",             "bullseye", "main"),
    ("armhf",  "buster"):   ("http://raspbian.raspberrypi.org/raspbian/", "buster",   "main"),
}


def fetch_packages_index(base_url: str, suite: str, component: str, arch: str) -> str:
    url = f"{base_url}dists/{suite}/{component}/binary-{arch}/Packages.gz"
    print(f"  index: {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return gzip.decompress(resp.read()).decode(errors="replace")


def parse_index(text: str) -> dict:
    """Return {package_name: {filename, deps: [str]}} for every package."""
    db: dict = {}
    for block in text.split("\n\n"):
        name_m = re.search(r"^Package: (.+)$", block, re.M)
        file_m = re.search(r"^Filename: (.+)$", block, re.M)
        if not (name_m and file_m):
            continue
        deps: list[str] = []
        dep_m = re.search(r"^Depends: (.+)$", block, re.M)
        if dep_m:
            for clause in dep_m.group(1).split(","):
                # take first alternative, strip version constraint
                alt = clause.strip().split("|")[0].strip()
                dep_name = re.split(r"\s*\(", alt)[0].strip()
                if dep_name:
                    deps.append(dep_name)
        db[name_m.group(1)] = {"filename": file_m.group(1), "deps": deps}
    return db


def resolve(db: dict, roots: list[str]) -> list[str]:
    """BFS dependency resolution, skipping base packages."""
    ordered: list[str] = []
    seen: set[str] = set(_BASE_PKGS)
    queue = list(roots)
    while queue:
        pkg = queue.pop(0)
        if pkg in seen:
            continue
        seen.add(pkg)
        if pkg not in db:
            print(f"  WARN: {pkg} not in index — skipping", file=sys.stderr)
            continue
        ordered.append(pkg)
        queue.extend(db[pkg]["deps"])
    return ordered


def download_debs(base_url: str, db: dict, pkgs: list[str], outdir: str) -> list[str]:
    os.makedirs(outdir, exist_ok=True)
    paths: list[str] = []
    for pkg in pkgs:
        rel_path = db[pkg]["filename"]
        url = base_url + rel_path
        dest = os.path.join(outdir, os.path.basename(rel_path))
        if os.path.exists(dest):
            print(f"  cached  {os.path.basename(dest)}", file=sys.stderr)
        else:
            print(f"  fetch   {os.path.basename(dest)}", file=sys.stderr)
            urllib.request.urlretrieve(url, dest)
        paths.append(dest)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch",     required=True, help="e.g. arm64 or armhf")
    parser.add_argument("--codename", required=True, help="e.g. bookworm or bullseye")
    parser.add_argument("--packages", nargs="+", required=True)
    parser.add_argument("--outdir",   default=".deb_cache")
    args = parser.parse_args()

    key = (args.arch, args.codename)
    if key not in _MIRRORS:
        print(f"ERROR: unsupported arch/codename pair: {key}", file=sys.stderr)
        print(f"       supported: {list(_MIRRORS.keys())}", file=sys.stderr)
        return 1

    base_url, suite, component = _MIRRORS[key]
    text  = fetch_packages_index(base_url, suite, component, args.arch)
    db    = parse_index(text)
    pkgs  = resolve(db, args.packages)
    files = download_debs(base_url, db, pkgs, args.outdir)
    for f in files:
        print(f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
