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
    FOLDER_KEY_PREFIX,
    SENSOR_MY_TICKETS,
    SENSOR_NEW,
    SENSOR_OPEN,
    SENSOR_PENDING,
    SENSOR_SNOOZED,
    SENSOR_UNASSIGNED,
)

_LOGGER = logging.getLogger(__name__)

# FreeScout PHP folder type constants
_FOLDER_TYPE_UNASSIGNED = 1
_FOLDER_TYPE_SNOOZED = 180
_FOLDER_TYPE_CUSTOM = 185


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

        # Populated on first refresh; used by sensor.py to create dynamic entities
        self.custom_folders: list[dict[str, str]] = []

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
            # Fetch all folder data and open/pending/new counts in parallel
            folders_task = asyncio.create_task(
                self._fetch_all_folders_for_mailboxes(session)
            )
            open_task = asyncio.create_task(
                self._get_count_for_mailboxes(session, {"status": "active"})
            )
            pending_task = asyncio.create_task(
                self._get_count_for_mailboxes(session, {"status": "pending"})
            )
            new_task = asyncio.create_task(self._check_new_conversations(session))

            all_folders, open_count, pending_count, new_count = await asyncio.gather(
                folders_task, open_task, pending_task, new_task
            )

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

        # Extract per-folder counts from the combined folder list
        unassigned_count = sum(
            f.get("activeCount", 0)
            for f in all_folders
            if f.get("type") == _FOLDER_TYPE_UNASSIGNED
        )
        snoozed_count = sum(
            f.get("activeCount", 0)
            for f in all_folders
            if f.get("type") == _FOLDER_TYPE_SNOOZED
        )

        # Aggregate custom folder counts by name across mailboxes
        custom_counts: dict[str, int] = {}
        for folder in all_folders:
            if folder.get("type") == _FOLDER_TYPE_CUSTOM:
                key = f"{FOLDER_KEY_PREFIX}{folder['name']}"
                custom_counts[key] = (
                    custom_counts.get(key, 0) + folder.get("activeCount", 0)
                )

        # Build the custom_folders list once (used by sensor.py for entity creation)
        if not self.custom_folders and custom_counts:
            self.custom_folders = [
                {"name": key[len(FOLDER_KEY_PREFIX):], "key": key}
                for key in custom_counts
            ]

        return {
            SENSOR_OPEN: open_count,
            SENSOR_UNASSIGNED: unassigned_count,
            SENSOR_PENDING: pending_count,
            SENSOR_SNOOZED: snoozed_count,
            SENSOR_NEW: new_count,
            SENSOR_MY_TICKETS: my_tickets_count,
            **custom_counts,
        }

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    async def _fetch_all_folders_for_mailboxes(
        self, session: aiohttp.ClientSession
    ) -> list[dict]:
        """Return all folders across all selected (or all) mailboxes."""
        if self.mailbox_ids:
            mailbox_list = self.mailbox_ids
        else:
            mailbox_list = await self._fetch_all_mailbox_ids(session)
            if not mailbox_list:
                return []

        per_mailbox = await asyncio.gather(
            *[
                self._fetch_all_folders_for_mailbox(session, mbid)
                for mbid in mailbox_list
            ]
        )
        return [folder for folders in per_mailbox for folder in folders]

    async def _fetch_all_folders_for_mailbox(
        self, session: aiohttp.ClientSession, mailbox_id: int
    ) -> list[dict]:
        """Fetch every page of folders for a single mailbox."""
        all_folders: list[dict] = []
        page = 1
        while True:
            async with session.get(
                f"{self.base_url}/api/mailboxes/{mailbox_id}/folders",
                headers=self._headers,
                params={"page": str(page)},
            ) as resp:
                if not resp.ok:
                    _LOGGER.warning(
                        "Could not fetch folders for mailbox %s (HTTP %s)",
                        mailbox_id,
                        resp.status,
                    )
                    break
                data: dict = await resp.json()

            all_folders.extend(data.get("_embedded", {}).get("folders", []))
            page_info = data.get("page", {})
            if page >= page_info.get("totalPages", 1):
                break
            page += 1

        return all_folders

    async def _fetch_all_mailbox_ids(
        self, session: aiohttp.ClientSession
    ) -> list[int]:
        """Fetch all mailbox IDs (used when no mailbox filter is set)."""
        async with session.get(
            f"{self.base_url}/api/mailboxes",
            headers=self._headers,
        ) as resp:
            if not resp.ok:
                return []
            data: dict = await resp.json()
        return [int(mb["id"]) for mb in data.get("_embedded", {}).get("mailboxes", [])]

    # ------------------------------------------------------------------
    # Conversation count helpers
    # ------------------------------------------------------------------

    async def _get_count_for_mailboxes(
        self, session: aiohttp.ClientSession, base_params: dict[str, str]
    ) -> int:
        """Return ticket count, summed across selected mailboxes (or all if none)."""
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
        """Return the totalElements count for a conversations query."""
        async with session.get(
            f"{self.base_url}/api/conversations",
            headers=self._headers,
            params={**params, "perPage": "1", "page": "1"},
        ) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()
        return data.get("page", {}).get("totalElements", 0)

    # ------------------------------------------------------------------
    # New-conversation detection
    # ------------------------------------------------------------------

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
        async with session.get(
            f"{self.base_url}/api/conversations",
            headers=self._headers,
            params={"status": "active", "perPage": "50", "page": "1", **extra_params},
        ) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()
        return data.get("_embedded", {}).get("conversations", [])
