"""Config flow for FreeScout integration."""
from __future__ import annotations

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_AGENT_ID,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAILBOX_IDS,
    CONF_SCAN_INTERVAL,
    DEFAULT_AGENT_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): str,
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_AGENT_ID, default=DEFAULT_AGENT_ID): vol.Coerce(int),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=10)
        ),
    }
)


class FreescoutConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for FreeScout."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY]

            error = await _test_connection(self.hass, base_url, api_key)
            if error:
                errors["base"] = error
            else:
                # Use the URL as the unique ID so the same instance can't be added twice
                await self.async_set_unique_id(base_url.lower())
                self._abort_if_unique_id_configured()

                user_input[CONF_BASE_URL] = base_url  # store normalised
                return self.async_create_entry(
                    title=_friendly_title(base_url),
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FreescoutOptionsFlow:
        return FreescoutOptionsFlow(config_entry)


class FreescoutOptionsFlow(config_entries.OptionsFlow):
    """Allow changing scan interval, agent ID, and mailbox filter after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            # SelectSelector returns strings; convert mailbox IDs back to ints
            raw_ids = user_input.get(CONF_MAILBOX_IDS, [])
            user_input[CONF_MAILBOX_IDS] = [int(mid) for mid in raw_ids]
            return self.async_create_entry(title="", data=user_input)

        current_interval = self._entry.options.get(
            CONF_SCAN_INTERVAL,
            self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
        current_agent = self._entry.options.get(
            CONF_AGENT_ID,
            self._entry.data.get(CONF_AGENT_ID, DEFAULT_AGENT_ID),
        )
        current_mailbox_ids: list[int] = self._entry.options.get(
            CONF_MAILBOX_IDS,
            self._entry.data.get(CONF_MAILBOX_IDS, []),
        )
        # SelectSelector works with strings
        current_mailbox_strs = [str(mid) for mid in current_mailbox_ids]

        mailboxes = await self._fetch_mailboxes()

        if mailboxes:
            mailbox_selector = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=str(mb["id"]), label=mb["name"])
                        for mb in mailboxes
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        else:
            # API unreachable — show an empty selector so the form still renders
            mailbox_selector = SelectSelector(
                SelectSelectorConfig(options=[], multiple=True)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=current_interval
                    ): vol.All(vol.Coerce(int), vol.Range(min=10)),
                    vol.Optional(
                        CONF_AGENT_ID, default=current_agent
                    ): vol.Coerce(int),
                    vol.Optional(
                        CONF_MAILBOX_IDS, default=current_mailbox_strs
                    ): mailbox_selector,
                }
            ),
        )

    async def _fetch_mailboxes(self) -> list[dict]:
        """Return the list of mailboxes from the FreeScout API, or [] on error."""
        base_url = self._entry.data[CONF_BASE_URL].rstrip("/")
        api_key = self._entry.data[CONF_API_KEY]
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{base_url}/api/mailboxes",
                headers={"X-FreeScout-API-Key": api_key},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if not resp.ok:
                    return []
                data: dict = await resp.json()
                return data.get("_embedded", {}).get("mailboxes", [])
        except aiohttp.ClientError:
            return []


async def _test_connection(hass, base_url: str, api_key: str) -> str | None:
    """Return an error key on failure, or None on success."""
    session = async_get_clientsession(hass)
    try:
        async with session.get(
            f"{base_url}/api/conversations",
            headers={"X-FreeScout-API-Key": api_key},
            params={"perPage": "1"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 401:
                return "invalid_auth"
            if resp.status == 404:
                return "cannot_connect"
            if not resp.ok:
                return "cannot_connect"
    except aiohttp.ClientConnectionError:
        return "cannot_connect"
    except aiohttp.ClientError:
        return "cannot_connect"
    return None


def _friendly_title(base_url: str) -> str:
    """Strip scheme to produce a readable entry title."""
    for prefix in ("https://", "http://"):
        if base_url.startswith(prefix):
            return base_url[len(prefix):]
    return base_url
