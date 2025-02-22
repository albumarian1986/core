"""The Nina integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from async_timeout import timeout
from pynina import ApiError, Nina, Warning as NinaWarning

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    _LOGGER,
    ATTR_EXPIRES,
    ATTR_HEADLINE,
    ATTR_ID,
    ATTR_SENT,
    ATTR_START,
    CONF_FILTER_CORONA,
    CONF_REGIONS,
    DOMAIN,
    SCAN_INTERVAL,
)

PLATFORMS: list[str] = [Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up platform from a ConfigEntry."""

    regions: dict[str, str] = entry.data[CONF_REGIONS]

    coordinator = NINADataUpdateCoordinator(
        hass, regions, entry.data[CONF_FILTER_CORONA]
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


class NINADataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching NINA data API."""

    def __init__(
        self, hass: HomeAssistant, regions: dict[str, str], corona_filter: bool
    ) -> None:
        """Initialize."""
        self._regions: dict[str, str] = regions
        self._nina: Nina = Nina(async_get_clientsession(hass))
        self.warnings: dict[str, Any] = {}
        self.corona_filter: bool = corona_filter

        for region in regions.keys():
            self._nina.addRegion(region)

        update_interval: timedelta = SCAN_INTERVAL

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data."""

        try:
            async with timeout(10):
                await self._nina.update()
                return self._parse_data()
        except ApiError as err:
            raise UpdateFailed(err) from err

    def _parse_data(self) -> dict[str, Any]:
        """Parse warning data."""

        return_data: dict[str, Any] = {}

        for (
            region_id
        ) in self._nina.warnings:  # pylint: disable=consider-using-dict-items
            raw_warnings: list[NinaWarning] = self._nina.warnings[region_id]

            warnings_for_regions: list[Any] = []

            for raw_warn in raw_warnings:
                if "corona" in raw_warn.headline.lower() and self.corona_filter:
                    continue

                warn_obj: dict[str, Any] = {
                    ATTR_ID: raw_warn.id,
                    ATTR_HEADLINE: raw_warn.headline,
                    ATTR_SENT: raw_warn.sent or "",
                    ATTR_START: raw_warn.start or "",
                    ATTR_EXPIRES: raw_warn.expires or "",
                }
                warnings_for_regions.append(warn_obj)

            return_data[region_id] = warnings_for_regions

        return return_data
