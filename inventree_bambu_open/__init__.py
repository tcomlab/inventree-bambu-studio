"""InvenTree integration for opening CAD attachments in Bambu Studio."""

from pathlib import PurePosixPath
import re

from common.models import Attachment
from plugin import InvenTreePlugin
from plugin.mixins import UserInterfaceMixin


SUPPORTED_EXTENSIONS = {".3mf", ".step", ".stp"}
PART_LOCATION_PATTERN = re.compile(r"^/part/(?P<part_id>\d+)(?:/|$)")


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


class BambuOpenPlugin(UserInterfaceMixin, InvenTreePlugin):
    """Expose the preferred 3D attachment as a primary action on Part pages."""

    NAME = "BambuOpen"
    SLUG = "bambuopen"
    TITLE = "Bambu Studio"
    DESCRIPTION = "Open and save Part 3D models with Bambu Studio"
    VERSION = "0.3.0"

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
