#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path
import inspect
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import etsy_auto_post


class _FakeStatusRadio:
    def __init__(self, page: "_FakeDraftPage", index: int, value: str, checked: bool = False, disabled: bool = False):
        self._page = page
        self.index = index
        self.value = str(value or "").strip().lower()
        self.checked = bool(checked)
        self.disabled = bool(disabled)

    async def get_attribute(self, name: str):
        if name == "value":
            return self.value
        return None

    async def is_checked(self):
        return bool(self.checked)

    async def is_enabled(self):
        return not self.disabled

    async def check(self, *, force: bool = False, timeout: int | None = None):
        self._page.check_calls.append((self.index, self.value, force, timeout))
        for radio in self._page.radios:
            radio.checked = False
        self.checked = True


class _FakeLocator:
    def __init__(self, page: "_FakeDraftPage", radios: list[_FakeStatusRadio]):
        self._page = page
        self._radios = radios

    async def count(self):
        return len(self._radios)

    def nth(self, index: int):
        if 0 <= index < len(self._radios):
            return _FakeLocator(self._page, [self._radios[index]])
        return _FakeLocator(self._page, [])

    async def is_checked(self):
        return bool(self._radios[0].checked) if self._radios else False

    async def is_enabled(self):
        return not self._radios[0].disabled if self._radios else False

    async def get_attribute(self, name: str):
        return await self._radios[0].get_attribute(name) if self._radios else None

    async def check(self, *, force: bool = False, timeout: int | None = None):
        if not self._radios:
            raise RuntimeError("Locator trống")
        await self._radios[0].check(force=force, timeout=timeout)


class _FakeDraftPage:
    def __init__(
        self,
        radio_specs: list[tuple[str, bool, bool]],
        *,
        cards=None,
        grid_state: dict | None = None,
    ):
        self.url = "https://www.etsy.com/your/shops/me/tools/listings"
        self.radios = [
            _FakeStatusRadio(self, idx, value, checked=checked, disabled=disabled)
            for idx, (value, checked, disabled) in enumerate(radio_specs)
        ]
        self.cards = cards or []
        self.grid_state = grid_state or {"loading": False, "ids": [], "emptyState": True}
        self.goto_calls: list[tuple[str, str | None]] = []
        self.wait_calls: list[int] = []
        self.check_calls: list[tuple[int, str, bool, int | None]] = []

    def locator(self, selector: str):
        if selector == 'input[name="item_status"]':
            return _FakeLocator(self, self.radios)
        if selector == 'input[name="item_status"][value="draft"]':
            return _FakeLocator(self, [r for r in self.radios if r.value == "draft"])
        return _FakeLocator(self, [])

    async def goto(self, url: str, wait_until: str | None = None):
        self.url = url
        self.goto_calls.append((url, wait_until))

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    async def evaluate(self, script: str):
        if "__etsyDraftGridStateMarker" in script:
            return self.grid_state
        if 'listing-editor/edit/' in script:
            return self.cards
        return {"loading": False, "ids": [], "emptyState": False}

    def set_checked_value(self, value: str):
        target = str(value).strip().lower()
        for radio in self.radios:
            radio.checked = radio.value == target


class _FakeDigitalElement:
    def __init__(
        self,
        page: "_FakeDigitalPage",
        kind: str,
        *,
        text: str = "",
        name: str = "",
        value: str = "",
        label: str = "",
        checked: bool = False,
        enabled: bool = True,
        after_check_value: str | None = None,
        after_check_label: str | None = None,
        input_value_value: str | None = None,
        accept: str = "",
        digital_scope: bool = False,
        attrs: dict[str, str] | None = None,
    ):
        self.page = page
        self.kind = kind
        self.text = text
        self.name = name or text
        self.value = value
        self.label = label
        self.checked = checked
        self.enabled = enabled
        self.after_check_value = after_check_value
        self.after_check_label = after_check_label
        self.input_value_value = input_value_value
        self.accept = accept
        self.digital_scope = digital_scope
        self.attrs = attrs or {}
        self.parent = None
        self.children = []
        self.readback_owner = None

    async def is_visible(self):
        if self.kind in {"listbox", "option"}:
            if self.page.listbox_open and getattr(self.page, "dropdown_option_delay_polls", 0) > 0:
                return False
            return self.page.listbox_open
        if self.kind in {"region", "heading", "button"}:
            remaining = getattr(self.page, "surface_delay_polls", 0)
            if remaining > 0 and self.kind in {"region", "button"}:
                return False
        return True

    async def inner_text(self):
        return self.text

    async def input_value(self):
        if self.input_value_value is None:
            raise RuntimeError("input_value unsupported for fake element")
        return self.input_value_value

    async def click(self):
        if self.kind == "button":
            if self.text == "Add file":
                self.page.add_file_clicks = getattr(self.page, "add_file_clicks", 0) + 1
                return
            self.page.listbox_open = True
            return
        if self.kind == "tab":
            self.page.tab_clicks = getattr(self.page, "tab_clicks", 0) + 1
            return
        if self.kind == "option":
            if self.page.dropdown_sticky:
                self.page.button.text = self.text
            self.page.listbox_open = False

    async def evaluate(self, _script: str, arg=None):
        if self.kind == "file_input":
            if "root.contains" in _script:
                root = arg
                return False if root is None else self._is_descendant_of(root)
            return self.digital_scope
        if self.kind == "region" and self.readback_owner is not None:
            return await self.readback_owner.next_state()
        was_checked = self.checked and bool(self.page.radio_check_calls)
        value = self.after_check_value if was_checked and self.after_check_value is not None else self.value
        label = self.after_check_label if was_checked and self.after_check_label is not None else self.label
        return {"value": value, "ariaLabel": "", "labels": [label] if label else []}

    async def element_handle(self):
        return self

    def _is_descendant_of(self, root):
        node = self
        for _ in range(20):
            if node is root:
                return True
            node = getattr(node, "parent", None)
            if node is None:
                return False
        return False

    async def get_attribute(self, name: str):
        if name in self.attrs:
            return self.attrs.get(name)
        if name == "accept":
            return self.accept
        if name == "aria-label":
            return self.name
        return None

    async def is_checked(self):
        return bool(self.checked)

    async def is_enabled(self):
        return bool(self.enabled)

    async def check(self, *, force: bool = False, timeout: int | None = None):
        self.page.radio_check_calls.append((self.value, self.label, force, timeout))
        if not self.page.legacy_sticky:
            return
        for radio in self.page.radios:
            radio.checked = False
        self.checked = True

    async def set_input_files(self, _paths, timeout: int | None = None):
        raise RuntimeError(f"direct input failed ({timeout})")


class _FakeDigitalLocator:
    def __init__(self, page: "_FakeDigitalPage", elements=None):
        self.page = page
        self.elements = list(elements or [])

    async def count(self):
        return len(self.elements)

    @property
    def first(self):
        return self.nth(0)

    def nth(self, index: int):
        if 0 <= index < len(self.elements):
            return _FakeDigitalLocator(self.page, [self.elements[index]])
        return _FakeDigitalLocator(self.page, [])

    async def is_visible(self):
        return bool(self.elements) and await self.elements[0].is_visible()

    async def inner_text(self):
        return await self.elements[0].inner_text() if self.elements else ""

    async def input_value(self):
        if not self.elements:
            raise RuntimeError("empty fake locator")
        return await self.elements[0].input_value()

    async def click(self):
        if not self.elements:
            raise RuntimeError("empty fake locator")
        await self.elements[0].click()

    async def evaluate(self, script: str):
        if not self.elements:
            raise RuntimeError("empty fake locator")
        return await self.elements[0].evaluate(script)

    async def element_handle(self):
        return await self.elements[0].element_handle() if self.elements else None

    async def get_attribute(self, name: str):
        if not self.elements:
            return None
        return await self.elements[0].get_attribute(name)

    async def is_checked(self):
        return bool(self.elements) and await self.elements[0].is_checked()

    async def is_enabled(self):
        return bool(self.elements) and await self.elements[0].is_enabled()

    async def check(self, *, force: bool = False, timeout: int | None = None):
        if not self.elements:
            raise RuntimeError("empty fake locator")
        await self.elements[0].check(force=force, timeout=timeout)

    async def set_input_files(self, paths, timeout: int | None = None):
        if not self.elements:
            raise RuntimeError("empty fake locator")
        await self.elements[0].set_input_files(paths, timeout=timeout)

    def get_by_role(self, role: str, name=None):
        if not self.elements:
            return _FakeDigitalLocator(self.page, [])
        if role == "option" and self.elements[0].kind == "listbox":
            return _FakeDigitalLocator(
                self.page,
                [option for option in self.page.options if _fake_name_matches(name, option.name)],
            )
        children = [
            child for element in self.elements for child in getattr(element, "children", [])
            if child.kind == role and _fake_name_matches(name, child.name or child.text)
        ]
        return _FakeDigitalLocator(self.page, children)

    def locator(self, selector: str):
        children = [child for element in self.elements for child in getattr(element, "children", [])]
        if selector == 'input[type="file"]':
            return _FakeDigitalLocator(self.page, [child for child in children if child.kind == "file_input"])
        if selector in {'label, [role="button"]', 'label, [role="button"]'}:
            return _FakeDigitalLocator(self.page, [child for child in children if child.kind == "button"])
        return _FakeDigitalLocator(self.page, [])


def _fake_name_matches(pattern, value: str) -> bool:
    if pattern is None:
        return True
    if hasattr(pattern, "search"):
        return bool(pattern.search(value))
    return str(pattern) == value


class _FakeDigitalPage:
    def __init__(
        self,
        *,
        dropdown: bool = False,
        dropdown_options=None,
        dropdown_sticky: bool = True,
        legacy_radios=None,
        legacy_sticky: bool = True,
        digital_region: bool = False,
        add_file: bool = False,
        personalization_add_file: bool = False,
        file_input_accept: str | None = None,
        file_input_scope: bool = False,
        surface_delay_polls: int = 0,
        dropdown_button_label: str | None = None,
        dropdown_accessible_name: str | None = None,
        dropdown_button_attrs: dict[str, str] | None = None,
        dropdown_option_delay_polls: int = 0,
        accessible_only_listing_type: bool = False,
        dropdown_input_value: str | None = None,
    ):
        self.listbox_open = False
        self.dropdown_sticky = dropdown_sticky
        self.legacy_sticky = legacy_sticky
        self.radio_check_calls = []
        self.wait_calls = []
        self.surface_delay_polls = surface_delay_polls
        self.dropdown_option_delay_polls = dropdown_option_delay_polls
        self.accessible_only_listing_type = accessible_only_listing_type
        self.add_file_clicks = 0
        self.tab_clicks = 0
        dropdown_label = dropdown_button_label or "Physical"
        button_attrs = dropdown_button_attrs or {
            "id": "category-mixed-listing-type",
            "aria-label": "Physical or digital listing type",
            "aria-haspopup": "listbox",
            "role": "button",
        }
        self.button = _FakeDigitalElement(
            self,
            "button",
            text=dropdown_label,
            name=dropdown_accessible_name or dropdown_label,
            input_value_value=dropdown_input_value,
            attrs=button_attrs,
        )
        self.dropdown_elements = [self.button] if dropdown else []
        self.listbox = _FakeDigitalElement(
            self,
            "listbox",
            name="Physical or digital listing type",
        )
        self.options = [
            _FakeDigitalElement(self, "option", text=text)
            for text in (dropdown_options or ["Physical", "Digital"])
        ] if dropdown else []
        self.radios = []
        for spec in (legacy_radios or []):
            value, label, checked, enabled, *after_check = spec
            self.radios.append(_FakeDigitalElement(
                self,
                "radio",
                value=value,
                label=label,
                checked=checked,
                enabled=enabled,
                after_check_value=after_check[0] if after_check else None,
                after_check_label=after_check[1] if len(after_check) > 1 else None,
            ))
        self.regions = [
            _FakeDigitalElement(self, "region", name="Digital files")
        ] if digital_region else []
        self.add_file = _FakeDigitalElement(self, "button", text="Add file") if add_file else None
        self.personalization_add_file = (
            _FakeDigitalElement(self, "button", text="Add file", name="Personalization Add file")
            if personalization_add_file else None
        )
        self.file_inputs = []
        if file_input_accept is not None:
            self.file_inputs.append(_FakeDigitalElement(
                self,
                "file_input",
                name="customer-file-input",
                accept=file_input_accept,
                digital_scope=file_input_scope,
            ))
        if self.regions:
            self.regions[0].children.extend(
                [child for child in [self.add_file, *self.file_inputs] if child is not None]
            )
            for child in self.regions[0].children:
                child.parent = self.regions[0]

    def locator(self, selector: str):
        if self.accessible_only_listing_type and selector not in {
            'input[name="listing_type_options_group"]',
            'input[type="file"]',
        } and "digital-file" not in selector and "digital file" not in selector:
            return _FakeDigitalLocator(self, [])
        if "category-mixed-listing-type" in selector:
            return _FakeDigitalLocator(self, self.dropdown_elements)
        if "aria-haspopup" in selector.lower() and ("physical" in selector.lower() or "listing" in selector.lower()):
            return _FakeDigitalLocator(self, self.dropdown_elements)
        if 'role="combobox"' in selector.lower() and "physical" in selector.lower() and "digital" in selector.lower():
            return _FakeDigitalLocator(self, self.dropdown_elements)
        if "[data-testid*=\"listing-type\"" in selector:
            return _FakeDigitalLocator(self, self.dropdown_elements)
        if selector == 'input[name="listing_type_options_group"]':
            return _FakeDigitalLocator(self, self.radios)
        if selector == 'input[type="file"]':
            return _FakeDigitalLocator(self, self.file_inputs)
        if "digital-file" in selector or "digital file" in selector:
            return _FakeDigitalLocator(self, self.regions)
        if selector in {'label, [role="button"]', 'label, [role="button"]'}:
            return _FakeDigitalLocator(self, [self.add_file] if self.add_file else [])
        return _FakeDigitalLocator(self, [])

    def get_by_role(self, role: str, name=None):
        if role == "listbox":
            elements = [self.listbox] if self.options and _fake_name_matches(name, self.listbox.name) else []
            return _FakeDigitalLocator(self, elements)
        if role in {"button", "combobox"}:
            dropdown_role = self.button.attrs.get("role") or "button"
            dropdown = self.button if dropdown_role == role else None
            affordances = [element for element in (self.add_file, self.personalization_add_file)
                           if element is not None and role == "button"]
            return _FakeDigitalLocator(
                self,
                [element for element in ([dropdown] if dropdown is not None else [])
                 + affordances if _fake_name_matches(name, element.name or element.text)],
            )
        if role in {"region", "heading"}:
            return _FakeDigitalLocator(
                self,
                [region for region in self.regions if _fake_name_matches(name, region.name)],
            )
        return _FakeDigitalLocator(self, [])

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)
        if self.surface_delay_polls > 0:
            self.surface_delay_polls -= 1
        if self.dropdown_option_delay_polls > 0:
            self.dropdown_option_delay_polls -= 1


class _FakeCategoryInputLocator:
    def __init__(self, page: "_FakeCategoryPage"):
        self.page = page

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def wait_for(self, state: str | None = None, timeout: int | None = None):
        self.page.wait_calls.append(("input_wait", state, timeout))

    async def click(self):
        self.page.input_clicked = True

    async def fill(self, value: str):
        self.page.input_value_state = value

    async def input_value(self):
        return self.page.input_value_state

    def nth(self, index: int):
        if index == 0:
            return self
        return _FakeEmptyCategoryLocator(self.page)


class _FakeCategoryClearButton:
    def __init__(self, page: "_FakeCategoryPage"):
        self.page = page

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.page.clear_button_visible else 0

    async def is_visible(self):
        return bool(self.page.clear_button_visible)

    async def scroll_into_view_if_needed(self):
        self.page.clear_button_scroll_called = True

    async def click(self, *, force: bool = False):
        if not self.page.clear_button_visible:
            raise RuntimeError("clear button is not visible")
        self.page.clear_button_force_click = bool(force)
        self.page.clear_button_click_count += 1
        self.page.input_value_state = ""


class _FakeCategoryOptionLocator:
    def __init__(self, page: "_FakeCategoryPage", options: list[str]):
        self.page = page
        self.options = options

    async def count(self):
        return len(self.options)

    async def is_visible(self):
        return True

    def nth(self, index: int):
        if 0 <= index < len(self.options):
            return _FakeCategoryOption(self.page, self.options[index])
        return _FakeEmptyCategoryLocator(self.page)


class _FakeCategoryOption:
    def __init__(self, page: "_FakeCategoryPage", text: str):
        self.page = page
        self.text = text

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text

    async def click(self):
        self.page.click_count += 1
        self.page.input_value_state = self.page.readback_text


class _FakeEmptyCategoryLocator:
    def __init__(self, page: "_FakeCategoryPage"):
        self.page = page

    async def count(self):
        return 0

    async def is_visible(self):
        return False

    async def inner_text(self):
        return ""

    async def click(self):
        raise RuntimeError("empty fake locator")

    async def wait_for(self, state: str | None = None, timeout: int | None = None):
        return None

    def nth(self, index: int):
        return self

    @property
    def first(self):
        return self

    async def fill(self, value: str):
        return None

    async def input_value(self):
        return self.page.input_value_state


class _FakeCategoryPage:
    def __init__(
        self,
        option_texts: list[str],
        *,
        readback_text: str,
        clear_button_visible: bool = False,
    ):
        self.option_texts = list(option_texts)
        self.readback_text = readback_text
        self.input_value_state = ""
        self.input_clicked = False
        self.click_count = 0
        self.clear_button_visible = clear_button_visible
        self.clear_button_click_count = 0
        self.clear_button_scroll_called = False
        self.clear_button_force_click = False
        self.wait_calls = []
        self.locator_calls = []

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        if selector == '[role="option"], li[class*="option"], li[class*="result"], div[role="option"]':
            return _FakeCategoryOptionLocator(self, self.option_texts)
        if "#category-field-search" in selector:
            return _FakeCategoryInputLocator(self)
        if "listing-editor_category-search-typeahead" in selector:
            return _FakeCategoryInputLocator(self)
        if "input[placeholder*=\"Type to search\" i]" in selector:
            return _FakeCategoryInputLocator(self)
        if 'input[aria-label*="category" i]' in selector:
            return _FakeCategoryInputLocator(self)
        if selector == '#field-category button[aria-label="Clear"]':
            return _FakeCategoryClearButton(self)
        return _FakeEmptyCategoryLocator(self)

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)


class _NoSaveListingPage:
    def __init__(self):
        self.url = "https://www.etsy.com/your/shops/me/listing-editor/edit/123"
        self.locator_calls = []

    async def goto(self, url: str, wait_until: str | None = None):
        self.url = url

    async def wait_for_timeout(self, _ms: int):
        return None

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        return _FakeDigitalLocator(_FakeDigitalPage(), [])


class _NoOpDetailsPage:
    async def wait_for_timeout(self, _ms: int):
        return None

    async def evaluate(self, _script: str):
        return None


class _FakeUploadReadbackPage:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0
        self.wait_calls = []
        self.surface_page = _FakeDigitalPage(digital_region=True)
        self.surface_page.regions[0].readback_owner = self

    async def next_state(self):
        if not self.states:
            return {}
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state

    async def evaluate(self, _script: str):
        return await self.next_state()

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)

    def get_by_role(self, role: str, name=None):
        return self.surface_page.get_by_role(role, name)

    def locator(self, selector: str):
        return self.surface_page.locator(selector)


class _FailingDirectInputPage(_FakeDigitalPage):
    def __init__(self):
        super().__init__(digital_region=True)
        self.file_input = _FakeDigitalElement(self, "file_input")

    def locator(self, selector: str):
        if selector == 'input[type="file"]':
            return _FakeDigitalLocator(self, [self.file_input])
        return super().locator(selector)


class TestCategoryInference(unittest.TestCase):
    def test_workbook_category_wins_over_inference(self):
        product = {
            "category": "Paper & Party Supplies > Paper > Stationery > Design & Templates > Templates > Planner Templates",
            "title": "Wildflower SVG Bundle | Cricut Cut Files",
            "keywords": "kdp interiors, low content",
            "tags": "planner",
            "description": "Planner tracker bundle",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.resolve_listing_category(product))

    def test_product_07_like_infers_planner_templates_for_kdp_low_content_bundle(self):
        product = {
            "category": "",
            "title": "KDP Interiors + 50 Templates Bundle",
            "keywords": "low-content book interior planners journals trackers",
            "tags": "planner bundle",
            "description": "Publishing set for digital planners and journals.",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.infer_listing_category(product))

    def test_product_08_like_infers_guides_and_how_tos_for_ai_prompt_guide_resource(self):
        product = {
            "category": "",
            "title": "800 AI Commands",
            "keywords": "AI Prompt Guide",
            "tags": "Etsy Sellers",
            "description": "Digital prompts and command templates for running your Etsy store.",
        }
        self.assertEqual("Guides & How Tos", etsy_auto_post.infer_listing_category(product))

    def test_product_206_like_infers_3d_printer_files_for_verified_3d_download(self):
        product = {
            "category": "",
            "title": "Kawaii Click Keychains 3D, Cute Clicker Keychain Design, Playful Kawaii Accessory",
            "keywords": "kawaii keychain, click keychain, 3d keychain, cute clicker",
            "tags": "3d kawaii, kawaii accessory",
            "description": "A downloadable 3D keychain design for a playful kawaii accessory.",
            "pdf_paths": ["/verified/product-206/Kawaii-Click-Keychains-3D-154741369.zip"],
        }
        self.assertEqual("3D Printer Files", etsy_auto_post.resolve_listing_category(product))

    def test_product_207_like_infers_3d_printer_files_for_stl_model_bundle(self):
        product = {
            "category": "",
            "title": "Halloween Kawaii Friends STL Bundle, Cute Spooky 3D Model Collection, Kawaii Halloween Designs",
            "keywords": "halloween stl, kawaii stl, stl bundle, 3d model bundle, 3d print stl",
            "tags": "cute halloween, 3d model, 3d print",
            "description": "A digital STL bundle for a cute spooky 3D model collection.",
        }
        self.assertEqual("3D Printer Files", etsy_auto_post.resolve_listing_category(product))

    def test_workbook_category_still_wins_over_3d_inference(self):
        product = {
            "category": "Craft Supplies & Tools > Patterns & How To > Planner Templates",
            "title": "Halloween Kawaii Friends STL Bundle",
            "keywords": "3D model, 3D print, STL",
            "tags": "3d printer files",
            "description": "Digital STL model bundle.",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.resolve_listing_category(product))

    def test_unqualified_3d_mention_fails_closed_without_download_evidence(self):
        product = {
            "category": "",
            "title": "Compact 3D Printer",
            "keywords": "desktop printer, machine",
            "tags": "3d printer, hardware",
            "description": "A physical 3D printer appliance for a workshop.",
        }
        self.assertEqual("", etsy_auto_post.infer_listing_category(product))


class TestCategoryOptionMatching(unittest.TestCase):
    def test_exact_match_for_category_option(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Guides & How Tos",
            )
        )

    def test_breadcrumb_leaf_match_with_metadata_suffix(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Books > Books > Guides & How Tos Digital",
            )
        )

    def test_same_line_combined_suffix_match(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Guides & How Tos Physical or digital",
            )
        )

    def test_breadcrumb_suffix_segment_match(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Books > Books > Guides & How Tos > Digital",
            )
        )

    def test_breadcrumb_combined_suffix_match(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Books > Books > Guides & How Tos Physical or digital",
            )
        )

    def test_full_breadcrumb_leaf_with_parenthesized_metadata_matches(self):
        self.assertTrue(
            etsy_auto_post.category_option_matches(
                "3D Printer Files",
                "Craft Supplies & Tools > Patterns & How To > Craft Machine Files > 3D Printer Files (Digital)",
            )
        )

    def test_unrelated_option_is_rejected(self):
        self.assertFalse(
            etsy_auto_post.category_option_matches(
                "Guides & How Tos",
                "Books > Books > Guidebook Strategy",
            )
        )

    def test_category_digital_metadata_requires_unambiguous_terminal_marker(self):
        self.assertTrue(
            etsy_auto_post._category_option_is_digital_only_metadata(
                "+ Cutting Machine Files (Digital)"
            )
        )
        self.assertTrue(
            etsy_auto_post._category_option_is_digital_only_metadata(
                "Guides & How Tos Digital"
            )
        )

    def test_category_physical_metadata_does_not_prove_digital(self):
        for option_text in (
            "+ Planner Templates (Physical)",
            "+ Planner Templates (Physical or digital)",
            "Books > Books > Planner Templates > Digital",
            "Digital",
        ):
            with self.subTest(option_text=option_text):
                self.assertFalse(
                    etsy_auto_post._category_option_is_digital_only_metadata(option_text)
                )


class TestFillCategoryTab(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_upload_happens_after_category_and_before_item_options_navigation(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )
        product = {
            "category": "3D Printer Files",
            "title": "3D printer files",
            "keywords": "STL",
            "tags": "",
            "description": "",
            "pdf_paths": ["/tmp/customer-files.zip"],
        }
        events = []

        async def tab_stub(_page, label, *_fallback_labels):
            events.append(str(label))

        async def upload_stub(_page, _product, **_kwargs):
            events.append("upload")

        with patch.object(etsy_auto_post, "click_tab", side_effect=tab_stub), \
                patch.object(
                    etsy_auto_post,
                    "_select_digital_listing_type_dropdown_if_present",
                    AsyncMock(return_value=False),
                ), patch.object(
                    etsy_auto_post,
                    "_prime_digital_listing_type_via_planner_templates",
                    AsyncMock(),
                ), patch.object(
                    etsy_auto_post,
                    "_find_visible_digital_files_container",
                    AsyncMock(return_value=None),
                ), patch.object(
                    etsy_auto_post,
                    "_establish_stable_digital_upload_surface",
                    AsyncMock(return_value=object()),
                ), patch.object(
                    etsy_auto_post,
                    "upload_digital_files",
                    side_effect=upload_stub,
                ) as upload_mock:
            await etsy_auto_post.fill_category_tab(page, product)
            await etsy_auto_post.fill_item_options_tab(page, product)

        self.assertEqual(["Category", "upload", "Item Options"], events)
        self.assertTrue(product["_digital_files_uploaded"])
        self.assertEqual(1, upload_mock.await_count)
        self.assertEqual(page, upload_mock.await_args.args[0])
        self.assertEqual(product, upload_mock.await_args.args[1])

    async def test_stable_surface_helper_fails_closed_when_transient_surface_unmounts(self):
        class SequenceSurface:
            def __init__(self):
                self.states = [True, False]

            async def is_visible(self):
                return self.states.pop(0) if self.states else False

        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)"],
            readback_text="3D Printer Files (Digital)",
        )
        surface = SequenceSurface()

        stable = await etsy_auto_post._establish_stable_digital_upload_surface(
            page,
            initial_surface=surface,
            settle_ms=0,
            hold_ms=10,
            poll_ms=1,
        )

        self.assertIsNone(stable)

    async def test_transient_surface_reselects_exact_category_before_upload(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)"],
            readback_text="3D Printer Files (Digital)",
        )
        product = {
            "category": "3D Printer Files",
            "title": "3D printer files",
            "keywords": "STL",
            "tags": "",
            "description": "",
            "pdf_paths": ["/tmp/customer-files.zip"],
        }
        initial_surface = object()
        stable_surface = object()
        selection_terms = []

        async def select_stub(_page, term, *, cat_input=None):
            selection_terms.append(term)
            return "3D Printer Files"

        async def upload_stub(_page, _product, **_kwargs):
            _product["_digital_files_uploaded"] = True

        with patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=False),
        ), patch.object(
            etsy_auto_post,
            "_select_category_with_exact_readback",
            AsyncMock(side_effect=select_stub),
        ) as select_mock, patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(return_value=initial_surface),
        ), patch.object(
            etsy_auto_post,
            "_establish_stable_digital_upload_surface",
            AsyncMock(side_effect=[None, stable_surface]),
        ) as stable_mock, patch.object(
            etsy_auto_post,
            "_prime_digital_listing_type_via_planner_templates",
            AsyncMock(),
        ) as prime_mock, patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(side_effect=upload_stub),
        ) as upload_mock:
            await etsy_auto_post.fill_category_tab(page, product)

        self.assertEqual(["3D Printer Files", "3D Printer Files"], selection_terms)
        self.assertEqual(2, select_mock.await_count)
        self.assertEqual(2, stable_mock.await_count)
        prime_mock.assert_not_awaited()
        upload_mock.assert_awaited_once_with(
            page,
            product,
            verified_surface={"locator": stable_surface},
        )
        self.assertTrue(product["_digital_files_uploaded"])

    async def test_category_upload_failure_does_not_mark_product_as_uploaded(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )
        product = {
            "category": "3D Printer Files",
            "title": "3D printer files",
            "keywords": "STL",
            "tags": "",
            "description": "",
            "pdf_paths": ["/tmp/customer-files.zip"],
        }
        upload_error = etsy_auto_post.DigitalListingTypeError("upload read-back failed")

        with patch.object(etsy_auto_post, "click_tab", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "_select_digital_listing_type_dropdown_if_present",
                    AsyncMock(return_value=False),
                ), patch.object(
                    etsy_auto_post,
                    "_prime_digital_listing_type_via_planner_templates",
                    AsyncMock(),
                ), patch.object(
                    etsy_auto_post,
                    "_find_visible_digital_files_container",
                    AsyncMock(return_value=None),
                ), patch.object(
                    etsy_auto_post,
                    "_establish_stable_digital_upload_surface",
                    AsyncMock(return_value=object()),
                ), patch.object(
                    etsy_auto_post,
                    "upload_digital_files",
                    AsyncMock(side_effect=upload_error),
                ):
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.fill_category_tab(page, product)

        self.assertNotIn("_digital_files_uploaded", product)

    async def test_fill_category_tab_verifies_category_after_sanitized_breadcrumb_readback(self):
        base_product = {
            "category": "",
            "title": "800 AI Commands",
            "keywords": "AI Prompt Guide",
            "tags": "Etsy Sellers",
            "description": "Digital prompts and command templates for running your Etsy store.",
        }
        for readback_text in (
            "Books > Books > Guides & How Tos Physical or digital",
            "Guides & How Tos Physical or digital",
        ):
            with self.subTest(readback_text=readback_text):
                page = _FakeCategoryPage(
                    [
                        "Books > Books > Guides & How Tos Digital",
                        "Books > Books > Planner Templates",
                    ],
                    readback_text=readback_text,
                )

                with patch.object(
                    etsy_auto_post,
                    "click_tab",
                    AsyncMock(),
                ), patch.object(
                    etsy_auto_post,
                    "_select_digital_listing_type_dropdown_if_present",
                    AsyncMock(return_value=False),
                ):
                    await etsy_auto_post.fill_category_tab(page, base_product)

                self.assertTrue(page.input_clicked)
                self.assertEqual(1, page.click_count)
                self.assertEqual(readback_text, page.input_value_state)

    async def test_fill_category_tab_uses_only_digital_category_metadata_as_proof(self):
        cases = (
            (
                "Cutting Machine Files",
                "+ Cutting Machine Files (Digital)",
                "category_digital_metadata",
            ),
            (
                "Planner Templates",
                "+ Planner Templates (Physical)",
                None,
            ),
            (
                "Planner Templates",
                "+ Planner Templates (Physical or digital)",
                None,
            ),
        )
        for category, option_text, expected_flag in cases:
            with self.subTest(option_text=option_text):
                product = {
                    "category": category,
                    "title": "Category metadata proof",
                    "keywords": "",
                    "tags": "",
                    "description": "",
                }
                page = _FakeCategoryPage([option_text], readback_text=option_text)

                with patch.object(etsy_auto_post, "click_tab", AsyncMock()), \
                        patch.object(
                            etsy_auto_post,
                            "_select_digital_listing_type_dropdown_if_present",
                            AsyncMock(return_value=False),
                        ):
                    await etsy_auto_post.fill_category_tab(page, product)

                self.assertEqual(expected_flag, product.get("_digital_listing_type_verified"))

    async def test_fill_category_tab_skips_digital_prime_when_surface_is_already_visible(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)"],
            readback_text="3D Printer Files (Digital)",
        )
        product = {
            "category": "3D Printer Files",
            "title": "3D printer files",
            "keywords": "STL",
            "tags": "",
            "description": "",
            "pdf_paths": ["/tmp/customer-files.zip"],
        }
        events = []
        verified_surface = {"locator": object()}

        async def upload_stub(_page, _product, **_kwargs):
            events.append("upload")
            _product["_digital_files_uploaded"] = True

        with patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(return_value=verified_surface),
        ), patch.object(
            etsy_auto_post,
            "_establish_stable_digital_upload_surface",
            AsyncMock(return_value=verified_surface),
        ), patch.object(
            etsy_auto_post,
            "_prime_digital_listing_type_via_planner_templates",
            AsyncMock(),
        ) as prime_mock, patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=False),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(side_effect=upload_stub),
        ) as upload_mock:
            await etsy_auto_post.fill_category_tab(page, product)

        self.assertEqual(["upload"], events)
        prime_mock.assert_not_awaited()
        self.assertEqual(1, upload_mock.await_count)
        self.assertEqual(page, upload_mock.await_args.args[0])
        self.assertEqual(product, upload_mock.await_args.args[1])
        self.assertEqual(
            {"locator": verified_surface},
            upload_mock.await_args.kwargs.get("verified_surface"),
        )
        self.assertEqual("digital_files", product.get("_digital_listing_type_verified"))

    async def test_fill_category_tab_primes_via_planner_templates_when_surface_missing(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )
        product = {
            "category": "3D Printer Files",
            "title": "3D printer files",
            "keywords": "STL",
            "tags": "",
            "description": "",
            "pdf_paths": ["/tmp/customer-files.zip"],
        }

        async def upload_stub(_page, _product, **_kwargs):
            _product["_digital_files_uploaded"] = True

        verified_surface = {"locator": object()}
        with patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(side_effect=[None, verified_surface]),
        ), patch.object(
            etsy_auto_post,
            "_establish_stable_digital_upload_surface",
            AsyncMock(side_effect=[None, verified_surface]),
        ), patch.object(
            etsy_auto_post,
            "_prime_digital_listing_type_via_planner_templates",
            AsyncMock(),
        ) as prime_mock, patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=False),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(side_effect=upload_stub),
        ) as upload_mock:
            await etsy_auto_post.fill_category_tab(page, product)

        self.assertEqual(1, prime_mock.await_count)
        self.assertEqual(page, prime_mock.await_args.args[0])
        self.assertEqual("3D Printer Files", prime_mock.await_args.args[1])
        self.assertEqual(1, upload_mock.await_count)
        self.assertEqual(page, upload_mock.await_args.args[0])
        self.assertEqual(product, upload_mock.await_args.args[1])
        self.assertEqual(
            {"locator": verified_surface},
            upload_mock.await_args.kwargs.get("verified_surface"),
        )
        self.assertTrue(product["_digital_files_uploaded"])

    async def test_prime_digital_listing_type_via_planner_templates_reselects_target_with_readback(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )
        calls: list[str] = []

        async def select_stub(_page, term, cat_input=None):
            calls.append(term)
            if term in {"3D Printer Files", "Planner Templates"}:
                return term
            raise RuntimeError(f"Unexpected term: {term}")

        with patch.object(
            etsy_auto_post,
            "_select_category_with_exact_readback",
            AsyncMock(side_effect=select_stub),
        ) as select_mock, patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=True),
        ) as dropdown_mock:
            await etsy_auto_post._prime_digital_listing_type_via_planner_templates(
                page,
                "3D Printer Files",
            )

        self.assertEqual(
            ["Planner Templates", "3D Printer Files"],
            [call[0][1] for call in select_mock.await_args_list],
        )
        dropdown_mock.assert_awaited_once_with(page)

    async def test_prime_digital_listing_type_via_planner_templates_clears_committed_category(self):
        page = _FakeCategoryPage(
            ["Planner Templates", "3D Printer Files (Digital)"],
            readback_text="Planner Templates",
            clear_button_visible=True,
        )

        with patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=True),
        ):
            await etsy_auto_post._prime_digital_listing_type_via_planner_templates(
                page,
                "Planner Templates",
            )

        self.assertEqual(2, page.clear_button_click_count)
        self.assertTrue(page.clear_button_scroll_called)
        self.assertTrue(page.clear_button_force_click)

    async def test_prime_digital_listing_type_via_planner_templates_emits_checkpoints(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )

        with patch.object(
            etsy_auto_post,
            "_select_category_with_exact_readback",
            AsyncMock(side_effect=["Planner Templates", "3D Printer Files"]),
        ) as select_mock, patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=True),
        ), patch("builtins.print") as print_mock:
            await etsy_auto_post._prime_digital_listing_type_via_planner_templates(
                page,
                "3D Printer Files",
            )

        checkpoint_messages = " | ".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("Planner Templates", checkpoint_messages)
        self.assertIn("Digital control", checkpoint_messages)
        self.assertIn("3D Printer Files", checkpoint_messages)
        self.assertEqual(2, select_mock.await_count)

    async def test_select_category_with_exact_readback_force_clears_committed_category(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)"],
            readback_text="3D Printer Files (Digital)",
            clear_button_visible=True,
        )

        original_locator = page.locator

        class _ForceClearButton:
            def __init__(self):
                self.scroll_calls = 0
                self.click_calls = 0
                self.force_calls = []

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def scroll_into_view_if_needed(self):
                self.scroll_calls += 1

            async def click(self, *, force: bool = False):
                self.click_calls += 1
                self.force_calls.append(force)

        force_clear = _ForceClearButton()

        def locator_side_effect(selector: str):
            if selector == '#field-category button[aria-label="Clear"]':
                return force_clear
            return original_locator(selector)

        with patch.object(page, "locator", side_effect=locator_side_effect):
            await etsy_auto_post._select_category_with_exact_readback(
                page,
                "3D Printer Files",
            )

        self.assertGreater(force_clear.scroll_calls, 0)
        self.assertEqual(1, force_clear.click_calls)
        self.assertIn(True, force_clear.force_calls)

    async def test_prime_digital_listing_type_via_planner_templates_fails_when_digital_missing(self):
        page = _FakeCategoryPage(
            ["3D Printer Files (Digital)", "Planner Templates"],
            readback_text="3D Printer Files (Digital)",
        )

        async def select_stub(_page, term, cat_input=None):
            return term

        with patch.object(
            etsy_auto_post,
            "_select_category_with_exact_readback",
            AsyncMock(side_effect=select_stub),
        ), patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=False),
        ):
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                await etsy_auto_post._prime_digital_listing_type_via_planner_templates(
                    page,
                    "3D Printer Files",
                )

    async def test_fill_category_tab_without_customer_files_does_not_prime(self):
        page = _FakeCategoryPage(
            ["Planner Templates"],
            readback_text="Planner Templates",
        )
        product = {
            "category": "Planner Templates",
            "title": "Planner template",
            "keywords": "template",
            "tags": "",
            "description": "",
            "pdf_paths": [],
        }

        with patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(return_value=None),
        ), patch.object(
            etsy_auto_post,
            "_prime_digital_listing_type_via_planner_templates",
            AsyncMock(),
        ) as prime_mock, patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(),
        ) as upload_mock, patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_select_digital_listing_type_dropdown_if_present",
            AsyncMock(return_value=False),
        ):
            await etsy_auto_post.fill_category_tab(page, product)

        prime_mock.assert_not_awaited()
        upload_mock.assert_not_awaited()
        self.assertNotIn("_digital_files_uploaded", product)

    def test_resume_category_inference_still_works(self):
        product = {
            "category": "",
            "title": "Professional Resume Cover + CV Pack",
            "keywords": "job search,cv",
            "tags": "",
            "description": "",
        }
        self.assertEqual("Résumé Templates", etsy_auto_post.infer_listing_category(product))

    def test_planner_plural_stem_inference_still_works(self):
        product = {
            "category": "",
            "title": "Weekly Planners & Journals",
            "keywords": "workbooks, worksheets, checklists",
            "tags": "trackers",
            "description": "",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.infer_listing_category(product))

    def test_svg_bundle_inference_still_works(self):
        product = {
            "category": "",
            "title": "Wildflower SVG Bundle | Cricut Cut Files",
            "keywords": "SVG, DXF",
            "tags": "",
            "description": "",
        }
        self.assertEqual("Cutting Machine Files", etsy_auto_post.infer_listing_category(product))

    def test_unknown_category_inference_fails_closed(self):
        product = {
            "category": "",
            "title": "Handmade acoustic speaker coaster",
            "keywords": "wood, gift",
            "tags": "desk",
            "description": "No matching taxonomy tokens.",
        }
        self.assertEqual("", etsy_auto_post.infer_listing_category(product))

    def test_digital_planner_section_falls_back_to_planner_templates(self):
        product = {
            "category": "",
            "title": "Bookstagram Instagram Canva Templates",
            "keywords": "book templates, canva templates, reading template",
            "tags": "book social media",
            "description": "Social media designs for book reviews and reading content.",
            "section": "Digital Planner",
        }
        self.assertEqual("Planner Templates", etsy_auto_post.infer_listing_category(product))


class TestExitCodeContract(unittest.TestCase):
    def test_exit_code_is_nonzero_when_failures_exist(self):
        self.assertEqual(1, etsy_auto_post._exit_code_for_failed_products(2))

    def test_exit_code_is_zero_when_no_failures(self):
        self.assertEqual(0, etsy_auto_post._exit_code_for_failed_products(0))


class TestDigitalListingTypeSelection(unittest.IsolatedAsyncioTestCase):
    async def test_current_dropdown_selects_and_verifies_digital(self):
        page = _FakeDigitalPage(dropdown=True)

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)
        self.assertFalse(page.listbox_open)

    async def test_data_attribute_listing_type_variant_is_supported(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_label="Physical",
            dropdown_button_attrs={
                "data-testid": "physical-digital-switch",
                "aria-haspopup": "listbox",
                "role": "combobox",
                "aria-label": "Physical / digital",
            },
        )

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)

    async def test_combobox_without_aria_has_popup_is_supported(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_label="Physical",
            dropdown_button_attrs={
                "role": "combobox",
                "aria-label": "Physical / digital",
            },
        )

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)

    async def test_accessible_name_combobox_is_supported_without_listing_metadata(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_attrs={"role": "combobox"},
            dropdown_accessible_name="What type of item is it?",
            accessible_only_listing_type=True,
        )

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)

    async def test_accessible_name_trailing_punctuation_is_tolerated(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_attrs={"role": "button"},
            dropdown_accessible_name="What type of item is it",
            accessible_only_listing_type=True,
        )

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)

    async def test_native_select_input_value_readback_proves_digital(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_label="Selected option text",
            dropdown_button_attrs={"role": "combobox"},
            dropdown_accessible_name="What type of item is it?",
            dropdown_input_value="Digital",
            accessible_only_listing_type=True,
        )

        self.assertTrue(
            await etsy_auto_post._control_has_digital_listing_readback(page.button)
        )
        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertFalse(page.listbox_open)

    async def test_native_select_physical_input_value_is_not_digital(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_button_attrs={"role": "combobox"},
            dropdown_accessible_name="What type of item is it?",
            dropdown_input_value="Physical",
            accessible_only_listing_type=True,
        )

        self.assertFalse(
            await etsy_auto_post._control_has_digital_listing_readback(page.button)
        )

    async def test_dropdown_listbox_is_waited_for_when_rendered_after_open(self):
        page = _FakeDigitalPage(dropdown=True, dropdown_option_delay_polls=2)

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertTrue(any(value >= 250 for value in page.wait_calls))

    async def test_digital_option_readback_ignores_non_exact_metadata_suffix(self):
        page = _FakeDigitalPage(dropdown=True)
        page.options[1].attrs["aria-label"] = "Digital option in listing type menu"

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertEqual("Digital", page.button.text)

    async def test_listing_type_control_waits_for_delayed_render(self):
        page = _FakeDigitalPage(dropdown=True, surface_delay_polls=2)

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("dropdown", mode)
        self.assertTrue(page.wait_calls)
        self.assertEqual("Digital", page.button.text)

    async def test_reordered_legacy_radios_selects_digital_by_semantics(self):
        page = _FakeDigitalPage(legacy_radios=[
            ("digital", "Digital", False, True),
            ("physical", "Physical", True, True),
        ])

        mode = await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertEqual("legacy_radio", mode)
        self.assertTrue(page.radios[0].checked)
        self.assertFalse(page.radios[1].checked)
        self.assertEqual([("digital", "Digital", True, 5000)], page.radio_check_calls)

    async def test_legacy_physical_radio_with_negated_digital_label_is_rejected(self):
        page = _FakeDigitalPage(legacy_radios=[
            ("physical", "Physical item, not a digital download", True, True),
        ])

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertIn("tìm thấy 0", str(error.exception))
        self.assertEqual([], page.radio_check_calls)

    async def test_legacy_radio_semantics_are_revalidated_after_selection(self):
        page = _FakeDigitalPage(legacy_radios=[
            (
                "digital",
                "Digital",
                False,
                True,
                "physical",
                "Physical item, not a digital download",
            ),
            ("physical", "Physical", True, True),
        ])

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertIn("không còn mang ngữ nghĩa Digital", str(error.exception))
        self.assertTrue(page.radios[0].checked)

    async def test_missing_digital_control_fails_closed(self):
        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            await etsy_auto_post.select_and_verify_digital_listing_type(_FakeDigitalPage())

    async def test_ambiguous_dropdown_digital_options_fail_closed(self):
        page = _FakeDigitalPage(
            dropdown=True,
            dropdown_options=["Physical", "Digital", "Digital"],
        )

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertIn("tìm thấy 2", str(error.exception))

    async def test_non_sticky_dropdown_digital_selection_fails_closed(self):
        page = _FakeDigitalPage(dropdown=True, dropdown_sticky=False)

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post.select_and_verify_digital_listing_type(page)

        self.assertIn("không giữ trạng thái Digital", str(error.exception))

    async def test_digital_files_region_is_required_when_checked(self):
        await etsy_auto_post._verify_digital_files_region(
            _FakeDigitalPage(digital_region=True)
        )
        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            await etsy_auto_post._verify_digital_files_region(_FakeDigitalPage())

    async def test_upload_surface_waits_for_delayed_react_render(self):
        page = _FakeDigitalPage(digital_region=True, surface_delay_polls=2)

        surface = await etsy_auto_post._resolve_digital_upload_surface(
            page,
            timeout_ms=3,
            poll_ms=1,
        )

        self.assertEqual("digital_files", surface["kind"])
        self.assertEqual([1, 1], page.wait_calls)

    async def test_add_file_only_surface_is_accepted(self):
        surface = await etsy_auto_post._resolve_digital_upload_surface(
            _FakeDigitalPage(digital_region=True, add_file=True),
            timeout_ms=0,
        )

        self.assertEqual("add_file", surface["kind"])

    async def test_two_add_file_buttons_choose_only_digital_files_container(self):
        page = _FakeDigitalPage(
            digital_region=True,
            add_file=True,
            personalization_add_file=True,
        )

        surface = await etsy_auto_post._resolve_digital_upload_surface(page, timeout_ms=0)
        add_button = await etsy_auto_post._find_exact_add_file_affordance(
            page,
            container=surface["locator"],
        )

        self.assertIsNotNone(add_button)
        self.assertIs(add_button.elements[0], page.add_file)

    async def test_unrelated_non_image_personalization_input_is_rejected(self):
        page = _FakeDigitalPage(digital_region=True)
        personalization_input = _FakeDigitalElement(
            page,
            "file_input",
            name="personalization-file-input",
            accept=".pdf,.zip",
            digital_scope=True,
        )
        page.file_inputs.append(personalization_input)

        container = await etsy_auto_post._find_visible_digital_files_container(page)
        scoped = await etsy_auto_post._find_scoped_customer_file_inputs(
            page,
            container=container,
        )

        self.assertEqual([], scoped)

    async def test_filename_outside_digital_files_container_is_not_counted(self):
        page = _FakeUploadReadbackPage([
            {
                "hasRegion": True,
                "names": [],
                "count": 0,
                "pending": False,
                "failed": False,
                "completedCount": 0,
            }
        ])
        outside_state = {
            "hasRegion": True,
            "names": ["outside.pdf"],
            "count": 1,
            "pending": False,
            "failed": False,
            "completedCount": 1,
        }

        async def global_state(_script):
            return outside_state

        page.evaluate = global_state
        surface = {"kind": "digital_files", "locator": page.surface_page.regions[0]}
        state = await etsy_auto_post._read_digital_file_upload_state(page, surface=surface)

        self.assertEqual([], state["names"])
        self.assertEqual(0, state["count"])

    async def test_image_only_input_is_rejected_as_customer_file_surface(self):
        page = _FakeDigitalPage(
            file_input_accept="image/*,.png",
            file_input_scope=True,
        )

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post._resolve_digital_upload_surface(page, timeout_ms=0)

        self.assertIn("customer files", str(error.exception))

    async def test_scoped_customer_file_input_is_accepted(self):
        surface = await etsy_auto_post._resolve_digital_upload_surface(
            _FakeDigitalPage(
                digital_region=True,
                file_input_accept=".pdf,.zip",
                file_input_scope=True,
            ),
            timeout_ms=0,
        )

        self.assertEqual("customer_file_input", surface["kind"])

    async def test_upload_surface_fails_closed_without_positive_evidence(self):
        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            await etsy_auto_post._resolve_digital_upload_surface(
                _FakeDigitalPage(),
                timeout_ms=0,
            )

    async def test_upload_preserves_current_tab_when_surface_is_already_present(self):
        page = _FakeDigitalPage(digital_region=True)
        with tempfile.NamedTemporaryFile(suffix=".pdf") as customer_file:
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": [customer_file.name]},
                )

        self.assertEqual(0, page.tab_clicks)

    async def test_item_details_entrypoint_uses_exact_tab_helper_only(self):
        page = _NoOpDetailsPage()
        product = {
            "title": "Exact Item Details contract test",
            "description": "Description",
            "tags": "",
            "section": "",
        }

        with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "_click_verified_item_details_tab",
                    AsyncMock(return_value=True),
                ) as exact_tab_mock, \
                patch.object(
                    etsy_auto_post,
                    "select_and_verify_digital_listing_type",
                    AsyncMock(return_value="dropdown"),
                ) as digital_type_mock, \
                patch.object(etsy_auto_post, "click_tab", AsyncMock()) as broad_tab_mock, \
                patch.object(etsy_auto_post, "smart_fill", AsyncMock(return_value=False)):
            await etsy_auto_post.fill_item_details_tab(page, product)

        exact_tab_mock.assert_awaited_once_with(page)
        digital_type_mock.assert_awaited_once_with(page)
        self.assertEqual("dropdown", product["_digital_listing_type_verified"])
        broad_tab_mock.assert_not_awaited()
        self.assertNotIn(
            'click_tab(page, "Item Details", "Details")',
            inspect.getsource(etsy_auto_post.fill_item_details_tab),
        )

    async def test_listing_type_is_selected_on_item_details_and_not_reselected_in_options(self):
        page = _NoOpDetailsPage()
        product = {
            "title": "Item Details listing type call order",
            "description": "Description",
            "tags": "",
            "section": "",
            "pdf_paths": [],
        }
        events = []

        async def open_item_details(_page):
            events.append("item_details")
            return True

        async def select_digital(_page):
            events.append("select_digital")
            return "dropdown"

        async def open_item_options(_page, *_labels):
            events.append("item_options")

        with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "_click_verified_item_details_tab",
                    side_effect=open_item_details,
                ), \
                patch.object(
                    etsy_auto_post,
                    "select_and_verify_digital_listing_type",
                    side_effect=select_digital,
                ) as select_mock, \
                patch.object(etsy_auto_post, "click_tab", side_effect=open_item_options), \
                patch.object(etsy_auto_post, "smart_fill", AsyncMock(return_value=False)):
            await etsy_auto_post.fill_item_details_tab(page, product)
            await etsy_auto_post.fill_item_options_tab(page, product)

        self.assertEqual(["item_details", "select_digital", "item_options"], events)
        select_mock.assert_awaited_once_with(page)
        self.assertEqual("dropdown", product["_digital_listing_type_verified"])

    async def test_item_details_runs_final_digital_file_persistence_gate(self):
        page = _NoOpDetailsPage()
        product = {
            "title": "Stable Item Details upload",
            "description": "Description",
            "tags": "",
            "section": "",
            "pdf_paths": ["/tmp/Kawaii-Click-Keychains-3D-154741369.zip"],
        }

        with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "_click_verified_item_details_tab",
                    AsyncMock(return_value=True),
                ), patch.object(
                    etsy_auto_post,
                    "select_and_verify_digital_listing_type",
                    AsyncMock(return_value="dropdown"),
                ), patch.object(
                    etsy_auto_post,
                    "smart_fill",
                    AsyncMock(return_value=False),
                ), patch.object(
                    etsy_auto_post,
                    "_reconcile_digital_files_on_item_details",
                    AsyncMock(),
                ) as reconcile_mock:
            await etsy_auto_post.fill_item_details_tab(page, product)

        reconcile_mock.assert_awaited_once_with(page, product)

    async def test_upload_pdf_files_rechecks_exact_item_details_before_upload(self):
        page = _NoOpDetailsPage()
        product = {
            "pdf_paths": ["/tmp/test.pdf"],
        }
        events = []

        async def open_item_options(*_args, **_kwargs):
            events.append("item_options")

        async def open_item_details(_page):
            events.append("item_details")
            return True

        async def upload_stub(_page, _product):
            events.append("upload")

        with patch.object(
            etsy_auto_post,
            "dismiss_alerts",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_click_verified_item_details_tab",
            side_effect=open_item_details,
        ) as exact_tab_mock, patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(side_effect=open_item_options),
        ), patch.object(
            etsy_auto_post,
            "select_and_verify_digital_listing_type",
            AsyncMock(return_value="dropdown"),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(side_effect=upload_stub),
        ) as upload_mock:
            await etsy_auto_post.fill_item_options_tab(page, product)

        self.assertEqual(["item_options", "item_details", "upload"], events)
        exact_tab_mock.assert_awaited_once_with(page)
        upload_mock.assert_awaited_once_with(page, product)

    async def test_item_options_does_not_upload_again_after_category_upload(self):
        page = _NoOpDetailsPage()
        product = {
            "pdf_paths": ["/tmp/test.pdf"],
            "_digital_files_uploaded": True,
            "_digital_listing_type_verified": "category_digital_metadata",
        }

        with patch.object(etsy_auto_post, "click_tab", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "upload_digital_files",
                    AsyncMock(),
                ) as upload_mock, patch.object(
                    etsy_auto_post,
                    "_click_verified_item_details_tab",
                    AsyncMock(),
                ) as exact_tab_mock:
            await etsy_auto_post.fill_item_options_tab(page, product)

        upload_mock.assert_not_awaited()
        exact_tab_mock.assert_not_awaited()

    async def test_upload_pdf_files_fails_closed_when_item_details_tab_cannot_be_found(self):
        page = _NoOpDetailsPage()
        product = {
            "pdf_paths": ["/tmp/test.pdf"],
        }
        events = []

        async def open_item_options(*_args, **_kwargs):
            events.append("item_options")

        with patch.object(
            etsy_auto_post,
            "dismiss_alerts",
            AsyncMock(),
        ), patch.object(
            etsy_auto_post,
            "_click_verified_item_details_tab",
            AsyncMock(return_value=False),
        ), patch.object(
            etsy_auto_post,
            "click_tab",
            AsyncMock(side_effect=open_item_options),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(),
        ) as upload_mock, patch.object(
            etsy_auto_post,
            "select_and_verify_digital_listing_type",
            AsyncMock(return_value="dropdown"),
        ):
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                await etsy_auto_post.fill_item_options_tab(page, product)

        self.assertEqual(["item_options"], events)
        upload_mock.assert_not_awaited()

    async def test_upload_readback_accepts_etsy_sanitized_names_after_stable_completion(self):
        completed = {
            "hasRegion": True,
            "names": ["2027CraftSellerPlanner.pdf", "BonusChecklist.zip"],
            "count": 2,
            "pending": False,
            "failed": False,
            "completedCount": 2,
        }
        page = _FakeUploadReadbackPage([completed, dict(completed)])

        state = await etsy_auto_post._wait_for_uploaded_digital_files(
            page,
            [
                "/tmp/2027 Craft Seller Planner.pdf",
                "/tmp/Bonus Checklist.zip",
            ],
            timeout_ms=10,
            poll_ms=1,
            stable_reads=2,
        )

        self.assertEqual(2, state["count"])
        self.assertEqual(2, page.index)

    async def test_upload_readback_fails_when_any_requested_file_is_missing(self):
        partial = {
            "hasRegion": True,
            "names": ["2027CraftSellerPlanner.pdf"],
            "count": 1,
            "pending": False,
            "failed": False,
            "completedCount": 1,
        }
        page = _FakeUploadReadbackPage([partial, dict(partial)])

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post._wait_for_uploaded_digital_files(
                page,
                [
                    "/tmp/2027 Craft Seller Planner.pdf",
                    "/tmp/Bonus Checklist.zip",
                ],
                timeout_ms=0,
                poll_ms=1,
                stable_reads=2,
            )

        self.assertIn("Bonus Checklist.zip", str(error.exception))

    async def test_direct_input_upload_failure_raises_contract_error(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as customer_file:
            product = {"pdf_paths": [customer_file.name]}

            with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
                await etsy_auto_post.upload_digital_files(
                    _FailingDirectInputPage(),
                    product,
                )

        self.assertIn("direct input", str(error.exception))

    async def test_selection_failure_aborts_fill_listing_before_save(self):
        page = _NoSaveListingPage()
        product = {
            "folder": "product-test",
            "title": "Digital listing contract test",
            "image_paths": [],
            "pdf_paths": [],
            "price": 4.99,
        }

        with patch.object(etsy_auto_post, "detect_form_type", AsyncMock(return_value="tabs")), \
                patch.object(etsy_auto_post, "fill_photo_tab", AsyncMock()), \
                patch.object(etsy_auto_post, "fill_category_tab", AsyncMock()), \
                patch.object(etsy_auto_post, "fill_item_details_tab", AsyncMock()), \
                patch.object(etsy_auto_post, "fill_pricing_tab", AsyncMock()), \
                patch.object(etsy_auto_post, "click_tab", AsyncMock()), \
                patch.object(
                    etsy_auto_post,
                    "select_and_verify_digital_listing_type",
                    AsyncMock(side_effect=etsy_auto_post.DigitalListingTypeError("Digital missing")),
                ):
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                await etsy_auto_post.fill_listing(
                    page,
                    product,
                    edit_url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                )

        self.assertFalse(any("Save" in selector for selector in page.locator_calls))

    async def test_upload_failure_aborts_tabs_form_before_save(self):
        page = _NoSaveListingPage()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as customer_file:
            product = {
                "folder": "product-upload-failure",
                "title": "Verified upload contract test",
                "image_paths": [],
                "pdf_paths": [customer_file.name],
                "price": 4.99,
            }

            async def mark_digital(_page, target):
                target["_digital_listing_type_verified"] = "dropdown"

            upload_error = etsy_auto_post.DigitalListingTypeError(
                "customer file read-back failed"
            )
            with patch.object(etsy_auto_post, "detect_form_type", AsyncMock(return_value="tabs")), \
                    patch.object(etsy_auto_post, "fill_photo_tab", AsyncMock()), \
                    patch.object(
                        etsy_auto_post,
                        "fill_category_tab",
                        AsyncMock(side_effect=mark_digital),
                    ), \
                    patch.object(etsy_auto_post, "fill_item_details_tab", AsyncMock()), \
                    patch.object(etsy_auto_post, "fill_pricing_tab", AsyncMock()), \
                    patch.object(etsy_auto_post, "click_tab", AsyncMock()), \
                    patch.object(
                        etsy_auto_post,
                        "_click_verified_item_details_tab",
                        AsyncMock(return_value=True),
                    ) as details_tab_mock, \
                    patch.object(
                        etsy_auto_post,
                        "upload_digital_files",
                        AsyncMock(side_effect=upload_error),
                    ) as upload_mock:
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.fill_listing(
                        page,
                        product,
                        edit_url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                    )

            details_tab_mock.assert_awaited_once_with(page)
            upload_mock.assert_awaited_once_with(page, product)

        self.assertFalse(any("Save" in selector for selector in page.locator_calls))

    async def test_long_form_with_requested_customer_files_aborts_before_save(self):
        page = _NoSaveListingPage()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as customer_file:
            product = {
                "folder": "product-long-form",
                "title": "Long form upload contract test",
                "image_paths": [],
                "pdf_paths": [customer_file.name],
                "price": 4.99,
            }

            with patch.object(etsy_auto_post, "detect_form_type", AsyncMock(return_value="single")), \
                    patch.object(
                        etsy_auto_post,
                        "select_and_verify_digital_listing_type",
                        AsyncMock(return_value="legacy_radio"),
                    ):
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
                    await etsy_auto_post.fill_listing(
                        page,
                        product,
                        edit_url="https://www.etsy.com/your/shops/me/listing-editor/edit/123",
                    )

        self.assertIn("trang dài", str(error.exception))
        self.assertFalse(any("Save" in selector for selector in page.locator_calls))

    def test_no_positional_listing_type_selector_remains(self):
        posting_paths = (
            inspect.getsource(etsy_auto_post.fill_item_options_tab)
            + inspect.getsource(etsy_auto_post.fill_listing)
        )
        self.assertNotIn(".nth(1)", posting_paths)


class TestEtsyCustomerFileStaging(unittest.TestCase):
    def test_actual_bad_filename_is_sanitized_to_etsy_contract(self):
        self.assertEqual(
            "800-AI-Commands-for-Etsy-Product-Sales-Strategy.pdf",
            etsy_auto_post._etsy_safe_customer_filename(
                "800+ AI Commands for Etsy Product Sales Strategy.pdf"
            ),
        )

    def test_valid_filename_is_preserved_and_extension_is_lowercase(self):
        self.assertEqual(
            "Seller_Guide-2026.pdf",
            etsy_auto_post._etsy_safe_customer_filename("Seller_Guide-2026.PDF"),
        )

    def test_unicode_and_invalid_characters_are_ascii_sanitized(self):
        value = etsy_auto_post._etsy_safe_customer_filename(
            "Bộ lệnh AI – résumé (final) #1.pdf"
        )
        self.assertRegex(value, r"^[A-Za-z0-9._-]{3,70}$")
        self.assertEqual("Bo-lenh-AI-resume-final-1.pdf", value)

    def test_empty_sanitized_stem_uses_safe_fallback(self):
        self.assertEqual("file.pdf", etsy_auto_post._etsy_safe_customer_filename("+++.pdf"))

    def test_truncation_and_collision_suffix_stay_within_limit(self):
        source = "A" * 120 + ".pdf"
        first = etsy_auto_post._etsy_safe_customer_filename(source)
        second = etsy_auto_post._etsy_safe_customer_filename(
            source,
            reserved_names={first},
            reserved_readback_names={first},
        )
        self.assertEqual(70, len(first))
        self.assertLessEqual(len(second), 70)
        self.assertTrue(second.endswith("-2.pdf"))

    def test_collision_checks_casefold_and_readback_normalization(self):
        first = etsy_auto_post._etsy_safe_customer_filename("a-b.pdf")
        second = etsy_auto_post._etsy_safe_customer_filename(
            "ab.pdf",
            reserved_names={first},
            reserved_readback_names={first},
        )
        third = etsy_auto_post._etsy_safe_customer_filename(
            "guide.PDF",
            reserved_names={"Guide.pdf"},
            reserved_readback_names={"Guide.pdf"},
        )
        self.assertEqual("a-b.pdf", first)
        self.assertEqual("ab-2.pdf", second)
        self.assertEqual("guide-2.pdf", third)

    def test_unsupported_missing_and_compound_extensions_fail_closed(self):
        for name in ("guide.exe", "guide", "guide.pdf.exe"):
            with self.subTest(name=name), self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                etsy_auto_post._etsy_safe_customer_filename(name)

    def test_staging_preserves_source_bytes_and_cleans_only_temp_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "800+ AI Commands for Etsy Product Sales Strategy.pdf"
            payload = b"customer-file-bytes"
            source.write_bytes(payload)
            source_paths = [str(source)]

            with etsy_auto_post._stage_etsy_customer_files(source_paths) as staged:
                self.assertEqual(1, len(staged))
                self.assertNotEqual(source, staged[0])
                self.assertEqual(payload, staged[0].read_bytes())
                self.assertRegex(staged[0].name, r"^[A-Za-z0-9._-]{3,70}$")
                staged_path = staged[0]
                self.assertTrue(staged_path.exists())

            self.assertFalse(staged_path.exists())
            self.assertEqual(payload, source.read_bytes())
            self.assertEqual(source_paths, [str(source)])

    def test_staging_rejects_duplicate_source_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "guide.pdf"
            source.write_bytes(b"x")
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                with etsy_auto_post._stage_etsy_customer_files([source, source]):
                    pass

            link = Path(tmp) / "link.pdf"
            link.symlink_to(source)
            with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                with etsy_auto_post._stage_etsy_customer_files([link]):
                    pass


class _FakeAwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def resolve():
            return self.value
        return resolve().__await__()


class _FakeUploadButton:
    def __init__(self, *, scroll_error=None):
        self.scroll_error = scroll_error
        self.scroll_timeouts = []

    async def scroll_into_view_if_needed(self, *, timeout=None):
        self.scroll_timeouts.append(timeout)
        if self.scroll_error is not None:
            raise self.scroll_error
        return None

    async def is_disabled(self):
        return False

    async def click(self):
        return None


class _NeverCompletingAddFileDisabledButton(_FakeUploadButton):
    def __init__(self):
        super().__init__()
        self.disabled_calls = 0

    async def is_disabled(self):
        self.disabled_calls += 1
        await asyncio.Event().wait()


class _FakeChooserContext:
    def __init__(self, chooser):
        self.info = types.SimpleNamespace(value=_FakeAwaitableValue(chooser))

    async def __aenter__(self):
        return self.info

    async def __aexit__(self, *_args):
        return False


class _FakeUploadResponse:
    status = 200
    url = "https://www.etsy.com/api/v3/ajax/shop/35505785/mission-control/listing-editor/files"
    request = types.SimpleNamespace(method="POST")

    async def json(self):
        return {
            "fileId": 1507965829724,
            "type": "application/zip",
            "name": "uploaded-customer-file.zip",
            "url": "/your/files/preview/uploaded-customer-file",
        }


class _FakeResponseContext:
    def __init__(self, response):
        self.info = types.SimpleNamespace(value=_FakeAwaitableValue(response))

    async def __aenter__(self):
        return self.info

    async def __aexit__(self, *_args):
        return False


class _DelayedAwaitableValue:
    def __init__(self, value, delay_seconds):
        self.value = value
        self.delay_seconds = delay_seconds

    def __await__(self):
        async def resolve():
            await asyncio.sleep(self.delay_seconds)
            return self.value
        return resolve().__await__()


class _DelayedResponseContext:
    def __init__(self, response, delay_seconds):
        self.info = types.SimpleNamespace(
            value=_DelayedAwaitableValue(response, delay_seconds)
        )

    async def __aenter__(self):
        return self.info

    async def __aexit__(self, *_args):
        return False


class _FakeUploadPage:
    def __init__(self, received):
        self.received = received
        self.wait_calls = []

    def expect_file_chooser(self, **_kwargs):
        page = self

        class Chooser:
            async def set_files(self, paths):
                paths = [Path(path) for path in paths]
                page.received.append((paths, [path.exists() for path in paths]))

        return _FakeChooserContext(Chooser())

    def expect_response(self, _predicate, **_kwargs):
        return _FakeResponseContext(_FakeUploadResponse())

    async def wait_for_timeout(self, _ms):
        self.wait_calls.append(_ms)


class _FakeSurfaceLocator:
    def __init__(self, visible: bool, *, visible_error=None):
        self.visible = bool(visible)
        self.visible_error = visible_error
        self.visible_calls = []

    async def is_visible(self):
        self.visible_calls.append(None)
        if self.visible_error is not None:
            raise self.visible_error
        return self.visible


class _NeverCompletingSurfaceLocator:
    def __init__(self):
        self.visible_calls = 0

    async def count(self):
        return 1

    def nth(self, _index):
        return self

    async def is_visible(self):
        self.visible_calls += 1
        await asyncio.Event().wait()


class _NeverCompletingAddFileCountLocator:
    async def count(self):
        await asyncio.Event().wait()


class _FakeEmptyLocator:
    async def count(self):
        return 0


class _FakeAddFileSurfaceContainer(_FakeSurfaceLocator):
    def __init__(self, add_file_locator):
        super().__init__(True)
        self.add_file_locator = add_file_locator

    def get_by_role(self, _role, *, name=None):
        return self.add_file_locator

    def locator(self, selector):
        if selector == 'label, [role="button"]':
            return _FakeEmptyLocator()
        raise AssertionError(f"Unexpected unscoped locator selector: {selector}")


class TestEtsyCustomerFileUploadIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_response_gate_accepts_slow_response_within_bounded_upload_budget(self):
        class DelayedResponsePage:
            def expect_response(self, _predicate, **_kwargs):
                return _DelayedResponseContext(_FakeUploadResponse(), 0.025)

        async def upload_operation():
            return None

        # Simulate a response arriving after the old 15ms test budget but
        # before the new bounded response budget; the live equivalent is a
        # large ZIP taking longer than 15 seconds to finish server processing.
        with patch.object(
            etsy_auto_post,
            "DIGITAL_FILE_UPLOAD_RESPONSE_TIMEOUT_MS",
            50,
        ):
            receipts = await etsy_auto_post._capture_customer_file_upload_response(
                DelayedResponsePage(),
                upload_operation,
            )

        self.assertEqual(1507965829724, receipts[0]["fileId"])

    def test_response_gate_budget_covers_positive_ui_upload_budget(self):
        self.assertGreaterEqual(
            etsy_auto_post.DIGITAL_FILE_UPLOAD_RESPONSE_TIMEOUT_MS,
            etsy_auto_post.DIGITAL_FILE_UPLOAD_WAIT_MS,
        )

    async def test_verified_upload_retains_server_file_id_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Kawaii-Click-Keychains-3D-154741369.zip"
            source.write_bytes(b"upload-bytes")
            received = []
            page = _FakeUploadPage(received)
            product = {"pdf_paths": [str(source)]}
            surface = {"locator": _FakeSurfaceLocator(True)}

            async def wait_readback(_page, expected_paths, **_kwargs):
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(
                etsy_auto_post,
                "_find_exact_add_file_affordance",
                AsyncMock(return_value=_FakeUploadButton()),
            ), patch.object(
                etsy_auto_post,
                "_wait_for_uploaded_digital_files",
                side_effect=wait_readback,
            ):
                await etsy_auto_post.upload_digital_files(
                    page,
                    product,
                    verified_surface=surface,
                )

            self.assertEqual(1, len(product["_digital_file_upload_receipts"]))
            receipt = product["_digital_file_upload_receipts"][0]
            self.assertEqual(1507965829724, receipt["fileId"])
            self.assertEqual("uploaded-customer-file.zip", receipt["name"])
            self.assertEqual(200, receipt["status"])

    async def test_saved_draft_readback_rejects_missing_digital_files(self):
        page = _FakeUploadReadbackPage([{
            "hasRegion": True,
            "names": [],
            "count": 0,
            "pending": False,
            "failed": False,
            "completedCount": 0,
        }])

        with patch.object(
            etsy_auto_post,
            "DIGITAL_SAVED_DRAFT_READBACK_TIMEOUT_MS",
            20,
        ), self.assertRaises(etsy_auto_post.DigitalListingTypeError) as error:
            await etsy_auto_post._verify_saved_draft_digital_files(
                page,
                {
                    "pdf_paths": ["/tmp/Kawaii-Click-Keychains-3D-154741369.zip"],
                    "_digital_file_upload_receipts": [{
                        "fileId": 1507965829724,
                        "name": "Kawaii-Click-Keychains-3D-154741369.zip",
                    }],
                },
            )

        self.assertIn("Digital files DOM", str(error.exception))

    async def test_saved_draft_dom_readback_polls_until_delayed_surface_appears(self):
        page = _FakeUploadReadbackPage([{
            "hasRegion": True,
            "names": ["Delayed Guide.pdf"],
            "count": 1,
            "pending": False,
            "failed": False,
            "completedCount": 1,
        }])
        surface = object()

        with patch.object(
            etsy_auto_post,
            "DIGITAL_SAVED_DRAFT_READBACK_TIMEOUT_MS",
            100,
        ), patch.object(
            etsy_auto_post,
            "DIGITAL_SAVED_DRAFT_READBACK_POLL_MS",
            1,
        ), patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(side_effect=[None, surface]),
        ) as find_surface_mock, patch.object(
            etsy_auto_post,
            "_read_digital_file_upload_state",
            AsyncMock(return_value={
                "hasRegion": True,
                "names": ["Delayed Guide.pdf"],
                "count": 1,
                "pending": False,
                "failed": False,
                "completedCount": 1,
            }),
        ):
            names = await etsy_auto_post._read_saved_draft_digital_file_names(page)

        self.assertEqual(["delayedguidepdf"], names)
        self.assertEqual(2, find_surface_mock.await_count)

    async def test_saved_draft_dom_readback_rejects_two_files_with_one_receipt(self):
        page = _FakeUploadReadbackPage([{
            "hasRegion": True,
            "names": ["First Guide.pdf", "Second Bundle.zip"],
            "count": 2,
            "pending": False,
            "failed": False,
            "completedCount": 2,
        }])

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            await etsy_auto_post._verify_saved_draft_digital_files(
                page,
                {
                    "pdf_paths": ["/tmp/First Guide.pdf", "/tmp/Second Bundle.zip"],
                    "_digital_file_upload_receipts": [{
                        "fileId": 1507965829724,
                        "name": "First Guide.pdf",
                    }],
                },
            )

    async def test_saved_draft_verifier_accepts_truncated_staged_alias_for_source(self):
        page = _FakeUploadReadbackPage([{
            "hasRegion": True,
            "names": ["A" * 66 + ".pdf"],
            "count": 1,
            "pending": False,
            "failed": False,
            "completedCount": 1,
        }])
        source_name = "A" * 100 + ".pdf"
        staged_name = "A" * 66 + ".pdf"
        product = {
            "pdf_paths": [f"/tmp/{source_name}"],
            "_digital_file_alias_groups": [{
                "source": source_name,
                "staged": staged_name,
                "aliases": [
                    etsy_auto_post._normalize_customer_filename(source_name),
                    etsy_auto_post._normalize_customer_filename(staged_name),
                ],
            }],
        }

        await etsy_auto_post._verify_saved_draft_digital_files(page, product)

    def test_saved_name_alias_matching_dedupes_filename_and_action_representations(self):
        groups = [
            {"aliases": ["abpdf"]},
            {"aliases": ["cdpdf"]},
        ]

        self.assertFalse(
            etsy_auto_post._saved_names_match_alias_groups(
                [
                    "a-b.pdf",
                    "Remove file a-b.pdf",
                    "Download file a-b.pdf",
                    "a-b.pdf Download file",
                ],
                groups,
            )
        )
        self.assertTrue(
            etsy_auto_post._saved_names_match_alias_groups(
                [
                    "a-b.pdf",
                    "Remove file a-b.pdf",
                    "Download file a-b.pdf",
                    "a-b.pdf Download file",
                    "c-d.pdf",
                    "Delete file c-d.pdf",
                    "c-d.pdf Remove file",
                ],
                groups,
            )
        )
        self.assertFalse(
            etsy_auto_post._saved_names_match_alias_groups(
                ["a-b.pdf", "File a-b.pdf"],
                groups,
            )
        )

    def test_non_equivalent_receipt_requires_deterministic_mapping(self):
        groups = etsy_auto_post._customer_file_alias_groups(
            ["/tmp/first.pdf", "/tmp/second.zip"],
            ["/tmp/first.pdf", "/tmp/second.zip"],
        )

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            etsy_auto_post._add_customer_receipt_aliases(
                groups,
                [{"fileId": 1, "name": "server-generated-name.zip"}],
            )

    def test_blank_positive_file_id_receipt_fails_closed(self):
        groups = etsy_auto_post._customer_file_alias_groups(
            ["/tmp/first.pdf"],
            ["/tmp/first.pdf"],
        )

        with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
            etsy_auto_post._add_customer_receipt_aliases(
                groups,
                [{"fileId": 1, "name": "  "}],
            )

    def test_receipt_cardinality_under_and_over_fail_closed(self):
        groups = etsy_auto_post._customer_file_alias_groups(
            ["/tmp/first.pdf", "/tmp/second.zip"],
            ["/tmp/first.pdf", "/tmp/second.zip"],
        )
        cases = [
            [{"fileId": 1, "name": "first.pdf"}],
            [
                {"fileId": 1, "name": "first.pdf"},
                {"fileId": 2, "name": "second.zip"},
                {"fileId": 3, "name": "third.pdf"},
            ],
        ]
        for receipts in cases:
            with self.subTest(receipts=receipts), self.assertRaises(
                etsy_auto_post.DigitalListingTypeError
            ):
                etsy_auto_post._add_customer_receipt_aliases(groups, receipts)

    def test_one_file_receipt_contract_still_maps_positionally(self):
        groups = etsy_auto_post._customer_file_alias_groups(
            ["/tmp/first.pdf"],
            ["/tmp/first.pdf"],
        )
        mapped = etsy_auto_post._add_customer_receipt_aliases(
            groups,
            [{"fileId": 1, "name": "server-generated-name.pdf"}],
        )
        self.assertIn("servergeneratednamepdf", mapped[0]["aliases"])

    def test_receipt_order_maps_non_equivalent_names_when_count_is_unambiguous(self):
        groups = etsy_auto_post._customer_file_alias_groups(
            ["/tmp/first.pdf", "/tmp/second.zip"],
            ["/tmp/first.pdf", "/tmp/second.zip"],
        )

        mapped = etsy_auto_post._add_customer_receipt_aliases(
            groups,
            [
                {"fileId": 1, "name": "server-first.bin"},
                {"fileId": 2, "name": "server-second.bin"},
            ],
        )

        self.assertIn("serverfirstbin", mapped[0]["aliases"])
        self.assertIn("serversecondbin", mapped[1]["aliases"])

    async def test_item_details_reconcile_reuploads_only_when_stable_scope_is_empty(self):
        page = _FakeUploadPage([])
        product = {"pdf_paths": ["/tmp/Kawaii-Click-Keychains-3D-154741369.zip"]}
        initial = object()
        stable = object()

        with patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(return_value=initial),
        ), patch.object(
            etsy_auto_post,
            "_establish_stable_digital_upload_surface",
            AsyncMock(return_value=stable),
        ), patch.object(
            etsy_auto_post,
            "_read_digital_file_upload_state",
            AsyncMock(return_value={
                "hasRegion": True,
                "names": [],
                "count": 0,
                "pending": False,
                "failed": False,
            }),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(),
        ) as upload_mock:
            await etsy_auto_post._reconcile_digital_files_on_item_details(page, product)

        upload_mock.assert_awaited_once_with(
            page,
            product,
            verified_surface={"locator": stable},
        )
        self.assertTrue(product["_digital_files_uploaded"])

    async def test_item_details_reconcile_does_not_upload_duplicate_when_names_are_present(self):
        page = _FakeUploadPage([])
        product = {"pdf_paths": ["/tmp/Kawaii-Click-Keychains-3D-154741369.zip"]}
        stable = object()

        with patch.object(
            etsy_auto_post,
            "_find_visible_digital_files_container",
            AsyncMock(return_value=stable),
        ), patch.object(
            etsy_auto_post,
            "_establish_stable_digital_upload_surface",
            AsyncMock(return_value=stable),
        ), patch.object(
            etsy_auto_post,
            "_read_digital_file_upload_state",
            AsyncMock(return_value={
                "hasRegion": True,
                "names": ["Kawaii-Click-Keychains-3D-154741369.zip"],
                "count": 1,
                "pending": False,
                "failed": False,
            }),
        ), patch.object(
            etsy_auto_post,
            "upload_digital_files",
            AsyncMock(),
        ) as upload_mock:
            await etsy_auto_post._reconcile_digital_files_on_item_details(page, product)

        upload_mock.assert_not_awaited()
        self.assertTrue(product["_digital_files_uploaded"])

    async def test_liveness_probe_has_real_coroutine_timeout(self):
        class ProbePage:
            async def wait_for_timeout(self, _ms):
                return None

        locator = _NeverCompletingSurfaceLocator()
        with patch.object(etsy_auto_post, "DIGITAL_SURFACE_LIVENESS_TIMEOUT_MS", 15):
            started = asyncio.get_running_loop().time()
            live = await etsy_auto_post._is_likely_live_surface(
                ProbePage(),
                locator=locator,
                attempts=1,
                wait_ms=0,
            )
            elapsed = asyncio.get_running_loop().time() - started

        self.assertFalse(live)
        self.assertEqual(1, locator.visible_calls)
        self.assertLess(elapsed, 0.5)

    async def test_verified_upload_bounds_add_file_lookup_and_uses_same_scoped_direct_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            received = []
            page = _FakeUploadPage(received)
            add_locator = _NeverCompletingSurfaceLocator()
            surface_locator = _FakeAddFileSurfaceContainer(add_locator)
            surface = {"locator": surface_locator}
            direct_scope_calls = []

            class DirectInput:
                async def set_input_files(self, paths, timeout=None):
                    received.append(([Path(path) for path in paths], timeout))

            async def find_scoped_inputs(_page, *, container=None):
                direct_scope_calls.append(container)
                return [(0, DirectInput())]

            async def wait_readback(_page, expected_paths, **_kwargs):
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(
                etsy_auto_post,
                "DIGITAL_ADD_FILE_VISIBILITY_TIMEOUT_MS",
                15,
            ), patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_resolve_digital_upload_surface",
                        AsyncMock(),
                    ) as resolve_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_find_scoped_customer_file_inputs",
                        side_effect=find_scoped_inputs,
                    ), \
                    patch.object(
                        etsy_auto_post,
                        "_wait_for_uploaded_digital_files",
                        side_effect=wait_readback,
                    ):
                started = asyncio.get_running_loop().time()
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": [str(source)]},
                    verified_surface=surface,
                )
                elapsed = asyncio.get_running_loop().time() - started

            dismiss_mock.assert_not_awaited()
            resolve_mock.assert_not_awaited()
            self.assertLess(elapsed, 0.5)
            self.assertEqual(1, add_locator.visible_calls)
            self.assertEqual([surface_locator], direct_scope_calls)
            self.assertEqual(1, len(received))

    async def test_verified_upload_bounds_add_file_count_discovery_and_requires_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            received = []
            readback_paths = []
            page = _FakeUploadPage(received)
            surface_locator = _FakeAddFileSurfaceContainer(
                _NeverCompletingAddFileCountLocator()
            )
            surface = {"locator": surface_locator}
            direct_scope_calls = []

            class DirectInput:
                async def set_input_files(self, paths, timeout=None):
                    received.append(([Path(path) for path in paths], timeout))

            async def find_scoped_inputs(_page, *, container=None):
                direct_scope_calls.append(container)
                return [(0, DirectInput())]

            async def wait_readback(_page, expected_paths, **_kwargs):
                readback_paths.append([Path(path) for path in expected_paths])
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(
                etsy_auto_post,
                "DIGITAL_ADD_FILE_VISIBILITY_TIMEOUT_MS",
                15,
            ), patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_resolve_digital_upload_surface",
                        AsyncMock(),
                    ) as resolve_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_find_scoped_customer_file_inputs",
                        side_effect=find_scoped_inputs,
                    ), \
                    patch.object(
                        etsy_auto_post,
                        "_wait_for_uploaded_digital_files",
                        side_effect=wait_readback,
                    ):
                started = asyncio.get_running_loop().time()
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": [str(source)]},
                    verified_surface=surface,
                )
                elapsed = asyncio.get_running_loop().time() - started

            dismiss_mock.assert_not_awaited()
            resolve_mock.assert_not_awaited()
            self.assertLess(elapsed, 0.5)
            self.assertEqual([surface_locator], direct_scope_calls)
            self.assertEqual(1, len(received))
            self.assertEqual(1, len(readback_paths))
            self.assertEqual(received[0][0], readback_paths[0])

    async def test_verified_upload_skips_add_file_disabled_probe_and_uses_chooser(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            received = []
            readback_paths = []
            page = _FakeUploadPage(received)
            surface_locator = _FakeSurfaceLocator(True)
            surface = {"locator": surface_locator}
            add_button = _NeverCompletingAddFileDisabledButton()
            direct_scope_calls = []

            class DirectInput:
                async def set_input_files(self, paths, timeout=None):
                    received.append(([Path(path) for path in paths], timeout))

            async def find_scoped_inputs(_page, *, container=None):
                direct_scope_calls.append(container)
                return [(0, DirectInput())]

            async def wait_readback(_page, expected_paths, **_kwargs):
                readback_paths.append([Path(path) for path in expected_paths])
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(
                etsy_auto_post,
                "DIGITAL_ADD_FILE_VISIBILITY_TIMEOUT_MS",
                15,
            ), patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_resolve_digital_upload_surface",
                        AsyncMock(),
                    ) as resolve_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_find_exact_add_file_affordance",
                        AsyncMock(return_value=add_button),
                    ), \
                    patch.object(
                        etsy_auto_post,
                        "_find_scoped_customer_file_inputs",
                        side_effect=find_scoped_inputs,
                    ), \
                    patch.object(
                        etsy_auto_post,
                        "_wait_for_uploaded_digital_files",
                        side_effect=wait_readback,
                    ):
                started = asyncio.get_running_loop().time()
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": [str(source)]},
                    verified_surface=surface,
                )
                elapsed = asyncio.get_running_loop().time() - started

            dismiss_mock.assert_not_awaited()
            resolve_mock.assert_not_awaited()
            self.assertLess(elapsed, 0.5)
            self.assertEqual(0, add_button.disabled_calls)
            self.assertEqual([], direct_scope_calls)
            self.assertEqual(1, len(received))
            self.assertEqual(1, len(readback_paths))
            self.assertEqual(received[0][0], readback_paths[0])

    async def test_upload_digital_files_reuses_verified_surface_and_skips_global_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            product_paths = [str(source)]
            received = []
            page = _FakeUploadPage(received)
            surface = {"locator": _FakeSurfaceLocator(True)}

            async def wait_readback(_page, expected_paths, **_kwargs):
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_resolve_digital_upload_surface",
                        AsyncMock(),
                    ) as resolve_mock, \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock(return_value=_FakeUploadButton())), \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", side_effect=wait_readback):
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": product_paths},
                    verified_surface=surface,
                )

            self.assertEqual(0, dismiss_mock.await_count)
            resolve_mock.assert_not_awaited()
            self.assertEqual([], page.wait_calls)
            self.assertEqual(1, len(received))

    async def test_verified_surface_continues_after_bounded_add_file_scroll_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            received = []
            page = _FakeUploadPage(received)
            surface_locator = _FakeSurfaceLocator(True)
            surface = {"locator": surface_locator}
            add_button = _FakeUploadButton(
                scroll_error=etsy_auto_post.PlaywrightTimeoutError("scroll timeout")
            )
            add_scope_calls = []

            async def find_add_file(_page, *, container=None, visibility_timeout_ms=None):
                add_scope_calls.append((container, visibility_timeout_ms))
                return add_button

            async def wait_readback(_page, expected_paths, **_kwargs):
                return {
                    "count": len(expected_paths),
                    "names": [Path(path).name for path in expected_paths],
                    "hasRegion": True,
                    "pending": False,
                    "failed": False,
                    "completedCount": len(expected_paths),
                }

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_resolve_digital_upload_surface",
                        AsyncMock(),
                    ) as resolve_mock, \
                    patch.object(
                        etsy_auto_post,
                        "_find_exact_add_file_affordance",
                        side_effect=find_add_file,
                    ), \
                    patch.object(
                        etsy_auto_post,
                        "_wait_for_uploaded_digital_files",
                        side_effect=wait_readback,
                    ):
                await etsy_auto_post.upload_digital_files(
                    page,
                    {"pdf_paths": [str(source)]},
                    verified_surface=surface,
                )

            dismiss_mock.assert_not_awaited()
            resolve_mock.assert_not_awaited()
            self.assertEqual(
                [
                    (
                        surface_locator,
                        etsy_auto_post.DIGITAL_ADD_FILE_VISIBILITY_TIMEOUT_MS,
                    )
                ],
                add_scope_calls,
            )
            self.assertEqual(
                [etsy_auto_post.DIGITAL_ADD_FILE_SCROLL_TIMEOUT_MS],
                add_button.scroll_timeouts,
            )
            self.assertEqual(1, len(received))

    async def test_upload_digital_files_rejected_when_verified_surface_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            product_paths = [str(source)]
            page = _FakeUploadPage([])
            surface_locator = _FakeSurfaceLocator(
                False,
                visible_error=etsy_auto_post.PlaywrightTimeoutError("stale surface"),
            )

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(etsy_auto_post, "_resolve_digital_upload_surface", AsyncMock()) as resolve_mock, \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock()) as add_mock, \
                    patch.object(etsy_auto_post, "_find_scoped_customer_file_inputs", AsyncMock()), \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", AsyncMock()) as readback_mock:
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.upload_digital_files(
                        page,
                        {"pdf_paths": product_paths},
                        verified_surface={"locator": surface_locator},
                    )

            resolve_mock.assert_not_awaited()
            dismiss_mock.assert_not_awaited()
            add_mock.assert_not_called()
            self.assertGreaterEqual(len(surface_locator.visible_calls), 1)
            self.assertEqual(
                3,
                len(surface_locator.visible_calls),
            )
            readback_mock.assert_not_called()

    async def test_upload_digital_files_rejected_when_verified_surface_locator_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            product_paths = [str(source)]
            page = _FakeUploadPage([])

            with patch.object(etsy_auto_post, "_resolve_digital_upload_surface", AsyncMock()) as resolve_mock, \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock()) as add_mock, \
                    patch.object(etsy_auto_post, "_find_scoped_customer_file_inputs", AsyncMock()), \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", AsyncMock()) as readback_mock:
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.upload_digital_files(
                        page,
                        {"pdf_paths": product_paths},
                        verified_surface={"locator": None},
                    )

            resolve_mock.assert_not_awaited()
            add_mock.assert_not_called()
            readback_mock.assert_not_called()

    async def test_upload_digital_files_rejected_when_verified_surface_locator_non_probeable(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "2027-Craft-Seller-Planner.pdf"
            source.write_bytes(b"upload-bytes")
            product_paths = [str(source)]
            page = _FakeUploadPage([])

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()) as dismiss_mock, \
                    patch.object(etsy_auto_post, "_resolve_digital_upload_surface", AsyncMock()) as resolve_mock, \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock()) as add_mock, \
                    patch.object(etsy_auto_post, "_find_scoped_customer_file_inputs", AsyncMock()) as scoped_input_mock, \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", AsyncMock()) as readback_mock:
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.upload_digital_files(
                        page,
                        {"pdf_paths": product_paths},
                        verified_surface={"locator": object()},
                    )

            dismiss_mock.assert_not_awaited()
            resolve_mock.assert_not_awaited()
            add_mock.assert_not_called()
            scoped_input_mock.assert_not_called()
            readback_mock.assert_not_called()

    async def test_chooser_receives_staged_paths_and_product_paths_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "800+ AI Commands for Etsy Product Sales Strategy.pdf"
            source.write_bytes(b"chooser-bytes")
            product_paths = [str(source)]
            received = []
            readback = []
            page = _FakeUploadPage(received)
            surface = {"locator": object()}

            async def wait_readback(_page, expected_paths, **_kwargs):
                readback.append([Path(path) for path in expected_paths])
                return {"count": 1}

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()), \
                    patch.object(etsy_auto_post, "_resolve_digital_upload_surface", AsyncMock(return_value=surface)), \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock(return_value=_FakeUploadButton())), \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", side_effect=wait_readback):
                await etsy_auto_post.upload_digital_files(page, {"pdf_paths": product_paths})

            self.assertEqual(product_paths, [str(source)])
            self.assertEqual(1, len(received))
            self.assertEqual(1, len(readback))
            self.assertEqual(received[0][0], readback[0])
            self.assertEqual([500], page.wait_calls)
            self.assertTrue(received[0][1])
            self.assertTrue(received[0][0][0].name.startswith("800-AI-Commands"))
            self.assertFalse(received[0][0][0].exists())
            self.assertEqual(b"chooser-bytes", source.read_bytes())

    async def test_direct_input_receives_same_staged_paths_and_cleans_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Bộ lệnh AI.pdf"
            source.write_bytes(b"direct-input-bytes")
            product_paths = [str(source)]
            received = []
            readback = []
            page = _FakeUploadPage(received)
            surface = {"locator": object()}

            class DirectInput:
                async def set_input_files(self, paths, timeout=None):
                    received.append(([Path(path) for path in paths], timeout))

            async def wait_readback(_page, expected_paths, **_kwargs):
                readback.append([Path(path) for path in expected_paths])
                raise etsy_auto_post.DigitalListingTypeError("readback failed")

            with patch.object(etsy_auto_post, "dismiss_alerts", AsyncMock()), \
                    patch.object(etsy_auto_post, "_resolve_digital_upload_surface", AsyncMock(return_value=surface)), \
                    patch.object(etsy_auto_post, "_find_exact_add_file_affordance", AsyncMock(return_value=None)), \
                    patch.object(etsy_auto_post, "_find_scoped_customer_file_inputs", AsyncMock(return_value=[(0, DirectInput())])), \
                    patch.object(etsy_auto_post, "_wait_for_uploaded_digital_files", side_effect=wait_readback):
                with self.assertRaises(etsy_auto_post.DigitalListingTypeError):
                    await etsy_auto_post.upload_digital_files(page, {"pdf_paths": product_paths})

            self.assertEqual(1, len(received))
            self.assertEqual(received[0][0], readback[0])
            self.assertFalse(received[0][0][0].exists())
            self.assertEqual(product_paths, [str(source)])


class TestResolveBrowserSessionDir(unittest.TestCase):
    def test_config_override_with_tilde_expands_to_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()

            cfg = {
                "daisyflowdigital": {
                    "browser_session": "~/.etsy_browser_session_daisyflowdigital"
                }
            }

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "daisyflowdigital",
                config=cfg,
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(home / ".etsy_browser_session_daisyflowdigital", resolved)

    def test_temply_falls_back_to_legacy_session_if_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()
            legacy = base / ".browser-session"
            legacy.mkdir()

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "templystudios",
                config={},
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(legacy, resolved)

    def test_unknown_shop_falls_back_to_home_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            base = Path(tmp) / "base"
            base.mkdir()
            (base / ".browser-session").mkdir()

            resolved = etsy_auto_post.resolve_browser_session_dir(
                "newshop123",
                config={},
                base_dir=base,
                home_dir=home,
            )

            self.assertEqual(home / ".etsy_browser_session_newshop123", resolved)


class TestForceDraftFilter(unittest.IsolatedAsyncioTestCase):
    async def test_no_click_when_draft_already_checked_but_disabled(self):
        page = _FakeDraftPage([
            ("draft", True, True),
            ("active", False, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertEqual(0, len(page.check_calls))
        self.assertEqual((etsy_auto_post.DRAFT_LISTINGS_URL, "domcontentloaded"), page.goto_calls[-1])
        self.assertTrue(page.radios[0].checked)

    async def test_multiple_draft_radios_selects_enabled_draft(self):
        page = _FakeDraftPage([
            ("draft", False, True),
            ("draft", False, False),
            ("active", True, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertEqual(1, len(page.check_calls))
        clicked_index, clicked_value, clicked_force, clicked_timeout = page.check_calls[0]
        self.assertEqual(1, clicked_index)
        self.assertEqual("draft", clicked_value)
        self.assertTrue(clicked_force)
        self.assertEqual(etsy_auto_post.DRAFT_FILTER_CHECK_TIMEOUT_MS, clicked_timeout)
        self.assertTrue(page.radios[1].checked)
        self.assertFalse(page.radios[2].checked)

    async def test_active_checked_still_gets_forced_to_draft(self):
        page = _FakeDraftPage([
            ("draft", False, False),
            ("active", True, False),
        ])

        await etsy_auto_post._force_draft_filter(page)

        self.assertTrue(page.radios[0].checked)
        self.assertFalse(page.radios[1].checked)
        self.assertEqual(1, len(page.check_calls))

    async def test_collect_drafts_revalidates_filter_after_grid_and_fails_on_drift(self):
        page = _FakeDraftPage([
            ("draft", True, False),
            ("active", False, False),
        ], cards=[{"id": "111", "title": "A", "sku": "A1", "status": "draft"}])

        async def drift_wait_grid(_page):
            page.set_checked_value("active")

        with patch.object(etsy_auto_post, "_wait_for_draft_grid_stable", side_effect=drift_wait_grid):
            with self.assertRaises(RuntimeError) as ex:
                await etsy_auto_post._collect_draft_cards(page)
        self.assertIn("Lọc Draft không còn chính xác sau khi grid ổn định", str(ex.exception))

    def test_wait_for_draft_grid_stable_default_timeout_is_15000ms(self):
        signature = inspect.signature(etsy_auto_post._wait_for_draft_grid_stable)
        self.assertEqual(15000, signature.parameters["max_wait_ms"].default)

    async def test_wait_for_draft_grid_stable_tolerates_delayed_anchor_ids(self):
        states = [
            {"loading": False, "ids": [], "emptyState": False},
            {"loading": False, "ids": [], "emptyState": True},
            {"loading": True, "ids": [], "emptyState": False},
            {"loading": False, "ids": ["303"], "emptyState": False},
            {"loading": False, "ids": ["303"], "emptyState": False},
        ]

        class _SequenceDraftPage:
            def __init__(self):
                self.calls = 0
                self.wait_calls = []

            async def wait_for_timeout(self, ms: int):
                self.wait_calls.append(ms)

            async def evaluate(self, script: str):
                if "__etsyDraftGridStateMarker" in script:
                    index = min(self.calls, len(states) - 1)
                    self.calls += 1
                    return states[index]
                return {"loading": False, "ids": [], "emptyState": False}

        page = _SequenceDraftPage()

        await etsy_auto_post._wait_for_draft_grid_stable(
            page,
            max_wait_ms=2000,
            pause_ms=0,
            min_settle_ms=0,
            stable_repeats=2,
        )

        self.assertGreaterEqual(page.calls, 4)

    def test_force_draft_filter_uses_check_not_default_click_timeout(self):
        source = inspect.getsource(etsy_auto_post._force_draft_filter)
        self.assertNotIn(".click(", source)
        self.assertIn("check(force=True", source)
        self.assertIn("timeout=", source)
        self.assertNotIn("timeout=30000", source)


class TestDraftUrlBaselineVerification(unittest.IsolatedAsyncioTestCase):
    async def test_empty_present_baseline_is_scanned_for_unique_new_draft(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": [],
        }

        async def _noop_timeout(_ms):
            return None

        page = types.SimpleNamespace(
            url="https://www.etsy.com/your/shops/me/tools/listings",
            wait_for_timeout=_noop_timeout,
        )
        cards = [{
            "id": "303",
            "title": "",
            "sku": "",
            "status": "draft",
        }]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards) as collect_mock:
            discovered = await etsy_auto_post._discover_new_draft_id(page, product)

        self.assertEqual("303", discovered)
        collect_mock.assert_awaited_once_with(page)

    def test_explicit_edit_url_provides_known_listing_id(self):
        self.assertEqual(
            "4558056602",
            etsy_auto_post._known_listing_id_from_edit_url(
                "https://www.etsy.com/your/shops/me/listing-editor/edit/4558056602"
            ),
        )
        self.assertIsNone(
            etsy_auto_post._known_listing_id_from_edit_url(
                "https://www.etsy.com/your/shops/me/tools/listings"
            )
        )

    async def test_redirect_to_listings_discovers_new_draft_outside_baseline_before_signature_match(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101"],
        }
        async def _noop_timeout(_ms):
            return None

        page = types.SimpleNamespace(
            url="https://www.etsy.com/your/shops/me/tools/listings",
            wait_for_timeout=_noop_timeout,
        )
        cards = [
            # Same title/SKU as the target, but this is an old baseline draft.
            {"id": "101", "title": "Brand New Printable", "sku": "NEW-394", "status": "draft"},
            {"id": "303", "title": "Brand New Printable", "sku": "NEW-394", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            discovered = await etsy_auto_post._discover_new_draft_id(page, product)

        self.assertEqual("303", discovered)

    async def test_duplicate_check_records_pre_create_draft_ids(self):
        product = {"title": "Brand New Printable", "sku": "NEW-394"}
        cards = [
            {"id": "101", "title": "Existing One", "sku": "OLD-101", "status": "draft"},
            {"id": "202", "title": "Existing Two", "sku": "OLD-202", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            is_duplicate = await etsy_auto_post.check_duplicate_draft(object(), product)

        self.assertFalse(is_duplicate)
        self.assertEqual(["101", "202"], product["_draft_ids_before_create"])

    async def test_uses_exactly_one_new_draft_id_when_signature_does_not_match(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101", "202"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
            {"id": "202", "title": "Existing Two", "sku": "", "status": "draft"},
            {"id": "303", "title": "", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual("https://www.etsy.com/listing/303", result)

    async def test_rejects_multiple_new_draft_ids(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
            {"id": "202", "title": "", "sku": "", "status": "draft"},
            {"id": "303", "title": "", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual(etsy_auto_post.UNVERIFIED_DRAFT_URL_SENTINEL, result)

    async def test_rejects_zero_new_draft_ids(self):
        product = {
            "title": "Brand New Printable",
            "sku": "NEW-394",
            "_draft_ids_before_create": ["101"],
        }
        async def _noop_timeout(ms): pass
        page = types.SimpleNamespace(url="https://www.etsy.com/your/shops/me/tools/listings", wait_for_timeout=_noop_timeout)
        cards = [
            {"id": "101", "title": "Existing One", "sku": "", "status": "draft"},
        ]

        with patch.object(etsy_auto_post, "_collect_draft_cards", return_value=cards):
            result = await etsy_auto_post.get_newly_created_listing_url(page, product)

        self.assertEqual(etsy_auto_post.UNVERIFIED_DRAFT_URL_SENTINEL, result)


class ImageThumbCountTests(unittest.IsolatedAsyncioTestCase):
    def test_media_selectors_prefer_delete_buttons_not_broad_image_labels(self):
        selectors = etsy_auto_post._get_media_thumbnail_selectors()
        joined = ", ".join(selectors)
        self.assertIn('button[data-testid="image-delete-button"]', joined)
        self.assertNotIn("le-aspect-ratio", joined)
        self.assertNotIn("aria-label*='image'", joined)
        self.assertNotIn('aria-label*="image"', joined)
        self.assertNotIn("aria-label*='thumbnail'", joined)
        fallback = ", ".join(etsy_auto_post._get_media_thumbnail_fallback_selectors())
        self.assertIn("le-aspect-ratio", fallback)

    async def test_count_prefers_delete_buttons_over_inflated_square_tiles(self):
        class _CountLocator:
            def __init__(self, n: int | Exception):
                self._n = n

            async def count(self):
                if isinstance(self._n, Exception):
                    raise self._n
                return self._n

        class _Page:
            def locator(self, sel: str):
                # New Etsy UI: 10 delete buttons, but 11 square tiles
                # (10 photos + 1 empty upload slot). Must return 10, not 11.
                if "image-delete-button" in sel:
                    return _CountLocator(10)
                if "le-aspect-ratio" in sel:
                    return _CountLocator(11)
                if "Remove" in sel or "Delete" in sel:
                    return _CountLocator(Exception("missing"))
                return _CountLocator(0)

        self.assertEqual(10, await etsy_auto_post._count_listing_image_thumbs(_Page()))

    async def test_count_falls_back_to_square_tiles_when_no_delete_buttons(self):
        class _CountLocator:
            def __init__(self, n: int):
                self._n = n

            async def count(self):
                return self._n

        class _Page:
            def locator(self, sel: str):
                if "le-aspect-ratio" in sel:
                    return _CountLocator(7)
                return _CountLocator(0)

        self.assertEqual(7, await etsy_auto_post._count_listing_image_thumbs(_Page()))

    async def test_wait_exact_passes_when_delete_count_matches(self):
        class _CountLocator:
            def __init__(self, n: int):
                self._n = n

            async def count(self):
                return self._n

        class _Page:
            def __init__(self):
                self.wait_calls = 0

            def locator(self, sel: str):
                if "image-delete-button" in sel:
                    return _CountLocator(10)
                return _CountLocator(0)

            async def evaluate(self, _script):
                return {"readable": True, "pending": False, "pendingCount": 0, "evidence": []}

            async def wait_for_timeout(self, _ms: int):
                self.wait_calls += 1

        page = _Page()
        ok = await etsy_auto_post._wait_for_expected_image_count(
            page, expected_count=10, exact=True, timeout_ms=500
        )
        self.assertTrue(ok)
        self.assertEqual(0, page.wait_calls)

    async def test_wait_does_not_report_matching_count_while_image_upload_alert_is_pending(self):
        class _CountLocator:
            async def count(self):
                return 5

        class _Page:
            def __init__(self):
                self.states = [
                    {"readable": True, "pending": True, "pendingCount": 5,
                     "evidence": ["Image is uploading"]},
                    {"readable": True, "pending": True, "pendingCount": 1,
                     "evidence": ["Image is uploading"]},
                    {"readable": True, "pending": False, "pendingCount": 0, "evidence": []},
                ]
                self.wait_calls = 0

            def locator(self, _sel: str):
                return _CountLocator()

            async def evaluate(self, _script):
                return self.states.pop(0) if self.states else {
                    "readable": True, "pending": False, "pendingCount": 0, "evidence": []
                }

            async def wait_for_timeout(self, _ms):
                self.wait_calls += 1

        page = _Page()
        ok = await etsy_auto_post._wait_for_expected_image_count(
            page, expected_count=5, exact=False, timeout_ms=1500
        )

        self.assertTrue(ok)
        self.assertEqual(2, page.wait_calls)

    async def test_upload_until_count_never_topups_a_pending_batch_with_duplicate_paths(self):
        upload_calls: list[list[str]] = []
        counts = [0, 5]

        async def fake_count(_page):
            return counts.pop(0) if counts else 5

        async def fake_upload(_page, paths):
            upload_calls.append(list(paths))

        async def fake_wait(_page, expected_count, exact=False, timeout_ms=180000, log_progress=False):
            return False

        async def fake_pending(_page):
            return {
                "readable": True,
                "pending": True,
                "pendingCount": 5,
                "evidence": ["Image is uploading"],
            }

        paths = [f"img-{i}.png" for i in range(1, 11)]
        with patch.object(etsy_auto_post, "_count_listing_image_thumbs", side_effect=fake_count), \
                patch.object(etsy_auto_post, "_upload_listing_photos", side_effect=fake_upload), \
                patch.object(etsy_auto_post, "_wait_for_expected_image_count", side_effect=fake_wait), \
                patch.object(etsy_auto_post, "_read_pending_photo_uploads", side_effect=fake_pending):
            with self.assertRaisesRegex(RuntimeError, "không upload trùng path"):
                await etsy_auto_post._upload_listing_photos_until_count(
                    object(),
                    paths,
                    expected_total=10,
                    exact=True,
                    batch_size=5,
                )

        self.assertEqual(1, len(upload_calls))
        self.assertEqual(paths[:5], upload_calls[0])

    async def test_upload_until_count_passes_after_batch_settles_without_duplicate_topup(self):
        upload_calls: list[list[str]] = []
        counts = [0, 5, 10, 10]

        async def fake_count(_page):
            return counts.pop(0) if counts else 10

        async def fake_upload(_page, paths):
            upload_calls.append(list(paths))

        async def fake_wait(_page, expected_count, exact=False, timeout_ms=180000, log_progress=False):
            return True

        paths = [f"img-{i}.png" for i in range(1, 11)]
        with patch.object(etsy_auto_post, "_count_listing_image_thumbs", side_effect=fake_count), \
                patch.object(etsy_auto_post, "_upload_listing_photos", side_effect=fake_upload), \
                patch.object(etsy_auto_post, "_wait_for_expected_image_count", side_effect=fake_wait):
            final = await etsy_auto_post._upload_listing_photos_until_count(
                object(),
                paths,
                expected_total=10,
                exact=True,
                batch_size=5,
            )

        self.assertEqual(10, final)
        self.assertEqual(2, len(upload_calls))
        self.assertEqual(paths[:5], upload_calls[0])
        self.assertEqual(paths[5:], upload_calls[1])

    async def test_upload_listing_photos_prefers_exact_add_photos_input_over_add_videos(self):
        selected = []

        class _FileInput:
            def __init__(self, name):
                self.name = name

            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def wait_for(self, *, state=None, timeout=None):
                return None

            async def set_input_files(self, paths, *, timeout=None):
                selected.append((self.name, list(paths), timeout))

        class _FileInputs:
            def __init__(self, items):
                self.items = list(items)

            @property
            def first(self):
                return self.items[0] if self.items else _EmptyFileInput()

            async def count(self):
                return len(self.items)

        class _EmptyFileInput:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

        video_input = _FileInput("Add videos")
        photo_input = _FileInput("Add photos")

        class _Page:
            def locator(self, selector):
                if selector == (
                    'xpath=//input[@name="listing-media-upload" and '
                    '@id=//label[@aria-label="Add photos"]/@for]'
                ):
                    return _FileInputs([photo_input])
                if selector == 'input[name="listing-media-upload"]':
                    return _FileInputs([video_input, photo_input])
                return _FileInputs([])

        await etsy_auto_post._upload_listing_photos(_Page(), ["photo-1.jpg"])

        self.assertEqual([("Add photos", ["photo-1.jpg"], 60000)], selected)


class TestShopManagerNavigation(unittest.IsolatedAsyncioTestCase):
    class _Page:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.goto_calls = []
            self.wait_calls = []

        async def goto(self, url, *, wait_until=None, timeout=None):
            self.goto_calls.append((url, wait_until, timeout))
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        async def wait_for_timeout(self, ms):
            self.wait_calls.append(ms)

    async def test_success_uses_explicit_timeout_and_exact_shop_manager_url(self):
        page = self._Page([None])

        await etsy_auto_post._navigate_to_shop_manager(page)

        self.assertEqual(
            [(
                etsy_auto_post.SHOP_MANAGER_LISTINGS_URL,
                "domcontentloaded",
                etsy_auto_post.SHOP_MANAGER_NAVIGATION_TIMEOUT_MS,
            )],
            page.goto_calls,
        )
        self.assertEqual([], page.wait_calls)

    async def test_transient_timeout_retries_same_page_and_then_succeeds(self):
        page = self._Page([
            etsy_auto_post.PlaywrightTimeoutError("transient navigation timeout"),
            None,
        ])

        await etsy_auto_post._navigate_to_shop_manager(page)

        self.assertEqual(2, len(page.goto_calls))
        self.assertEqual(
            [etsy_auto_post.SHOP_MANAGER_NAVIGATION_RETRY_DELAY_MS],
            page.wait_calls,
        )
        self.assertTrue(all(call[0] == etsy_auto_post.SHOP_MANAGER_LISTINGS_URL for call in page.goto_calls))
        self.assertTrue(all(call[2] == etsy_auto_post.SHOP_MANAGER_NAVIGATION_TIMEOUT_MS for call in page.goto_calls))

    async def test_timeout_exhaustion_fails_closed(self):
        timeout = etsy_auto_post.PlaywrightTimeoutError("persistent navigation timeout")
        page = self._Page([timeout, etsy_auto_post.PlaywrightTimeoutError("persistent navigation timeout")])

        with self.assertRaises(RuntimeError) as raised:
            await etsy_auto_post._navigate_to_shop_manager(page)

        self.assertIn("Không thể vào Shop Manager", str(raised.exception))
        self.assertEqual(etsy_auto_post.SHOP_MANAGER_NAVIGATION_ATTEMPTS, len(page.goto_calls))
        self.assertEqual(
            [etsy_auto_post.SHOP_MANAGER_NAVIGATION_RETRY_DELAY_MS],
            page.wait_calls,
        )

    async def test_non_timeout_error_propagates_without_retry(self):
        error = RuntimeError("CDP/page closed")
        page = self._Page([error])

        with self.assertRaises(RuntimeError) as raised:
            await etsy_auto_post._navigate_to_shop_manager(page)

        self.assertIs(raised.exception, error)
        self.assertEqual(1, len(page.goto_calls))
        self.assertEqual([], page.wait_calls)


if __name__ == "__main__":
    unittest.main()
