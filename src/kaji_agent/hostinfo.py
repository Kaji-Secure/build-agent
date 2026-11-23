"""What a host is, for the host role: its packages, its ``/etc/portage``, and the documents kaji keeps for it.

The agent reads the machine and **builds the documents here** (nothing is deployed anywhere): a PortageConfig (``/etc/portage``), the profile it uses (read, with its parents), a PackageBuild
for what the user asked for (the world file) and one for what is installed, and a lock file in the shape pap writes. The gateway bounds and keeps
them under the host (``waldmeister_web.console.hostreport``).

Only text is read, and secrets are kept out: keys and the ``gnupg`` directory are skipped, and a password in the address of a binhost is replaced.
"""

import hashlib
import json
import os
import platform
import re

from . import sysinfo

VAR_DB_PKG = "/var/db/pkg"
WORLD = "/var/lib/portage/world"
ETC_PORTAGE = "/etc/portage"
MAX_FILE = 64 * 1024  # a config file larger than this is not a config file
MAX_FILES_TOTAL = 1024 * 1024
SKIP_DIRS = {"gnupg"}
SKIP_SUFFIXES = (".key", ".gpg", ".pem", ".asc", ".kbx")
REPOS = "/var/db/repos"
MAX_PROFILES = 40  # the profiles of a chain: Gentoo's are a dozen deep
MAX_PROFILE_FILES_TOTAL = 512 * 1024
PROFILE_DIRS = (
    "package.",
    "use.",
)  # directories of a profile that hold its files (package.use.mask/, ...)
SYS_BLOCK = "/sys/block"
DMI = "/sys/class/dmi/id"
USERINFO = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@"
)  # https://user:secret@binhost/ -> https://REDACTED@binhost/


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def installed(db=VAR_DB_PKG):
    """``{category/name-version: {slot, repository, use}}`` of every installed package, from Portage's database. A package being merged is not one yet."""
    found = {}
    try:
        categories = sorted(os.listdir(db))
    except OSError:
        return found
    for category in categories:
        try:
            names = sorted(os.listdir(os.path.join(db, category)))
        except OSError:
            continue  # a file, not a category
        for name in names:
            if name.startswith("-MERGING-"):
                continue
            where = os.path.join(db, category, name)
            if not os.path.isdir(where):
                continue
            slot = (_read(os.path.join(where, "SLOT")) or "").strip().partition("/")[0]
            entry = {"slot": slot or "0"}
            if repository := (_read(os.path.join(where, "repository")) or "").strip():
                entry["repository"] = repository
            if use := (_read(os.path.join(where, "USE")) or "").split():
                entry["use"] = sorted(use)
            found[f"{category}/{name}"] = entry
    return found


def world(path=WORLD):
    """What the user asked to have installed: the atoms of the world file, in the order of the file, once each."""
    return list(
        dict.fromkeys(
            ln.strip()
            for ln in (_read(path) or "").splitlines()
            if ln.strip() and not ln.startswith("#")
        )
    )


def redact(text):
    """The text without the passwords and user names in the addresses it holds."""
    return USERINFO.sub(r"\g<scheme>REDACTED@", text)


def portage_config(root=ETC_PORTAGE):
    """``{"files": {path under /etc/portage: text}, "profile": "default/linux/amd64/23.0"}``: the shape of a PortageConfig's spec. Binary and oversized files, keys
    and symlinks (except ``make.profile``, which names the profile) are left out."""
    files, total, profile = {}, 0, ""
    link = os.path.join(root, "make.profile")
    if os.path.islink(link):
        target = os.readlink(link)
        profile = target.split("/profiles/", 1)[1] if "/profiles/" in target else ""
    for here, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in SKIP_DIRS and not os.path.islink(os.path.join(here, d))
        )
        for name in sorted(names):
            path = os.path.join(here, name)
            if os.path.islink(path) or name.endswith(SKIP_SUFFIXES):
                continue
            try:
                if os.path.getsize(path) > MAX_FILE:
                    continue
            except OSError:
                continue
            if (text := _read(path)) is None or total + len(text) > MAX_FILES_TOTAL:
                continue
            total += len(text)
            files[os.path.relpath(path, root)] = redact(text)
    return {"files": files, **({"profile": profile} if profile else {})}


def _profile_name(path):
    """``repo:path`` of a profile directory, as ``parent`` files name them (``gentoo:default/linux/amd64/23.0``); the bare path if it is in no ``profiles/``."""
    head, sep, rel = path.rpartition("/profiles/")
    return f"{os.path.basename(head)}:{rel}" if sep else path


def _parents(path, repos):
    """The directories a profile's ``parent`` file names: relative to the profile, or ``repo:path`` of a repository."""
    found = []
    for line in (_read(os.path.join(path, "parent")) or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        repo, colon, rel = line.partition(":")
        found.append(
            os.path.normpath(
                os.path.join(repos, repo, "profiles", rel)
                if colon
                else os.path.join(path, line)
            )
        )
    return found


def _profile_files(path, budget):
    files = {}
    for here, dirs, names in os.walk(path, followlinks=False):
        # the profile's own files, and the directories of package.use.mask and the like; a sub-profile is a profile of its own
        dirs[:] = sorted(d for d in dirs if here == path and d.startswith(PROFILE_DIRS))
        for name in sorted(names):
            file = os.path.join(here, name)
            if os.path.islink(file):
                continue
            try:
                if os.path.getsize(file) > MAX_FILE:
                    continue
            except OSError:
                continue
            if (text := _read(file)) is not None and len(text) <= budget[0]:
                budget[0] -= len(text)
                files[os.path.relpath(file, path)] = text
    return files


def profile(root=ETC_PORTAGE, repos=REPOS):
    """The profile ``make.profile`` points to, read: ``{"name": "gentoo:default/linux/amd64/23.0", "chain": [{"name", "files"}, ...]}``. The chain is the profile
    first, then its ``parent`` profiles, depth first, once each, as Portage stacks them (the last of the chain is the lowest); a profile that is not there is
    left out. Empty when ``make.profile`` is not a link to a directory."""
    link = os.path.join(root, "make.profile")
    if not os.path.islink(link) or not os.path.isdir(link):
        return {}
    top = os.path.realpath(link)
    chain, seen, budget = [], set(), [MAX_PROFILE_FILES_TOTAL]

    def visit(path):
        path = os.path.realpath(path)
        if path in seen or len(chain) >= MAX_PROFILES or not os.path.isdir(path):
            return
        seen.add(path)
        chain.append(
            {"name": _profile_name(path), "files": _profile_files(path, budget)}
        )
        for parent in _parents(path, repos):
            visit(parent)

    visit(top)
    return {"name": chain[0]["name"], "chain": chain} if chain else {}


def lockfile(packages, generator="kaji-agent"):
    """A lock file of ``packages`` (what :func:`installed` returns) in the shape pap writes, with what Portage knows of each package alongside."""
    body = {cpv: {"dependencies": {}, **info} for cpv, info in sorted(packages.items())}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "lockfile_version": "1.0",
        "generator": generator,
        "state_hash": f"sha256:{digest}",
        "packages": body,
    }


ARCHES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "riscv64": "riscv",
}


def package_builds(wanted, packages, arch):
    """The PackageBuilds that say what is on the host, as ``{"world": spec, "installed": spec}``: ``world`` has the atoms the user chose, ``installed`` every
    package at the version installed. They are documents under the host (kaji names them after it, and they use its PortageConfig): no cluster gets them."""
    common = {
        "plannerBackend": "portage",
        "planOnly": True,
        **({"arch": arch} if arch else {}),
    }
    out = {}
    if wanted:
        out["world"] = {"atoms": wanted, **common}
    if packages:
        out["installed"] = {"atoms": [f"={cpv}" for cpv in sorted(packages)], **common}
    return out


def disks(sys_block=SYS_BLOCK):
    """The disks of the machine, ``[{name, size, model, rotational}]``: real devices only, not loop, RAM or device-mapper ones. Sizes are in bytes."""
    found = []
    try:
        names = sorted(os.listdir(sys_block))
    except OSError:
        return found
    for name in names:
        if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr", "fd", "nbd")):
            continue
        base = os.path.join(sys_block, name)
        sectors = (_read(os.path.join(base, "size")) or "").strip()
        if not sectors.isdigit() or not int(sectors):
            continue
        entry = {"name": name, "size": int(sectors) * 512}
        if model := " ".join(
            (_read(os.path.join(base, "device", "model")) or "").split()
        ):
            entry["model"] = model
        if (
            rotational := (
                _read(os.path.join(base, "queue", "rotational")) or ""
            ).strip()
        ) in ("0", "1"):
            entry["rotational"] = rotational == "1"
        found.append(entry)
    return found


def machine(dmi=DMI):
    """The vendor and product the firmware names (``Dell Inc.``, ``PowerEdge R640``); what is not there is left out. The serial number is root's to read and is not asked."""
    out = {}
    for key, file in (("vendor", "sys_vendor"), ("product", "product_name")):
        if value := " ".join((_read(os.path.join(dmi, file)) or "").split()):
            out[key] = value
    return out


def hardware(kube=None):
    """What the machine is: what the builder's *Refresh status* shows (CPU, memory, disk), its disks one by one, and the firmware's name for it."""
    out = sysinfo.collect(kube)
    if found := disks():
        out["disks"] = found
    if found := machine():
        out["machine"] = found
    return out


def report(etc=ETC_PORTAGE, db=VAR_DB_PKG, world_file=WORLD, repos=REPOS):
    """The message body of a host report: the machine and the documents."""
    packages = installed(db)
    return {
        "hostname": platform.node(),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "hardware": hardware(),
        "portage_config": portage_config(etc),
        "profile": profile(etc, repos),
        "lockfile": lockfile(packages),
        "package_builds": package_builds(
            world(world_file), packages, ARCHES.get(platform.machine(), "")
        ),
    }
