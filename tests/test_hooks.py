"""Tests for agents.hooks — safety hooks."""

from agents.hooks import (
    _check_ssh_blocklist,
    scrub_credentials,
    contains_credentials,
    _validate_no_delete,
    CREDENTIAL_REDACTION,
)


class TestSSHBlocklist:
    def test_blocks_rm(self):
        assert _check_ssh_blocklist("rm -rf /tmp/test") is not None

    def test_blocks_reboot(self):
        assert _check_ssh_blocklist("sudo reboot") is not None

    def test_blocks_banned_host(self):
        from config_loader import load_run_config
        rc = load_run_config()
        for host in rc.get("banned_hosts", []):
            assert _check_ssh_blocklist(f"ssh {host} ls") is not None, f"Expected {host} to be blocked"

    def test_blocks_banned_path(self):
        from config_loader import load_run_config
        rc = load_run_config()
        for path in rc.get("banned_paths", []):
            assert _check_ssh_blocklist(f"cat {path}file") is not None, f"Expected {path} to be blocked"

    def test_blocks_git_force_push(self):
        assert _check_ssh_blocklist("git push --force") is not None

    def test_allows_cat(self):
        assert _check_ssh_blocklist("cat /etc/os-release") is None

    def test_allows_ls(self):
        assert _check_ssh_blocklist("ls -la /var/log") is None

    def test_allows_grep(self):
        assert _check_ssh_blocklist("grep error /var/log/messages") is None

    def test_case_insensitive(self):
        assert _check_ssh_blocklist("RM -rf /tmp") is not None


class TestCredentialScrubbing:
    def test_scrubs_github_pat(self):
        text = "token: ghp_abcdefghij1234567890abcdefghij123456"
        result = scrub_credentials(text)
        assert "ghp_" not in result
        assert CREDENTIAL_REDACTION in result

    def test_scrubs_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        result = scrub_credentials(text)
        assert "PRIVATE KEY" not in result

    def test_scrubs_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.test"
        result = scrub_credentials(text)
        assert "eyJhbGci" not in result

    def test_leaves_normal_text(self):
        text = "This is a normal log message with no secrets"
        assert scrub_credentials(text) == text

    def test_contains_credentials_true(self):
        assert contains_credentials("ghp_abcdefghij1234567890abcdefghij123456")

    def test_contains_credentials_false(self):
        assert not contains_credentials("just a normal string")


class TestValidateNoDelete:
    def test_allows_commenting_out(self):
        original = "- name: Install package\n  yum: name=pkg"
        replacement = "# [AGENT-DISABLED] - name: Install package\n# [AGENT-DISABLED]   yum: name=pkg\n# [AGENT-ADDED] new task"
        assert _validate_no_delete(original, replacement) is None

    def test_blocks_deletion(self):
        original = "- name: Install package\n  yum: name=pkg"
        replacement = "- name: Different task\n  shell: echo hi"
        result = _validate_no_delete(original, replacement)
        assert result is not None
        assert "BLOCKED" in result

    def test_allows_comments_to_be_removed(self):
        original = "# just a comment\n- name: task"
        replacement = "- name: task"
        assert _validate_no_delete(original, replacement) is None

    def test_allows_empty_lines_removed(self):
        original = "\n\n- name: task\n\n"
        replacement = "- name: task"
        assert _validate_no_delete(original, replacement) is None
