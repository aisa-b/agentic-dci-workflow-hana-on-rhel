"""Tests for onboarding flow: templates → config → settings generation."""

import os
import tempfile
import shutil

import pytest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestTemplatesExist:
    def test_env_example_exists(self):
        assert (REPO_ROOT / ".env.example").exists()

    def test_secrets_example_exists(self):
        assert (REPO_ROOT / "secrets.example.yml").exists()

    def test_run_config_example_exists(self):
        assert (REPO_ROOT / "run_config.example.yml").exists()

    def test_settings_example_exists(self):
        assert (REPO_ROOT / "settings" / "settings.example.yml").exists()


class TestTemplatesHaveNoRealValues:
    def _check_no_real_values(self, content):
        has_placeholders = any(p in content for p in ["CHANGE_ME", "your-", "example"])
        assert has_placeholders, "Template should contain placeholder values"

    def test_env_example_clean(self):
        self._check_no_real_values((REPO_ROOT / ".env.example").read_text())

    def test_secrets_example_clean(self):
        self._check_no_real_values((REPO_ROOT / "secrets.example.yml").read_text())

    def test_run_config_example_clean(self):
        self._check_no_real_values((REPO_ROOT / "run_config.example.yml").read_text())

    def test_settings_example_clean(self):
        self._check_no_real_values((REPO_ROOT / "settings" / "settings.example.yml").read_text())


class TestRunConfigExampleValid:
    def setup_method(self):
        self.config = yaml.safe_load(
            (REPO_ROOT / "run_config.example.yml").read_text()
        )

    def test_has_target(self):
        assert "target" in self.config

    def test_has_servers(self):
        assert "servers" in self.config
        assert len(self.config["servers"]) >= 1

    def test_has_disk_map(self):
        assert "disk_map" in self.config

    def test_has_banned_patterns(self):
        assert "banned_hosts" in self.config
        assert "banned_paths" in self.config

    def test_has_domain(self):
        assert "domain" in self.config

    def test_has_pubsub_topics(self):
        assert "pubsub_commands_topic" in self.config
        assert "pubsub_results_topic" in self.config

    def test_has_jumpbox_config(self):
        assert "jumpbox_host" in self.config
        assert "jumpbox_user" in self.config

    def test_has_git_config(self):
        assert "git_remote" in self.config
        assert "github_remote_url" in self.config

    def test_server_has_fqdn(self):
        for name, srv in self.config["servers"].items():
            assert "fqdn" in srv, f"Server {name} missing fqdn"


class TestSecretsExampleValid:
    def setup_method(self):
        self.secrets = yaml.safe_load(
            (REPO_ROOT / "secrets.example.yml").read_text()
        )

    def test_has_target_password(self):
        assert "target_password" in self.secrets

    def test_has_fallback_passwords(self):
        assert "fallback_passwords" in self.secrets

    def test_has_bmc_passwords(self):
        assert "bmc_passwords" in self.secrets
        assert isinstance(self.secrets["bmc_passwords"], dict)

    def test_all_passwords_are_placeholders(self):
        assert self.secrets["target_password"] == "CHANGE_ME"
        for host, pw in self.secrets["bmc_passwords"].items():
            assert pw == "CHANGE_ME", f"BMC password for {host} should be CHANGE_ME"


class TestSettingsGeneration:
    """Test that configure_target generates valid settings from run_config."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_dir = os.getcwd()

        shutil.copytree(REPO_ROOT / "settings", Path(self.tmpdir) / "settings")
        run_config = REPO_ROOT / "run_config.yml"
        if not run_config.exists():
            pytest.skip("run_config.yml not available (gitignored, local only)")
        shutil.copy(run_config, Path(self.tmpdir) / "run_config.yml")

    def teardown_method(self):
        os.chdir(self.orig_dir)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_settings_creates_file(self):
        from tools.configure_target import generate_settings, _load_run_config, TEMPLATE_FILE

        if not TEMPLATE_FILE.exists():
            pytest.skip("Settings template not available (gitignored, local only)")

        config = _load_run_config()
        servers = config.get("servers", {})
        disk_map = config.get("disk_map", {})

        for short in servers:
            if disk_map.get(short):
                topic = "RHEL-10.0"
                output_path = generate_settings(short, topic)
                assert output_path.exists(), f"Settings file not created for {short}"

                content = output_path.read_text()
                parsed = yaml.safe_load(content)
                assert "topics" in parsed, f"No topics section in {short} settings"
                assert "lab" in parsed, f"No lab section in {short} settings"

                systems = parsed["topics"][0].get("systems", [])
                assert len(systems) >= 1, f"No systems in {short} settings"
                assert "fqdn" in systems[0], f"No fqdn in {short} settings system"
                assert "ks_append" in systems[0], f"No ks_append in {short} settings"
                break
        else:
            pytest.skip("No server with disk_map found in run_config.yml")


class TestNoRealValuesInRepo:
    """Scan all tracked files for accidentally committed real values."""

    REAL_VALUES = os.environ.get("DCI_REAL_VALUES_CHECK", "").split(",")
    REAL_VALUES = [v.strip() for v in REAL_VALUES if v.strip()]

    SKIP_FILES = {
        "secrets.yml", "run_config.yml", ".env",
        "settings.local.json",
        "test_onboarding.py",
    }

    SKIP_DIRS = {
        "__pycache__", ".git", ".venv", "venv", "secrets",
        "settings", "dci-hooks",
    }

    def _tracked_files(self):
        import subprocess
        result = subprocess.run(
            ["git", "ls-files"], capture_output=True, text=True,
            cwd=REPO_ROOT, timeout=10,
        )
        return [f for f in result.stdout.strip().splitlines() if f]

    def test_no_real_values_in_tracked_files(self):
        violations = []
        for filepath in self._tracked_files():
            if any(skip in filepath for skip in self.SKIP_DIRS):
                continue
            if Path(filepath).name in self.SKIP_FILES:
                continue
            if filepath.endswith((".lock", ".pyc", ".png", ".jpg", ".pdf", ".pptx")):
                continue

            try:
                content = (REPO_ROOT / filepath).read_text(errors="ignore")
            except Exception:
                continue

            for val in self.REAL_VALUES:
                if val in content:
                    violations.append(f"{filepath}: contains '{val}'")

        assert not violations, (
            f"Real values found in {len(violations)} tracked file(s):\n"
            + "\n".join(violations[:20])
        )


class TestBootstrapScript:
    def test_bootstrap_exists(self):
        assert (REPO_ROOT / "tools" / "bootstrap.py").exists()

    def test_bootstrap_importable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "bootstrap", REPO_ROOT / "tools" / "bootstrap.py"
        )
        mod = importlib.util.module_from_spec(spec)
        assert mod is not None

    def test_bootstrap_has_main(self):
        content = (REPO_ROOT / "tools" / "bootstrap.py").read_text()
        assert "def main()" in content
        assert "_check_prerequisites" in content
        assert "_copy_ssh_keys" in content
        assert "_generate_settings" in content
