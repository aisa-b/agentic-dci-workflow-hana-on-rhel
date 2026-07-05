"""
Generate per-hostname DCI settings files and manage the disk_map.

Usage:
    python -m tools.configure_target generate <hostname> [topic]
    python -m tools.configure_target discover <hostname> [--disk scsi-XXX]
    python -m tools.configure_target show
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CONFIG = REPO_ROOT / "run_config.yml"
SETTINGS_DIR = REPO_ROOT / "settings"
TEMPLATE_FILE = SETTINGS_DIR / "settings_current_run.yml"


# ---------------------------------------------------------------------------
# YAML helpers (preserve formatting as much as possible)
# ---------------------------------------------------------------------------

def _load_run_config() -> dict:
    with open(RUN_CONFIG) as f:
        return yaml.safe_load(f)


def _load_run_config_text() -> str:
    return RUN_CONFIG.read_text()


def _save_run_config_text(text: str) -> None:
    RUN_CONFIG.write_text(text)


# ---------------------------------------------------------------------------
# ks_meta derivation
# ---------------------------------------------------------------------------

def _disk_path(disk: str) -> str:
    if disk.startswith(("scsi-", "wwn-")):
        return f"/dev/disk/by-id/{disk}"
    return f"/dev/{disk}"


def build_ks_meta(disk: str) -> list[dict]:
    """Derive ks_meta from a disk identifier.

    Uses ignoredisk --only-use to restrict installation to the target disk.
    """
    path = _disk_path(disk)
    return [
        {"ignoredisk": f"--only-use={path}"},
        {"no_autopart": True},
    ]


def build_ks_append(disk: str, default_ks_append: str) -> str:
    """Return ks_append as-is from the template. No modifications."""
    return default_ks_append


# ---------------------------------------------------------------------------
# Settings file generation
# ---------------------------------------------------------------------------

def generate_settings(hostname: str, topic: str) -> Path:
    """Generate settings/settings_current_<hostname>.yml.

    Reads the template for everything above 'topics:', then writes
    the topics section from the server profile and disk_map.

    Returns the path to the generated file.
    """
    config = _load_run_config()
    servers = config.get("servers", {})
    disk_map = config.get("disk_map", {})

    short = _short_hostname(hostname)

    if short not in servers:
        print(f"Error: Server '{short}' not in run_config.yml servers section.", file=sys.stderr)
        sys.exit(1)

    disk = disk_map.get(short)
    if not disk:
        print(
            f"Error: Server '{short}' has no disk mapping in disk_map.\n"
            f"Run: python -m tools.configure_target discover {short}",
            file=sys.stderr,
        )
        sys.exit(1)

    server = servers[short]
    domain = config.get("domain", "example.corp")
    fqdn = server.get("fqdn", f"{short}.{domain}")
    ks_meta = build_ks_meta(disk)
    default_ks_append = server.get("ks_append", config.get("default_ks_append", ""))
    ks_append = build_ks_append(disk, default_ks_append)
    template_text = TEMPLATE_FILE.read_text()
    topics_idx = template_text.find("\ntopics:")
    if topics_idx == -1:
        print("Error: Template file has no 'topics:' section.", file=sys.stderr)
        sys.exit(1)

    header = template_text[: topics_idx + 1]

    # Rewrite the comment header with current target/topic/date
    header = _update_header_comment(header, fqdn, topic)

    ks_meta_yaml = yaml.dump(ks_meta, default_flow_style=False).strip()
    ks_meta_indented = "\n".join(f"          {line}" for line in ks_meta_yaml.splitlines())

    ks_append_indented = "\n".join(f"          {line}" for line in ks_append.strip().splitlines())

    topics_section = (
        f"topics:\n"
        f"  - topic: {topic}\n"
        f"    archs:\n"
        f"      - x86_64\n"
        f"    with_debug: true\n"
        f"    systems:\n"
        f"      - fqdn: {fqdn}\n"
        f"        ks_meta:\n"
        f"{ks_meta_indented}\n"
        f"        ks_append: |\n"
        f"{ks_append_indented}\n"
    )

    output = header + topics_section
    output_path = SETTINGS_DIR / f"settings_current_{short}.yml"
    output_path.write_text(output)

    # Validate
    try:
        yaml.safe_load(output)
    except yaml.YAMLError as e:
        print(f"Warning: Generated YAML has errors: {e}", file=sys.stderr)

    print(f"Generated: {output_path.name}")
    print(f"  Target:  {fqdn}")
    print(f"  Topic:   {topic}")
    print(f"  Disk:    {disk}")
    print(f"  Settings: /etc/dci-rhel-agent/settings_current_{short}.yml")

    return output_path


def _update_header_comment(header: str, fqdn: str, topic: str) -> str:
    """Update the comment lines at the top of the settings file."""
    from datetime import date

    lines = header.splitlines(keepends=True)
    new_lines = []
    for line in lines:
        if line.startswith("# Target:"):
            new_lines.append(f"# Target: {fqdn}\n")
        elif line.startswith("# Topic:"):
            new_lines.append(f"# Topic: {topic}\n")
        elif line.startswith("# Generated:"):
            new_lines.append(f"# Generated: {date.today().isoformat()}\n")
        else:
            new_lines.append(line)
    return "".join(new_lines)


# ---------------------------------------------------------------------------
# Disk discovery
# ---------------------------------------------------------------------------

def discover_disk(hostname: str, disk_override: str = "") -> str:
    """Discover the install disk for a server.

    If disk_override is provided, use it directly.
    Otherwise, prompt for lsblk + /dev/disk/by-id/ output and detect.

    Returns the disk identifier (e.g. 'scsi-3EXAMPLE00000002' or 'sdb').
    """
    short = _short_hostname(hostname)

    if disk_override:
        disk = disk_override
        print(f"Using provided disk: {disk}")
    else:
        print("Paste the output of: lsblk")
        print("(end with an empty line)")
        lsblk_text = _read_multiline_input()

        print("\nPaste the output of: ls -la /dev/disk/by-id/")
        print("(end with an empty line)")
        byid_text = _read_multiline_input()

        install_dev = _find_install_disk(lsblk_text)
        if not install_dev:
            print("Error: Could not determine install disk from lsblk output.", file=sys.stderr)
            sys.exit(1)

        print(f"\nDetected install disk: /dev/{install_dev}")

        disk = _find_scsi_id(byid_text, install_dev)
        if disk:
            print(f"Resolved SCSI ID: {disk}")
        else:
            disk = install_dev
            print(f"No SCSI symlink found, using device name: {disk}")

    _save_disk_to_map(short, disk)
    print(f"\nSaved to disk_map: {short} -> {disk}")
    return disk


def _find_install_disk(lsblk_text: str) -> str | None:
    """Parse lsblk output to find the physical disk where / is mounted.

    Handles both LVM and direct partition layouts by tracing from /
    up through parents to the physical disk.
    """
    # Try JSON format first (from lsblk -J)
    try:
        data = json.loads(lsblk_text)
        return _find_install_disk_json(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fall back to text parsing
    return _find_install_disk_text(lsblk_text)


def _find_install_disk_json(data: dict) -> str | None:
    """Parse lsblk JSON output to find the install disk."""
    devices = data.get("blockdevices", [])

    def walk(dev, parent_disk=None):
        name = dev.get("name", "").replace("/dev/", "")
        dtype = dev.get("type", "")
        mount = dev.get("mountpoint") or dev.get("mountpoints", [None])[0]

        current_disk = name if dtype == "disk" else parent_disk

        if mount == "/":
            return current_disk

        for child in dev.get("children", []):
            result = walk(child, current_disk)
            if result:
                return result
        return None

    for dev in devices:
        result = walk(dev)
        if result:
            return result
    return None


def _find_install_disk_text(text: str) -> str | None:
    """Parse plain lsblk text output to find the install disk.

    Looks for the line with mountpoint /, then traces up the tree
    structure to find the parent disk device.
    """
    lines = text.strip().splitlines()

    # Find the line where / is mounted (exact match, not /boot or /home)
    root_line_idx = None
    for i, line in enumerate(lines):
        parts = line.split()
        if not parts:
            continue
        # Mountpoint is typically the last column; match exact "/"
        if parts[-1] == "/":
            root_line_idx = i
            break

    if root_line_idx is None:
        return None

    # Walk upward through the tree to find the parent disk.
    # Tree characters (├─, └─, │) indicate nesting depth.
    root_indent = _get_indent_level(lines[root_line_idx])

    for i in range(root_line_idx - 1, -1, -1):
        indent = _get_indent_level(lines[i])
        if indent < root_indent:
            # Check if this is a disk device
            clean_name = _extract_device_name(lines[i])
            if clean_name:
                parts = lines[i].split()
                # If this line has "disk" type, we found it
                if "disk" in parts:
                    return clean_name
                # Otherwise keep walking up
                root_indent = indent

    # If we never found a "disk" type, try the topmost parent
    for i in range(root_line_idx, -1, -1):
        if _get_indent_level(lines[i]) == 0:
            name = _extract_device_name(lines[i])
            if name:
                return name

    return None


def _get_indent_level(line: str) -> int:
    """Count the visual indent level of a lsblk tree line."""
    indent = 0
    for ch in line:
        if ch in " │├└─":
            indent += 1
        else:
            break
    return indent


def _extract_device_name(line: str) -> str | None:
    """Extract the device name from a lsblk line, stripping tree chars."""
    # Remove tree-drawing characters
    clean = re.sub(r"[│├└─\s]+", "", line.split()[0]) if line.split() else ""
    if not clean:
        # Try harder: strip all tree chars from the start
        clean = re.sub(r"^[│├└─\s]+", "", line).split()
        clean = clean[0] if clean else ""
    # Device names are like sda, sdb, nvme0n1, etc.
    if re.match(r"^[a-z]", clean):
        return clean
    return None


def _find_scsi_id(byid_text: str, device: str) -> str | None:
    """Find the best scsi-* symlink for a device from ls -la /dev/disk/by-id/ output.

    Prefers shorter numeric SCSI IDs over model-serial forms.
    Skips partition links (-part1, -part2).
    """
    candidates = []
    for line in byid_text.splitlines():
        # Match: scsi-XXXX -> ../../sdX
        m = re.search(r"(scsi-\S+)\s+->\s+\.\./\.\./(\S+)", line)
        if not m:
            continue
        scsi_id = m.group(1)
        target = m.group(2)

        # Must point to our device exactly (not a partition)
        if target != device:
            continue
        # Skip partition links
        if "-part" in scsi_id:
            continue

        candidates.append(scsi_id)

    if not candidates:
        return None

    # Prefer shorter IDs (numeric scsi-3XXXX over scsi-SVENDOR_MODEL_SERIAL)
    candidates.sort(key=lambda x: (len(x), x))
    return candidates[0]


def _save_disk_to_map(short: str, disk: str) -> None:
    """Add or update an entry in disk_map in run_config.yml."""
    text = _load_run_config_text()

    # Match "  <hostname>:" or "  <hostname>: old-value" — the key with optional value
    pattern = rf"^(  {re.escape(short)}:)(.*)$"
    match = re.search(pattern, text, flags=re.MULTILINE)

    if match:
        # Entry exists (possibly empty) — replace the whole line
        text = text[: match.start()] + f"  {short}: {disk}" + text[match.end() :]
    else:
        # Entry doesn't exist — add before the end of disk_map
        dm_match = re.search(r"^disk_map:\s*$", text, flags=re.MULTILINE)
        if dm_match:
            after = text[dm_match.end() :]
            insert_pos = dm_match.end()
            for line in after.splitlines(keepends=True):
                if line.strip() and not line.startswith(" ") and not line.startswith("#"):
                    break
                insert_pos += len(line)
            text = text[:insert_pos] + f"  {short}: {disk}\n" + text[insert_pos:]
        else:
            text += f"\ndisk_map:\n  {short}: {disk}\n"

    _save_run_config_text(text)


def _read_multiline_input() -> str:
    """Read lines from stdin until an empty line is entered."""
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

def show_config() -> None:
    """Print the current disk_map and active configuration."""
    config = _load_run_config()
    disk_map = config.get("disk_map", {})
    servers = config.get("servers", {})

    print("Current configuration:")
    print(f"  Target:     {config.get('target', '(not set)')}")
    print(f"  Topic:      {config.get('rhel_topic', '(not set)')}")
    print()
    print("Disk map:")
    print(f"  {'Hostname':<12} {'Disk Identifier':<60} {'Status'}")
    print(f"  {'--------':<12} {'---------------':<60} {'------'}")
    for short, info in servers.items():
        disk = disk_map.get(short, "")
        status = "ready" if disk else "needs --discover"
        disk_display = disk if disk else "(not configured)"
        print(f"  {short:<12} {disk_display:<60} {status}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short_hostname(hostname: str) -> str:
    """Extract short hostname: server1.example.corp -> server1."""
    return hostname.split(".")[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate DCI settings files and manage disk mappings.",
        prog="python -m tools.configure_target",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("generate", help="Generate settings file for a server")
    gen.add_argument("hostname", help="Server hostname (e.g. <hostname>)")
    gen.add_argument("topic", nargs="?", help="RHEL topic (e.g. RHEL-9.8). Uses default if omitted.")

    disc = subparsers.add_parser("discover", help="Discover install disk for a new server")
    disc.add_argument("hostname", help="Server hostname (e.g. <hostname>)")
    disc.add_argument("--disk", default="", help="Provide SCSI ID directly (e.g. scsi-3EXAMPLE00000002)")

    subparsers.add_parser("show", help="Show current disk_map and configuration")

    args = parser.parse_args()

    if args.command == "generate":
        topic = args.topic
        if not topic:
            print("Error: Topic is required. Usage: generate <hostname> <RHEL-X.Y>", file=sys.stderr)
            sys.exit(1)
        generate_settings(args.hostname, topic)

    elif args.command == "discover":
        discover_disk(args.hostname, args.disk)

    elif args.command == "show":
        show_config()


if __name__ == "__main__":
    main()
