from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import etsy_push_update


class _BodyLocator:
    async def inner_text(self, timeout=None):
        return "Etsy listing editor"


class _PushAllPage:
    def __init__(self) -> None:
        self.url = "https://www.etsy.com/your/shops/me/listing-editor/edit/123456"
        self.goto = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.screenshot = AsyncMock()

    def locator(self, selector: str):
        if selector == "body":
            return _BodyLocator()
        raise AssertionError(f"Unexpected page locator: {selector}")


class _Locator:
    def __init__(self, *, count=0, visible=False, disabled=False) -> None:
        self._count = count
        self._visible = visible
        self._disabled = disabled
        self.first = self
        self.set_input_files = AsyncMock()

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def is_disabled(self):
        return self._disabled

    def locator(self, selector: str):
        raise AssertionError(f"Unexpected nested locator: {selector}")


class _DigitalContainer(_Locator):
    def __init__(self, add_button: _Locator, file_input: _Locator) -> None:
        super().__init__(count=1, visible=True)
        self.add_button = add_button
        self.file_input = file_input
        self.nested_selectors = []

    def locator(self, selector: str):
        self.nested_selectors.append(selector)
        if selector == etsy_push_update.DIGITAL_FILE_ADD_BUTTON_SELECTOR:
            return self.add_button
        if selector == etsy_push_update.DIGITAL_FILE_INPUT_SELECTOR:
            return self.file_input
        raise AssertionError(f"Unexpected nested locator: {selector}")


class _AbsentContainer(_Locator):
    def __init__(self) -> None:
        super().__init__(count=0, visible=False)

    def locator(self, _selector: str):
        return _Locator(count=0, visible=False)


class _DigitalFilesPage:
    def __init__(self, container: _DigitalContainer, uploaded_name: str) -> None:
        self.container = container
        self.uploaded_name = uploaded_name
        self.page_selectors = []
        self.wait_for_timeout = AsyncMock()
        self._file_reads = 0

    def locator(self, selector: str):
        self.page_selectors.append(selector)
        if selector == etsy_push_update.DIGITAL_FILES_CONTAINER_SELECTOR:
            return self.container
        raise AssertionError(f"Global/non-digital selector used: {selector}")

    async def evaluate(self, script: str, *args):
        if "getElementById('field-digitalFiles')" not in script:
            raise AssertionError("Digital-file inspection escaped #field-digitalFiles")
        self._file_reads += 1
        if self._file_reads <= 2:
            return []
        return [{"index": 0, "filename": self.uploaded_name}]


class EtsyPushUpdateFieldsPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_files_preflight_runs_before_any_image_mutation(self) -> None:
        page = _PushAllPage()
        order = []

        def record(name, result=None):
            order.append(name)
            return result

        product = {
            "shop_id": "daisyflowdigital",
            "_cloud_asset_resolution": {"source": "local"},
        }
        with patch.object(
            etsy_push_update,
            "dismiss_alerts",
            AsyncMock(),
        ), patch.object(
            etsy_push_update,
            "preflight_digital_files",
            AsyncMock(side_effect=lambda _page: record("preflight")),
        ), patch.object(
            etsy_push_update,
            "push_images",
            AsyncMock(side_effect=lambda _page, _product: record("images")),
        ), patch.object(
            etsy_push_update,
            "push_files",
            AsyncMock(side_effect=lambda _page, _product: record("files")),
        ), patch.object(
            etsy_push_update,
            "save_listing",
            AsyncMock(side_effect=lambda _page: record("save", True)),
        ):
            result = await etsy_push_update.push_all(
                page,
                "123456",
                product,
                {"images", "files"},
            )

        self.assertTrue(result)
        self.assertEqual(["preflight", "images", "files", "save"], order)

    async def test_failed_files_preflight_prevents_image_mutation(self) -> None:
        page = _PushAllPage()
        image_push = AsyncMock()
        with patch.object(
            etsy_push_update,
            "dismiss_alerts",
            AsyncMock(),
        ), patch.object(
            etsy_push_update,
            "preflight_digital_files",
            AsyncMock(side_effect=etsy_push_update.DigitalFilesPreflightError(
                "Listing đang ở loại Physical"
            )),
        ), patch.object(
            etsy_push_update,
            "push_images",
            image_push,
        ):
            with self.assertRaisesRegex(
                etsy_push_update.DigitalFilesPreflightError,
                "Physical",
            ):
                await etsy_push_update.push_all(
                    page,
                    "123456",
                    {"shop_id": "daisyflowdigital", "_cloud_asset_resolution": {"source": "local"}},
                    {"images", "files"},
                )

        image_push.assert_not_awaited()

    async def test_missing_container_reports_physical_listing_clearly(self) -> None:
        absent = _AbsentContainer()
        page = SimpleNamespace(
            locator=lambda _selector: absent,
            wait_for_timeout=AsyncMock(),
        )
        with patch.object(
            etsy_push_update,
            "detect_form_type",
            AsyncMock(return_value="single"),
        ), patch.object(
            etsy_push_update,
            "_current_listing_type",
            AsyncMock(return_value="physical"),
        ):
            with self.assertRaisesRegex(
                etsy_push_update.DigitalFilesPreflightError,
                "Listing đang ở loại Physical",
            ):
                await etsy_push_update.preflight_digital_files(page)

    async def test_file_upload_uses_only_exact_digital_container_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            digital_file = Path(temp_dir) / "download.pdf"
            digital_file.write_bytes(b"pdf")
            add_button = _Locator(count=0, visible=False)
            file_input = _Locator(count=1, visible=False)
            container = _DigitalContainer(add_button, file_input)
            page = _DigitalFilesPage(container, digital_file.name)

            with patch.object(
                etsy_push_update,
                "detect_form_type",
                AsyncMock(return_value="single"),
            ):
                await etsy_push_update.push_files(
                    page,
                    {"file_paths": [str(digital_file)]},
                )

        self.assertEqual(
            [etsy_push_update.DIGITAL_FILES_CONTAINER_SELECTOR],
            page.page_selectors,
        )
        self.assertEqual(
            [
                etsy_push_update.DIGITAL_FILE_ADD_BUTTON_SELECTOR,
                etsy_push_update.DIGITAL_FILE_INPUT_SELECTOR,
            ],
            container.nested_selectors,
        )
        file_input.set_input_files.assert_awaited_once_with(
            [str(digital_file)],
            timeout=60000,
        )


if __name__ == "__main__":
    unittest.main()
