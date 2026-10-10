"""NibePi: curve-offset control of a Nibe heat pump through the NibePi gateway.

The gateway (on the Pi) owns the RS485 bus and is the safety boundary; this
integration computes the indoor + price + weather curve offset and, in
``active`` mode, holds register 47011/47010 through a TTL lease over MQTT.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import PLATFORMS
from .runtime import NibePiRuntime

if TYPE_CHECKING:
    NibePiConfigEntry = ConfigEntry[NibePiRuntime]
else:
    NibePiConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: NibePiConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("MQTT integration is not available")
    runtime = NibePiRuntime(hass, entry)
    entry.runtime_data = runtime
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


async def _async_options_updated(hass: HomeAssistant, entry: NibePiConfigEntry) -> None:
    # Options are read on every tick; no reload, so no lease release and
    # re-acquire (which would make the gateway restore the register).
    # A changed indoor source may need a register the gateway is not
    # polling yet, so subscriptions are refreshed.
    await entry.runtime_data.async_options_updated()
