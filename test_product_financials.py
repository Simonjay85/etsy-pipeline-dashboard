from decimal import Decimal

import pytest

import product_financials as pf


def config(**overrides):
    value = {
        "listing_currency": "USD", "payout_currency": "USD", "bank_country": "US",
        "fx_vnd_per_unit": {"USD": "25000", "VND": "1"}, "fx_date": "2026-10-01",
        "processing_percent": "4.5", "processing_fixed": {"amount": "0.30", "currency": "USD"},
        "transaction_percent": "6.5", "regulatory_percent": "0", "conversion_percent": "0",
        "listing_fee": {"amount": "0.20", "currency": "USD"}, "fee_vat_percent": "0",
        "tax_mode": "none", "confirmed": True,
    }
    value.update(overrides)
    return value


def complete_input(**fields):
    value = {
        "listing_type": "physical", "discount_percent": "0", "target_margin_percent": None,
        "costs": {name: {"amount": "0", "currency": "USD"}
                  for name in ("cogs", "packaging", "labor", "other", "etsy_ads")},
        "scenarios": [{"id": "us", "name": "US", "country": "US",
                       "freight": {"amount": "10", "currency": "USD"},
                       "freight_discount_percent": "0",
                       "insurance": {"amount": "0", "currency": "USD"},
                       "buyer_shipping": {"amount": "10", "currency": "USD"},
                       "free_shipping": False, "buyer_tax": {"amount": "10", "currency": "USD"},
                       "offsite_percent": "0"}],
    }
    value.update(fields)
    return value


def test_fee_bases_costs_and_shipping_reduction():
    financials = complete_input()
    financials["costs"]["cogs"]["amount"] = "20"
    financials["scenarios"][0]["freight_discount_percent"] = "50"
    row = pf.calculate(financials, config(), "100")["scenarios"][0]
    assert row["status"] == "complete"
    assert row["revenue"] == "110.00"
    assert row["fees"]["processing"] == "5.40"  # includes the $10 buyer tax
    assert row["fees"]["transaction"] == "7.15"  # excludes buyer tax
    assert row["operating_costs"] == "25.00"  # $20 COGS + discounted $5 freight
    assert row["profit"] == "71.95"


def test_free_shipping_and_offsite_cap_are_separate():
    financials = complete_input()
    scenario = financials["scenarios"][0]
    scenario.update({"free_shipping": True, "offsite_percent": "15"})
    row = pf.calculate(financials, config(), "1000")["scenarios"][0]
    assert row["revenue"] == "1000.00"
    assert row["operating_costs"] == "10.00"  # seller still pays freight
    assert row["fees"]["offsite"] == "100.00"  # $100 USD order cap
    assert {r["offsite_percent"] for r in row["offsite_comparisons"]} == {"0", "12", "15"}


def test_fx_discount_tax_and_target_price_use_same_calculation():
    financials = complete_input(discount_percent="10", target_margin_percent="20")
    financials["listing_type"] = "digital"
    financials["costs"]["cogs"] = {"amount": "500000", "currency": "VND"}
    financials["scenarios"][0]["freight"] = {"amount": "0", "currency": "USD"}
    financials["scenarios"][0]["buyer_shipping"] = {"amount": "25000", "currency": "VND"}
    financials["scenarios"][0]["buyer_tax"] = {"amount": "0", "currency": "USD"}
    vn = config(bank_country="VN", tax_mode="vn_individual")
    result = pf.calculate(financials, vn, "100")["scenarios"][0]
    assert result["status"] == "complete"
    assert result["operating_costs"] == "20.00"
    assert result["taxes"] == "1.82"  # 2% of discounted digital sale incl. any shipping
    assert Decimal(result["profit_vnd"]) == Decimal(result["profit"]) * 25000
    target = Decimal(result["target_price"])
    at_target = pf.calculate(financials, vn, str(target))["scenarios"][0]
    assert Decimal(at_target["margin_percent"]) >= Decimal("20")
    break_even = Decimal(result["break_even_price"])
    at_break_even = pf.calculate(financials, vn, str(break_even))["scenarios"][0]
    assert abs(Decimal(at_break_even["profit"])) < Decimal("0.02")


@pytest.mark.parametrize(
    ("tax_mode", "listing_type", "buyer_country", "expected"),
    [
        ("vn_individual", "physical", "US", "0.65"),
        ("vn_individual", "physical", "VN", "1.95"),
        ("vn_individual", "digital", "US", "2.20"),
        ("vn_individual", "digital", "VN", "7.70"),
        ("vn_business", "physical", "VN", "0.00"),
        ("vn_business", "digital", "US", "0.00"),
    ],
)
def test_vietnam_tax_matrix(tax_mode, listing_type, buyer_country, expected):
    financials = complete_input()
    financials["listing_type"] = listing_type
    financials["scenarios"][0]["country"] = buyer_country
    row = pf.calculate(financials, config(bank_country="VN", tax_mode=tax_mode), "100")["scenarios"][0]
    assert row["status"] == "complete"
    assert row["taxes"] == expected


def test_vietnam_individual_requires_buyer_country_and_target_rounds_up():
    financials = complete_input(target_margin_percent="25")
    financials["scenarios"][0]["country"] = ""
    missing = pf.calculate(financials, config(bank_country="VN", tax_mode="vn_individual"), "100")["scenarios"][0]
    assert missing["status"] == "incomplete"
    assert "Quốc gia người mua để tính thuế VN" in missing["missing"]
    assert missing["profit"] is None

    financials["scenarios"][0]["country"] = "US"
    row = pf.calculate(financials, config(bank_country="VN", tax_mode="vn_individual"), "100")["scenarios"][0]
    target = Decimal(row["target_price"])
    at_target = pf.calculate(financials, config(bank_country="VN", tax_mode="vn_individual"), str(target))["scenarios"][0]
    assert Decimal(at_target["margin_percent"]) >= Decimal("25")


def test_optional_unit_costs_default_to_zero_usd_but_cogs_and_freight_stay_required():
    financials = complete_input()
    financials["costs"].pop("packaging")
    financials["costs"]["labor"] = None
    financials["costs"]["other"] = {"amount": "", "currency": "VND"}
    financials["costs"]["etsy_ads"] = {"amount": "  ", "currency": "EUR"}

    normalized = pf.validate_financials(financials)
    for name in ("packaging", "labor", "other", "etsy_ads"):
        assert normalized["costs"][name] == {"amount": "0", "currency": "USD"}
    row = pf.calculate(financials, config(), "100")["scenarios"][0]
    assert row["status"] == "complete"
    assert row["operating_costs"] == "10.00"

    financials["costs"].pop("cogs")
    financials["scenarios"][0]["freight"]["amount"] = None
    row = pf.calculate(financials, config(), "100")["scenarios"][0]
    assert row["status"] == "incomplete"
    assert row["profit"] is None
    assert "Chi phí cogs" in row["missing"]
    assert "Cước vận chuyển" in row["missing"]


def test_explicit_nonzero_optional_unit_costs_keep_their_amounts():
    financials = complete_input()
    financials["costs"].update({
        "packaging": {"amount": "3", "currency": "USD"},
        "labor": {"amount": "4", "currency": "USD"},
        "other": {"amount": "5", "currency": "USD"},
        "etsy_ads": {"amount": "6", "currency": "USD"},
    })
    row = pf.calculate(financials, config(), "100")["scenarios"][0]
    assert row["status"] == "complete"
    assert row["operating_costs"] == "28.00"
    assert pf.validate_financials(financials)["costs"]["packaging"] == {"amount": "3", "currency": "USD"}


def test_optional_cost_defaults_do_not_bypass_unknown_shop_profile_blockers():
    financials = complete_input()
    for name in ("packaging", "labor", "other", "etsy_ads"):
        financials["costs"][name] = None
    settings = config(bank_country="", fx_date="", tax_mode="unknown", confirmed=False)
    row = pf.calculate(financials, settings, "100")["scenarios"][0]
    assert row["status"] == "incomplete"
    assert row["profit"] is None
    assert "Cấu hình bank_country" in row["missing"]
    assert "Cấu hình fx_date" in row["missing"]
    assert "Xác nhận chế độ thuế" in row["missing"]


def test_missing_data_never_returns_a_profit():
    row = pf.calculate({"scenarios": [{"id": "us"}]}, {"listing_currency": "USD"}, "800")["scenarios"][0]
    assert row["status"] == "incomplete"
    assert row["profit"] is None
    assert row["margin_percent"] is None
    assert row["break_even_price"] is None
    assert row["target_price"] is None
    assert row["missing"]


def test_bad_nested_payload_and_negative_price_are_rejected():
    with pytest.raises(pf.FinancialStoreError, match="costs must be an object"):
        pf.validate_financials({"costs": "bad"})
    with pytest.raises(pf.FinancialStoreError, match="fx_vnd_per_unit"):
        pf.validate_settings({"fx_vnd_per_unit": []})
    with pytest.raises(pf.FinancialStoreError, match="Giá bán"):
        pf.calculate(complete_input(), config(), "-1")
    with pytest.raises(pf.FinancialStoreError, match="expected_version"):
        pf._expected({"version": 0}, True)
    with pytest.raises(pf.FinancialStoreError, match="uppercase currency"):
        pf.validate_settings({"listing_currency": "usd"})
    with pytest.raises(pf.FinancialStoreError, match="bank_country"):
        pf.validate_settings({"bank_country": "Vietnam"})
    with pytest.raises(pf.FinancialStoreError, match="representative_id"):
        pf.validate_financials({"representative_id": "missing", "scenarios": [{"id": "actual"}]})


def test_store_rejects_symlink_roots_and_invalid_product_structure(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / "shops").symlink_to(target, target_is_directory=True)
    with pytest.raises(pf.FinancialStoreError, match="symlinks"):
        pf.read_store(tmp_path, "shop1")

    (tmp_path / "shops").unlink()
    shop_dir = tmp_path / "shops" / "shop1"
    shop_dir.mkdir(parents=True)
    path = shop_dir / "product_financials.json"
    path.write_text('{"version":0,"shop_id":"shop1","settings":{},"products":{"product-01":"bad"}}', encoding="utf-8")
    with pytest.raises(pf.FinancialStoreError, match="invalid product data"):
        pf.read_store(tmp_path, "shop1")


def test_store_version_lock_corruption_and_merge(tmp_path):
    (tmp_path / "shops" / "shop1").mkdir(parents=True)
    store = pf.save_settings(tmp_path, "shop1", {"listing_currency": "USD"}, 0)
    assert store["version"] == 1
    financials = complete_input()
    store = pf.save_product(tmp_path, "shop1", "product-01", financials, 1)
    assert store["version"] == 2
    with pytest.raises(pf.FinancialConflict):
        pf.save_product(tmp_path, "shop1", "product-02", financials, 1)
    pf.assert_merge_compatible(tmp_path, "shop1", ["product-01", "product-02"])
    pf.move_product(tmp_path, "shop1", "product-01", "product-02")
    assert "product-02" in pf.read_store(tmp_path, "shop1")["products"]
    path = tmp_path / "shops" / "shop1" / "product_financials.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(pf.FinancialStoreError, match="corrupt"):
        pf.read_store(tmp_path, "shop1")
