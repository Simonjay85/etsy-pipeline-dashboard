"""WordPress + WooCommerce REST API client."""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import httpx

WP_TIMEOUT = 60.0


class WPClient:
    def __init__(
        self,
        site_url: str,
        wp_username: str = "",
        wp_app_password: str = "",
        wc_consumer_key: str = "",
        wc_consumer_secret: str = "",
    ):
        self.site_url = site_url.rstrip("/")
        self.wp_username = wp_username
        self.wp_app_password = wp_app_password.replace(" ", "")
        self.wc_consumer_key = wc_consumer_key
        self.wc_consumer_secret = wc_consumer_secret

    def _wp_headers(self) -> dict:
        if not self.wp_username or not self.wp_app_password:
            return {}
        token = base64.b64encode(
            f"{self.wp_username}:{self.wp_app_password}".encode()
        ).decode()
        return {"Authorization": f"Basic {token}"}

    def _wc_auth(self) -> tuple[str, str] | None:
        if self.wc_consumer_key and self.wc_consumer_secret:
            return (self.wc_consumer_key, self.wc_consumer_secret)
        return None

    async def test_wp_connection(self) -> dict:
        url = f"{self.site_url}/wp-json/wp/v2/users/me"
        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            r = await client.get(url, headers=self._wp_headers())
            if r.status_code == 200:
                data = r.json()
                return {"ok": True, "name": data.get("name"), "id": data.get("id")}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    async def test_wc_connection(self) -> dict:
        auth = self._wc_auth()
        if not auth:
            return {"ok": False, "error": "Missing WooCommerce consumer key/secret"}
        url = f"{self.site_url}/wp-json/wc/v3/products?per_page=1"
        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            r = await client.get(url, auth=auth)
            if r.status_code == 200:
                return {"ok": True, "count_hint": len(r.json())}
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}

    async def upload_media(self, file_path: Path) -> dict:
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"
        url = f"{self.site_url}/wp-json/wp/v2/media"
        headers = {
            **self._wp_headers(),
            "Content-Disposition": f'attachment; filename="{file_path.name}"',
            "Content-Type": mime,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(url, headers=headers, content=file_path.read_bytes())
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Media upload failed: HTTP {r.status_code} {r.text[:300]}")
            return r.json()

    async def create_or_update_product(self, product: dict, existing_id: str | None = None) -> dict:
        auth = self._wc_auth()
        if not auth:
            raise RuntimeError("WooCommerce credentials not configured")

        images = []
        for img in product.get("image_paths", []):
            media = await self.upload_media(Path(img))
            images.append({"id": media["id"], "src": media.get("source_url", "")})

        payload: dict[str, Any] = {
            "name": product["title"],
            "type": "simple",
            "regular_price": str(product.get("price", "4.99")),
            "description": product.get("description", ""),
            "short_description": (product.get("description", "") or "")[:300],
            "sku": product.get("sku", ""),
            "virtual": True,
            "downloadable": bool(product.get("download_paths")),
            "manage_stock": False,
            "status": product.get("status", "draft"),
            "images": images,
        }

        if product.get("tags"):
            payload["tags"] = [{"name": t.strip()} for t in product["tags"].split(",") if t.strip()]

        downloads = []
        for dl_path in product.get("download_paths", []):
            p = Path(dl_path)
            if not p.exists():
                continue
            media = await self.upload_media(p)
            downloads.append({
                "name": p.name,
                "file": media.get("source_url", ""),
            })
        if downloads:
            payload["downloads"] = downloads

        async with httpx.AsyncClient(timeout=120.0) as client:
            if existing_id:
                url = f"{self.site_url}/wp-json/wc/v3/products/{existing_id}"
                r = await client.put(url, auth=auth, json=payload)
            else:
                url = f"{self.site_url}/wp-json/wc/v3/products"
                r = await client.post(url, auth=auth, json=payload)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"WooCommerce product failed: HTTP {r.status_code} {r.text[:400]}")
            return r.json()

    async def list_products(self, per_page: int = 20) -> list[dict]:
        auth = self._wc_auth()
        if not auth:
            return []
        url = f"{self.site_url}/wp-json/wc/v3/products?per_page={per_page}"
        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            r = await client.get(url, auth=auth)
            r.raise_for_status()
            return r.json()

    async def create_or_update_post(self, post: dict, existing_id: str | None = None) -> dict:
        featured_media = None
        feat_path = post.get("featured_image_path")
        if feat_path and Path(feat_path).exists():
            media = await self.upload_media(Path(feat_path))
            featured_media = media["id"]

        payload: dict[str, Any] = {
            "title": post["title"],
            "content": post.get("content", ""),
            "excerpt": post.get("excerpt", ""),
            "status": post.get("status", "draft"),
        }
        if post.get("slug"):
            payload["slug"] = post["slug"]
        if featured_media:
            payload["featured_media"] = featured_media

        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            if existing_id:
                url = f"{self.site_url}/wp-json/wp/v2/posts/{existing_id}"
                r = await client.put(url, headers=self._wp_headers(), json=payload)
            else:
                url = f"{self.site_url}/wp-json/wp/v2/posts"
                r = await client.post(url, headers=self._wp_headers(), json=payload)
            if r.status_code not in (200, 201):
                raise RuntimeError(f"WP post failed: HTTP {r.status_code} {r.text[:400]}")
            return r.json()

    async def get_post(self, post_id: str) -> dict:
        url = f"{self.site_url}/wp-json/wp/v2/posts/{post_id}"
        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            r = await client.get(url, headers=self._wp_headers())
            r.raise_for_status()
            return r.json()

    async def list_posts(self, per_page: int = 20) -> list[dict]:
        url = f"{self.site_url}/wp-json/wp/v2/posts?per_page={per_page}"
        async with httpx.AsyncClient(timeout=WP_TIMEOUT) as client:
            r = await client.get(url, headers=self._wp_headers())
            r.raise_for_status()
            return r.json()
