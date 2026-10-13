import os
import shutil
import tarfile
import tempfile

from . import config


def ensure():
    directory = config.webkit_dir()
    usr = os.path.join(directory, "usr")
    # Kodi's ZIP install loses symlinks, so the add-on carries usr/ as a tarball.
    if not os.path.isdir(usr):
        if not os.path.isfile(os.path.join(directory, "usr.tar.gz")):
            raise RuntimeError("the Oriel runtime is not installed")
        temporary = tempfile.mkdtemp(prefix=".unpack-", dir=directory)
        try:
            with tarfile.open(os.path.join(directory, "usr.tar.gz")) as archive:
                archive.extractall(temporary, filter="data")
            if not os.path.isdir(os.path.join(temporary, "usr", "lib")):
                raise RuntimeError("the Oriel runtime payload is incomplete")
            os.rename(os.path.join(temporary, "usr"), usr)
        finally:
            shutil.rmtree(temporary)
    if not os.path.isdir(os.path.join(usr, "lib")):
        raise RuntimeError("the Oriel runtime is not installed")
    return usr
