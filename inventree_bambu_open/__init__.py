"""InvenTree integration for Bambu Studio and Bambuddy print production."""

import json
from importlib import resources
from pathlib import PurePosixPath
import re
from urllib import error as urllib_error
from urllib import request as urllib_request

from common.models import Attachment
from django.http import HttpResponse, JsonResponse
from django.urls import path
from InvenTree.permissions import auth_exempt
from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, UrlsMixin, UserInterfaceMixin


SUPPORTED_EXTENSIONS = {".3mf", ".step", ".stp"}
PART_LOCATION_PATTERN = re.compile(r"^/part/(?P<part_id>\d+)(?:/|$)")
BUILD_LOCATION_PATTERN = re.compile(r"^/build/(?P<build_id>\d+)(?:/|$)")


def part_id_from_context(context):
    """Extract the Part identifier from either supported UI context shape."""
    if context.get("target_model") == "part":
        candidate = context.get("target_id")
    else:
        match = PART_LOCATION_PATTERN.match(context.get("location", ""))
        candidate = match.group("part_id") if match else None

    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def build_id_from_context(context):
    """Extract the Build Order identifier from supported UI context shapes."""
    if context.get("target_model") == "build":
        candidate = context.get("target_id")
    else:
        match = BUILD_LOCATION_PATTERN.match(context.get("location", ""))
        candidate = match.group("build_id") if match else None

    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


class BambuOpenPlugin(
    SettingsMixin,
    UrlsMixin,
    UserInterfaceMixin,
    InvenTreePlugin,
):
    """Expose Bambu Studio actions and Bambuddy Build Order progress."""

    NAME = "BambuOpen"
    SLUG = "bambuopen"
    TITLE = "Bambu Studio"
    DESCRIPTION = "Open and save Part 3D models with Bambu Studio"
    VERSION = "0.4.2"

    SETTINGS = {
        "SYNC_SERVICE_URL": {
            "name": "Bambuddy sync service URL",
            "description": "Internal URL reachable from the InvenTree server",
            "default": "http://192.168.1.5:8088",
        },
        "SYNC_SERVICE_TOKEN": {
            "name": "Bambuddy sync service token",
            "description": "X-Service-Token configured on the sync service",
            "default": "",
            "protected": True,
        },
        "BAMBUDDY_WEB_URL": {
            "name": "Bambuddy browser URL",
            "description": "Optional URL shown as an Open in Bambuddy link",
            "default": "",
        },
    }

    def setup_urls(self):
        """Provide authenticated same-origin proxies for the Build Order panel."""
        return [
            path(
                "assets/build_order_panel_v1.js",
                self.build_order_panel_script_view,
                name="build-order-panel-script",
            ),
            path(
                "build-order/<int:build_order_id>/",
                self.build_order_status_view,
                name="build-order-status",
            ),
            path(
                "build-order/<int:build_order_id>/queue/",
                self.build_order_queue_view,
                name="build-order-queue",
            ),
        ]

    @auth_exempt
    def build_order_panel_script_view(self, request):
        """Serve the panel module directly so plugin updates need no collectstatic run."""
        script = (
            resources.files(__package__)
            .joinpath("static", "build_order_panel_v1.js")
            .read_text(encoding="utf-8")
        )
        response = HttpResponse(script, content_type="text/javascript; charset=utf-8")
        response["Cache-Control"] = "private, max-age=300"
        return response

    def build_order_status_view(self, request, build_order_id):
        if not request.user.is_authenticated or not request.user.has_perm("build.view_build"):
            return JsonResponse({"detail": "Permission denied"}, status=403)
        if request.method != "GET":
            return JsonResponse({"detail": "Method not allowed"}, status=405)
        return self._sync_service_request("GET", f"/build-orders/{build_order_id}")

    def build_order_queue_view(self, request, build_order_id):
        if not request.user.is_authenticated or not request.user.has_perm("build.change_build"):
            return JsonResponse({"detail": "Permission denied"}, status=403)
        if request.method != "POST":
            return JsonResponse({"detail": "Method not allowed"}, status=405)
        try:
            payload = json.loads(request.body or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return JsonResponse({"detail": "Invalid JSON request"}, status=400)
        return self._sync_service_request(
            "POST",
            f"/build-orders/{build_order_id}/queue",
            payload,
        )

    def _sync_service_request(self, method, route, payload=None):
        base_url = str(self.get_setting("SYNC_SERVICE_URL") or "").rstrip("/")
        if not base_url:
            return JsonResponse({"detail": "Sync service URL is not configured"}, status=503)

        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        token = str(self.get_setting("SYNC_SERVICE_TOKEN") or "")
        if token:
            headers["X-Service-Token"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
        upstream = urllib_request.Request(
            f"{base_url}{route}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib_request.urlopen(upstream, timeout=45) as response:
                response_body = response.read()
                status = response.status
        except urllib_error.HTTPError as exc:
            response_body = exc.read()
            status = exc.code
        except (urllib_error.URLError, TimeoutError) as exc:
            return JsonResponse(
                {"detail": f"Bambuddy sync service is unavailable: {exc}"},
                status=502,
            )

        try:
            data = json.loads(response_body or b"{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {"detail": (response_body or b"").decode("utf-8", errors="replace")[:1000]}
        return JsonResponse(data, status=status, safe=isinstance(data, dict))

    def get_ui_panels(self, request, context, **kwargs):
        context = context or {}
        build_id = build_id_from_context(context)
        if build_id is None or not request.user.has_perm("build.view_build"):
            return []
        root = f"/plugin/{self.SLUG}/build-order/{build_id}/"
        return [
            {
                "key": f"bambuddy-build-order-{build_id}",
                "title": "Bambuddy друк",
                "description": "Черга і поточний стан друку",
                "icon": "ti:printer:outline",
                "source": (
                    f"/plugin/{self.SLUG}/assets/build_order_panel_v1.js"
                    ":renderBuildOrderPanel"
                ),
                "context": {
                    "buildId": build_id,
                    "statusUrl": root,
                    "queueUrl": f"{root}queue/",
                    "bambuddyUrl": str(self.get_setting("BAMBUDDY_WEB_URL") or ""),
                    "canQueue": request.user.has_perm("build.change_build"),
                },
            }
        ]

    def get_ui_primary_actions(self, request, context, **kwargs):
        """Return one action for the preferred printable attachment on a Part."""
        context = context or {}

        part_id = part_id_from_context(context)

        if part_id is None:
            return []

        attachments = list(
            Attachment.objects.filter(
                model_type="part",
                model_id=part_id,
                attachment__isnull=False,
            ).order_by("-id")
        )

        candidates = []

        for item in attachments:
            if item.attachment:
                filename = PurePosixPath(item.attachment.name).name
                suffix = PurePosixPath(filename).suffix.lower()
                if suffix in SUPPORTED_EXTENSIONS:
                    candidates.append((item, filename, suffix))

        if not candidates:
            return []

        # Prefer the newest Bambu Studio project. Fall back to the newest STEP.
        selected = next(
            (candidate for candidate in candidates if candidate[2] == ".3mf"),
            candidates[0],
        )
        item, filename, _ = selected
        download_url = item.attachment.url

        if request is not None:
            download_url = request.build_absolute_uri(download_url)

        return [
            {
                "key": f"bambu-open-{item.pk}",
                "title": "3D Друк",
                "description": filename,
                "icon": "ti:printer:outline",
                "source": self.plugin_static_file(
                    "bambu_open_v5.js:openBambuAttachment"
                ),
                "context": {
                    "url": download_url,
                    "filename": filename,
                    "partId": part_id,
                    "attachmentId": item.pk,
                },
                "options": {"color": "green"},
            }
        ]
