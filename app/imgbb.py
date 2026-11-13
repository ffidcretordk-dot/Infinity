import base64
from datetime import datetime, timezone
import httpx
from app.db import db

# In-memory round-robin pointer for load balancing across serverless warm instances
_rr_index = 0


async def upload_with_load_balancer(image_bytes: bytes | None) -> tuple[str | None, bool]:
    """
    Uploads image to ImgBB using active keys in round-robin load balancing.
    - If a key fails, automatically fails over to the next active key.
    - If NO keys exist or ALL keys fail (or no image provided), returns (None, True)
      for a DUMMY submit so UX works smoothly without storing any real image URL.
    """
    global _rr_index

    if not image_bytes:
        return None, True

    try:
        keys = await db.select("imgbb_keys", filters={"is_active": "true"}, order="usage_count.asc,id.asc")
    except Exception:
        keys = []

    if not keys:
        # No ImgBB API key configured -> Dummy Submit
        return None, True

    total = len(keys)
    start = _rr_index % total
    _rr_index += 1
    ordered_keys = keys[start:] + keys[:start]

    encoded_image = base64.b64encode(image_bytes).decode("utf-8")

    async with httpx.AsyncClient(timeout=20.0) as client:
        for key_row in ordered_keys:
            api_key = key_row["api_key"]
            try:
                resp = await client.post(
                    "https://api.imgbb.com/1/upload",
                    data={"key": api_key, "image": encoded_image},
                )
                result = resp.json()
                if resp.status_code == 200 and result.get("success"):
                    img_url = result["data"]["url"]
                    await db.update(
                        "imgbb_keys",
                        {"id": key_row["id"]},
                        {
                            "usage_count": (key_row.get("usage_count") or 0) + 1,
                            "last_used_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    return img_url, False
                else:
                    await db.update(
                        "imgbb_keys",
                        {"id": key_row["id"]},
                        {"fail_count": (key_row.get("fail_count") or 0) + 1},
                    )
            except Exception:
                try:
                    await db.update(
                        "imgbb_keys",
                        {"id": key_row["id"]},
                        {"fail_count": (key_row.get("fail_count") or 0) + 1},
                    )
                except Exception:
                    pass

    # All API keys failed -> Dummy Submit (user sees success, no real image link saved)
    return None, True
