from celery import app as celery_app_module, states
from django.http import JsonResponse
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .. import models, settings as wooey_settings
from ..backend.utils import valid_user
from .utils import requires_login


celery_app = celery_app_module.app_or_default()

# States from which a job can be re-run.
# REVOKED comes from celery.states and is NOT in WooeyJob.TERMINAL_STATES.
RERUN_STATES = models.WooeyJob.TERMINAL_STATES | {states.REVOKED}


def _get_job_for_user(request, job_id):
    """Fetch a job and verify the requesting user may operate on it.

    Returns (job, None) on success or (None, error_response) on failure.
    """
    job = models.WooeyJob.objects.get(id=job_id)
    if not job.can_user_view(request.user):
        return None, JsonResponse(
            {
                "valid": False,
                "errors": {
                    "__all__": [
                        force_str(_("You are not permitted to access this job."))
                    ]
                },
            },
            status=403,
        )
    script_access = valid_user(job.script_version.script, request.user)
    if not script_access.get("valid"):
        return None, JsonResponse(
            {
                "valid": False,
                "errors": {
                    "__all__": [force_str(script_access.get("error", ""))]
                },
            },
            status=403,
        )
    return job, None


def _conflict_response(message):
    return JsonResponse(
        {
            "valid": False,
            "errors": {"__all__": [force_str(message)]},
        },
        status=409,
    )


@csrf_exempt
@require_http_methods(["GET"])
@requires_login
def job_status(request, job_id):
    job = models.WooeyJob.objects.get(id=job_id)
    if job.can_user_view(request.user):
        return JsonResponse(
            {
                "status": job.status,
                "is_complete": job.status in models.WooeyJob.TERMINAL_STATES,
            }
        )
    else:
        return JsonResponse(
            {
                "valid": False,
                "errors": {
                    "__all__": [
                        force_str(_("You are not permitted to access this job."))
                    ]
                },
            },
            status=403,
        )


@csrf_exempt
@require_http_methods(["GET"])
@requires_login
def job_details(request, job_id):
    job = models.WooeyJob.objects.get(id=job_id)
    if job.can_user_view(request.user):
        assets = []
        is_terminal = job.status in models.WooeyJob.TERMINAL_STATES
        if is_terminal:
            for asset in job.userfile_set.all():
                assets.append(
                    {
                        "name": asset.filename,
                        "url": request.build_absolute_uri(
                            asset.system_file.filepath.url
                        ),
                    }
                )
        return JsonResponse(
            {
                "status": job.status,
                "is_complete": is_terminal,
                "uuid": job.uuid,
                "job_name": job.job_name,
                "job_description": job.job_description,
                "stdout": job.stdout,
                "stderr": job.stderr,
                "assets": assets,
            }
        )
    else:
        return JsonResponse(
            {
                "valid": False,
                "errors": {
                    "__all__": [
                        force_str(_("You are not permitted to access this job."))
                    ]
                },
            },
            status=403,
        )


@csrf_exempt
@require_http_methods(["POST"])
@requires_login
def stop_job(request, job_id):
    job, error = _get_job_for_user(request, job_id)
    if error:
        return error
    if job.status not in (models.WooeyJob.RUNNING, models.WooeyJob.SUBMITTED):
        return _conflict_response(
            _("Cannot stop a job that is not running or submitted.")
        )
    if wooey_settings.WOOEY_CELERY:
        celery_app.control.revoke(job.celery_id, signal="SIGKILL", terminate=True)
    job.status = states.REVOKED
    job.save()
    return JsonResponse(
        {
            "valid": True,
            "job_id": job.pk,
            "status": job.status,
            "job_url": reverse("wooey:celery_results", kwargs={"job_id": job.pk}),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@requires_login
def rerun_job(request, job_id):
    job, error = _get_job_for_user(request, job_id)
    if error:
        return error
    if job.status not in RERUN_STATES:
        return _conflict_response(
            _("Cannot rerun a job that has not completed, failed, or been stopped.")
        )
    job.submit_to_celery(user=request.user, rerun=True)
    job.refresh_from_db()
    return JsonResponse(
        {
            "valid": True,
            "job_id": job.pk,
            "status": job.status,
            "job_url": reverse("wooey:celery_results", kwargs={"job_id": job.pk}),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@requires_login
def resubmit_job(request, job_id):
    job, error = _get_job_for_user(request, job_id)
    if error:
        return error
    if job.status == models.WooeyJob.DELETED:
        return _conflict_response(_("Cannot resubmit a deleted job."))
    original_id = job.pk
    new_job = job.submit_to_celery(resubmit=True, user=request.user)
    return JsonResponse(
        {
            "valid": True,
            "job_id": new_job.pk,
            "original_job_id": original_id,
            "status": new_job.status,
            "job_url": reverse(
                "wooey:celery_results", kwargs={"job_id": new_job.pk}
            ),
        }
    )


@csrf_exempt
@require_http_methods(["POST"])
@requires_login
def delete_job(request, job_id):
    job, error = _get_job_for_user(request, job_id)
    if error:
        return error
    if job.status == models.WooeyJob.DELETED:
        return _conflict_response(_("Cannot delete a job that is already deleted."))
    job.status = models.WooeyJob.DELETED
    job.save()
    return JsonResponse(
        {
            "valid": True,
            "job_id": job.pk,
            "status": job.status,
        }
    )
