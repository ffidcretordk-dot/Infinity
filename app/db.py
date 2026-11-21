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

    async def count(self, table: str, filters: dict[str, Any] | None = None) -> int:
        """Exact fast count using HEAD + Prefer: count=exact (Works for 1k, 10k, 100k+ rows)."""
        params: dict[str, str] = {"select": "*"}
        if filters:
            for k, v in filters.items():
                params[k] = f"eq.{v}"
        headers = {**self.headers, "Prefer": "count=exact"}

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.head(f"{self.base_url}/{table}", headers=headers, params=params)
            r.raise_for_status()
            cr = r.headers.get("content-range", "")
            if "/" in cr:
                total_str = cr.split("/")[-1].strip()
                if total_str.isdigit():
                    return int(total_str)
        return 0

    async def select(
        self,
        table: str,
        filters: dict[str, Any] | None = None,
        order: str | None = None,
        limit: int | None = None,
        single: bool = False,
        columns: str = "*",
    ) -> Any:
        base_params: dict[str, str] = {"select": columns}
        if filters:
            for k, v in filters.items():
                base_params[k] = f"eq.{v}"
        if order:
            base_params["order"] = order

        async with httpx.AsyncClient(timeout=20.0) as client:
            if single:
                params = {**base_params, "limit": "1"}
                r = await client.get(f"{self.base_url}/{table}", headers=self.headers, params=params)
                r.raise_for_status()
                data = r.json()
                return data[0] if data else None

            if limit is not None and limit <= 1000:
                params = {**base_params, "limit": str(limit)}
                r = await client.get(f"{self.base_url}/{table}", headers=self.headers, params=params)
                r.raise_for_status()
                return r.json()

            # Auto-paginate in chunks of 1000 so counts/lists work accurately beyond 1,000 rows
            all_rows: list[dict] = []
            offset = 0
            chunk_size = 1000
            while True:
                take = chunk_size if limit is None else min(chunk_size, limit - len(all_rows))
                if take <= 0:
                    break
                params = {**base_params, "limit": str(take), "offset": str(offset)}
                r = await client.get(f"{self.base_url}/{table}", headers=self.headers, params=params)
                r.raise_for_status()
                chunk = r.json()
                all_rows.extend(chunk)
                if len(chunk) < take:
                    break
                offset += take
                if limit is not None and len(all_rows) >= limit:
                    break
            return all_rows

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
