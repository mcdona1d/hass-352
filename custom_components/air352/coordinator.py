import asyncio
import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import Air352ApiClient, Air352AuthError, Air352ConnectionError, Air352ApiError
from .const import DOMAIN, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class Air352Coordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, api: Air352ApiClient) -> None:
        super().__init__(
            hass, _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self.devices: list[dict] = []
        self.device_infos: dict[str, dict] = {}

    async def async_setup(self) -> None:
        try:
            await self.api.authenticate()
            self.devices = await self.api.get_device_list()
            tasks = [self.api.get_device_info(d["iotId"]) for d in self.devices]
            infos = await asyncio.gather(*tasks, return_exceptions=True)
            for dev, info in zip(self.devices, infos):
                if isinstance(info, dict):
                    self.device_infos[dev["iotId"]] = info
        except Air352AuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except (Air352ConnectionError, Air352ApiError) as e:
            raise UpdateFailed(str(e)) from e

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            tasks = [self.api.get_device_properties(d["iotId"]) for d in self.devices]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except Air352AuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except (Air352ConnectionError, Air352ApiError) as e:
            raise UpdateFailed(str(e)) from e

        data = {}
        for dev, result in zip(self.devices, results):
            iot_id = dev["iotId"]
            if isinstance(result, Air352AuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, Exception):
                _LOGGER.warning("Failed to get properties for %s: %s", iot_id, result)
            else:
                data[iot_id] = result
        if self.devices and not data:
            raise UpdateFailed("Could not read properties from any 352 device")
        return data
