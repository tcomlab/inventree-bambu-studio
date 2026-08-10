"""InvenTree integration for opening CAD attachments in Bambu Studio."""

from pathlib import PurePosixPath
import re

from common.models import Attachment
from plugin import InvenTreePlugin
from plugin.mixins import UserInterfaceMixin


SUPPORTED_EXTENSIONS = {".step", ".stp"}
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
    """Expose STEP attachments as primary actions on Part pages."""

    NAME = "BambuOpen"
    SLUG = "bambuopen"
    TITLE = "Bambu Studio"
    DESCRIPTION = "Open Part STEP attachments in Bambu Studio"
    VERSION = "0.2.1"

    def get_ui_primary_actions(self, request, context, **kwargs):
        """Return one download action for each STEP attachment on a Part."""
        context = context or {}

        part_id = part_id_from_context(context)

        if part_id is None:
            return []

        attachments = Attachment.objects.filter(
            model_type="part",
            model_id=part_id,
            attachment__isnull=False,
        ).order_by("id")

        actions = []

        for item in attachments:
            if not item.attachment:
                continue

            filename = PurePosixPath(item.attachment.name).name
            suffix = PurePosixPath(filename).suffix.lower()

            if suffix not in SUPPORTED_EXTENSIONS:
                continue

            download_url = item.attachment.url

            if request is not None:
                download_url = request.build_absolute_uri(download_url)

            actions.append(
                {
                    "key": f"bambu-open-{item.pk}",
                    "title": "3D Друк",
                    "description": filename,
                    "icon": "ti:printer:outline",
                    "source": self.plugin_static_file(
                        "bambu_open_v4.js:openBambuAttachment"
                    ),
                    "context": {"url": download_url, "filename": filename},
                    "options": {"color": "green"},
                }
            )

        return actions
