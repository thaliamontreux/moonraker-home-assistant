"""Live control panel views for the Moonraker integration."""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import async_timeout
from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_PORT,
    CONF_TLS,
    CONF_URL,
    DEFAULT_PORT,
    DOMAIN,
    METHODS,
    TIMEOUT,
)

if TYPE_CHECKING:
    from . import MoonrakerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PANEL_URL = "/api/moonraker/panel"
PANEL_DATA_URL = "/api/moonraker/panel_data"
PANEL_THUMB_URL = "/api/moonraker/panel_thumbnail"
PANEL_ACTION_URL = "/api/moonraker/panel_action"

_GCODE_ROOT = "gcodes"
_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "panel.html")

_META_KEYS = (
    "estimated_time",
    "filament_total",
    "layer_count",
    "layer_height",
    "first_layer_height",
    "object_height",
    "thumbnails_path",
    "gcode_start_byte",
    "gcode_end_byte",
)

_DIRECT_ACTIONS = {
    "pause": METHODS.PRINTER_PRINT_PAUSE,
    "resume": METHODS.PRINTER_PRINT_RESUME,
    "cancel": METHODS.PRINTER_PRINT_CANCEL,
    "emergency_stop": METHODS.PRINTER_EMERGENCY_STOP,
    "firmware_restart": METHODS.PRINTER_FIRMWARE_RESTART,
    "server_restart": METHODS.SERVER_RESTART,
    "host_restart": METHODS.HOST_RESTART,
    "host_shutdown": METHODS.HOST_SHUTDOWN,
    "start_queue": METHODS.SERVER_JOB_QUEUE_START,
}

_GCODE_ACTIONS = {
    "set_speed": "M220 S{}",
    "set_flow": "M221 S{}",
    "set_fan": "M106 S{}",
    "set_extruder": "M104 S{}",
    "set_bed": "M140 S{}",
    "z_adjust": "SET_GCODE_OFFSET Z_ADJUST={}",
    "z_set": "SET_GCODE_OFFSET Z={}",
    "motors_off": "M84",
    "cooldown": "M104 S0\nM140 S0\nM107",
    "home_x": "G28 X",
    "home_y": "G28 Y",
    "home_z": "G28 Z",
    "home_all": "G28",
}

_PREHEAT_PRESETS = {
    "pla": (200, 60),
    "petg": (240, 80),
    "abs": (245, 100),
}


def _read_template() -> str:
    """Read the panel template from disk; intended to run in the executor."""
    with open(_TEMPLATE_PATH, encoding="utf-8") as file:
        return file.read()


def _entry_port(entry) -> int:
    """Return the effective Moonraker port for a config entry."""
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    return int(port) if port not in (None, "") else DEFAULT_PORT


def _webcam_url(entry, url: str | None) -> str | None:
    """Resolve a Moonraker webcam URL to an absolute URL."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    scheme = "https" if entry.data.get(CONF_TLS, False) else "http"
    host = entry.data.get(CONF_URL)
    port = _entry_port(entry)
    return f"{scheme}://{host}:{port}/{url.lstrip('/')}"


class _PanelViewBase(HomeAssistantView):
    """Shared helpers for panel views."""

    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        self.hass = hass

    def _coordinators(self) -> dict[str, "MoonrakerDataUpdateCoordinator"]:
        from . import MoonrakerDataUpdateCoordinator

        return {
            entry_id: coordinator
            for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items()
            if isinstance(coordinator, MoonrakerDataUpdateCoordinator)
        }

    def _resolve(self, entry_id: str | None):
        coordinators = self._coordinators()
        if entry_id:
            return coordinators.get(entry_id)
        return next(iter(coordinators.values()), None)


class MoonrakerPanelView(_PanelViewBase):
    """Serve the live control panel page."""

    url = PANEL_URL
    name = "api:moonraker:panel"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the view."""
        super().__init__(hass)
        self._template: str | None = None

    async def get(self, request):
        """Render the panel page."""
        if self._template is None:
            self._template = await self.hass.async_add_executor_job(_read_template)
        return web.Response(text=self._template, content_type="text/html")


class MoonrakerPanelDataView(_PanelViewBase):
    """Return a JSON snapshot of coordinator data for the panel."""

    url = PANEL_DATA_URL
    name = "api:moonraker:panel_data"

    async def get(self, request):
        """Return the current coordinator data snapshot."""
        coordinator = self._resolve(request.query.get("printer"))
        if coordinator is None:
            return self.json({"error": "no printer configured"}, status_code=404)

        data = coordinator.data or {}
        status = data.get("status") or {}
        file_list = data.get("file_list") or {}
        files = file_list.get("files") if isinstance(file_list, dict) else file_list
        if not isinstance(files, list):
            files = []

        webcams = []
        try:
            webcam_result = await coordinator.async_get_webcams()
        except Exception:  # noqa: BLE001 - webcams are optional
            webcam_result = {}
        entry = coordinator.config_entry
        for webcam in (webcam_result or {}).get("webcams", []):
            if not isinstance(webcam, dict):
                continue
            webcams.append(
                {
                    "name": webcam.get("name"),
                    "stream_url": _webcam_url(entry, webcam.get("stream_url")),
                    "snapshot_url": _webcam_url(entry, webcam.get("snapshot_url")),
                }
            )

        power = data.get("power_devices") or {}
        power_devices = [
            {
                "device": device.get("device"),
                "status": device.get("status"),
                "locked_while_printing": device.get("locked_while_printing"),
            }
            for device in power.get("devices", [])
            if isinstance(device, dict) and device.get("device")
        ]

        macros = sorted(
            obj.partition(" ")[2]
            for obj in status
            if obj.startswith("gcode_macro ")
        )

        return self.json(
            {
                "entry_id": coordinator.config_entry.entry_id,
                "printer": coordinator.api_device_name,
                "available": coordinator.last_update_success,
                "selected_file": coordinator.selected_file,
                "status": status,
                "printer_info": data.get("printer.info") or {},
                "meta": {key: data.get(key) for key in _META_KEYS},
                "history": data.get("history") or {},
                "queue": data.get("queue") or {},
                "spoolman": data.get("spoolman") or {},
                "machine_update": data.get("machine_update") or {},
                "power_devices": power_devices,
                "macros": macros,
                "webcams": webcams,
                "gcode_responses": list(coordinator.gcode_responses)[-80:],
                "files": [
                    {
                        "path": file.get("path"),
                        "size": file.get("size"),
                        "modified": file.get("modified"),
                    }
                    for file in files
                    if isinstance(file, dict) and file.get("path")
                ],
                "entries": [
                    {"id": entry_id, "name": other.api_device_name}
                    for entry_id, other in self._coordinators().items()
                ],
            }
        )


class MoonrakerPanelThumbnailView(_PanelViewBase):
    """Proxy the current job's thumbnail from Moonraker."""

    url = PANEL_THUMB_URL
    name = "api:moonraker:panel_thumbnail"

    async def get(self, request):
        """Return the thumbnail image for the active job."""
        coordinator = self._resolve(request.query.get("printer"))
        if coordinator is None:
            return web.Response(status=404)

        thumbnail_path = (coordinator.data or {}).get("thumbnails_path")
        if not thumbnail_path:
            return web.Response(status=404)

        entry = coordinator.config_entry
        scheme = "https" if entry.data.get(CONF_TLS, False) else "http"
        url = (
            f"{scheme}://{entry.data.get(CONF_URL)}:{_entry_port(entry)}"
            f"/server/files/{_GCODE_ROOT}/{quote(thumbnail_path, safe='/')}"
        )
        api_key = entry.data.get(CONF_API_KEY)
        headers = {"X-Api-Key": api_key} if api_key else {}
        session = async_get_clientsession(self.hass, verify_ssl=False)
        try:
            async with (
                async_timeout.timeout(TIMEOUT),
                session.get(url, headers=headers) as response,
            ):
                if response.status != 200:
                    return web.Response(status=404)
                body = await response.read()
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exception:
            _LOGGER.debug("Could not fetch thumbnail from Moonraker: %s", exception)
            return web.Response(status=502)
        return web.Response(body=body, content_type=response.content_type)


class MoonrakerPanelActionView(_PanelViewBase):
    """Execute panel control actions against the printer."""

    url = PANEL_ACTION_URL
    name = "api:moonraker:panel_action"

    async def post(self, request):
        """Handle a JSON action payload."""
        try:
            payload: dict[str, Any] = await request.json()
        except (ValueError, TypeError):
            return self.json({"error": "invalid json"}, status_code=400)

        coordinator = self._resolve(payload.get("entry_id"))
        if coordinator is None:
            return self.json({"error": "unknown printer"}, status_code=404)

        action = str(payload.get("action") or "")
        try:
            error = await self._run_action(coordinator, action, payload)
        except Exception as exception:  # noqa: BLE001 - surfaced to the UI
            _LOGGER.debug("Moonraker panel action %s failed", action, exc_info=True)
            return self.json({"error": str(exception)}, status_code=502)
        if error:
            return self.json({"error": error}, status_code=400)
        return self.json({"ok": True})

    async def _run_action(self, coordinator, action: str, payload) -> str | None:
        """Execute a panel action; return an error string or None."""
        if action in _DIRECT_ACTIONS:
            await coordinator.async_send_data(_DIRECT_ACTIONS[action])
            return None

        if action == "jog":
            axis = str(payload.get("axis") or "").upper()
            try:
                distance = float(payload.get("dist"))
            except (TypeError, ValueError):
                return "numeric distance required"
            if axis not in ("X", "Y", "Z"):
                return "axis must be X, Y, or Z"
            feed = 600 if axis == "Z" else 6000
            await coordinator.async_send_data(
                METHODS.PRINTER_GCODE_SCRIPT,
                {"script": f"G91\nG1 {axis}{distance} F{feed}\nG90"},
            )
            return None

        if action == "preheat":
            preset = str(payload.get("preset") or "").lower()
            temps = _PREHEAT_PRESETS.get(preset)
            if temps is None:
                return "unknown preset"
            await coordinator.async_send_data(
                METHODS.PRINTER_GCODE_SCRIPT,
                {"script": f"M104 S{temps[0]}\nM140 S{temps[1]}"},
            )
            return None

        if action in ("run_gcode", "run_macro"):
            script = str(payload.get("script") or "").strip()
            if not script:
                return "script required"
            await coordinator.async_send_data(
                METHODS.PRINTER_GCODE_SCRIPT, {"script": script}
            )
            return None

        if action in ("select_file", "print_file", "enqueue_file"):
            filename = str(payload.get("filename") or "").strip() or (
                coordinator.selected_file or ""
            )
            if not filename:
                return "no file selected"
            if action == "select_file":
                coordinator.selected_file = filename
            elif action == "print_file":
                coordinator.selected_file = filename
                await coordinator.async_send_data(
                    METHODS.PRINTER_PRINT_START, {"filename": filename}
                )
            else:
                await coordinator.async_send_data(
                    METHODS.SERVER_JOB_QUEUE_POST_JOB,
                    {"filenames": [filename]},
                )
            return None

        if action == "delete_file":
            filename = str(payload.get("filename") or "").strip()
            if not filename:
                return "filename required"
            await coordinator.async_send_data(
                METHODS.SERVER_FILES_DELETE_FILE, {"path": filename}
            )
            await coordinator.async_refresh_files()
            return None

        if action == "power":
            device = str(payload.get("device") or "").strip()
            power_action = str(payload.get("power_action") or "").lower()
            if not device or power_action not in ("on", "off"):
                return "device and power_action=on|off required"
            await coordinator.async_send_data(
                METHODS.MACHINE_DEVICE_POWER_POST_DEVICE,
                {"device": device, "action": power_action},
            )
            return None

        if action == "refresh_files":
            await coordinator.async_refresh_files()
            return None

        if action in _GCODE_ACTIONS:
            template = _GCODE_ACTIONS[action]
            if "{}" in template:
                try:
                    value = float(payload.get("value"))
                except (TypeError, ValueError):
                    return "numeric value required"
                script = template.format(value)
            else:
                script = template
            await coordinator.async_send_data(
                METHODS.PRINTER_GCODE_SCRIPT, {"script": script}
            )
            return None

        return "unknown action"


def register_panel_views(hass: HomeAssistant) -> None:
    """Register all panel HTTP views."""
    hass.http.register_view(MoonrakerPanelView(hass))
    hass.http.register_view(MoonrakerPanelDataView(hass))
    hass.http.register_view(MoonrakerPanelThumbnailView(hass))
    hass.http.register_view(MoonrakerPanelActionView(hass))
