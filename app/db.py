import httpx
from typing import Any
from app.config import SUPABASE_URL, SUPABASE_SERVICE_KEY


class SupabaseClient:
    def __init__(self):
        self.base_url = f"{SUPABASE_URL}/rest/v1"
        self.headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def is_configured(self) -> bool:
        return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

    async def select(
        self,
        table: str,
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        limit: int | None = None,
        single: bool = False,
    ) -> Any:
        params: dict[str, str] = {"select": "*"}
        if filters:
            for k, v in filters.items():
                params[k] = f"eq.{v}"
        if order:
            params["order"] = order
        if limit:
            params["limit"] = str(limit)

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{self.base_url}/{table}", headers=self.headers, params=params)
            r.raise_for_status()
            data = r.json()
            if single:
                return data[0] if data else None
            return data

    async def insert(self, table: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{self.base_url}/{table}", headers=self.headers, json=payload)
            r.raise_for_status()
            data = r.json()
            return data[0] if isinstance(data, list) and data else data

    async def update(self, table: str, filters: dict[str, Any], payload: dict[str, Any]) -> list[dict]:
        params = {k: f"eq.{v}" for k, v in filters.items()}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.patch(f"{self.base_url}/{table}", headers=self.headers, params=params, json=payload)
            r.raise_for_status()
            return r.json()

    async def delete(self, table: str, filters: dict[str, Any]) -> None:
        params = {k: f"eq.{v}" for k, v in filters.items()}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.delete(f"{self.base_url}/{table}", headers=self.headers, params=params)
            r.raise_for_status()


db = SupabaseClient()
