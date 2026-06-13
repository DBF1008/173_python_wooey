import json
import os
import shutil
import tempfile

from django.test import Client, TransactionTestCase
from django.urls import reverse

from wooey.models import VirtualEnvironment
from wooey.tasks import setup_venv

from . import factories
from .test_api import ApiTestMixin


class TestVirtualEnvironmentDiagnostic(ApiTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.make_staff()
        self.venv_dir = tempfile.mkdtemp()
        self.venv = factories.VirtualEnvFactory(venv_directory=self.venv_dir)
        install_path = self.venv.get_install_path()
        if os.path.exists(install_path):
            shutil.rmtree(install_path)

    def tearDown(self):
        if os.path.exists(self.venv_dir):
            shutil.rmtree(self.venv_dir)
        super().tearDown()

    def test_diagnostic_not_installed(self):
        """Venv record exists but install_path doesn't on disk."""
        info = self.venv.get_diagnostic_info()
        self.assertFalse(info["is_installed"])
        self.assertFalse(info["executable_exists"])
        self.assertEqual(info["bound_scripts_count"], 0)
        self.assertEqual(info["bound_scripts"], [])
        self.assertEqual(info["name"], self.venv.name)
        self.assertEqual(info["python_binary"], self.venv.python_binary)
        self.assertEqual(info["venv_directory"], self.venv.venv_directory)

    def test_diagnostic_installed(self):
        """After setup_venv(), is_installed and executable_exists are True."""
        setup_venv(self.venv)
        info = self.venv.get_diagnostic_info()
        self.assertTrue(info["is_installed"])
        self.assertTrue(info["executable_exists"])
        self.assertEqual(
            info["executable_path"], self.venv.get_venv_python_binary()
        )
        self.assertEqual(info["install_path"], self.venv.get_install_path())

    def test_diagnostic_bound_scripts(self):
        """Scripts assigned to this venv appear in diagnostic."""
        script1 = factories.ScriptFactory(
            script_name="alpha-script",
            virtual_environment=self.venv,
        )
        script2 = factories.ScriptFactory(
            script_name="beta-script",
            virtual_environment=self.venv,
        )
        info = self.venv.get_diagnostic_info()
        self.assertEqual(info["bound_scripts_count"], 2)
        self.assertIn("alpha-script", info["bound_scripts"])
        self.assertIn("beta-script", info["bound_scripts"])

    def test_diagnostic_after_config_patch(self):
        """After installing then patching requirements, diagnostic reflects current state."""
        setup_venv(self.venv)

        # Patch requirements via API
        patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_virtual_environment",
                kwargs={"virtual_environment_id": self.venv.id},
            ),
            data=json.dumps({"requirements": "flask\ndjango"}),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)

        # Refresh from DB
        self.venv.refresh_from_db()
        info = self.venv.get_diagnostic_info()
        # Disk state unchanged — still installed
        self.assertTrue(info["is_installed"])
        # Requirements reflect new value
        self.assertEqual(info["requirements"], "flask\ndjango")

    def test_diagnostic_uninitialized_directory(self):
        """Venv directory points to a nonexistent path — no exception, graceful."""
        nonexistent_dir = os.path.join(tempfile.gettempdir(), "nonexistent_wooey_test_dir_xyz")
        if os.path.exists(nonexistent_dir):
            shutil.rmtree(nonexistent_dir)
        self.venv.venv_directory = nonexistent_dir
        self.venv.save()
        # Should not raise
        info = self.venv.get_diagnostic_info()
        self.assertFalse(info["is_installed"])
        self.assertFalse(info["executable_exists"])

    def test_list_includes_diagnostic_fields(self):
        """GET list endpoint includes diagnostic fields for each venv."""
        list_response = self.client.get(
            reverse("wooey:api_list_virtual_environments")
        )
        self.assertEqual(list_response.status_code, 200)
        venvs = list_response.json()["virtual_environments"]
        self.assertTrue(len(venvs) >= 1)
        for venv_data in venvs:
            self.assertIn("is_installed", venv_data)
            self.assertIn("executable_exists", venv_data)
            self.assertIn("executable_path", venv_data)
            self.assertIn("bound_scripts", venv_data)
            self.assertIn("bound_scripts_count", venv_data)

    def test_diagnostic_requires_staff(self):
        """Non-staff users cannot access the diagnose endpoint."""
        # Reset to non-staff
        self.api_key.profile.user.is_staff = False
        self.api_key.profile.user.save()
        response = self.client.get(
            reverse(
                "wooey:api_diagnose_virtual_environment",
                kwargs={"virtual_environment_id": self.venv.id},
            )
        )
        self.assertEqual(response.status_code, 403)

    def test_diagnostic_not_found(self):
        """Requesting a non-existent venv ID returns 404."""
        response = self.client.get(
            reverse(
                "wooey:api_diagnose_virtual_environment",
                kwargs={"virtual_environment_id": 99999},
            )
        )
        self.assertEqual(response.status_code, 404)
        data = response.json()
        self.assertFalse(data["valid"])
