import json
import os
import shutil
import tempfile
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TransactionTestCase
from django.urls import reverse

from .. import models
from ..models import WooeyJob
from . import factories, mixins


class ApiTestMixin(object):
    def setUp(self):
        self.api_key = factories.APIKeyFactory()
        self.client = Client(
            HTTP_AUTHORIZATION="Bearer {}".format(self.api_key._api_key)
        )
        return super().setUp()

    def make_staff(self):
        self.api_key.profile.user.is_staff = True
        self.api_key.profile.user.save()


class TestJobStatus(mixins.ScriptFactoryMixin, ApiTestMixin, TransactionTestCase):
    def test_reports_when_job_is_complete(self):
        job = factories.generate_job(self.translate_script)
        response = self.client.get(
            reverse("wooey:api_job_status", kwargs={"job_id": job.id})
        )
        self.assertFalse(response.json()["is_complete"])
        job.status = WooeyJob.COMPLETED
        job.save()
        response = self.client.get(
            reverse("wooey:api_job_status", kwargs={"job_id": job.id})
        )
        self.assertTrue(response.json()["is_complete"])

    def test_error_when_invalid_user(self):
        another_user = factories.UserFactory(username="bob")
        job = factories.generate_job(self.translate_script)
        job.user = another_user
        job.save()
        response = self.client.get(
            reverse("wooey:api_job_status", kwargs={"job_id": job.id})
        )
        self.assertFalse(response.json()["valid"])
        self.assertEqual(response.status_code, 403)


class TestJobDetails(
    mixins.ScriptFactoryMixin,
    mixins.FileCleanupMixin,
    ApiTestMixin,
    TransactionTestCase,
):
    def test_job_details(self):
        job = self.create_job_with_output_files()
        response = self.client.get(
            reverse("wooey:api_job_details", kwargs={"job_id": job.id})
        )
        data = response.json()
        self.assertIn("url", data["assets"][0])
        self.assertTrue(data["is_complete"])
        self.assertEqual(job.job_name, data["job_name"])


class TestScriptAddition(mixins.ScriptFactoryMixin, ApiTestMixin, TransactionTestCase):
    def test_only_staff_can_add_new_scripts(self):
        payload = {
            "group": "test group",
            "translate-script": open(self.translate_script_path, "rb"),
            "choice-script": open(self.choice_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        self.make_staff()
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        data = response.json()
        self.assertEqual(data[0]["script"], "translate-script")
        self.assertEqual(data[1]["script"], "choice-script")

    def test_can_update_existing_script(self):
        self.make_staff()
        payload = {
            "group": "test group",
            "update-script": open(self.version1_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        data = response.json()
        self.assertEqual(data[0]["script"], "update-script")
        self.assertEqual(data[0]["iteration"], 1)
        self.assertEqual(data[0]["version"], "1")
        payload = {
            "group": "test group",
            "update-script": open(self.version2_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        data = response.json()
        self.assertEqual(data[0]["script"], "update-script")
        self.assertEqual(data[0]["version"], "1")
        self.assertEqual(data[0]["iteration"], 2)
        self.assertEqual(data[0]["is_default"], True)

    def test_update_existing_script_preserves_group_and_import_flags_when_omitted(self):
        self.make_staff()
        create_response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "group": "custom update group",
                "ignore_bad_imports": True,
                "update-script": open(self.version1_script_path, "rb"),
            },
        )
        create_data = create_response.json()
        script = models.Script.objects.get(pk=create_data[0]["id"])

        update_response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "update-script": open(self.version2_script_path, "rb"),
            },
        )
        update_data = update_response.json()

        script.refresh_from_db()
        self.assertEqual(update_data[0]["id"], script.id)
        self.assertEqual(script.script_group.group_name, "custom update group")
        self.assertTrue(script.ignore_bad_imports)
        self.assertEqual(
            models.Script.objects.filter(script_name="update-script").count(), 1
        )

    def test_can_disable_default_update(self):
        self.make_staff()
        payload = {
            "group": "test group 2",
            "update-script": open(self.version1_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        data = response.json()
        self.assertEqual(data[0]["script"], "update-script")
        self.assertEqual(data[0]["iteration"], 1)
        self.assertEqual(data[0]["version"], "1")
        payload = {
            "group": "test group 2",
            "default": False,
            "update-script": open(self.version2_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        data = response.json()
        self.assertEqual(data[0]["script"], "update-script")
        self.assertEqual(data[0]["version"], "1")
        self.assertEqual(data[0]["iteration"], 2)
        self.assertEqual(data[0]["is_default"], False)

    def test_requires_a_script_file(self):
        self.make_staff()
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={"group": "test group"},
        )
        data = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data["valid"])
        self.assertIn("script_file", data["errors"])

    def test_reports_parser_errors(self):
        self.make_staff()
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "group": "test group",
                "broken-script": SimpleUploadedFile(
                    "broken.py",
                    b"def broken(:\n",
                    content_type="text/x-python",
                ),
            },
        )
        data = response.json()
        self.assertFalse(data[0]["success"])
        self.assertIn("ParserError", data[0]["errors"])

    def test_can_apply_script_metadata_during_upload(self):
        self.make_staff()
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "group": "custom group",
                "script_description": "custom description",
                "documentation": "custom docs",
                "script_order": 5,
                "is_active": False,
                "ignore_bad_imports": True,
                "execute_full_path": False,
                "save_path": "custom/output/path",
                "metadata-script": open(self.translate_script_path, "rb"),
            },
        )
        data = response.json()
        script = models.Script.objects.get(pk=data[0]["id"])
        self.assertEqual(script.script_group.group_name, "custom group")
        self.assertEqual(script.script_description, "custom description")
        self.assertEqual(script.documentation, "custom docs")
        self.assertEqual(script.script_order, 5)
        self.assertFalse(script.is_active)
        self.assertTrue(script.ignore_bad_imports)
        self.assertFalse(script.execute_full_path)
        self.assertEqual(script.save_path, "custom/output/path")


class TestScriptManagementApi(
    mixins.ScriptFactoryMixin, ApiTestMixin, TransactionTestCase
):
    def test_management_endpoints_require_authentication(self):
        response = Client().get(reverse("wooey:api_list_scripts"))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {
                "valid": False,
                "errors": {"__all__": ["Must be authenticated to use this method."]},
            },
        )

    def test_management_endpoints_require_staff(self):
        list_response = self.client.get(reverse("wooey:api_list_scripts"))
        detail_response = self.client.get(
            reverse(
                "wooey:api_script_detail",
                kwargs={"slug": self.translate_script.script.slug},
            )
        )
        patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script",
                kwargs={"slug": self.translate_script.script.slug},
            ),
            data=json.dumps({"script_name": "updated"}),
            content_type="application/json",
        )
        version_patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script_version",
                kwargs={
                    "slug": self.translate_script.script.slug,
                    "version_id": self.translate_script.id,
                },
            ),
            data=json.dumps({"default_version": True}),
            content_type="application/json",
        )

        self.assertEqual(list_response.status_code, 403)
        self.assertEqual(detail_response.status_code, 403)
        self.assertEqual(patch_response.status_code, 403)
        self.assertEqual(version_patch_response.status_code, 403)

    def test_can_list_scripts_and_report_missing_default_versions(self):
        self.make_staff()
        models.ScriptVersion.objects.filter(script=self.translate_script.script).update(
            default_version=False
        )

        response = self.client.get(reverse("wooey:api_list_scripts"))
        self.assertEqual(response.status_code, 200)
        data = response.json()

        translate_data = next(
            i for i in data["scripts"] if i["slug"] == self.translate_script.script.slug
        )
        self.assertEqual(
            translate_data["script_name"], self.translate_script.script.script_name
        )
        self.assertIn("default version", " ".join(translate_data["issues"]).lower())

    def test_can_get_script_detail(self):
        self.make_staff()
        response = self.client.get(
            reverse(
                "wooey:api_script_detail",
                kwargs={"slug": self.version1_script.script.slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["script"]
        self.assertEqual(data["script_name"], self.version1_script.script.script_name)
        self.assertEqual(len(data["versions"]), 2)

    def test_script_detail_includes_version_download_url(self):
        self.make_staff()
        response = self.client.get(
            reverse(
                "wooey:api_script_detail",
                kwargs={"slug": self.translate_script.script.slug},
            )
        )
        data = response.json()["script"]
        self.assertTrue(data["versions"][0]["script_url"])
        self.assertIn(
            data["versions"][0]["script_filename"], data["versions"][0]["script_path"]
        )

    def test_can_patch_script(self):
        self.make_staff()
        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script",
                kwargs={"slug": self.translate_script.script.slug},
            ),
            data=json.dumps(
                {
                    "script_name": "updated script name",
                    "group": "updated group",
                    "script_description": "updated description",
                    "documentation": "updated docs",
                    "script_order": 7,
                    "is_active": False,
                    "ignore_bad_imports": True,
                    "execute_full_path": False,
                    "save_path": "updated/output",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        script = self.translate_script.script
        script.refresh_from_db()
        self.assertEqual(script.script_name, "updated script name")
        self.assertEqual(script.script_group.group_name, "updated group")
        self.assertEqual(script.script_description, "updated description")
        self.assertEqual(script.documentation, "updated docs")
        self.assertEqual(script.script_order, 7)
        self.assertFalse(script.is_active)
        self.assertTrue(script.ignore_bad_imports)
        self.assertFalse(script.execute_full_path)
        self.assertEqual(script.save_path, "updated/output")

    def test_patch_script_does_not_apply_defaults_to_omitted_fields(self):
        self.make_staff()
        script = self.translate_script.script
        script.script_order = 9
        script.is_active = False
        script.ignore_bad_imports = True
        script.execute_full_path = False
        script.save()

        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script",
                kwargs={"slug": script.slug},
            ),
            data=json.dumps(
                {
                    "script_description": "only the description should change",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        script.refresh_from_db()
        self.assertEqual(
            script.script_description, "only the description should change"
        )
        self.assertEqual(script.script_order, 9)
        self.assertFalse(script.is_active)
        self.assertTrue(script.ignore_bad_imports)
        self.assertFalse(script.execute_full_path)

    def test_can_switch_default_script_version(self):
        self.make_staff()
        self.version1_script.refresh_from_db()
        self.assertFalse(self.version1_script.default_version)

        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script_version",
                kwargs={
                    "slug": self.version1_script.script.slug,
                    "version_id": self.version1_script.id,
                },
            ),
            data=json.dumps({"default_version": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        self.version1_script.refresh_from_db()
        self.version2_script.refresh_from_db()
        self.assertTrue(self.version1_script.default_version)
        self.assertFalse(self.version2_script.default_version)

    def test_can_disable_non_default_script_version(self):
        self.make_staff()
        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script_version",
                kwargs={
                    "slug": self.version1_script.script.slug,
                    "version_id": self.version1_script.id,
                },
            ),
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        self.version1_script.refresh_from_db()
        self.assertFalse(self.version1_script.is_active)
        self.assertFalse(self.version1_script.default_version)

    def test_can_disable_default_script_version(self):
        self.make_staff()
        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script_version",
                kwargs={
                    "slug": self.version2_script.script.slug,
                    "version_id": self.version2_script.id,
                },
            ),
            data=json.dumps({"is_active": False}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        self.version2_script.refresh_from_db()
        self.assertFalse(self.version2_script.is_active)
        self.assertFalse(self.version2_script.default_version)

    def test_can_reenable_disabled_script_version(self):
        self.make_staff()
        self.version1_script.is_active = False
        self.version1_script.default_version = False
        self.version1_script.save()

        response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_script_version",
                kwargs={
                    "slug": self.version1_script.script.slug,
                    "version_id": self.version1_script.id,
                },
            ),
            data=json.dumps({"is_active": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        self.version1_script.refresh_from_db()
        self.assertTrue(self.version1_script.is_active)
        self.assertFalse(self.version1_script.default_version)

    def test_records_script_and_version_audit_users(self):
        self.make_staff()
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "group": "audit group",
                "audit-script": open(self.translate_script_path, "rb"),
            },
        )
        data = response.json()
        slug = data[0]["slug"]

        detail_response = self.client.get(
            reverse("wooey:api_script_detail", kwargs={"slug": slug})
        )
        detail = detail_response.json()["script"]
        username = self.api_key.profile.user.username

        self.assertEqual(detail["created_by"], username)
        self.assertEqual(detail["modified_by"], username)
        self.assertEqual(detail["versions"][0]["created_by"], username)
        self.assertEqual(detail["versions"][0]["modified_by"], username)

    def test_patch_script_updates_last_modified_user(self):
        self.make_staff()
        upload_response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data={
                "group": "audit group",
                "audited-script": open(self.translate_script_path, "rb"),
            },
        )
        slug = upload_response.json()[0]["slug"]

        other_key = factories.APIKeyFactory(profile__user__username="editor-user")
        other_key.profile.user.is_staff = True
        other_key.profile.user.save()
        other_client = Client(HTTP_AUTHORIZATION="Bearer {}".format(other_key._api_key))

        patch_response = other_client.generic(
            "PATCH",
            reverse("wooey:api_patch_script", kwargs={"slug": slug}),
            data=json.dumps({"script_description": "updated by another staff user"}),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)

        detail_response = other_client.get(
            reverse("wooey:api_script_detail", kwargs={"slug": slug})
        )
        detail = detail_response.json()["script"]
        self.assertEqual(detail["created_by"], self.api_key.profile.user.username)
        self.assertEqual(detail["modified_by"], other_key.profile.user.username)


class TestVirtualEnvironmentManagementApi(ApiTestMixin, TransactionTestCase):
    def test_virtual_environment_endpoints_require_staff(self):
        virtual_environment = factories.VirtualEnvFactory()

        list_response = self.client.get(reverse("wooey:api_list_virtual_environments"))
        create_response = self.client.post(
            reverse("wooey:api_create_virtual_environment"),
            data={
                "name": "new-venv",
                "python_binary": "/usr/bin/python3",
                "venv_directory": "/tmp",
            },
        )
        patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_virtual_environment",
                kwargs={"virtual_environment_id": virtual_environment.id},
            ),
            data=json.dumps({"name": "updated-venv"}),
            content_type="application/json",
        )

        self.assertEqual(list_response.status_code, 403)
        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(patch_response.status_code, 403)

    def test_can_create_list_and_patch_virtual_environments(self):
        self.make_staff()

        create_response = self.client.post(
            reverse("wooey:api_create_virtual_environment"),
            data={
                "name": "new-venv",
                "python_binary": "/usr/bin/python3",
                "requirements": "django\ncelery",
                "venv_directory": "/tmp",
            },
        )
        self.assertEqual(create_response.status_code, 200)
        virtual_environment_id = create_response.json()["virtual_environment"]["id"]

        list_response = self.client.get(reverse("wooey:api_list_virtual_environments"))
        self.assertEqual(list_response.status_code, 200)
        listed_virtual_environment = next(
            virtual_environment
            for virtual_environment in list_response.json()["virtual_environments"]
            if virtual_environment["id"] == virtual_environment_id
        )
        self.assertEqual(listed_virtual_environment["name"], "new-venv")
        self.assertEqual(listed_virtual_environment["requirements"], "django\ncelery")

        patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_virtual_environment",
                kwargs={"virtual_environment_id": virtual_environment_id},
            ),
            data=json.dumps(
                {
                    "name": "updated-venv",
                    "python_binary": "/usr/bin/python3.11",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 200)

        virtual_environment = models.VirtualEnvironment.objects.get(
            pk=virtual_environment_id
        )
        self.assertEqual(virtual_environment.name, "updated-venv")
        self.assertEqual(virtual_environment.python_binary, "/usr/bin/python3.11")


class TestVirtualEnvironmentDiagnosticsApi(ApiTestMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.venv_directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.venv_directory, ignore_errors=True)

    def _create_virtual_environment(self, **overrides):
        params = {
            "name": "diagnostics-venv",
            "python_binary": "/usr/bin/python3",
            "venv_directory": self.venv_directory,
        }
        params.update(overrides)
        return factories.VirtualEnvFactory(**params)

    def _diagnostics_url(self, virtual_environment_id):
        return reverse(
            "wooey:api_diagnostics_virtual_environment",
            kwargs={"virtual_environment_id": virtual_environment_id},
        )

    def test_diagnostics_requires_staff(self):
        virtual_environment = self._create_virtual_environment()
        response = self.client.get(self._diagnostics_url(virtual_environment.id))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["valid"])

    def test_diagnostics_returns_404_for_unknown_environment(self):
        self.make_staff()
        response = self.client.get(self._diagnostics_url(999999))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["valid"])

    def test_diagnostics_for_empty_environment(self):
        self.make_staff()
        virtual_environment = self._create_virtual_environment(requirements="django")

        response = self.client.get(self._diagnostics_url(virtual_environment.id))

        self.assertEqual(response.status_code, 200)
        data = response.json()["virtual_environment"]
        self.assertFalse(data["installed"])
        self.assertEqual(data["bound_scripts"], [])
        self.assertEqual(
            data["python_executable"], virtual_environment.get_venv_python_binary()
        )
        self.assertEqual(data["install_path"], virtual_environment.get_install_path())
        # Key configuration summary travels with the diagnostics payload.
        self.assertEqual(data["name"], "diagnostics-venv")
        self.assertEqual(data["python_binary"], "/usr/bin/python3")
        self.assertEqual(data["requirements"], "django")
        self.assertEqual(data["venv_directory"], self.venv_directory)

    def test_diagnostics_lists_bound_scripts(self):
        self.make_staff()
        virtual_environment = self._create_virtual_environment()
        first_script = factories.ScriptFactory(
            script_name="alpha-script", virtual_environment=virtual_environment
        )
        second_script = factories.ScriptFactory(
            script_name="beta-script", virtual_environment=virtual_environment
        )
        # A script bound to a different environment must not leak in.
        other_environment = self._create_virtual_environment(name="other-venv")
        factories.ScriptFactory(
            script_name="unrelated-script", virtual_environment=other_environment
        )

        response = self.client.get(self._diagnostics_url(virtual_environment.id))

        data = response.json()["virtual_environment"]
        bound_scripts = data["bound_scripts"]
        self.assertEqual(
            [entry["name"] for entry in bound_scripts],
            ["alpha-script", "beta-script"],
        )
        self.assertEqual(bound_scripts[0]["id"], first_script.id)
        self.assertEqual(bound_scripts[0]["slug"], first_script.slug)
        self.assertEqual(bound_scripts[1]["id"], second_script.id)
        self.assertEqual(bound_scripts[1]["slug"], second_script.slug)

    def test_diagnostics_reflect_configuration_updates(self):
        self.make_staff()
        virtual_environment = self._create_virtual_environment(requirements="django")

        patch_response = self.client.generic(
            "PATCH",
            reverse(
                "wooey:api_patch_virtual_environment",
                kwargs={"virtual_environment_id": virtual_environment.id},
            ),
            data=json.dumps(
                {
                    "python_binary": "/usr/bin/python3.11",
                    "requirements": "flask",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(patch_response.status_code, 200)
        # The patch response exposes the same diagnostics shape as the query.
        patched = patch_response.json()["virtual_environment"]
        self.assertIn("installed", patched)
        self.assertIn("bound_scripts", patched)
        self.assertEqual(patched["python_binary"], "/usr/bin/python3.11")
        self.assertEqual(patched["requirements"], "flask")

        response = self.client.get(self._diagnostics_url(virtual_environment.id))
        data = response.json()["virtual_environment"]
        virtual_environment.refresh_from_db()
        self.assertEqual(data["python_binary"], "/usr/bin/python3.11")
        self.assertEqual(data["requirements"], "flask")
        # Path-affecting config changes are reflected in the resolved paths.
        self.assertEqual(data["install_path"], virtual_environment.get_install_path())
        self.assertEqual(
            data["python_executable"], virtual_environment.get_venv_python_binary()
        )

    def test_diagnostics_for_uninitialized_directory(self):
        self.make_staff()
        virtual_environment = self._create_virtual_environment(
            venv_directory=os.path.join(self.venv_directory, "missing")
        )

        response = self.client.get(self._diagnostics_url(virtual_environment.id))

        self.assertEqual(response.status_code, 200)
        data = response.json()["virtual_environment"]
        self.assertFalse(data["installed"])
        # Even without an initialized directory the metadata stays usable.
        self.assertEqual(
            data["python_executable"], virtual_environment.get_venv_python_binary()
        )
        self.assertEqual(data["install_path"], virtual_environment.get_install_path())
        self.assertFalse(os.path.exists(data["install_path"]))

    def test_diagnostics_reports_installed_environment(self):
        self.make_staff()
        virtual_environment = self._create_virtual_environment()
        executable = virtual_environment.get_venv_python_binary()
        os.makedirs(os.path.dirname(executable), exist_ok=True)
        with open(executable, "w"):
            pass

        response = self.client.get(self._diagnostics_url(virtual_environment.id))

        data = response.json()["virtual_environment"]
        self.assertTrue(data["installed"])
        self.assertEqual(data["python_executable"], executable)


class TestScriptSubmission(
    mixins.ScriptFactoryMixin, ApiTestMixin, TransactionTestCase
):
    def test_disabled_version_cannot_be_submitted(self):
        self.make_staff()
        self.version1_script.is_active = False
        self.version1_script.save()
        payload = {
            "job_name": "test",
            "iteration": 1,
            "command": "--one 1",
        }
        response = self.client.post(
            reverse("wooey:api_submit_script", kwargs={"slug": "version_test"}),
            data=payload,
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["valid"])

    def test_defaults_to_latest_script(self):
        self.make_staff()
        payload = {
            "group": "test group 2",
            "update-script": open(self.version1_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        payload = {
            "group": "test group 2",
            "update-script": open(self.version2_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        payload = {
            "job_name": "test",
            "command": "--one 1 --two 2",
        }
        response = self.client.post(
            reverse("wooey:api_submit_script", kwargs={"slug": "update-script"}),
            data=payload,
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["valid"])
        job = WooeyJob.objects.get(id=data["job_id"])
        self.assertEqual(job.script_version.script_iteration, 2)

    def test_can_specify_version(self):
        self.make_staff()
        payload = {
            "group": "test group 2",
            "update-script": open(self.version1_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        payload = {
            "group": "test group 2",
            "update-script": open(self.version2_script_path, "rb"),
        }
        response = self.client.post(
            reverse("wooey:api_add_or_update_script"),
            data=payload,
        )
        payload = {
            "job_name": "test",
            "iteration": 1,
            "command": "--one 1",
        }
        response = self.client.post(
            reverse("wooey:api_submit_script", kwargs={"slug": "update-script"}),
            data=payload,
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["valid"])
        job = WooeyJob.objects.get(id=data["job_id"])
        self.assertEqual(job.script_version.script_iteration, 1)

    def test_can_submit_script_with_json(self):
        script_version = self.translate_script
        payload = {
            "job_name": "test",
            "job_description": "a test job",
            "command": "--sequence aaa",
        }
        response = self.client.post(
            reverse(
                "wooey:api_submit_script", kwargs={"slug": script_version.script.slug}
            ),
            data=payload,
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["valid"])
        job = WooeyJob.objects.get(id=data["job_id"])
        self.assertEqual(job.job_description, "a test job")
        self.assertEqual(
            job.scriptparameters_set.get(parameter__slug="sequence").value, "aaa"
        )

    def test_submit_script_with_mixed_form_data(self):
        script_version = self.translate_script
        contents = b">test sequence\naaaggg\n"
        fasta_file = BytesIO(contents)
        payload = {
            "job_name": "test",
            "command": "--fasta fasta",
            "fasta": fasta_file,
        }
        response = self.client.post(
            reverse(
                "wooey:api_submit_script", kwargs={"slug": script_version.script.slug}
            ),
            data=payload,
        )
        data = response.json()
        self.assertTrue(data["valid"])
        job = WooeyJob.objects.get(id=data["job_id"])
        self.assertEqual(
            job.scriptparameters_set.get(parameter__slug="fasta").value.read(), contents
        )

    def test_submit_multiple_files_and_inputs(self):
        script_version = self.choice_script
        contents1 = b"foo"
        contents2 = b"bar"
        file1 = BytesIO(contents1)
        file2 = BytesIO(contents2)
        payload = {
            "job_name": "test",
            "command": "--need-at-least-one-numbers 1 2 3 --multiple-file-choices file1 file2",
            "file1": file1,
            "file2": file2,
        }
        response = self.client.post(
            reverse(
                "wooey:api_submit_script", kwargs={"slug": script_version.script.slug}
            ),
            data=payload,
        )
        data = response.json()
        self.assertTrue(data["valid"])
        job = WooeyJob.objects.get(id=data["job_id"])
        uploaded_files = {
            os.path.basename(i.value.name): i.value
            for i in job.scriptparameters_set.filter(
                parameter__slug="multiple_file_choices"
            )
        }
        self.assertEqual(len(uploaded_files), 2)
        self.assertEqual(uploaded_files["file1"].read(), contents1)
        self.assertEqual(uploaded_files["file2"].read(), contents2)
        self.assertEqual(
            sorted(
                [
                    i.value
                    for i in job.scriptparameters_set.filter(
                        parameter__slug="need_at_least_one_numbers"
                    ).all()
                ]
            ),
            [1, 2, 3],
        )
