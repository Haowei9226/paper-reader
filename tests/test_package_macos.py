"""Mac archive structure can be checked on the Windows build host."""
import io
import stat
import tarfile
import zipfile

import pytest

from scripts.package_macos import Writer, install_runtime, safe_name


def runtime_archive(path, symlink="python3.12"):
    with tarfile.open(path, "w:gz") as archive:
        binary = tarfile.TarInfo("python/bin/python3.12")
        binary.mode, binary.size = 0o755, 6
        archive.addfile(binary, io.BytesIO(b"binary"))
        link = tarfile.TarInfo("python/bin/python3")
        link.type, link.linkname, link.mode = tarfile.SYMTYPE, symlink, 0o755
        archive.addfile(link)


def test_mac_runtime_keeps_symlinks_modes_and_wheel_data_layout(tmp_path):
    runtime = tmp_path / "python.tar.gz"
    runtime_archive(runtime)
    wheel = tmp_path / "demo-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("demo/__init__.py", "")
        archive.writestr("demo-1.0.data/purelib/pure.py", "pure")
        archive.writestr("demo-1.0.data/platlib/plat.py", "plat")
        archive.writestr("demo-1.0.data/scripts/tool", "script")
        archive.writestr("demo-1.0.data/data/share/demo/LICENSE", "license")
        archive.writestr("demo-1.0.data/headers/demo.h", "header")
    output = tmp_path / "app.zip"
    with zipfile.ZipFile(output, "w") as archive:
        install_runtime(Writer(archive), "runtime/arm64", {"archive": runtime, "wheels": [wheel]})
    with zipfile.ZipFile(output) as archive:
        prefix = "runtime/arm64/python/"
        link = archive.getinfo(prefix + "bin/python3")
        assert stat.S_ISLNK(link.external_attr >> 16)
        assert archive.read(link) == b"python3.12"
        assert archive.getinfo(prefix + "bin/python3.12").external_attr >> 16 & 0o111 == 0o111
        assert archive.read(prefix + "lib/python3.12/site-packages/pure.py") == b"pure"
        assert archive.read(prefix + "lib/python3.12/site-packages/plat.py") == b"plat"
        assert archive.read(prefix + "bin/tool") == b"script"
        assert archive.read(prefix + "share/demo/LICENSE") == b"license"
        assert archive.read(prefix + "include/python3.12/demo.h") == b"header"


def test_mac_runtime_rejects_symlink_outside_runtime(tmp_path):
    runtime = tmp_path / "python.tar.gz"
    runtime_archive(runtime, "../../../private")
    with zipfile.ZipFile(tmp_path / "app.zip", "w") as archive:
        with pytest.raises(ValueError, match="escapes"):
            install_runtime(Writer(archive), "runtime/arm64", {"archive": runtime, "wheels": []})


@pytest.mark.parametrize("path", ["../private", "/absolute", "C:/private", "safe\\private"])
def test_mac_package_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        safe_name(path)
