"""Serve the NibePi Lovelace cards (``frontend/nibepi-cards.js``).

The directory is a static path and the cards module an extra frontend
module, registered once per Home Assistant run. The integration's version
is the module's query string, so a browser fetches the new cards after an
update (with a hard refresh where it kept the old page).
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

URL_BASE = "/nibepi_cards"
CARDS_MODULE = "nibepi-cards.js"
_REGISTERED = "%s_cards_registered" % DOMAIN


async def async_register_cards(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED) or getattr(hass, "http", None) is None:
        return
    hass.data[_REGISTERED] = True
    integration = await async_get_integration(hass, DOMAIN)
    directory = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(URL_BASE, str(directory), True)])
    add_extra_js_url(hass, "%s/%s?v=%s" % (URL_BASE, CARDS_MODULE, integration.version))
