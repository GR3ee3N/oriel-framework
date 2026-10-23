"""Package the framework add-on and @oriel/web, including licenses and source archives."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile

REPO = Path(__file__).resolve().parent.parent
GITHUB = "GR3ee3N/oriel-framework"
LICENSE_FILE = re.compile(r"^(?:licen[cs]e|copying|copyright|notice|unlicense)(?:[-._].*)?$", re.I)


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def digest(file):
    with Path(file).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tar_tree(directory, destination):
    with tarfile.open(destination, "w:gz" if destination.name.endswith((".gz", ".tgz")) else "w:xz") as archive:
        archive.add(directory, arcname=directory.name)


def zip_tree(directory, destination):
    # Clamp upstream file timestamps to ZIP's minimum year of 1980.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for file in sorted(directory.rglob("*")):
            require(not file.is_symlink(), f"ZIP must not contain Unix links: {file}")
            archive.write(file, f"{directory.name}/{file.relative_to(directory).as_posix()}")


def checksum(file):
    file.with_name(file.name + ".sha256").write_text(f"{digest(file)}  {file.name}\n")


def versions():
    return os.environ["VERSION"], os.environ["WEBKIT_VERSION"], os.environ["DISPLAY_VERSION"]


def package(runtime, source, licenses, destination):
    v, w, d = versions()
    url = f"https://github.com/{GITHUB}/releases/download/v{v}/{source.name}.tar.xz"
    stage = destination / "packages"
    files = destination / "files"
    files.mkdir(parents=True)
    addon = stage / "script.module.oriel.framework"
    shutil.copytree(runtime / "script.module.oriel.framework", addon, symlinks=True)
    shutil.copy(REPO / "LICENSE", addon / "LICENSE.txt")
    shutil.copytree(licenses, addon / "LICENSES")
    (addon / "THIRD-PARTY-NOTICES.txt").write_text(
        "Third-party components keep their own licenses; see LICENSES/. Oriel's\n"
        "own code is GPL-3.0-or-later (LICENSE.txt).\n"
        "\n"
        f"The shell links against the libraries of Oriel WebKit {w}\n"
        "(script.module.oriel.webkit), which carries their licenses and links their\n"
        "sources. You may rebuild and replace the shell.\n"
        f"Complete source and build scripts: {url}\n"
    )
    (addon / "SOURCE.txt").write_text(url + "\n")
    zip_name = f"script.module.oriel.framework-{v}.zip"
    zip_tree(addon, files / zip_name)
    # Ship TypeScript sources for compilation by each app's build tools.
    web_package = stage / "package"
    shutil.copytree(REPO / "web/src", web_package / "src")
    (web_package / "package.json").write_text((REPO / "web/package.json").read_text().replace("@VERSION@", v))
    shutil.copy(REPO / "LICENSE", web_package / "LICENSE")
    (web_package / "SOURCE.txt").write_text(url + "\n")
    (web_package / "THIRD-PARTY-NOTICES.txt").write_text("solid-js is supplied by the consuming app and is not bundled in this package.\n")
    tar_tree(web_package, files / f"oriel-web-{v}.tgz")
    shutil.copytree(licenses, source / "licenses")
    tar_tree(source, files / f"{source.name}.tar.xz")
    # Publish the Kodi index beside its referenced ZIP in the latest release.
    addons = ET.Element("addons")
    element = ET.fromstring((REPO / "addon/addon.xml").read_text().replace("@VERSION@", v).replace("@WEBKIT_VERSION@", w).replace("@DISPLAY_VERSION@", d).encode())
    ET.SubElement(element.find('extension[@point="xbmc.addon.metadata"]'), "path").text = zip_name
    addons.append(element)
    ET.indent(addons)
    ET.ElementTree(addons).write(files / "addons.xml", encoding="UTF-8", xml_declaration=True)
    for name in (zip_name, "addons.xml"):
        checksum(files / name)


def main():
    commit, snapshot = sys.argv[1:]
    rust = Path(subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip())
    runtime = REPO / ".cache/runtime"
    v, w, d = versions()
    destination = REPO / ".cache/release.next"
    if destination.exists():
        shutil.rmtree(destination)
    source = destination / f"oriel-framework-{v}-sources"
    source.mkdir(parents=True)
    with tarfile.open(snapshot) as archive:
        archive.extractall(source, filter="data")
    project = source / "oriel-framework"
    # Include path dependencies explicitly because cargo vendor omits them.
    display = REPO / ".cache/display"
    shutil.copytree(display / "protocol", project / ".cache/display/protocol")
    shutil.copy(display / f"oriel-display-{d}-protocol.tar.xz", project / ".cache/display")
    vendor = project / "vendor"
    cargo_config = subprocess.check_output(["cargo", "vendor", "--locked", "--versioned-dirs", str(vendor)], text=True, cwd=project)
    (project / ".cargo").mkdir(exist_ok=True)
    (project / ".cargo/config.toml").write_text(cargo_config.replace(str(vendor), "vendor"))
    (source / "COMMIT").write_text(commit + "\n")
    (source / "README.txt").write_text(
        "Build instructions: oriel-framework/README.md. Cargo dependencies are\n"
        "vendored; .cargo/config.toml selects them. The shell links against\n"
        f"Oriel WebKit {w}, whose sources are published with that release.\n"
        "Build tools, the Rust toolchain and npm check dependencies are\n"
        "downloaded by the build.\n"
    )

    licenses = destination / "licenses"
    licenses.mkdir()
    # Include licenses for third-party code in the statically linked Rust standard library.
    shutil.copytree(rust / "share/doc/rust", licenses / "rust", ignore=shutil.ignore_patterns("README.md"))
    for crate in sorted(vendor.iterdir()):
        found = [file for file in crate.iterdir() if file.is_file() and LICENSE_FILE.match(file.name)]
        require(found, f"no license file in Cargo dependency {crate.name}")
        (licenses / "cargo" / crate.name).mkdir(parents=True)
        for file in found:
            shutil.copy(file, licenses / "cargo" / crate.name / file.name)

    protocol_license = licenses / "cargo/oriel-display-protocol"
    protocol_license.mkdir(parents=True)
    shutil.copy(display / "protocol/LICENSE", protocol_license / "LICENSE")

    package(runtime, source, licenses, destination)
    # Record the source commit so tools/release.sh can verify package provenance.
    (destination / "COMMIT").write_text(commit + "\n")
    for leftover in (destination / "packages", source, licenses):
        shutil.rmtree(leftover)
    release = REPO / ".cache/release"
    if release.exists():
        shutil.rmtree(release)
    destination.rename(release)
    print(f"Release packages, licenses and source: {release / 'files'}")


if __name__ == "__main__":
    main()
