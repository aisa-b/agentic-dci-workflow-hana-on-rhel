# OS Deployment Domain Knowledge

Reference material for the os-deploy-expert subagent. Read on demand, not
loaded into every invocation.

## How Phase 1 Works

1. DCI provisioner reads `topics[].systems[].ks_meta` and `ks_append` from settings
2. Generates a kickstart file incorporating those directives
3. Sets up PXE boot environment (TFTP, DHCP, HTTP for kickstart)
4. Reboots server via IPMI (`ipmitool -I lanplus -H <bmc> -U <user> -P <pass> power cycle`)
5. Server PXE boots, loads Anaconda installer, downloads kickstart
6. Anaconda installs RHEL according to kickstart directives
7. Server reboots into fresh OS with new SSH host keys
8. Post-install Ansible tasks run (deploytype, satellite registration, tuned)

## Settings File Structure

```yaml
topics:
  - topic: RHEL-10.2
    systems:
      - fqdn: target.example.corp
        ks_meta:
          - ignoredisk: --only-use=/dev/disk/by-id/scsi-<disk-id>
          - no_autopart: true
        ks_append: |
          zerombr
          clearpart --all --initlabel
          reqpart
          part /boot --size=1024
          part /SWAP --size=4096
          part / --grow
```

### ks_meta Options

| Option | Purpose |
|--------|---------|
| `ignoredisk: --only-use=<path>` | Restrict install to a specific disk |
| `no_autopart: true` | Disable automatic partitioning (use manual ks_append) |

### ks_append Directives

| Directive | Purpose |
|-----------|---------|
| `zerombr` | Clear any existing MBR |
| `clearpart --all --initlabel` | Wipe all partitions, initialize disk label |
| `reqpart` | Create required platform partitions (biosboot or EFI SP) |
| `part /boot --size=1024` | 1GB /boot partition |
| `part /SWAP --size=4096` | 4GB swap |
| `part / --grow` | Root partition using remaining space |

## RHEL Version Partitioning Requirements

| RHEL Version | Boot Mode | Required Partitions |
|-------------|-----------|-------------------|
| RHEL 8.x | Legacy BIOS | biosboot (1M) + /boot + others |
| RHEL 8.x | UEFI | EFI SP (200M+) + /boot + others |
| RHEL 9.x | Legacy BIOS | biosboot (1M) + /boot + others |
| RHEL 9.x | UEFI | EFI SP (200M+) + /boot + others |
| RHEL 10.x | Legacy BIOS | biosboot (1M) + /boot + others |
| RHEL 10.x | UEFI | EFI SP (600M+) + /boot + others |

`reqpart` auto-creates the right boot partition based on detected boot mode.

For legacy BIOS (explicit alternative to reqpart):
```
part biosboot --fstype=biosboot --size=1 --ondisk=<disk>
```

For UEFI:
```
part /boot/efi --fstype=efi --size=600 --ondisk=<disk>
```

## BMC/iLO Access

Each server has an IPMI management interface accessible via `power_address`
in the settings file (e.g., `target-bmc.example.corp`).

### Useful IPMI Commands

```
ipmitool -I lanplus -H <power_address> -U <user> -P <pass> power status
ipmitool -I lanplus -H <power_address> -U <user> -P <pass> chassis status
ipmitool -I lanplus -H <power_address> -U <user> -P <pass> sel elist last 20
ipmitool -I lanplus -H <power_address> -U <user> -P <pass> lan print 1
ipmitool -I lanplus -H <power_address> -U <user> -P <pass> chassis bootparam get 5
```

### iLO Redfish API Endpoints

```
https://<power_address>/redfish/v1/Systems/1/          — power state, boot, BIOS mode
https://<power_address>/redfish/v1/Systems/1/Bios/     — BIOS settings
https://<power_address>/redfish/v1/Managers/1/LogServices/IEL/Entries/  — event log
https://<power_address>/redfish/v1/Systems/1/Storage/  — disk inventory
```

## Target Server Diagnostics (via SSH)

```
cat /etc/redhat-release
lsblk
ls -la /dev/disk/by-id/ | grep scsi
fdisk -l 2>/dev/null | head -30
cat /proc/cmdline
efibootmgr -v 2>/dev/null
dmesg | grep -i -E 'error|fail|boot|disk|scsi' | tail -30
cat /root/anaconda-ks.cfg 2>/dev/null | head -50
```

## Jumpbox Provisioner Diagnostics

```
ps aux | grep -E 'provisioner|httpd|dnsmasq|dhcp'
cat /var/log/messages | grep -i -E 'dhcp|pxe|tftp' | tail -30
```

## Live Install Monitoring

### Anaconda syslog (from jumpbox)

```
grep -i anaconda /var/log/messages | tail -30
grep -i 'anaconda.*storage\|anaconda.*partition' /var/log/messages | tail -20
grep -i 'anaconda.*error\|anaconda.*fail' /var/log/messages | tail -20
```

### Interpreting Results

| Syslog shows | IPMI shows | Likely situation |
|-------------|-----------|------------------|
| Anaconda messages flowing | Power on | Install progressing normally |
| No messages for >5 min | Power on | Stuck at interactive prompt — check iLO console |
| No messages | Power off | Powered down unexpectedly — check SEL |
| Partitioning errors | Power on | Kickstart partitioning failed |
| No messages | Power on, boot=disk | Booted from disk instead of PXE — boot order wrong |

## Common Failure Patterns

### "kickstart insufficient" / "installation destination" prompt
Anaconda can't auto-partition. Usually missing `reqpart` or wrong boot mode.

### Install timeout (>60 min, retries exhausted)
Anaconda stuck at interactive prompt. Check via iLO console.

### PXE boot failure
BIOS boot order wrong (disk before network), DHCP not serving, MAC mismatch.

### Post-install SSH "Permission denied"
Password mismatch. Check `target_password` in `run_config.yml`.

### Post-install SSH "Connection refused" / timeout
Server didn't boot after install (grub issue) or network config changed.

### Disk not found during install
SCSI ID in `disk_map` is wrong or disk was replaced. Re-discover with
`python -m tools.configure_target discover <hostname>`.

### "python: command not found" after install
RHEL 10+ doesn't ship `/usr/bin/python`. Ansible needs `ansible_python_interpreter`
set to `/usr/bin/python3`. Check the inventory file and DCI agent env vars.

## Key Files

- `run_config.yml` — disk_map, default_ks_append, server profiles
- `settings/settings_current_<hostname>.yml` — generated settings with ks_meta/ks_append
- `tools/configure_target.py` — generates settings, manages disk_map
- `dci-hooks/dude/deploytype/baremetal.yml` — post-install tasks
- `dci-hooks/config-variables.yml` — global Ansible variables
