from typing import Any

import httpx

from app.logging_config import logger
from app.settings import settings


class HomeAssistantClient:
    def __init__(self) -> None:
        self._base_url = settings.ha_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.ha_long_lived_token}",
            "Content-Type": "application/json",
        }

    async def get_states(self, entity_ids: list[str]) -> dict[str, Any]:
        """Returns {entity_id: state_dict} for the requested entities only."""
        states: dict[str, Any] = {}
        async with httpx.AsyncClient(timeout=10) as client:
            for entity_id in entity_ids:
                resp = await client.get(
                    f"{self._base_url}/api/states/{entity_id}",
                    headers=self._headers,
                )
                if resp.status_code == 200:
                    states[entity_id] = resp.json()
                else:
                    logger.warning(
                        "Could not fetch state for %s: HTTP %s", entity_id, resp.status_code
                    )
        return states

    async def call_service(
        self, domain: str, service: str, entity_id: str, service_data: dict | None = None
    ) -> tuple[bool, str]:
        """Calls /api/services/<domain>/<service>. Returns (success, message)."""
        payload = {"entity_id": entity_id, **(service_data or {})}
        url = f"{self._base_url}/api/services/{domain}/{service}"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, headers=self._headers, json=payload)
        except httpx.HTTPError as exc:
            logger.error("HA service call failed: %s", exc)
            return False, f"Could not reach Home Assistant: {exc}"

        if resp.status_code == 200:
            return True, "ok"
        logger.error("HA service call returned HTTP %s: %s", resp.status_code, resp.text)
        return False, f"Home Assistant returned HTTP {resp.status_code}"


ha_client = HomeAssistantClient()
