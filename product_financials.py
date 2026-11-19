"""Per-shop product cost storage and deterministic Etsy profit estimates.

Financial inputs are deliberately kept outside the SEO workbook and listing
payloads. All arithmetic uses Decimal; API-facing monetary values are strings.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any


class FinancialStoreError(ValueError):
    """Invalid or unreadable financial data."""


class FinancialConflict(FinancialStoreError):
    """Optimistic version check failed."""


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_CURRENCIES = {"USD": 2, "VND": 0, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "JPY": 0}
_thread_lock = threading.RLock()
_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_OPTIONAL_ZERO_USD_COSTS = {"packaging", "labor", "other", "etsy_ads"}


def _safe_id(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not _ID_RE.fullmatch(result) or result in {".", ".."}:
        raise FinancialStoreError(f"Invalid {label}")
    return result


def _store_path(base_dir: str | Path, shop_id: str) -> Path:
    shop = _safe_id(shop_id, "shop id")
    base = Path(base_dir).resolve()
    shops_path = base / "shops"
    shop_path = shops_path / shop
    if shops_path.is_symlink() or shop_path.is_symlink():
        raise FinancialStoreError("Shop data directories cannot be symlinks")
    if shops_path.exists() and not shops_path.is_dir():
        raise FinancialStoreError("Shop data root must be a directory")
    if shop_path.exists() and not shop_path.is_dir():
        raise FinancialStoreError("Shop data path must be a directory")
    root = shop_path.resolve()
    shops_root = shops_path.resolve()
    if root.parent != shops_root:
        raise FinancialStoreError("Shop path escapes shop data root")
    return root / "product_financials.json"


def _decimal(value: object, label: str, *, optional: bool = False) -> Decimal | None:
    if value is None or value == "":
        if optional:
            return None
        raise FinancialStoreError(f"Missing {label}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FinancialStoreError(f"Invalid {label}") from exc
    if not number.is_finite():
        raise FinancialStoreError(f"Invalid {label}")
    return number


def _money(value: Decimal, currency: str) -> str:
    places = _CURRENCIES.get(currency, 2)
    quantum = Decimal(1).scaleb(-places)
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _blank_store(shop_id: str) -> dict:
    return {"version": 0, "shop_id": shop_id, "settings": {}, "products": {}}


def read_store(base_dir: str | Path, shop_id: str) -> dict:
    path = _store_path(base_dir, shop_id)
    if path.is_symlink():
        raise FinancialStoreError("Financial store cannot be a symlink")
    if not path.exists():
        return _blank_store(shop_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinancialStoreError("Financial store is unreadable or corrupt") from exc
    if (not isinstance(data, dict) or data.get("shop_id") != shop_id
            or type(data.get("version")) is not int or data.get("version") < 0
            or not isinstance(data.get("settings"), dict)
            or not isinstance(data.get("products"), dict)):
        raise FinancialStoreError("Financial store has an invalid structure")
    for folder in data["products"]:
        _safe_id(folder, "product folder")
        try:
            validate_financials(data["products"][folder])
        except FinancialStoreError as exc:
            raise FinancialStoreError("Financial store contains invalid product data") from exc
    try:
        validate_settings(data["settings"])
    except FinancialStoreError as exc:
        raise FinancialStoreError("Financial store contains invalid shop settings") from exc
    return data


@contextmanager
def product_financials_lock(base_dir: str | Path, shop_id: str):
    """Hold the cross-process exclusive lock for lifecycle transactions."""
    path = _store_path(base_dir, shop_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _thread_lock:
        if path.is_symlink() or lock_path.is_symlink():
            raise FinancialStoreError("Financial store path cannot be a symlink")
        if path.exists() and not path.is_file():
            raise FinancialStoreError("Financial store must be a regular file")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def write_store_locked(base_dir: str | Path, shop_id: str, store: dict) -> dict:
    """Atomically persist a store while caller holds product_financials_lock."""
    if (not isinstance(store, dict) or store.get("shop_id") != shop_id
            or type(store.get("version")) is not int or store.get("version") < 0
            or not isinstance(store.get("settings"), dict)
            or not isinstance(store.get("products"), dict)):
        raise FinancialStoreError("Financial store has an invalid structure")
    for folder in store["products"]:
        _safe_id(folder, "product folder")
        validate_financials(store["products"][folder])
    validate_settings(store["settings"])
    path = _store_path(base_dir, shop_id)
    temp_fd, temp_name = tempfile.mkstemp(prefix=".product_financials-", dir=path.parent)
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return deepcopy(store)


def _write_store(base_dir: str | Path, shop_id: str, mutate) -> dict:
    with product_financials_lock(base_dir, shop_id):
        store = read_store(base_dir, shop_id)
        updated = mutate(store)
        updated["version"] = store["version"] + 1
        return write_store_locked(base_dir, shop_id, updated)


def _expected(store: dict, expected_version: object) -> None:
    if type(expected_version) is not int or expected_version < 0:
        raise FinancialStoreError("expected_version must be a non-negative integer")
    expected = expected_version
    if expected != store["version"]:
        raise FinancialConflict(f"Version conflict: current version is {store['version']}")


def validate_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise FinancialStoreError("settings must be an object")
    for key in ("listing_currency", "payout_currency"):
        value = settings.get(key)
        if value not in (None, "") and (not isinstance(value, str) or value != value.upper() or value not in _CURRENCIES):
            raise FinancialStoreError(f"Invalid {key}; use an uppercase currency code")
    for key in ("processing_percent", "transaction_percent", "regulatory_percent", "conversion_percent", "fee_vat_percent"):
        if settings.get(key) not in (None, ""):
            value = _decimal(settings[key], key)
            if value < 0 or value > 100:
                raise FinancialStoreError(f"{key} must be between 0 and 100")
    rates = settings.get("fx_vnd_per_unit", {})
    if not isinstance(rates, dict):
        raise FinancialStoreError("fx_vnd_per_unit must be an object")
    for currency, value in rates.items():
        if not isinstance(currency, str) or currency != currency.upper() or currency not in _CURRENCIES or _decimal(value, f"FX {currency}") <= 0:
            raise FinancialStoreError(f"Invalid FX rate for {currency}")
    for key in ("processing_fixed", "listing_fee"):
        amount = settings.get(key)
        if amount is not None:
            if not isinstance(amount, dict):
                raise FinancialStoreError(f"{key} must be an object")
            value = _decimal(amount.get("amount"), key, optional=True)
            currency = amount.get("currency")
            if value is not None and (value < 0 or not isinstance(currency, str)
                                      or currency != currency.upper() or currency not in _CURRENCIES):
                raise FinancialStoreError(f"Invalid {key}")
    tax_mode = settings.get("tax_mode")
    if tax_mode not in (None, "", "unknown", "vn_individual", "vn_business", "none"):
        raise FinancialStoreError("Invalid tax_mode")
    country = settings.get("bank_country")
    if country not in (None, "") and (not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country)):
        raise FinancialStoreError("bank_country must be an uppercase two-letter country code")
    if "confirmed" in settings and type(settings["confirmed"]) is not bool:
        raise FinancialStoreError("confirmed must be a boolean")
    return deepcopy(settings)


def _blank_cost_amount(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _validate_cost(item: object, label: str, *, blank_as_zero: bool = False) -> None:
    if item is None:
        return
    if not isinstance(item, dict):
        raise FinancialStoreError(f"{label} must be an object")
    raw_amount = item.get("amount")
    if blank_as_zero and _blank_cost_amount(raw_amount):
        return
    amount = _decimal(raw_amount, label, optional=True)
    currency = item.get("currency")
    if amount is not None and amount < 0:
        raise FinancialStoreError(f"{label} cannot be negative")
    if amount is not None and (not isinstance(currency, str) or currency != currency.upper()
                               or currency not in _CURRENCIES):
        raise FinancialStoreError(f"Invalid currency for {label}")


def validate_financials(financials: dict) -> dict:
    if not isinstance(financials, dict):
        raise FinancialStoreError("financials must be an object")
    listing_type = financials.get("listing_type")
    if listing_type not in (None, "", "physical", "digital"):
        raise FinancialStoreError("listing_type must be physical or digital")
    cost_values = financials.get("costs") or {}
    if not isinstance(cost_values, dict):
        raise FinancialStoreError("costs must be an object")
    for name, item in cost_values.items():
        _validate_cost(item, f"Chi phí {name}", blank_as_zero=name in _OPTIONAL_ZERO_USD_COSTS)
    discount = _decimal(financials.get("discount_percent", 0), "discount_percent")
    if discount < 0 or discount >= 100:
        raise FinancialStoreError("discount_percent must be in [0, 100)")
    target = _decimal(financials.get("target_margin_percent"), "target_margin_percent", optional=True)
    if target is not None and (target < 0 or target >= 100):
        raise FinancialStoreError("target_margin_percent must be in [0, 100)")
    scenarios = financials.get("scenarios", [])
    if not isinstance(scenarios, list) or len(scenarios) > 100:
        raise FinancialStoreError("scenarios must be a list with at most 100 entries")
    seen = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise FinancialStoreError("Every scenario must be an object")
        scenario_id = str(scenario.get("id") or "")
        if not scenario_id or scenario_id in seen:
            raise FinancialStoreError("Scenario ids must be present and unique")
        seen.add(scenario_id)
        country = scenario.get("country")
        if country not in (None, "") and (not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country)):
            raise FinancialStoreError("scenario country must be an uppercase two-letter country code")
        for key in ("freight", "insurance", "buyer_shipping", "buyer_tax"):
            _validate_cost(scenario.get(key), key)
        offsite = _decimal(scenario.get("offsite_percent", 0), "offsite_percent")
        if offsite not in (Decimal(0), Decimal(12), Decimal(15)):
            raise FinancialStoreError("offsite_percent must be 0, 12, or 15")
        freight_discount = _decimal(scenario.get("freight_discount_percent", 0), "freight_discount_percent")
        if freight_discount < 0 or freight_discount > 100:
            raise FinancialStoreError("freight_discount_percent must be in [0, 100]")
    representative_id = financials.get("representative_id")
    if representative_id not in (None, "") and representative_id not in seen:
        raise FinancialStoreError("representative_id must refer to an existing scenario")
    normalized = deepcopy(financials)
    normalized_costs = deepcopy(cost_values)
    for name in _OPTIONAL_ZERO_USD_COSTS:
        item = normalized_costs.get(name)
        if item is None or (isinstance(item, dict) and _blank_cost_amount(item.get("amount"))):
            normalized_costs[name] = {"amount": "0", "currency": "USD"}
    normalized["costs"] = normalized_costs
    return normalized


def save_settings(base_dir: str | Path, shop_id: str, settings: dict, expected_version: object) -> dict:
    settings = validate_settings(settings)
    def apply(store):
        _expected(store, expected_version)
        store["settings"] = deepcopy(settings)
        return store
    return _write_store(base_dir, shop_id, apply)


def save_product(base_dir: str | Path, shop_id: str, folder: str, financials: dict,
                 expected_version: object) -> dict:
    product = _safe_id(folder, "product folder")
    financials = validate_financials(financials)
    def apply(store):
        _expected(store, expected_version)
        store["products"][product] = deepcopy(financials)
        return store
    return _write_store(base_dir, shop_id, apply)


def assert_merge_compatible(base_dir: str | Path, shop_id: str, folders: list[str]) -> None:
    store = read_store(base_dir, shop_id)
    present = [store["products"].get(_safe_id(folder, "product folder")) for folder in folders]
    populated = [item for item in present if item is not None]
    if len(populated) > 1 and any(item != populated[0] for item in populated[1:]):
        raise FinancialConflict("Folders have different financial data; resolve before merging")


def move_product(base_dir: str | Path, shop_id: str, source: str, target: str) -> dict:
    src, dst = _safe_id(source, "source folder"), _safe_id(target, "target folder")
    if src == dst:
        return read_store(base_dir, shop_id)
    def apply(store):
        if src in store["products"]:
            if dst in store["products"] and store["products"][dst] != store["products"][src]:
                raise FinancialConflict("Target folder already has different financial data")
            store["products"][dst] = store["products"].pop(src)
        return store
    return _write_store(base_dir, shop_id, apply)


def _fx(settings: dict, currency: str, listing: str, missing: list[str]) -> Decimal | None:
    if currency == listing:
        return Decimal(1)
    rates = settings.get("fx_vnd_per_unit") or {}
    try:
        vnd_per_currency = _decimal(rates.get(currency), f"FX {currency}")
        vnd_per_listing = _decimal(rates.get(listing), f"FX {listing}")
        if vnd_per_currency <= 0 or vnd_per_listing <= 0:
            raise FinancialStoreError("FX rates must be positive")
    except FinancialStoreError:
        missing.append(f"Tỷ giá {currency}/{listing}")
        return None
    return vnd_per_currency / vnd_per_listing


def _convert(amount: object, currency: object, settings: dict, listing: str,
             missing: list[str], label: str) -> Decimal | None:
    value = _decimal(amount, label, optional=True)
    if value is None:
        missing.append(label)
        return None
    curr = str(currency or "").upper()
    if not curr:
        missing.append(f"Tiền tệ {label}")
        return None
    rate = _fx(settings, curr, listing, missing)
    return value * rate if rate is not None else None


def _optional_cost(financials: dict, key: str, settings: dict, listing: str,
                   missing: list[str]) -> Decimal | None:
    item = (financials.get("costs") or {}).get(key)
    if not isinstance(item, dict):
        missing.append(f"Chi phí {key}")
        return None
    return _convert(item.get("amount"), item.get("currency"), settings, listing,
                    missing, f"Chi phí {key}")


def _fee(settings: dict, name: str, base: Decimal, missing: list[str]) -> Decimal:
    try:
        rate = _decimal(settings.get(name), name, optional=True)
    except FinancialStoreError:
        rate = None
    if rate is None:
        missing.append(f"Cấu hình {name}")
        return _ZERO
    if rate < 0 or rate > 100:
        missing.append(f"Cấu hình {name} không hợp lệ")
        return _ZERO
    return base * rate / Decimal(100)


def _settings_missing(settings: dict, listing: str, missing: list[str]) -> None:
    for key in ("payout_currency", "bank_country", "fx_date"):
        if not settings.get(key):
            missing.append(f"Cấu hình {key}")
    if settings.get("tax_mode", "unknown") not in {"unknown", "vn_individual", "vn_business", "none"}:
        missing.append("Chế độ thuế không hợp lệ")
    if settings.get("tax_mode", "unknown") == "unknown":
        missing.append("Xác nhận chế độ thuế")
    if settings.get("payout_currency") and settings["payout_currency"] != listing:
        _fx(settings, settings["payout_currency"], listing, missing)
    for currency in ("USD", "VND"):
        _fx(settings, listing, currency, missing)
    fixed = settings.get("processing_fixed") or {}
    if not isinstance(fixed, dict):
        missing.append("Phí processing cố định")
    else:
        _convert(fixed.get("amount"), fixed.get("currency"), settings, listing,
                 missing, "Phí processing cố định")
    listing_fee = settings.get("listing_fee") or {}
    if not isinstance(listing_fee, dict):
        missing.append("Phí listing")
    else:
        _convert(listing_fee.get("amount"), listing_fee.get("currency"), settings,
                 listing, missing, "Phí listing")
    for key in ("transaction_percent", "processing_percent", "regulatory_percent", "conversion_percent", "fee_vat_percent"):
        _fee(settings, key, _ZERO, missing)


def _price_for_target(measure, margin: Decimal, currency: str) -> Decimal | None:
    """Solve using the same exact fee/tax evaluator used by the displayed result."""
    if margin < 0 or margin >= 100:
        return None
    low, high = Decimal(0), Decimal(1)
    for _ in range(256):
        row = measure(high)
        if row["profit"] >= row["revenue"] * margin / 100:
            break
        high *= 2
    else:
        return None
    for _ in range(160):
        mid = (low + high) / 2
        row = measure(mid)
        if row["profit"] >= row["revenue"] * margin / 100:
            high = mid
        else:
            low = mid
    quantum = Decimal(1).scaleb(-_CURRENCIES.get(currency, 2))
    return high.quantize(quantum, rounding=ROUND_CEILING)


def _vn_withholding(settings: dict, scenario: dict, listing_type: str, item_revenue: Decimal,
                    shipping_revenue: Decimal, revenue: Decimal, missing: list[str]) -> Decimal:
    """Vietnam seller withholding estimate by entity, listing type, and buyer country."""
    tax_mode = settings.get("tax_mode")
    if tax_mode != "vn_individual":
        # Valid ERC business accounts do not receive the individual withholding.
        return _ZERO
    buyer_country = scenario.get("country")
    if not isinstance(buyer_country, str) or not re.fullmatch(r"[A-Z]{2}", buyer_country):
        missing.append("Quốc gia người mua để tính thuế VN")
        return _ZERO
    domestic = buyer_country == "VN"
    if listing_type == "digital":
        return revenue * (Decimal("0.07") if domestic else Decimal("0.02"))
    if domestic:
        return item_revenue * Decimal("0.015") + shipping_revenue * Decimal("0.045")
    return item_revenue * Decimal("0.005") + shipping_revenue * Decimal("0.015")


def calculate(financials: dict, settings: dict, price: object) -> dict:
    if not isinstance(financials, dict) or not isinstance(settings, dict):
        raise FinancialStoreError("financials and settings must be objects")
    financials = validate_financials(financials)
    settings = validate_settings(settings)
    listing = str(settings.get("listing_currency") or "").upper()
    if not listing:
        return {"scenarios": [], "currency": "", "fx_date": settings.get("fx_date"), "missing": ["Cấu hình listing_currency"]}
    try:
        gross_price = _decimal(price, "Giá bán")
    except FinancialStoreError:
        return {"scenarios": [], "currency": listing, "fx_date": settings.get("fx_date"), "missing": ["Giá bán"]}
    if gross_price < 0:
        raise FinancialStoreError("Giá bán không được âm")
    missing_settings: list[str] = []
    _settings_missing(settings, listing, missing_settings)
    base_missing = list(dict.fromkeys(missing_settings))
    cost_missing: list[str] = []
    costs = {name: _optional_cost(financials, name, settings, listing, cost_missing)
             for name in ("cogs", "packaging", "labor", "other", "etsy_ads")}
    for name, amount in costs.items():
        if amount is None:
            cost_missing.append(f"Chi phí {name}")
        elif amount < 0:
            cost_missing.append(f"Chi phí {name} không được âm")
    scenarios = financials.get("scenarios") or []
    if not isinstance(scenarios, list):
        raise FinancialStoreError("scenarios must be a list")
    outputs = []
    discount = _decimal(financials.get("discount_percent", 0), "discount_percent")
    if discount < 0 or discount >= 100:
        raise FinancialStoreError("discount_percent must be in [0, 100)")
    configuration_confirmed = settings.get("confirmed") is True
    provisional_warning = None if configuration_confirmed else (
        "Ước tính theo các giả định đã nhập; cấu hình phí/thuế shop chưa được xác nhận, "
        "nên đây chưa phải lợi nhuận ròng đã xác minh."
    )
    listing_type = str(financials.get("listing_type") or "").lower()
    if listing_type not in {"physical", "digital"}:
        cost_missing.append("Loại sản phẩm physical/digital")
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        missing = list(base_missing) + list(cost_missing)
        freight_cfg = scenario.get("freight") or {}
        insurance_cfg = scenario.get("insurance") or {"amount": "0", "currency": listing}
        buyer_shipping_cfg = scenario.get("buyer_shipping") or {"amount": "0", "currency": listing}
        ship_cost = _convert(freight_cfg.get("amount"), freight_cfg.get("currency"), settings, listing, missing, "Cước vận chuyển")
        freight_discount = _decimal(scenario.get("freight_discount_percent", 0), "freight_discount_percent")
        insurance = _convert(insurance_cfg.get("amount"), insurance_cfg.get("currency"), settings, listing, missing, "Bảo hiểm/phụ phí")
        buyer_shipping = _convert(buyer_shipping_cfg.get("amount"), buyer_shipping_cfg.get("currency"), settings, listing, missing, "Ship thu khách")
        buyer_tax_cfg = scenario.get("buyer_tax") or {"amount": "0", "currency": listing}
        buyer_tax = _convert(buyer_tax_cfg.get("amount"), buyer_tax_cfg.get("currency"), settings, listing, missing, "Thuế người mua")
        if scenario.get("free_shipping") is True:
            buyer_shipping = Decimal(0)
        if freight_discount < 0 or freight_discount > 100:
            raise FinancialStoreError("freight_discount_percent must be in [0, 100]")
        for value, label in ((ship_cost, "Cước vận chuyển"), (insurance, "Bảo hiểm/phụ phí"),
                             (buyer_shipping, "Ship thu khách"), (buyer_tax, "Thuế người mua")):
            if value is not None and value < 0:
                raise FinancialStoreError(f"{label} không được âm")
        freight_net = (ship_cost or _ZERO) * (1 - freight_discount / 100)
        buyer_ship = buyer_shipping or _ZERO
        tax_paid_by_buyer = buyer_tax or _ZERO
        revenue = gross_price * (1 - discount / 100) + buyer_ship
        processing_base = revenue + tax_paid_by_buyer
        transaction = _fee(settings, "transaction_percent", revenue, missing)
        processing = _fee(settings, "processing_percent", processing_base, missing)
        fixed_cfg = settings.get("processing_fixed") or {}
        processing_fixed = _convert(fixed_cfg.get("amount"), fixed_cfg.get("currency"), settings, listing, missing, "Phí processing cố định") if isinstance(fixed_cfg, dict) else None
        regulatory = _fee(settings, "regulatory_percent", revenue, missing)
        listing_cfg = settings.get("listing_fee") or {}
        listing_cost = _convert(listing_cfg.get("amount"), listing_cfg.get("currency"), settings, listing, missing, "Phí listing") if isinstance(listing_cfg, dict) else None
        conversion = _fee(settings, "conversion_percent", revenue, missing) if settings.get("payout_currency") != listing else _ZERO
        offsite_rate = _decimal(scenario.get("offsite_percent", 0), "offsite_percent")
        if offsite_rate not in (Decimal(0), Decimal(12), Decimal(15)):
            missing.append("Offsite Ads phải là 0%, 12% hoặc 15%")
        cap = Decimal(100) * (_fx(settings, "USD", listing, missing) or Decimal(0))
        offsite = min(revenue * offsite_rate / 100, cap)
        fee_subtotal = transaction + processing + (processing_fixed or _ZERO) + regulatory + (listing_cost or _ZERO) + conversion + offsite
        fee_vat = _fee(settings, "fee_vat_percent", fee_subtotal, missing)
        item_revenue = gross_price * (1 - discount / 100)
        tax = _vn_withholding(settings, scenario, listing_type, item_revenue,
                              buyer_ship, revenue, missing)
        ads = costs["etsy_ads"] or _ZERO
        other_costs = sum((costs[k] or _ZERO) for k in ("cogs", "packaging", "labor", "other"))
        operating = other_costs + ads + freight_net + (insurance or _ZERO)
        total_etsy_fees = fee_subtotal + fee_vat
        proceeds = revenue - total_etsy_fees
        profit = proceeds - tax - operating
        offsite_comparisons = []
        for rate in (Decimal(0), Decimal(12), Decimal(15)):
            ad_fee = min(revenue * rate / 100, cap)
            comparison_fees = fee_subtotal - offsite + ad_fee
            comparison_fees += _fee(settings, "fee_vat_percent", fee_subtotal - offsite + ad_fee, [])
            offsite_comparisons.append({"offsite_percent": str(rate),
                                        "profit": _money(revenue - comparison_fees - tax - operating, listing) if not missing else None})
        target_margin = _decimal(financials.get("target_margin_percent"), "target_margin_percent", optional=True)
        scenario_missing = list(dict.fromkeys(missing))
        complete = not scenario_missing
        def measure(candidate: Decimal) -> dict:
            candidate_item = candidate * (1 - discount / 100)
            candidate_revenue = candidate_item + buyer_ship
            candidate_transaction = _fee(settings, "transaction_percent", candidate_revenue, [])
            candidate_processing = _fee(settings, "processing_percent", candidate_revenue + tax_paid_by_buyer, [])
            candidate_regulatory = _fee(settings, "regulatory_percent", candidate_revenue, [])
            candidate_conversion = _fee(settings, "conversion_percent", candidate_revenue, []) if settings.get("payout_currency") != listing else _ZERO
            candidate_offsite = min(candidate_revenue * offsite_rate / 100, cap)
            candidate_subtotal = candidate_transaction + candidate_processing + (processing_fixed or _ZERO) + candidate_regulatory + (listing_cost or _ZERO) + candidate_conversion + candidate_offsite
            candidate_fees = candidate_subtotal + _fee(settings, "fee_vat_percent", candidate_subtotal, [])
            candidate_tax = _vn_withholding(settings, scenario, listing_type, candidate_item,
                                             buyer_ship, candidate_revenue, [])
            candidate_profit = candidate_revenue - candidate_fees - candidate_tax - operating
            return {"revenue": candidate_revenue, "profit": candidate_profit}
        break_even = _price_for_target(measure, Decimal(0), listing) if complete else None
        target_price = _price_for_target(measure, target_margin, listing) if complete and target_margin is not None else None
        scenario_missing = list(dict.fromkeys(scenario_missing))
        complete = not scenario_missing
        status = "incomplete" if not complete else ("complete" if configuration_confirmed else "provisional")
        outputs.append({
            "id": str(scenario.get("id", "")), "name": str(scenario.get("name", "")),
            "country": str(scenario.get("country", "")), "status": status,
            "warning": provisional_warning if status == "provisional" else None,
            "missing": scenario_missing, "currency": listing,
            "revenue": _money(revenue, listing) if complete else None,
            "etsy_fees": _money(total_etsy_fees, listing) if complete else None,
            "taxes": _money(tax, listing) if complete else None,
            "operating_costs": _money(operating, listing) if complete else None,
            "proceeds": _money(proceeds, listing) if complete else None,
            "profit": _money(profit, listing) if complete else None,
            "profit_usd": _money(profit * (_fx(settings, listing, "USD", []) or Decimal(0)), "USD") if complete else None,
            "profit_vnd": _money(profit * (_fx(settings, listing, "VND", []) or Decimal(0)), "VND") if complete else None,
            "margin_percent": _money(profit / revenue * 100 if revenue else _ZERO, "USD") if complete else None,
            "break_even_price": _money(break_even, listing) if break_even is not None else None,
            "target_price": _money(target_price, listing) if target_price is not None else None,
            "fees": {"transaction": _money(transaction, listing) if complete else None, "processing": _money(processing, listing) if complete else None,
                     "processing_fixed": _money(processing_fixed or _ZERO, listing), "regulatory": _money(regulatory, listing),
                     "listing": _money(listing_cost or _ZERO, listing), "conversion": _money(conversion, listing),
                     "offsite": _money(offsite, listing), "fee_vat": _money(fee_vat, listing)},
            "offsite_comparisons": offsite_comparisons if complete else [],
        })
    return {"scenarios": outputs, "currency": listing, "fx_date": settings.get("fx_date"),
            "configuration_source": settings.get("source"),
            "configuration_checked_at": settings.get("checked_at"),
            "missing": list(dict.fromkeys(base_missing + cost_missing))}
