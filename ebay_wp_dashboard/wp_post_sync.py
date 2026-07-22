"""Sync blog posts between Excel and WordPress."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from excel_helpers import posts_from_excel, save_post_row
from wp_client import WPClient


def load_site_config(site_id: str, dash_dir: Path) -> dict:
    import json
    cfg_path = dash_dir / "ebay_wp_config.json"
    secrets_path = dash_dir / "ebay_wp_secrets.json"
    with open(cfg_path, encoding="utf-8") as f:
        sites = json.load(f)
    site = sites.get(site_id, {})
    if secrets_path.exists():
        with open(secrets_path, encoding="utf-8") as f:
            secrets = json.load(f).get(site_id, {})
            site.update(secrets)
    return site


def make_client(site: dict) -> WPClient:
    return WPClient(
        site_url=site.get("wordpress_url", ""),
        wp_username=site.get("wp_username", ""),
        wp_app_password=site.get("wp_app_password", ""),
        wc_consumer_key=site.get("wc_consumer_key", ""),
        wc_consumer_secret=site.get("wc_consumer_secret", ""),
    )


async def publish_post_row(site_id: str, row: int, dash_dir: Path | None = None):
    dash_dir = dash_dir or Path(__file__).parent
    base_dir = dash_dir.parent
    site = load_site_config(site_id, dash_dir)
    site_dir = base_dir / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    posts = posts_from_excel(excel_path)
    post = next((p for p in posts if p["row"] == row), None)
    if not post:
        raise ValueError(f"Post row {row} not found")

    client = make_client(site)
    feat_path = post.get("featured_image", "")
    if feat_path and not Path(feat_path).is_absolute():
        feat_path = str(site_dir / feat_path)

    result = await client.create_or_update_post(
        {
            "title": post["title"],
            "content": post["content"],
            "excerpt": post["excerpt"],
            "slug": post["slug"],
            "status": "publish" if post.get("wp_status") == "publish" else "draft",
            "featured_image_path": feat_path or None,
        },
        existing_id=post["wp_post_id"] or None,
    )
    save_post_row(excel_path, row, {
        "wp_post_id": str(result["id"]),
        "wp_url": result.get("link", ""),
        "wp_status": result.get("status", "draft"),
        "slug": result.get("slug", post["slug"]),
    })
    print(f"[WP] Published post row {row}: {result.get('link')}")


async def sync_from_wp(site_id: str, dash_dir: Path | None = None):
    dash_dir = dash_dir or Path(__file__).parent
    base_dir = dash_dir.parent
    site = load_site_config(site_id, dash_dir)
    site_dir = base_dir / "shops_wp" / site_id
    excel_path = site_dir / "Platform_Manager.xlsx"
    client = make_client(site)
    remote_posts = await client.list_posts(per_page=50)
    from excel_helpers import add_post_row
    existing = {p["wp_post_id"]: p for p in posts_from_excel(excel_path) if p["wp_post_id"]}
    for rp in remote_posts:
        pid = str(rp["id"])
        if pid in existing:
            save_post_row(excel_path, existing[pid]["row"], {
                "title": rp.get("title", {}).get("rendered", ""),
                "wp_url": rp.get("link", ""),
                "wp_status": rp.get("status", ""),
                "slug": rp.get("slug", ""),
            })
        else:
            add_post_row(excel_path, {
                "title": rp.get("title", {}).get("rendered", ""),
                "slug": rp.get("slug", ""),
                "content": rp.get("content", {}).get("rendered", ""),
                "excerpt": rp.get("excerpt", {}).get("rendered", ""),
                "wp_post_id": pid,
                "wp_url": rp.get("link", ""),
                "wp_status": rp.get("status", "draft"),
            })
    print(f"[WP] Synced {len(remote_posts)} posts from WordPress")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--row", type=int, help="Publish specific row")
    parser.add_argument("--sync-from-wp", action="store_true")
    args = parser.parse_args()
    dash = Path(__file__).parent
    if args.sync_from_wp:
        asyncio.run(sync_from_wp(args.site, dash))
    elif args.row:
        asyncio.run(publish_post_row(args.site, args.row, dash))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
