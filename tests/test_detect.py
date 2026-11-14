import pytest

from app import isos

WINDOWS = {"sources/boot.wim": "", "boot/BCD": "", "boot/boot.sdi": "", "setup.exe": "", "sources/install.wim": ""}
CASES = [
    (WINDOWS, "windows"),
    ({"proxmox/packages": "", "boot/linux26": "", "boot/initrd.img": "", ".cd-info": "PRODUCT='pve'\n"}, "proxmox"),
    ({"casper/vmlinuz": "", "casper/initrd": ""}, "ubuntu"),
    ({"live/vmlinuz-6.1": "", "live/initrd.img-6.1": "", "live/filesystem.squashfs": ""}, "debian_live"),
    ({"install.amd/vmlinuz": "", "install.amd/initrd.gz": "", "dists/trixie/Release": "", "dists/stable": ""},
     "debian_installer"),
    ({"images/pxeboot/vmlinuz": "", "images/pxeboot/initrd.img": "", ".treeinfo": ""}, "rhel"),
    ({"images/pxeboot/vmlinuz": "", "images/pxeboot/initrd.img": "", "LiveOS/squashfs.img": ""}, "fedora_live"),
    ({"arch/boot/x86_64/vmlinuz-linux": "", "arch/boot/x86_64/initramfs-linux.img": ""}, "arch"),
    ({"memtest.bin": ""}, "sanboot"),
]


@pytest.mark.parametrize("files,method", CASES)
def test_detect(tree, files, method):
    assert isos.detect(tree(files)).method == method


def test_detect_is_case_insensitive(tree):
    files = {k.upper(): v for k, v in WINDOWS.items()}
    assert isos.detect(tree(files)).method == "windows"


def test_debian_codename(tree):
    root = tree({"install.amd/vmlinuz": "", "install.amd/initrd.gz": "", "dists/trixie/Release": ""})
    assert isos.detect(root).info["codename"] == "trixie"


def test_proxmox_product(tree):
    root = tree({"proxmox/x": "", "boot/linux26": "", "boot/initrd.img": "", ".cd-info": "PRODUCT='pve'\n"})
    assert isos.detect(root).info["product"] == "pve"


@pytest.mark.parametrize("bad", ["../etc", "a/../../b", ".hidden", "a/.git"])
def test_safe_relpath_rejects(bad):
    with pytest.raises(ValueError):
        isos.safe_relpath(bad)


def test_safe_relpath_normalises():
    assert isos.safe_relpath("/Linux//Ubuntu/") == "Linux/Ubuntu"
