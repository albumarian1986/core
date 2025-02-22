"""Support for AVM FRITZ!Box classes."""
from __future__ import annotations

from collections.abc import Callable, ValuesView
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from types import MappingProxyType
from typing import Any, TypedDict, cast

from fritzconnection import FritzConnection
from fritzconnection.core.exceptions import (
    FritzActionError,
    FritzConnectionException,
    FritzSecurityError,
    FritzServiceError,
)
from fritzconnection.lib.fritzhosts import FritzHosts
from fritzconnection.lib.fritzstatus import FritzStatus

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.device_tracker.const import (
    CONF_CONSIDER_HOME,
    DEFAULT_CONSIDER_HOME,
)
from homeassistant.components.switch import DOMAIN as DEVICE_SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import update_coordinator
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    async_entries_for_config_entry,
    async_get,
)
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    RegistryEntry,
    async_entries_for_device,
)
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_USERNAME,
    DOMAIN,
    SERVICE_CLEANUP,
    SERVICE_REBOOT,
    SERVICE_RECONNECT,
)

_LOGGER = logging.getLogger(__name__)


def _is_tracked(mac: str, current_devices: ValuesView) -> bool:
    """Check if device is already tracked."""
    for tracked in current_devices:
        if mac in tracked:
            return True
    return False


def device_filter_out_from_trackers(
    mac: str,
    device: FritzDevice,
    current_devices: ValuesView,
) -> bool:
    """Check if device should be filtered out from trackers."""
    reason: str | None = None
    if device.ip_address == "":
        reason = "Missing IP"
    elif _is_tracked(mac, current_devices):
        reason = "Already tracked"

    if reason:
        _LOGGER.debug(
            "Skip adding device %s [%s], reason: %s", device.hostname, mac, reason
        )
    return bool(reason)


def _cleanup_entity_filter(device: RegistryEntry) -> bool:
    """Filter only relevant entities."""
    return device.domain == DEVICE_TRACKER_DOMAIN or (
        device.domain == DEVICE_SWITCH_DOMAIN and "_internet_access" in device.entity_id
    )


class ClassSetupMissing(Exception):
    """Raised when a Class func is called before setup."""

    def __init__(self) -> None:
        """Init custom exception."""
        super().__init__("Function called before Class setup")


@dataclass
class Device:
    """FRITZ!Box device class."""

    mac: str
    ip_address: str
    name: str
    wan_access: bool


class HostInfo(TypedDict):
    """FRITZ!Box host info class."""

    mac: str
    name: str
    ip: str
    status: bool


class FritzBoxTools(update_coordinator.DataUpdateCoordinator):
    """FrtizBoxTools class."""

    def __init__(
        self,
        hass: HomeAssistant,
        password: str,
        username: str = DEFAULT_USERNAME,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize FritzboxTools class."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=f"{DOMAIN}-{host}-coordinator",
            update_interval=timedelta(seconds=30),
        )

        self._devices: dict[str, FritzDevice] = {}
        self._options: MappingProxyType[str, Any] | None = None
        self._unique_id: str | None = None
        self.connection: FritzConnection = None
        self.fritz_hosts: FritzHosts = None
        self.fritz_status: FritzStatus = None
        self.hass = hass
        self.host = host
        self.password = password
        self.port = port
        self.username = username
        self._mac: str | None = None
        self._model: str | None = None
        self._current_firmware: str | None = None
        self._latest_firmware: str | None = None
        self._update_available: bool = False

    async def async_setup(
        self, options: MappingProxyType[str, Any] | None = None
    ) -> None:
        """Wrap up FritzboxTools class setup."""
        self._options = options
        await self.hass.async_add_executor_job(self.setup)

    def setup(self) -> None:
        """Set up FritzboxTools class."""
        self.connection = FritzConnection(
            address=self.host,
            port=self.port,
            user=self.username,
            password=self.password,
            timeout=60.0,
            pool_maxsize=30,
        )

        if not self.connection:
            _LOGGER.error("Unable to establish a connection with %s", self.host)
            return

        self.fritz_status = FritzStatus(fc=self.connection)
        info = self.connection.call_action("DeviceInfo:1", "GetInfo")
        if not self._unique_id:
            self._unique_id = info["NewSerialNumber"]

        self._model = info.get("NewModelName")
        self._current_firmware = info.get("NewSoftwareVersion")

        self._update_available, self._latest_firmware = self._update_device_info()

    @callback
    async def _async_update_data(self) -> None:
        """Update FritzboxTools data."""
        try:
            self.fritz_hosts = FritzHosts(fc=self.connection)
            await self.async_scan_devices()
        except (FritzSecurityError, FritzConnectionException) as ex:
            raise update_coordinator.UpdateFailed from ex

    @property
    def unique_id(self) -> str:
        """Return unique id."""
        if not self._unique_id:
            raise ClassSetupMissing()
        return self._unique_id

    @property
    def model(self) -> str:
        """Return device model."""
        if not self._model:
            raise ClassSetupMissing()
        return self._model

    @property
    def current_firmware(self) -> str:
        """Return current SW version."""
        if not self._current_firmware:
            raise ClassSetupMissing()
        return self._current_firmware

    @property
    def latest_firmware(self) -> str | None:
        """Return latest SW version."""
        return self._latest_firmware

    @property
    def update_available(self) -> bool:
        """Return if new SW version is available."""
        return self._update_available

    @property
    def mac(self) -> str:
        """Return device Mac address."""
        if not self._unique_id:
            raise ClassSetupMissing()
        return self._unique_id

    @property
    def devices(self) -> dict[str, FritzDevice]:
        """Return devices."""
        return self._devices

    @property
    def signal_device_new(self) -> str:
        """Event specific per FRITZ!Box entry to signal new device."""
        return f"{DOMAIN}-device-new-{self._unique_id}"

    @property
    def signal_device_update(self) -> str:
        """Event specific per FRITZ!Box entry to signal updates in devices."""
        return f"{DOMAIN}-device-update-{self._unique_id}"

    def _update_hosts_info(self) -> list[HostInfo]:
        """Retrieve latest hosts information from the FRITZ!Box."""
        try:
            return self.fritz_hosts.get_hosts_info()  # type: ignore [no-any-return]
        except Exception as ex:  # pylint: disable=[broad-except]
            if not self.hass.is_stopping:
                raise HomeAssistantError("Error refreshing hosts info") from ex
        return []

    def _update_device_info(self) -> tuple[bool, str | None]:
        """Retrieve latest device information from the FRITZ!Box."""
        version = self.connection.call_action("UserInterface1", "GetInfo").get(
            "NewX_AVM-DE_Version"
        )
        return bool(version), version

    async def async_scan_devices(self, now: datetime | None = None) -> None:
        """Wrap up FritzboxTools class scan."""
        await self.hass.async_add_executor_job(self.scan_devices, now)

    def scan_devices(self, now: datetime | None = None) -> None:
        """Scan for new devices and return a list of found device ids."""
        _LOGGER.debug("Checking devices for FRITZ!Box router %s", self.host)

        _default_consider_home = DEFAULT_CONSIDER_HOME.total_seconds()
        if self._options:
            consider_home = self._options.get(
                CONF_CONSIDER_HOME, _default_consider_home
            )
        else:
            consider_home = _default_consider_home

        new_device = False
        for known_host in self._update_hosts_info():
            if not known_host.get("mac"):
                continue

            dev_mac = known_host["mac"]
            dev_name = known_host["name"]
            dev_ip = known_host["ip"]
            dev_home = known_host["status"]
            dev_wan_access = True
            if dev_ip:
                wan_access = self.connection.call_action(
                    "X_AVM-DE_HostFilter:1",
                    "GetWANAccessByIP",
                    NewIPv4Address=dev_ip,
                )
                if wan_access:
                    dev_wan_access = not wan_access.get("NewDisallow")

            dev_info = Device(dev_mac, dev_ip, dev_name, dev_wan_access)

            if dev_mac in self._devices:
                self._devices[dev_mac].update(dev_info, dev_home, consider_home)
            else:
                device = FritzDevice(dev_mac, dev_name)
                device.update(dev_info, dev_home, consider_home)
                self._devices[dev_mac] = device
                new_device = True

        dispatcher_send(self.hass, self.signal_device_update)
        if new_device:
            dispatcher_send(self.hass, self.signal_device_new)

        _LOGGER.debug("Checking host info for FRITZ!Box router %s", self.host)
        self._update_available, self._latest_firmware = self._update_device_info()

    async def async_trigger_firmware_update(self) -> bool:
        """Trigger firmware update."""
        results = await self.hass.async_add_executor_job(
            self.connection.call_action, "UserInterface:1", "X_AVM-DE_DoUpdate"
        )
        return cast(bool, results["NewX_AVM-DE_UpdateState"])

    async def async_trigger_reboot(self) -> None:
        """Trigger device reboot."""
        await self.hass.async_add_executor_job(
            self.connection.call_action, "DeviceConfig1", "Reboot"
        )

    async def async_trigger_reconnect(self) -> None:
        """Trigger device reconnect."""
        await self.hass.async_add_executor_job(
            self.connection.call_action, "WANIPConn1", "ForceTermination"
        )

    async def service_fritzbox(
        self, service_call: ServiceCall, config_entry: ConfigEntry
    ) -> None:
        """Define FRITZ!Box services."""
        _LOGGER.debug("FRITZ!Box router: %s", service_call.service)

        if not self.connection:
            raise HomeAssistantError("Unable to establish a connection")

        try:
            if service_call.service == SERVICE_REBOOT:
                _LOGGER.warning(
                    'Service "fritz.reboot" is deprecated, please use the corresponding button entity instead'
                )
                await self.hass.async_add_executor_job(
                    self.connection.call_action, "DeviceConfig1", "Reboot"
                )
                return

            if service_call.service == SERVICE_RECONNECT:
                _LOGGER.warning(
                    'Service "fritz.reconnect" is deprecated, please use the corresponding button entity instead'
                )
                await self.hass.async_add_executor_job(
                    self.connection.call_action,
                    "WANIPConn1",
                    "ForceTermination",
                )
                return

            if service_call.service == SERVICE_CLEANUP:
                device_hosts_list: list = await self.hass.async_add_executor_job(
                    self.fritz_hosts.get_hosts_info
                )

        except (FritzServiceError, FritzActionError) as ex:
            raise HomeAssistantError("Service or parameter unknown") from ex
        except FritzConnectionException as ex:
            raise HomeAssistantError("Service not supported") from ex

        entity_reg: EntityRegistry = (
            await self.hass.helpers.entity_registry.async_get_registry()
        )

        ha_entity_reg_list: list[
            RegistryEntry
        ] = self.hass.helpers.entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        entities_removed: bool = False

        device_hosts_macs = {device["mac"] for device in device_hosts_list}

        for entry in ha_entity_reg_list:
            if (
                not _cleanup_entity_filter(entry)
                or entry.unique_id.split("_")[0] in device_hosts_macs
            ):
                continue
            _LOGGER.info("Removing entity: %s", entry.name or entry.original_name)
            entity_reg.async_remove(entry.entity_id)
            entities_removed = True

        if entities_removed:
            self._async_remove_empty_devices(entity_reg, config_entry)

    @callback
    def _async_remove_empty_devices(
        self, entity_reg: EntityRegistry, config_entry: ConfigEntry
    ) -> None:
        """Remove devices with no entities."""

        device_reg = async_get(self.hass)
        device_list = async_entries_for_config_entry(device_reg, config_entry.entry_id)
        for device_entry in device_list:
            if not async_entries_for_device(
                entity_reg,
                device_entry.id,
                include_disabled_entities=True,
            ):
                _LOGGER.info("Removing device: %s", device_entry.name)
                device_reg.async_remove_device(device_entry.id)


@dataclass
class FritzData:
    """Storage class for platform global data."""

    tracked: dict = field(default_factory=dict)
    profile_switches: dict = field(default_factory=dict)


class FritzDeviceBase(update_coordinator.CoordinatorEntity):
    """Entity base class for a device connected to a FRITZ!Box router."""

    def __init__(self, router: FritzBoxTools, device: FritzDevice) -> None:
        """Initialize a FRITZ!Box device."""
        super().__init__(router)
        self._router = router
        self._mac: str = device.mac_address
        self._name: str = device.hostname or DEFAULT_DEVICE_NAME

    @property
    def name(self) -> str:
        """Return device name."""
        return self._name

    @property
    def ip_address(self) -> str | None:
        """Return the primary ip address of the device."""
        if self._mac:
            return self._router.devices[self._mac].ip_address
        return None

    @property
    def mac_address(self) -> str:
        """Return the mac address of the device."""
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return hostname of the device."""
        if self._mac:
            return self._router.devices[self._mac].hostname
        return None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, self._mac)},
            default_manufacturer="AVM",
            default_model="FRITZ!Box Tracked device",
            default_name=self.name,
            identifiers={(DOMAIN, self._mac)},
            via_device=(
                DOMAIN,
                self._router.unique_id,
            ),
        )

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    async def async_process_update(self) -> None:
        """Update device."""
        raise NotImplementedError()

    async def async_on_demand_update(self) -> None:
        """Update state."""
        await self.async_process_update()
        self.async_write_ha_state()


class FritzDevice:
    """Representation of a device connected to the FRITZ!Box."""

    def __init__(self, mac: str, name: str) -> None:
        """Initialize device info."""
        self._mac = mac
        self._name = name
        self._ip_address: str | None = None
        self._last_activity: datetime | None = None
        self._connected = False
        self._wan_access = False

    def update(self, dev_info: Device, dev_home: bool, consider_home: float) -> None:
        """Update device info."""
        utc_point_in_time = dt_util.utcnow()

        if self._last_activity:
            consider_home_evaluated = (
                utc_point_in_time - self._last_activity
            ).total_seconds() < consider_home
        else:
            consider_home_evaluated = dev_home

        if not self._name:
            self._name = dev_info.name or self._mac.replace(":", "_")

        self._connected = dev_home or consider_home_evaluated

        if dev_home:
            self._last_activity = utc_point_in_time

        self._ip_address = dev_info.ip_address
        self._wan_access = dev_info.wan_access

    @property
    def is_connected(self) -> bool:
        """Return connected status."""
        return self._connected

    @property
    def mac_address(self) -> str:
        """Get MAC address."""
        return self._mac

    @property
    def hostname(self) -> str:
        """Get Name."""
        return self._name

    @property
    def ip_address(self) -> str | None:
        """Get IP address."""
        return self._ip_address

    @property
    def last_activity(self) -> datetime | None:
        """Return device last activity."""
        return self._last_activity

    @property
    def wan_access(self) -> bool:
        """Return device wan access."""
        return self._wan_access


class SwitchInfo(TypedDict):
    """FRITZ!Box switch info class."""

    description: str
    friendly_name: str
    icon: str
    type: str
    callback_update: Callable
    callback_switch: Callable


class FritzBoxBaseEntity:
    """Fritz host entity base class."""

    def __init__(self, fritzbox_tools: FritzBoxTools, device_name: str) -> None:
        """Init device info class."""
        self._fritzbox_tools = fritzbox_tools
        self._device_name = device_name

    @property
    def mac_address(self) -> str:
        """Return the mac address of the main device."""
        return self._fritzbox_tools.mac

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information."""
        return DeviceInfo(
            configuration_url=f"http://{self._fritzbox_tools.host}",
            connections={(CONNECTION_NETWORK_MAC, self.mac_address)},
            identifiers={(DOMAIN, self._fritzbox_tools.unique_id)},
            manufacturer="AVM",
            model=self._fritzbox_tools.model,
            name=self._device_name,
            sw_version=self._fritzbox_tools.current_firmware,
        )
