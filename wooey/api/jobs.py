from celery import app, states
from django.http import JsonResponse
from django.utils.encoding import force_str
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .. import models
from ..backend.utils import valid_user
from .utils import get_submitted_data, requires_login

celery_app = app.app_or_default()

JOB_COMMANDS = {"stop", "rerun", "resubmit", "delete"}
ACTIVE_STATES = {models.WooeyJob.RUNNING, models.WooeyJob.SUBMITTED}


def _job_error(message, field="__all__", status=400):
    return JsonResponse(
        {"valid": False, "errors": {field: [force_str(message)]}}, status=status
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
def job_command(request, job_id):
    """Run a structured operation against a job.

    Mirrors the website's ``celery_task_command`` view (stop, rerun, resubmit,
    delete) but returns stable JSON suitable for automated callers instead of
    redirect URLs.
    """
    try:
        job = models.WooeyJob.objects.select_related("script_version__script").get(
            id=job_id
        )
    except models.WooeyJob.DoesNotExist:
        return _job_error(_("Unable to find job."), field="job", status=404)

    # Permission: identical to the web page (celery_task_command).
    if not valid_user(job.script_version.script, request.user).get("valid") or not (
        job.user is None or job.user == request.user
    ):
        return _job_error(_("You are not permitted to access this job."), status=403)

    command = get_submitted_data(request).get("command")
    if command not in JOB_COMMANDS:
        return _job_error(_("Unknown command."))

    original_id = job.id

    if command == "stop":
        if job.status not in ACTIVE_STATES:
            return _job_error(_("Job is not running and cannot be stopped."))
        celery_app.control.revoke(job.celery_id, signal="SIGKILL", terminate=True)
        job.status = states.REVOKED
        job.save()
    elif command == "rerun":
        if job.status in ACTIVE_STATES or job.status == models.WooeyJob.DELETED:
            return _job_error(_("Job cannot be rerun in its current state."))
        job.submit_to_celery(user=request.user, rerun=True)
    elif command == "resubmit":
        if job.status == models.WooeyJob.DELETED:
            return _job_error(_("A deleted job cannot be resubmitted."))
        job = job.submit_to_celery(resubmit=True, user=request.user)
    elif command == "delete":
        if job.status == models.WooeyJob.DELETED:
            return _job_error(_("Job is already deleted."))
        job.status = models.WooeyJob.DELETED
        job.save()

    response = {
        "valid": True,
        "command": command,
        "job_id": job.id,
        "status": job.status,
    }
    if command == "resubmit":
        response["original_job_id"] = original_id
    return JsonResponse(response)
