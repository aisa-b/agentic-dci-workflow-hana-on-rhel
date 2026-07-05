"""Tests for tools.configure_target — settings generation and disk discovery."""

from tools.configure_target import (
    build_ks_meta,
    build_ks_append,
    _find_install_disk_text,
    _find_scsi_id,
    _short_hostname,
    _disk_path,
)


class TestBuildKsMeta:
    def test_ignoredisk_and_no_autopart(self):
        result = build_ks_meta("scsi-3EXAMPLE00000001")
        assert result == [
            {"ignoredisk": "--only-use=/dev/disk/by-id/scsi-3EXAMPLE00000001"},
            {"no_autopart": True},
        ]

    def test_disk_all(self):
        result = build_ks_meta("all")
        assert result == [
            {"ignoredisk": "--only-use=/dev/all"},
            {"no_autopart": True},
        ]


class TestBuildKsAppend:
    def test_disk_all_returns_default(self):
        default = "zerombr\nclearpart --all\npart / --grow\n"
        result = build_ks_append("all", default)
        assert result == default

    def test_specific_disk_returns_default_unchanged(self):
        default = "zerombr\nclearpart --all --initlabel\nreqpart\npart /boot --size=1024\npart swap --size=4096\npart / --grow\n"
        result = build_ks_append("scsi-3EXAMPLE00000001", default)
        assert result == default

    def test_sda_returns_default_unchanged(self):
        default = "zerombr\npart / --grow\n"
        result = build_ks_append("sda", default)
        assert result == default


class TestDiskPath:
    def test_scsi_id(self):
        assert _disk_path("scsi-3EXAMPLE00000001") == "/dev/disk/by-id/scsi-3EXAMPLE00000001"

    def test_wwn_id(self):
        assert _disk_path("wwn-0x50014ee") == "/dev/disk/by-id/wwn-0x50014ee"

    def test_device_name(self):
        assert _disk_path("sda") == "/dev/sda"


class TestFindInstallDiskText:
    def test_simple_lsblk(self):
        text = """NAME   MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS
sda      8:0    0 745.2G  0 disk
├─sda1   8:1    0     1G  0 part /boot
├─sda2   8:2    0     4G  0 part [SWAP]
└─sda3   8:3    0 740.2G  0 part /"""
        assert _find_install_disk_text(text) == "sda"

    def test_lvm_layout(self):
        text = """NAME            MAJ:MIN RM   SIZE RO TYPE MOUNTPOINTS
sda               8:0    0 745.2G  0 disk
├─sda1            8:1    0     1G  0 part /boot
└─sda2            8:2    0 744.2G  0 part
  └─rhel-root 253:0    0 744.2G  0 lvm  /"""
        assert _find_install_disk_text(text) == "sda"

    def test_no_root_mount(self):
        text = """NAME   MAJ:MIN RM  SIZE RO TYPE MOUNTPOINTS
sda      8:0    0  50G  0 disk
└─sda1   8:1    0  50G  0 part /data"""
        assert _find_install_disk_text(text) is None


class TestFindScsiId:
    def test_finds_scsi_id(self):
        byid = """lrwxrwxrwx 1 root root  9 May 29 10:00 scsi-3EXAMPLE00000001 -> ../../sda
lrwxrwxrwx 1 root root 10 May 29 10:00 scsi-3EXAMPLE00000001-part1 -> ../../sda1
lrwxrwxrwx 1 root root  9 May 29 10:00 scsi-SATA_VBOX_HARDDISK_VB12345678-12345678 -> ../../sda"""
        result = _find_scsi_id(byid, "sda")
        assert result == "scsi-3EXAMPLE00000001"

    def test_prefers_shorter_id(self):
        byid = """lrwxrwxrwx 1 root root 9 May 29 scsi-3abc -> ../../sda
lrwxrwxrwx 1 root root 9 May 29 scsi-SVENDOR_MODEL_SERIAL_LONG -> ../../sda"""
        result = _find_scsi_id(byid, "sda")
        assert result == "scsi-3abc"

    def test_no_match(self):
        byid = """lrwxrwxrwx 1 root root 9 May 29 scsi-3abc -> ../../sdb"""
        assert _find_scsi_id(byid, "sda") is None

    def test_skips_partitions(self):
        byid = """lrwxrwxrwx 1 root root 10 May 29 scsi-3abc-part1 -> ../../sda"""
        assert _find_scsi_id(byid, "sda") is None


class TestShortHostname:
    def test_fqdn(self):
        assert _short_hostname("target-1.example.com") == "target-1"

    def test_short(self):
        assert _short_hostname("target-1") == "target-1"
