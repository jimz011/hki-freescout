"""DataUpdateCoordinator for FreeScout."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_AGENT_ID,
    CONF_API_KEY,
    CONF_BASE_URL,
    CONF_MAILBOX_IDS,
    CONF_SCAN_INTERVAL,
    DEFAULT_AGENT_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_NEW_CONVERSATION,
    SENSOR_MY_TICKETS,
    SENSOR_NEW,
    SENSOR_OPEN,
    SENSOR_UNASSIGNED,
)

_LOGGER = logging.getLogger(__name__)


class FreescoutCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the FreeScout API and fires HA events for new conversations."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.base_url: str = entry.data[CONF_BASE_URL].rstrip("/")
        self.api_key: str = entry.data[CONF_API_KEY]
        self.agent_id: int = entry.data.get(CONF_AGENT_ID, DEFAULT_AGENT_ID)
        self.mailbox_ids: list[int] = entry.options.get(
            CONF_MAILBOX_IDS,
            entry.data.get(CONF_MAILBOX_IDS, []),
        )

        # Track known conversation IDs to detect new arrivals
        self._known_ids: set[int] = set()
        self._first_refresh: bool = True

        scan_interval: int = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-FreeScout-API-Key": self.api_key}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch counts from FreeScout and fire events for new conversations."""
        session = async_get_clientsession(self.hass)

        try:
            open_count = await self._get_count_for_mailboxes(
                session, {"status": "active"}
            )
            unassigned_count = await self._get_count_for_mailboxes(
                session, {"status": "active", "assignedTo": ""}
            )
            new_count = await self._check_new_conversations(session)

            my_tickets_count: int | None = None
            if self.agent_id:
                my_tickets_count = await self._get_count_for_mailboxes(
                    session, {"status": "active", "assignedTo": str(self.agent_id)}
                )

        except aiohttp.ClientResponseError as err:
            raise UpdateFailed(
                f"FreeScout API error {err.status}: {err.message}"
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(
                f"Could not connect to FreeScout: {err}"
            ) from err

        return {
            SENSOR_OPEN: open_count,
            SENSOR_UNASSIGNED: unassigned_count,
            SENSOR_NEW: new_count,
            SENSOR_MY_TICKETS: my_tickets_count,
        }

    async def _get_count_for_mailboxes(
        self, session: aiohttp.ClientSession, base_params: dict[str, str]
    ) -> int:
        """Return ticket count, summed across selected mailboxes (or all if none selected)."""
        if not self.mailbox_ids:
            return await self._get_count(session, base_params)
        counts = await asyncio.gather(
            *[
                self._get_count(session, {**base_params, "mailboxId": str(mbid)})
                for mbid in self.mailbox_ids
            ]
        )
        return sum(counts)

    async def _get_count(
        self, session: aiohttp.ClientSession, params: dict[str, str]
    ) -> int:
        """Return the total element count for a conversation query."""
        query = {**params, "perPage": "1", "page": "1"}
        async with session.get(
            f"{self.base_url}/api/conversations",
            headers=self._headers,
            params=query,
        ) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()
        return data.get("page", {}).get("totalElements", 0)

    async def _check_new_conversations(
        self, session: aiohttp.ClientSession
    ) -> int:
        """
        Detect conversations that arrived since the last poll.

        Fires a freescout_new_conversation event for each new conversation
        and returns the count of newly detected conversations.
        On the very first refresh we just record existing IDs without firing.
        """
        if not self.mailbox_ids:
            conversations = await self._fetch_recent_conversations(session, {})
        else:
            per_mailbox = await asyncio.gather(
                *[
                    self._fetch_recent_conversations(
                        session, {"mailboxId": str(mbid)}
                    )
                    for mbid in self.mailbox_ids
                ]
            )
            # Flatten and deduplicate by conversation ID
            seen: set[int] = set()
            conversations = []
            for convs in per_mailbox:
                for c in convs:
                    cid = int(c["id"])
                    if cid not in seen:
                        seen.add(cid)
                        conversations.append(c)

        current_ids = {int(c["id"]) for c in conversations}

        if self._first_refresh:
            self._known_ids = current_ids
            self._first_refresh = False
            return 0

        new_ids = current_ids - self._known_ids
        new_count = len(new_ids)

        for conv in conversations:
            if int(conv["id"]) in new_ids:
                self.hass.bus.async_fire(
                    EVENT_NEW_CONVERSATION,
                    {
                        "conversation_id": conv["id"],
                        "subject": conv.get("subject", ""),
                        "status": conv.get("status", ""),
                        "mailbox_id": conv.get("mailboxId"),
                        "assignee_id": conv.get("assignee", {}).get("id")
                        if conv.get("assignee")
                        else None,
                        "created_at": conv.get("createdAt", ""),
                        "preview": conv.get("preview", ""),
                    },
                )
                _LOGGER.debug("New FreeScout conversation detected: %s", conv["id"])

        self._known_ids = current_ids
        return new_count

    async def _fetch_recent_conversations(
        self, session: aiohttp.ClientSession, extra_params: dict[str, str]
    ) -> list[dict]:
        """Fetch the most recent active conversations (up to 50)."""
        params = {
            "status": "active",
            "perPage": "50",
            "page": "1",
            **extra_params,
        }
        async with session.get(
            f"{self.base_url}/api/conversations",
            headers=self._headers,
            params=params,
        ) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()
        return data.get("_embedded", {}).get("conversations", [])
