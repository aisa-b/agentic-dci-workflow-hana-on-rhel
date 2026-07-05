---
name: dci-configure
description: Discover and register the install disk for a new or unconfigured server
disable-model-invocation: true
---

# DCI Configure — Disk Discovery

One-time setup for new servers. Discovers the install disk and saves it to
the `disk_map` in `run_config.yml`. Once a server is in the disk_map,
`/dci-run` handles everything else automatically.

## Input

`$ARGUMENTS` specifies the operation:

- `/dci-configure show` — show the current disk_map and server status
- `/dci-configure --discover target-2` — interactive disk discovery for target-2
- `/dci-configure --discover target-2 --disk scsi-<disk-id>` — use a known SCSI ID

## Safety Rules

- **Git push ONLY to** `github.com/aisa-b/agentic-dci-workflow` (remote `origin`).
- **NEVER add `Co-authored-by` to commit messages.**

## Show (default)

If `$ARGUMENTS` is empty or `show`, run:

```bash
python3 -m tools.configure_target show
```

This prints the disk_map table showing which servers are configured and which
need discovery.

## Discover

When `$ARGUMENTS` contains `--discover`:

### Option A: User provides a SCSI ID

If `--disk scsi-XXX` is in the arguments:

```bash
python3 -m tools.configure_target discover <hostname> --disk <scsi-id>
```

### Option B: Interactive discovery

If no `--disk` is provided, the tool will prompt for:

1. `lsblk` output — paste the output from the server
2. `ls -la /dev/disk/by-id/` output — paste the symlink listing

The tool will:
1. Find which disk has `/` mounted (traces through LVM → partition → parent disk)
2. Find the `scsi-*` symlink for that disk
3. Save the mapping to `disk_map` in `run_config.yml`

You can also gather this info via SSH and pipe it:

```bash
# If the server is reachable via the relay
dci_ssh_execute("lsblk")
dci_ssh_execute("ls -la /dev/disk/by-id/")
```

Then pass the SCSI ID directly with `--disk`.

### After discovery

Commit and push the updated `run_config.yml`:

```bash
git add run_config.yml
git commit -m "Add disk mapping for <hostname>: <disk-id>"
git push origin HEAD
```

Print confirmation:

```
**Disk configured:**
- Server: <hostname>
- Disk: <scsi-id or device name>
- Path: /dev/disk/by-id/<scsi-id> (or /dev/<sdX>)

Ready to run. Use /dci-run <hostname> [topic] to start a workflow.
```

## Adding a brand new server

If the server isn't in `run_config.yml` at all, the operator needs to add
it to the `servers:` section first:

```yaml
servers:
  <short-name>:
    fqdn: <short-name>.example.corp
```

And add an empty entry to `disk_map`:

```yaml
disk_map:
  <short-name>:
```

Then run `/dci-configure --discover <short-name>` to populate the disk.
