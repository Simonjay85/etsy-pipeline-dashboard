#!/usr/bin/env python3
"""Focused non-live tests for public Etsy listing image fallback."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import dashboard_app


def _il_url(filename: str) -> str:
    return f"https://i.etsystatic.com/12345/r/il/gallery/{filename}"


REAL_MODERN_URL = (
    "https://i.etsystatic.com/35256901/r/il/659058/8174734356/"
    "il_1588xN.8174734356_7xwt.jpg"
)
REAL_794_URL = REAL_MODERN_URL.replace("il_1588xN", "il_794xN")
REAL_FULLXFULL_URL = (
    REAL_MODERN_URL.rsplit("/", 1)[0] + "/il_fullxfull.8174734356_7xwt.jpg"
)


def _gallery_node(index: int | str, *candidates: str) -> dict[str, object]:
    return {"index": str(index), "candidates": list(candidates)}


class _DelayedGalleryPage:
    """Return an empty gallery until the mock represents lazy-loaded DOM."""

    def __init__(self, payloads: list[object]) -> None:
        self._payloads = list(payloads)
        self.evaluate_calls: list[str] = []

    async def evaluate(self, script: str) -> object:
        self.evaluate_calls.append(script)
        if self._payloads:
            return self._payloads.pop(0)
        return []


class _EditorHydrationLocator:
    @property
    def first(self) -> "_EditorHydrationLocator":
        return self

    async def count(self) -> int:
        return 0

    async def is_visible(self) -> bool:
        return False

    async def click(self) -> None:
        return None


class _DelayedEditorHydrationPage:
    """Return draft media states as the editor hydrates its image DOM."""

    def __init__(
        self,
        payloads: list[dict[str, object]],
        *,
        excluded_dom_nodes: list[dict[str, object]] | None = None,
    ) -> None:
        self._payloads = list(payloads)
        self._last_payload: dict[str, object] = {"image_nodes": [], "delete_count": 0}
        self.excluded_dom_nodes = list(excluded_dom_nodes or [])
        self.evaluate_calls: list[str] = []
        self.waits: list[int] = []

    def get_by_role(self, _role: str, *, name: str, exact: bool) -> _EditorHydrationLocator:
        return _EditorHydrationLocator()

    def locator(self, _selector: str) -> _EditorHydrationLocator:
        return _EditorHydrationLocator()

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    async def evaluate(self, script: str) -> dict[str, object]:
        self.evaluate_calls.append(script)
        if self._payloads:
            payload = self._payloads.pop(0)
            self._last_payload = payload
            return payload
        return self._last_payload


class _ScopedGalleryPage:
    """Model a gallery and recommendation area separated by the DOM contract."""

    def __init__(self, gallery_nodes: list[dict[str, object]], recommendation_nodes: list[dict[str, object]]) -> None:
        self.gallery_nodes = gallery_nodes
        self.recommendation_nodes = recommendation_nodes
        self.evaluate_calls: list[str] = []

    async def evaluate(self, script: str) -> list[dict[str, object]]:
        self.evaluate_calls.append(script)
        # If the production query escapes #photos, the mock deliberately leaks
        # recommendation candidates so the test catches the scope regression.
        scoped = (
            'document.querySelector("#photos")' in script
            and 'gallery_root.querySelectorAll("img.carousel-image")' in script
        )
        return self.gallery_nodes if scoped else self.gallery_nodes + self.recommendation_nodes


class _EmptyLocator:
    @property
    def first(self) -> "_EmptyLocator":
        return self

    async def count(self) -> int:
        return 0


class _EmptyEditorPage:
    def locator(self, _selector: str) -> _EmptyLocator:
        return _EmptyLocator()


class _PublicPage:
    def __init__(self, final_url: str, status: int = 200) -> None:
        self.url = final_url
        self.status = status
        self.goto_calls: list[tuple[str, str | None, int | None]] = []
        self.close = AsyncMock()

    async def goto(
        self,
        url: str,
        *,
        wait_until: str | None = None,
        timeout: int | None = None,
    ) -> SimpleNamespace:
        self.goto_calls.append((url, wait_until, timeout))
        return SimpleNamespace(status=self.status)


class _BrowserContext:
    def __init__(self, public_page: _PublicPage) -> None:
        self.new_page = AsyncMock(return_value=public_page)
        self.request = Mock()
        self.request.get = AsyncMock()
        self.cdp_session = _CdpSession()
        self.new_cdp_session = AsyncMock(return_value=self.cdp_session)


class _CdpSession:
    def __init__(self) -> None:
        self.send_calls: list[tuple[str, dict[str, object]]] = []
        self.detached = False

    async def send(self, method: str, params: dict[str, object]) -> None:
        self.send_calls.append((method, params))

    async def detach(self) -> None:
        self.detached = True


MOCK_DIGITAL_FILE_BODY = b"mock digital file" * 16


class _DigitalFileDownload:
    suggested_filename = "guide.pdf"

    def __init__(self) -> None:
        self.body = MOCK_DIGITAL_FILE_BODY
        self.saved_paths: list[Path] = []

    async def save_as(self, path: str) -> None:
        target = Path(path)
        self.saved_paths.append(target)
        target.write_bytes(self.body)


class _RedirectedDigitalFileDownload(_DigitalFileDownload):
    """Reproduce CDP writing the real file while save_as leaves zero bytes."""

    async def save_as(self, path: str) -> None:
        target = Path(path)
        self.saved_paths.append(target)
        target.write_bytes(b"")
        (target.parent / self.suggested_filename).write_bytes(self.body)


class _DownloadInfo:
    def __init__(self, download: _DigitalFileDownload) -> None:
        async def resolve_download() -> _DigitalFileDownload:
            return download

        self.value = resolve_download()


class _DownloadContext:
    def __init__(self, download: _DigitalFileDownload) -> None:
        self.info = _DownloadInfo(download)

    async def __aenter__(self) -> _DownloadInfo:
        return self.info

    async def __aexit__(self, exc_type, exc_value, traceback) -> bool:
        return False


class _DigitalDownloadControl:
    def __init__(self, item: "_DigitalFileItem") -> None:
        self.item = item

    async def count(self) -> int:
        if "digital_file_action_download" not in self.item.download_selector:
            return 0
        return self.item.control_count

    async def click(self) -> None:
        self.item.click_count += 1


class _DigitalFileItem:
    def __init__(self, control_count: int) -> None:
        self.control_count = control_count
        self.click_count = 0
        self.download_selector = ""

    async def evaluate(self, _script: str) -> dict[str, str]:
        return {
            "name": "guide.pdf",
            "href": "",
            "size_text": f"{len(MOCK_DIGITAL_FILE_BODY)} B",
        }

    def locator(self, selector: str) -> _DigitalDownloadControl:
        self.download_selector = selector
        return _DigitalDownloadControl(self)


class _DigitalFileItems:
    def __init__(self, item: _DigitalFileItem) -> None:
        self.item = item

    async def count(self) -> int:
        return 1

    def nth(self, index: int) -> _DigitalFileItem:
        if index != 0:
            raise IndexError(index)
        return self.item


class _DigitalFileEditorPage:
    def __init__(self, control_count: int, download: _DigitalFileDownload | None = None) -> None:
        self.item = _DigitalFileItem(control_count)
        self.download = download or _DigitalFileDownload()

    def locator(self, selector: str):
        if selector == "#field-digitalFiles":
            return _StaticCountLocator(1)
        if "WtUploadItem" in selector or ".wt-upload__item" in selector:
            return _DigitalFileItems(self.item)
        return _StaticCountLocator(0)

    def expect_download(self, timeout: int) -> _DownloadContext:
        return _DownloadContext(self.download)


class _StaticCountLocator:
    def __init__(self, count: int) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _AssetResponse:
    def __init__(self, *, ok: bool, status: int, content_type: str, body: bytes) -> None:
        self.ok = ok
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body

    async def body(self) -> bytes:
        return self._body


class TestDashboardEtsyPublicImages(IsolatedAsyncioTestCase):
    async def _run_digital_file_sync(
        self,
        control_count: int,
        download: _DigitalFileDownload | None = None,
    ) -> tuple:
        listing_id = "4527467265"
        public_page = _PublicPage(f"https://www.etsy.com/listing/{listing_id}")
        browser_ctx = _BrowserContext(public_page)
        editor_page = _DigitalFileEditorPage(control_count, download)
        extract_editor = AsyncMock(return_value={"images": [], "image_count": 0})
        extract_public = AsyncMock(
            return_value={
                "urls": [],
                "complete": False,
                "gallery_node_count": 0,
                "gallery_index_count": 0,
                "reason": "timeout",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(dashboard_app, "_extract_editor_listing_images", new=extract_editor), \
            patch.object(dashboard_app, "_extract_public_listing_images", new=extract_public):
            product_path = Path(tmpdir) / "product-01"
            report = await dashboard_app._sync_listing_assets(
                browser_ctx,
                editor_page,
                listing_id,
                product_path,
            )
            target = product_path / "files" / "guide.pdf"
            return (
                report,
                editor_page.item,
                target.exists(),
                target.read_bytes() if target.exists() else b"",
                browser_ctx.cdp_session,
                list(editor_page.download.saved_paths),
                (product_path / ".sync_staging").exists(),
            )

    async def test_wt_upload_item_data_testid_download_button_is_clicked_and_saved(self) -> None:
        report, item, exists, body, _, _, _ = await self._run_digital_file_sync(control_count=1)

        self.assertIn("button[data-testid", item.download_selector)
        self.assertIn("digital_file_action_download", item.download_selector)
        self.assertEqual(1, item.click_count)
        self.assertTrue(exists)
        self.assertEqual(MOCK_DIGITAL_FILE_BODY, body)
        self.assertEqual(1, report["files_found"])
        self.assertEqual(1, report["files_downloaded"])
        self.assertEqual("", report["file_warning"])

    async def test_multiple_digital_file_download_controls_remain_unsafe(self) -> None:
        report, item, exists, _, _, _, _ = await self._run_digital_file_sync(control_count=2)

        self.assertIn("digital_file_action_download", item.download_selector)
        self.assertEqual(0, item.click_count)
        self.assertFalse(exists)
        self.assertEqual(1, report["files_found"])
        self.assertEqual(0, report["files_downloaded"])
        self.assertTrue(report["file_warning"])

    async def test_cdp_button_download_is_redirected_to_product_staging_then_reset(self) -> None:
        report, _, exists, body, cdp_session, saved_paths, staging_exists = (
            await self._run_digital_file_sync(control_count=1)
        )

        self.assertEqual(
            ["Page.setDownloadBehavior", "Page.setDownloadBehavior"],
            [method for method, _ in cdp_session.send_calls],
        )
        redirect = cdp_session.send_calls[0][1]
        reset = cdp_session.send_calls[1][1]
        self.assertEqual("allow", redirect["behavior"])
        self.assertEqual((".sync_staging", "files"), Path(redirect["downloadPath"]).parts[-2:])
        self.assertNotEqual(Path.home() / "Downloads", Path(redirect["downloadPath"]))
        self.assertEqual({"behavior": "default"}, reset)
        self.assertTrue(cdp_session.detached)
        self.assertEqual(1, len(saved_paths))
        self.assertEqual((".sync_staging", "files", "guide.pdf.part"), saved_paths[0].parts[-3:])
        self.assertFalse(staging_exists)
        self.assertTrue(exists)
        self.assertEqual(MOCK_DIGITAL_FILE_BODY, body)
        self.assertEqual(1, report["files_downloaded"])

    async def test_zero_byte_save_as_uses_unique_complete_redirected_sibling(self) -> None:
        report, _, exists, body, _, saved_paths, staging_exists = (
            await self._run_digital_file_sync(
                control_count=1,
                download=_RedirectedDigitalFileDownload(),
            )
        )

        self.assertEqual(1, len(saved_paths))
        self.assertEqual(0, saved_paths[0].stat().st_size if saved_paths[0].exists() else 0)
        self.assertFalse(staging_exists)
        self.assertTrue(exists)
        self.assertEqual(MOCK_DIGITAL_FILE_BODY, body)
        self.assertEqual(1, report["files_downloaded"])
        self.assertEqual("", report["file_warning"])

    async def test_cdp_download_redirect_resets_and_detaches_after_click_failure(self) -> None:
        public_page = _PublicPage("https://www.etsy.com/listing/4527467265")
        browser_ctx = _BrowserContext(public_page)

        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir) / "product-01" / ".sync_staging" / "files"
            with self.assertRaisesRegex(RuntimeError, "mock click failed"):
                async with dashboard_app._redirect_cdp_downloads_to_staging(
                    browser_ctx,
                    public_page,
                    staging,
                ):
                    raise RuntimeError("mock click failed")

        self.assertEqual(
            ["allow", "default"],
            [params["behavior"] for _, params in browser_ctx.cdp_session.send_calls],
        )
        self.assertTrue(browser_ctx.cdp_session.detached)

    async def test_editor_media_waits_for_stable_ten_image_hydration_and_excludes_site_assets(self) -> None:
        avatar_url = "https://i.etsystatic.com/35256901/avatar.jpg"
        site_asset_url = "https://www.etsy.com/images/site-logo.png"
        raw_urls = [_il_url(f"{index}/il_794xN.jpg") for index in range(10)]
        expected_urls = [_il_url(f"{index}/il_fullxfull.jpg") for index in range(10)]

        def _media_state(nodes: list[dict[str, object]], delete_count: int) -> dict[str, object]:
            return {"image_nodes": nodes, "delete_count": delete_count}

        partial_nodes = [
            {
                "index": "0",
                "candidates": [avatar_url, raw_urls[0], site_asset_url],
            }
        ]
        final_nodes = [
            {
                "index": str(index),
                "candidates": [avatar_url, raw_url, site_asset_url],
            }
            for index, raw_url in enumerate(raw_urls)
        ]
        final_state = _media_state(final_nodes, delete_count=10)
        thumbnail_node = {
            "tag": "img",
            "data-testid": "thumbnail_image",
            "src": "",
        }
        page = _DelayedEditorHydrationPage(
            [
                _media_state([], delete_count=0),
                _media_state(partial_nodes, delete_count=1),
                final_state,
                final_state,
            ],
            excluded_dom_nodes=[thumbnail_node],
        )

        payload = await dashboard_app._extract_editor_listing_images(
            page,
            timeout_ms=100,
            poll_ms=0,
        )

        self.assertEqual(4, len(page.evaluate_calls))
        self.assertTrue(payload["complete"])
        self.assertEqual(10, payload["delete_count"])
        self.assertEqual(10, payload["gallery_node_count"])
        self.assertEqual(10, payload["gallery_index_count"])
        self.assertEqual(10, payload["image_count"])
        self.assertEqual(10, len(payload["images"]))
        self.assertEqual(expected_urls, payload["images"])
        self.assertEqual([thumbnail_node], page.excluded_dom_nodes)
        self.assertNotIn(avatar_url, payload["images"])
        self.assertNotIn(site_asset_url, payload["images"])

        script = page.evaluate_calls[-1]
        self.assertIn('document.querySelector(\'#media\')', script)
        self.assertIn("data-testid", script)
        self.assertRegex(script, r"testId\s*===\s*['\"]thumbnail_image['\"]")
        for attribute in (
            "currentSrc",
            "getAttribute('src')",
            "getAttribute('srcset')",
            "getAttribute('data-src')",
            "getAttribute('data-srcset')",
        ):
            self.assertIn(attribute, script)

    async def test_editor_partial_timeout_is_incomplete_and_preserves_stale_images(self) -> None:
        listing_id = "4527467265"
        valid_url = _il_url("0/il_794xN.jpg")
        partial_page = _DelayedEditorHydrationPage(
            [
                {
                    "image_nodes": [
                        {
                            "index": "0",
                            "candidates": [
                                valid_url,
                                "https://i.etsystatic.com/35256901/avatar.jpg",
                                "https://www.etsy.com/images/site-logo.png",
                            ],
                        }
                    ],
                    "delete_count": 10,
                }
            ]
        )
        partial_payload = await dashboard_app._extract_editor_listing_images(
            partial_page,
            timeout_ms=0,
            poll_ms=0,
        )

        self.assertFalse(partial_payload["complete"])
        self.assertEqual("incomplete_editor_hydration", partial_payload["reason"])
        self.assertEqual([_il_url("0/il_fullxfull.jpg")], partial_payload["images"])
        self.assertEqual(10, partial_payload["image_count"])
        self.assertEqual(1, partial_payload["gallery_node_count"])
        self.assertEqual(1, partial_payload["gallery_index_count"])
        self.assertEqual(10, partial_payload["delete_count"])

        public_page = _PublicPage(f"https://www.etsy.com/listing/{listing_id}")
        browser_ctx = _BrowserContext(public_page)
        editor_page = _EmptyEditorPage()
        extract_editor = AsyncMock(return_value=partial_payload)
        extract_public = AsyncMock(
            return_value={
                "urls": [],
                "complete": False,
                "gallery_node_count": 0,
                "gallery_index_count": 0,
                "reason": "timeout",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            product_path = Path(tmpdir) / "product-01"
            stale_path = product_path / "images" / "etsy_stale.jpg"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_bytes(b"stale-image")

            with patch.object(dashboard_app, "_extract_editor_listing_images", new=extract_editor), \
                patch.object(dashboard_app, "_extract_public_listing_images", new=extract_public):
                report = await dashboard_app._sync_listing_assets(
                    browser_ctx,
                    editor_page,
                    listing_id,
                    product_path,
                )

            self.assertTrue(report["image_warning"])
            self.assertIn("gallery", report["image_warning"].lower())
            self.assertEqual(0, report["images_downloaded"])
            self.assertFalse((product_path / "images" / "etsy_01.jpg").exists())
            self.assertTrue(stale_path.exists())
            self.assertEqual(b"stale-image", stale_path.read_bytes())

        extract_public.assert_awaited_once_with(public_page)
        browser_ctx.request.get.assert_not_awaited()
        public_page.close.assert_awaited_once()

    async def test_delayed_gallery_waits_then_normalizes_dedupes_preserves_order_and_caps(self) -> None:
        gallery_nodes = [
            _gallery_node(
                0,
                _il_url("il_794xN.jpg?width=1000"),
                _il_url("il_fullxfull.jpg#same-image"),
                "https://cdn.example.invalid/recommendation.jpg",
            ),
            _gallery_node(1, _il_url("il_674xN.png")),
            _gallery_node(2, _il_url("il_300xN.webp")),
            _gallery_node(3, _il_url("il_100xN.gif")),
        ]
        gallery_nodes.extend(
            _gallery_node(index, _il_url(f"{index}/il_794xN.jpg"))
            for index in range(4, 20)
        )

        page = _DelayedGalleryPage([[], gallery_nodes, gallery_nodes])
        payload = await dashboard_app._extract_public_listing_images(
            page,
            timeout_ms=100,
            poll_ms=1,
        )

        self.assertTrue(payload["complete"])
        self.assertEqual(20, payload["gallery_node_count"])
        self.assertEqual(20, payload["gallery_index_count"])
        result = payload["urls"]
        self.assertEqual(20, len(result))
        self.assertEqual(
            [
                _il_url("il_fullxfull.jpg"),
                _il_url("il_fullxfull.png"),
                _il_url("il_fullxfull.webp"),
                _il_url("il_fullxfull.gif"),
            ],
            result[:4],
        )
        self.assertEqual(
            [
                _il_url("il_fullxfull.jpg"),
                _il_url("il_fullxfull.png"),
                _il_url("il_fullxfull.webp"),
                _il_url("il_fullxfull.gif"),
            ]
            + [_il_url(f"{size}/il_fullxfull.jpg") for size in range(4, 20)],
            result,
        )
        self.assertNotIn("https://cdn.example.invalid/recommendation.jpg", result)

        script = page.evaluate_calls[-1]
        self.assertIn('document.querySelector("#photos")', script)
        self.assertIn('gallery_root.querySelectorAll("img.carousel-image")', script)
        for attribute in ("currentSrc", 'getAttribute("src")', 'getAttribute("data-src")',
                          'getAttribute("srcset")', 'getAttribute("data-srcset")'):
            self.assertIn(attribute, script)

    async def test_recommendation_images_are_excluded_by_photos_carousel_scope(self) -> None:
        gallery_url = _il_url("il_794xN.jpg")
        recommendation_url = _il_url("il_640xN.jpg")
        page = _ScopedGalleryPage(
            [_gallery_node(0, gallery_url)],
            [_gallery_node("recommendation", recommendation_url)],
        )

        payload = await dashboard_app._extract_public_listing_images(
            page,
            timeout_ms=100,
            poll_ms=0,
        )

        self.assertTrue(payload["complete"])
        self.assertEqual([_il_url("il_fullxfull.jpg")], payload["urls"])
        self.assertNotIn(recommendation_url, payload["urls"])
        script = page.evaluate_calls[0]
        self.assertIn('document.querySelector("#photos")', script)
        self.assertIn('gallery_root.querySelectorAll("img.carousel-image")', script)
        self.assertNotIn('document.querySelectorAll("img.carousel-image")', script)

    async def test_http_200_empty_gallery_sets_clear_public_warning(self) -> None:
        listing_id = "4527467265"
        public_page = _PublicPage(f"https://www.etsy.com/listing/{listing_id}")
        browser_ctx = _BrowserContext(public_page)
        editor_page = _EmptyEditorPage()
        extract_editor = AsyncMock(return_value={"images": [], "image_count": 0})
        extract_public = AsyncMock(
            return_value={
                "urls": [],
                "complete": False,
                "gallery_node_count": 0,
                "gallery_index_count": 0,
                "reason": "timeout",
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(dashboard_app, "_extract_editor_listing_images", new=extract_editor), \
            patch.object(dashboard_app, "_extract_public_listing_images", new=extract_public):
            report = await dashboard_app._sync_listing_assets(
                browser_ctx,
                editor_page,
                listing_id,
                Path(tmpdir) / "product-01",
            )

        self.assertEqual(200, report["public_image_status"])
        self.assertEqual("public", report["images_source"])
        self.assertEqual(0, report["images_found"])
        self.assertIn("gallery", report["image_warning"].lower())
        self.assertIn("#photos", report["image_warning"])
        self.assertIn(listing_id, report["image_warning"])
        extract_public.assert_awaited_once_with(public_page)
        public_page.close.assert_awaited_once()
        browser_ctx.request.get.assert_not_awaited()

    async def test_exact_public_final_url_prefix_collision_fails_closed_before_image_fallback(self) -> None:
        listing_id = "4527467265"
        public_page = _PublicPage("https://www.etsy.com/listing/45274672650")
        browser_ctx = _BrowserContext(public_page)
        editor_page = _EmptyEditorPage()
        extract_editor = AsyncMock(return_value={"images": [], "image_count": 0})
        extract_public = AsyncMock(
            return_value={
                "urls": [_il_url("il_794xN.jpg")],
                "complete": True,
                "gallery_node_count": 1,
                "gallery_index_count": 1,
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir, \
            patch.object(dashboard_app, "_extract_editor_listing_images", new=extract_editor), \
            patch.object(dashboard_app, "_extract_public_listing_images", new=extract_public):
            report = await dashboard_app._sync_listing_assets(
                browser_ctx,
                editor_page,
                listing_id,
                Path(tmpdir) / "product-01",
            )

        self.assertEqual(200, report["public_image_status"])
        self.assertEqual("editor", report["images_source"])
        self.assertIn("listing", report["image_warning"].lower())
        self.assertIn("không cho phép fallback", report["image_warning"].lower())
        extract_public.assert_not_awaited()
        browser_ctx.request.get.assert_not_awaited()
        public_page.close.assert_awaited_once()

    async def test_real_modern_etsy_url_normalizes_and_794_variant_dedupes(self) -> None:
        self.assertEqual(REAL_FULLXFULL_URL, dashboard_app._to_il_fullxfull_url(REAL_MODERN_URL))
        self.assertEqual(REAL_FULLXFULL_URL, dashboard_app._to_il_fullxfull_url(REAL_794_URL))
        self.assertIsNotNone(dashboard_app._to_il_fullxfull_url(REAL_MODERN_URL))

        page = _DelayedGalleryPage(
            [
                [_gallery_node(0, REAL_MODERN_URL, REAL_794_URL)],
                [_gallery_node(0, REAL_MODERN_URL, REAL_794_URL)],
            ]
        )
        payload = await dashboard_app._extract_public_listing_images(
            page,
            timeout_ms=100,
            poll_ms=0,
        )

        self.assertTrue(payload["complete"])
        self.assertEqual([REAL_FULLXFULL_URL], payload["urls"])
        self.assertEqual(1, payload["gallery_node_count"])
        self.assertEqual(1, payload["gallery_index_count"])
        self.assertIsNone(
            dashboard_app._to_il_fullxfull_url(
                REAL_MODERN_URL.replace("https://i.etsystatic.com", "https://cdn.example.invalid")
            )
        )
        self.assertIsNone(
            dashboard_app._to_il_fullxfull_url(REAL_MODERN_URL.replace("/r/il/", "/images/"))
        )

    def test_legacy_simple_etsy_filename_normalizes_to_fullxfull(self) -> None:
        legacy_url = _il_url("il_794xN.jpg")
        self.assertEqual(_il_url("il_fullxfull.jpg"), dashboard_app._to_il_fullxfull_url(legacy_url))

    async def test_progressive_hydration_does_not_return_after_one_gallery_node(self) -> None:
        one_node = [_gallery_node(0, _il_url("0/il_794xN.jpg"))]
        full_gallery = [
            _gallery_node(index, _il_url(f"{index}/il_794xN.jpg"))
            for index in range(10)
        ]
        page = _DelayedGalleryPage([[], one_node, full_gallery, full_gallery])

        payload = await dashboard_app._extract_public_listing_images(
            page,
            timeout_ms=100,
            poll_ms=0,
        )

        self.assertEqual(4, len(page.evaluate_calls))
        self.assertTrue(payload["complete"])
        self.assertEqual(10, payload["gallery_node_count"])
        self.assertEqual(10, payload["gallery_index_count"])
        self.assertEqual(10, len(payload["urls"]))
        self.assertEqual(_il_url("9/il_fullxfull.jpg"), payload["urls"][-1])

    async def test_partial_gallery_timeout_warns_without_commit_or_stale_deletion(self) -> None:
        listing_id = "4527467265"
        partial_page = _DelayedGalleryPage(
            [
                [
                    _gallery_node(0, _il_url("0/il_794xN.jpg")),
                    _gallery_node(1, "https://cdn.example.invalid/not-an-etsy-image.jpg"),
                ]
            ]
        )
        partial_payload = await dashboard_app._extract_public_listing_images(
            partial_page,
            timeout_ms=0,
            poll_ms=0,
        )
        self.assertFalse(partial_payload["complete"])
        self.assertEqual(2, partial_payload["gallery_node_count"])
        self.assertEqual(2, partial_payload["gallery_index_count"])
        self.assertEqual(1, len(partial_payload["urls"]))
        self.assertEqual("incomplete_gallery_hydration", partial_payload["reason"])

        public_page = _PublicPage(f"https://www.etsy.com/listing/{listing_id}")
        browser_ctx = _BrowserContext(public_page)
        browser_ctx.request.get = AsyncMock(
            return_value=_AssetResponse(
                ok=True,
                status=200,
                content_type="image/jpeg",
                body=b"mock-image" * 512,
            )
        )
        editor_page = _EmptyEditorPage()
        extract_editor = AsyncMock(return_value={"images": [], "image_count": 0})
        extract_public = AsyncMock(return_value=partial_payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            product_path = Path(tmpdir) / "product-01"
            stale_path = product_path / "images" / "etsy_stale.jpg"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_bytes(b"stale-image")

            with patch.object(dashboard_app, "_extract_editor_listing_images", new=extract_editor), \
                patch.object(dashboard_app, "_extract_public_listing_images", new=extract_public), \
                patch.object(dashboard_app, "_validate_etsy_image_bytes", return_value=(True, None)):
                report = await dashboard_app._sync_listing_assets(
                    browser_ctx,
                    editor_page,
                    listing_id,
                    product_path,
                )

            self.assertEqual(1, report["images_found"])
            self.assertEqual(0, report["images_downloaded"])
            self.assertIn("chưa hydrate đủ", report["image_warning"])
            self.assertIn("1 ảnh / 2 nút", report["image_warning"])
            self.assertFalse((product_path / "images" / "etsy_01.jpg").exists())
            self.assertTrue(stale_path.exists())
            self.assertEqual(b"stale-image", stale_path.read_bytes())

        self.assertEqual(1, browser_ctx.request.get.await_count)
        public_page.close.assert_awaited_once()


if __name__ == "__main__":
    import unittest

    unittest.main()
