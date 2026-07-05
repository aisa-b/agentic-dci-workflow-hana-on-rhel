"""
Remote tools -- operations that execute on the jumpbox/target via relay.

Only 3 remote operations remain after the local git refactor:
- workflow.run: trigger dci-rhel-agent-ctl
- ssh.execute: run diagnostic commands on the target server
- ssh.diagnostics: run diagnostic suites on the target server
- git.pull: pull latest changes on the jumpbox before a workflow run
"""

from . import pubsub_client as bridge


async def run_dci_workflow(
    settings_file: str = "",
    verbosity: int = 0,
    target_host: str = "",
) -> dict:
    """
    Trigger the DCI Ansible workflow on the jumpbox via the relay.

    The relay will git-pull the latest changes on the jumpbox first,
    then run dci-rhel-agent-ctl. This can take up to 2 hours.

    Args:
        settings_file: Path to the DCI settings file on the jumpbox.
                       Defaults to the configured DCI_SETTINGS_FILE.
        verbosity: Ansible verbosity level (0-4). Use 0 for first run,
                   increase on re-runs for more diagnostic detail.
        target_host: FQDN of the target server (e.g. target-1.example.corp).
                     Required for parallel runs.
    """
    payload = {"verbosity": verbosity}
    if settings_file:
        payload["settings_file"] = settings_file
    if target_host:
        payload["target_host"] = target_host
    return await bridge.send_command("workflow.run", payload)


async def ssh_execute(command: str, timeout: int = 120, target_host: str = "") -> dict:
    """
    Execute a shell command on the target server via SSH (two-hop through jumpbox).

    Only read-only diagnostics and reversible service operations are allowed.
    Destructive commands (rm, mkfs, reboot, etc.) are blocked by the relay.

    Args:
        command: The shell command to run on the target server.
        timeout: Command timeout in seconds. Defaults to 120.
        target_host: FQDN of the target server. Defaults to config.
    """
    payload = {"command": command, "timeout": timeout}
    if target_host:
        payload["target_host"] = target_host
    return await bridge.send_command("ssh.execute", payload)


async def gather_diagnostics(context_hint: str = "", target_host: str = "") -> dict:
    """
    Run a standard set of diagnostic commands on the target server.

    Provide a context hint to focus diagnostics on a specific area.

    Args:
        context_hint: Area to focus on. One of: deployment, sap_prepare,
                      benchmark, results, hana, storage, network, satellite,
                      tuned, selinux.
        target_host: FQDN of the target server. Defaults to config.
    """
    payload = {"context_hint": context_hint}
    if target_host:
        payload["target_host"] = target_host
    return await bridge.send_command("ssh.diagnostics", payload)
