import mock
import io
import os
from datetime import timedelta

from django.test import TestCase

from wooey import settings as wooey_settings
from wooey.backend import utils
from wooey.backend.utils import add_wooey_script
from wooey.models import (
    WooeyJob,
)
from wooey.tasks import (
    cleanup_dead_jobs,
    get_latest_script,
)

from . import config, mixins, factories


class TaskTests(mixins.ScriptFactoryMixin, TestCase):
    def test_job_cleanup(self):
        from ..models import WooeyJob
        from ..tasks import cleanup_wooey_jobs
        import time

        anon_job = factories.generate_job(self.translate_script)
        user_job = factories.generate_job(self.translate_script)
        user = factories.UserFactory()
        user_job.user = user
        user_job.save()
        wooey_settings.WOOEY_JOB_EXPIRATION.update(
            {
                "user": timedelta(hours=1),
                "anonymous": timedelta(hours=1),
            }
        )
        cleanup_wooey_jobs()
        self.assertListEqual(list(WooeyJob.objects.all()), [anon_job, user_job])
        time.sleep(0.1)
        wooey_settings.WOOEY_JOB_EXPIRATION.update(
            {
                "user": timedelta(hours=1),
                "anonymous": timedelta(microseconds=1),
            }
        )
        cleanup_wooey_jobs()
        self.assertListEqual(list(WooeyJob.objects.all()), [user_job])

        wooey_settings.WOOEY_JOB_EXPIRATION.update(
            {
                "user": timedelta(microseconds=1),
                "anonymous": timedelta(microseconds=1),
            }
        )

        cleanup_wooey_jobs()
        self.assertListEqual(list(WooeyJob.objects.all()), [])


class TestGetLatestScript(mixins.FileMixin, mixins.ScriptTearDown, TestCase):
    def setUp(self):
        super(TestGetLatestScript, self).setUp()
        script = os.path.join(config.WOOEY_TEST_SCRIPTS, "versioned_script", "v1.py")
        with open(script) as o:
            v1 = self.storage.save(self.filename_func("v1.py"), o)
        res = add_wooey_script(script_path=v1, script_name="test_versions")
        self.first_version = self.rename_script(res["script"])

    def rename_script(self, script_version):
        # Because we are on local storage, the script uploaded will already be present, so
        # we rename it to mimic it being absent on a worker node
        new_name = self.storage.save(
            script_version.script_path.name, script_version.script_path.file
        )
        script_version.script_path.name = new_name
        script_version.save()
        return script_version

    def test_get_latest_script_loads_initial(self):
        self.assertTrue(get_latest_script(self.first_version))

    def test_get_latest_script_doesnt_redownload_same_script(self):
        self.assertTrue(get_latest_script(self.first_version))
        self.assertFalse(get_latest_script(self.first_version))

    def test_get_latest_script_downloads_new_script(self):
        get_latest_script(self.first_version)

        # Update the script version
        script = os.path.join(config.WOOEY_TEST_SCRIPTS, "versioned_script", "v2.py")
        with open(script) as o:
            v2 = self.storage.save(self.filename_func("v2.py"), o)

        res = add_wooey_script(script_path=v2, script_name="test_versions")
        second_version = self.rename_script(res["script"])

        self.assertTrue(get_latest_script(second_version))

    def test_first_pull_verifies_content(self):
        """First pull (file absent locally) downloads the correct file content."""
        from ..models import ScriptVersion

        # Capture the canonical content from remote storage (same bytes
        # that get_latest_script will download).
        with self.storage.open(self.first_version.script_path.name) as f:
            expected_content = f.read()

        self.assertTrue(get_latest_script(self.first_version))

        # Verify the downloaded content matches what is in remote storage
        local_storage = utils.get_storage(local=True)
        with local_storage.open(self.first_version.script_path.name) as f:
            cached_content = f.read()
        self.assertEqual(cached_content, expected_content)

        # Checksums must match so the next call skips the download
        local_checksum = utils.get_checksum(buff=cached_content)
        sv = ScriptVersion.objects.get(pk=self.first_version.pk)
        self.assertEqual(local_checksum, sv.checksum)

    def test_version_update_redownloads_correct_content(self):
        """After the remote script is updated, get_latest_script fetches the
        new content rather than keeping the stale local cache."""
        from ..models import ScriptVersion

        # Step 1: initial pull — populates local cache with v1
        get_latest_script(self.first_version)
        local_storage = utils.get_storage(local=True)

        # Step 2: simulate a remote-side version upgrade — overwrite the
        # remote file with v2 content and update the DB checksum, bypassing
        # signals so no extra ScriptVersion row is created.
        v2_path = os.path.join(
            config.WOOEY_TEST_SCRIPTS, "versioned_script", "v2.py"
        )
        with open(v2_path, "rb") as f:
            v2_content = f.read()

        self.storage.delete(self.first_version.script_path.name)
        self.storage.save(
            self.first_version.script_path.name, io.BytesIO(v2_content)
        )

        new_checksum = utils.get_checksum(buff=v2_content)
        ScriptVersion.objects.filter(pk=self.first_version.pk).update(
            checksum=new_checksum
        )
        self.first_version.refresh_from_db()

        # Step 3: the checksum-mismatch branch must fetch fresh content
        self.assertTrue(get_latest_script(self.first_version))

        with local_storage.open(self.first_version.script_path.name) as f:
            cached_content = f.read()
        self.assertEqual(cached_content, v2_content)

        # Sanity: the cache is now consistent — next call is a no-op
        self.assertFalse(get_latest_script(self.first_version))

    def test_cache_corruption_recovers(self):
        """A corrupted local cache (checksum mismatch) triggers a clean
        re-download from remote storage."""
        # Capture the canonical content from remote storage before we
        # touch the local cache.
        with self.storage.open(self.first_version.script_path.name) as f:
            expected_content = f.read()

        # Step 1: initial pull
        get_latest_script(self.first_version)
        local_storage = utils.get_storage(local=True)

        # Step 2: corrupt the local cache
        local_storage.delete(self.first_version.script_path.name)
        local_storage.save(
            self.first_version.script_path.name,
            io.BytesIO(b"# corrupted cache\n"),
        )

        # Sanity: the corruption is detectable via checksum
        with local_storage.open(self.first_version.script_path.name) as f:
            self.assertNotEqual(
                utils.get_checksum(buff=f.read()),
                self.first_version.checksum,
            )

        # Step 3: get_latest_script must detect and recover
        self.assertTrue(get_latest_script(self.first_version))

        # Step 4: content matches the remote copy, not the corrupted bytes
        with local_storage.open(self.first_version.script_path.name) as f:
            cached_content = f.read()
        self.assertEqual(cached_content, expected_content)


class TestCleanupDeadJobs(mixins.ScriptFactoryMixin, TestCase):
    def test_handles_unresponsive_workers(self):
        # Ensure that if we cannot connect to celery, we do nothing.
        with mock.patch("wooey.tasks.celery_app.control.inspect") as inspect_mock:
            running_job = factories.generate_job(self.translate_script)
            running_job.status = WooeyJob.RUNNING
            running_job.save()

            inspect_mock.return_value = mock.Mock(
                active=mock.Mock(
                    return_value=None,
                )
            )
            cleanup_dead_jobs()
            self.assertEqual(
                WooeyJob.objects.get(pk=running_job.id).status, WooeyJob.RUNNING
            )

    def test_cleans_up_dead_jobs(self):
        # Make a job that is running but not active, and a job that is running and active.
        dead_job = factories.generate_job(self.translate_script)
        dead_job.status = WooeyJob.RUNNING
        dead_job.save()
        active_job = factories.generate_job(self.translate_script)
        active_job.status = WooeyJob.RUNNING
        active_job.celery_id = "celery-id"
        active_job.save()
        with mock.patch("wooey.tasks.celery_app.control.inspect") as inspect_mock:
            inspect_mock.return_value = mock.Mock(
                active=mock.Mock(
                    return_value={
                        "worker-id": [
                            {
                                "id": active_job.celery_id,
                            }
                        ]
                    },
                )
            )
            cleanup_dead_jobs()

            # Assert the dead job is updated
            self.assertEqual(
                WooeyJob.objects.get(pk=dead_job.id).status, WooeyJob.FAILED
            )
            self.assertEqual(
                WooeyJob.objects.get(pk=active_job.id).status, WooeyJob.RUNNING
            )
