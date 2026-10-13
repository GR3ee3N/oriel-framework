"""Checks the shell's ARM64 ELF architecture and library requirements.

    build/check-shell.py SHELL WEBKIT_DEV

Required libraries and symbol versions must be provided by WebKit or CoreELEC.
"""
from pathlib import Path
import re
import subprocess
import sys


def box(dev):
    return dict(line.split("=", 1) for line in (dev / "box.env").read_text().splitlines() if line and not line.startswith("#"))


def version(text):
    return tuple(map(int, text.split(".")))


def problems(dynamic, versions, dev):
    target = box(dev)
    provided = set((dev / "provided.txt").read_text().split())
    system = re.compile(target["LIBRARIES"])
    found = [f"needs {name}, which neither WebKit's add-on nor the box provides"
             for name in re.findall(r"\(NEEDED\).*\[(.*?)\]", dynamic) if name not in provided and not system.fullmatch(name)]
    # Only requirements, not the versions a library itself exports.
    needs = versions.partition("Version needs section")[2]
    for name, required in re.findall(r"\b(GLIBC|GLIBCXX|CXXABI|GCC)_([0-9]+(?:\.[0-9]+)*)\b", needs):
        if version(required) > version(target[name]):
            found.append(f"requires {name}_{required}; the box provides {name}_{target[name]}")
    return found


def main():
    shell, dev = map(Path, sys.argv[1:])
    header = subprocess.check_output(["aarch64-linux-gnu-readelf", "-h", str(shell)], text=True)
    found = [] if re.search(r"Machine:\s+AArch64", header) else ["is not an ARM64 binary"]
    found += problems(subprocess.check_output(["aarch64-linux-gnu-readelf", "-d", str(shell)], text=True),
                      subprocess.check_output(["aarch64-linux-gnu-readelf", "-V", str(shell)], text=True), dev)
    if found:
        raise SystemExit("\n".join(f"{shell.name} {problem}" for problem in found))


if __name__ == "__main__":
    main()
