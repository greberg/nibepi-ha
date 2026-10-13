"""NibePi: control of a Nibe heat pump through the NibePi gateway.

The gateway (on the Pi) owns the RS485 bus and is the safety boundary; this
integration computes the indoor + price + weather curve offset and the hot
water control and, in ``active`` mode, holds the registers (47011/47010,
47041/47134/48132) through TTL leases over MQTT.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict

import voluptuous as vol
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_ENABLED,
    ATTR_PATH,
    ATTR_SETBACK,
    ATTR_UNTIL,
    AWAY_STORE_VERSION,
    DOMAIN,
    OPT_STORE_VERSION,
    PLATFORMS,
    SERVICE_IMPORT_VV_PROFILE,
    SERVICE_SET_AWAY,
    VV_STORE_VERSION,
    away_store_key,
    opt_store_key,
    vv_store_key,
)
from .frontend_cards import async_register_cards
from .logic.vvimport import ProfileImportError
from .runtime import NibePiRuntime, _local_tz

if TYPE_CHECKING:
    NibePiConfigEntry = ConfigEntry[NibePiRuntime]
else:
    NibePiConfigEntry = ConfigEntry


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_SCHEMA = vol.Schema(
    {vol.Required(ATTR_PATH): cv.string, vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
)
SET_AWAY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_UNTIL): vol.Any(None, cv.datetime),
        vol.Optional(ATTR_SETBACK): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0, max=10))),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def _loaded_runtime(hass: HomeAssistant, call: ServiceCall) -> NibePiRuntime:
    """The runtime of the entry the action names, or the only loaded one."""
    entries = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if len(entries) != 1:
        raise ServiceValidationError(
            "No loaded NibePi entry %s" % wanted
            if wanted
            else "Choose the NibePi entry (config_entry_id): %d are loaded" % len(entries)
        )
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def _import_vv_profile(call: ServiceCall) -> Dict[str, Any]:
        runtime = _loaded_runtime(hass, call)
        try:
            return await runtime.async_import_vv_profile(call.data[ATTR_PATH])
        except (ProfileImportError, OSError) as err:
            raise ServiceValidationError(str(err)) from err

    async def _set_away(call: ServiceCall) -> None:
        runtime = _loaded_runtime(hass, call)
        until = call.data.get(ATTR_UNTIL)
        if until is not None and until.tzinfo is None:
            # A time without a zone is Home Assistant's local time.
            until = until.replace(tzinfo=_local_tz())
        await runtime.async_set_away(call.data[ATTR_ENABLED], until, call.data.get(ATTR_SETBACK))

    hass.services.async_register(DOMAIN, SERVICE_SET_AWAY, _set_away, schema=SET_AWAY_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_VV_PROFILE,
        _import_vv_profile,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    # The Lovelace cards, once per run (not per entry or reload).
    await async_register_cards(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NibePiConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT integration is not available")
    runtime = NibePiRuntime(hass, entry)
    entry.runtime_data = runtime
    # The Away state first: the switch shows it from the start.
    await runtime.async_load_away()
    # Platforms first: the mode select restores its state before the first
    # tick. Until then every system is in shadow mode.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await runtime.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NibePiConfigEntry) -> bool:
    # Releases held leases: the gateway restores the register at once.
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: NibePiConfigEntry) -> None:
    # The learned hot water profile and the optimiser's history and models
    # belong to the entry.
    await Store(hass, VV_STORE_VERSION, vv_store_key(entry.entry_id)).async_remove()
    await Store(hass, OPT_STORE_VERSION, opt_store_key(entry.entry_id)).async_remove()
    await Store(hass, AWAY_STORE_VERSION, away_store_key(entry.entry_id)).async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: NibePiConfigEntry) -> None:
    # Options are read on every tick; no reload, so no lease release and
    # re-acquire (which would make the gateway restore the register).
    # A changed indoor source may need a register the gateway is not
    # polling yet, so subscriptions are refreshed.
    await entry.runtime_data.async_options_updated()
