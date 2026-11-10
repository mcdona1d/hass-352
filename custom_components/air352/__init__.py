import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import Air352ApiClient, create_client_session
from .const import DOMAIN
from .coordinator import Air352Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.FAN, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = create_client_session()
    api = Air352ApiClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = Air352Coordinator(hass, api)
    try:
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as exc:
        await api.async_close()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if isinstance(exc, UpdateFailed):
            raise ConfigEntryNotReady(str(exc)) from exc
        raise

    async def async_close_session(_event) -> None:
        await api.async_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_close_session)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.api.async_close()
    return unload_ok
