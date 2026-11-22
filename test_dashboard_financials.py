import openpyxl
from fastapi.testclient import TestClient

import dashboard_app as dashboard


def fixture_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dashboard, "SHOPS", {"shop1": {"id": "shop1", "name": "Test"}})
    monkeypatch.setattr(dashboard, "_active_shop_id", "shop1")
    shop_dir = tmp_path / "shops" / "shop1"
    (shop_dir / "product-01").mkdir(parents=True)
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "Listings"
    ws.cell(4, 2, "product-01")
    ws.cell(4, 5, 42.5)
    workbook.save(shop_dir / "Etsy_SEO_Generator.xlsx")
    return TestClient(dashboard.app)


def test_financial_put_requires_token_and_roundtrips(tmp_path, monkeypatch):
    client = fixture_dashboard(tmp_path, monkeypatch)
    path = "/api/shops/shop1/financial-settings"
    denied = client.put(path, json={"expected_version": 0, "settings": {"listing_currency": "USD"}})
    assert denied.status_code == 403
    assert not (tmp_path / "shops" / "shop1" / "product_financials.json").exists()

    headers = {dashboard._DASHBOARD_MUTATION_TOKEN_HEADER: dashboard._DASHBOARD_MUTATION_TOKEN}
    saved = client.put(path, headers=headers,
                       json={"expected_version": 0, "settings": {"listing_currency": "USD"}})
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    read = client.get(path)
    assert read.status_code == 200
    assert read.json()["settings"]["listing_currency"] == "USD"


def test_product_financials_uses_workbook_price_and_rejects_stale_version(tmp_path, monkeypatch):
    client = fixture_dashboard(tmp_path, monkeypatch)
    headers = {dashboard._DASHBOARD_MUTATION_TOKEN_HEADER: dashboard._DASHBOARD_MUTATION_TOKEN}
    payload = {"expected_version": 0, "financials": {"listing_type": "digital", "scenarios": []}}
    saved = client.put("/api/shops/shop1/products/product-01/financials", headers=headers, json=payload)
    assert saved.status_code == 200
    assert saved.json()["version"] == 1
    stale = client.put("/api/shops/shop1/products/product-01/financials", headers=headers, json=payload)
    assert stale.status_code == 409
    read = client.get("/api/shops/shop1/products/product-01/financials")
    assert read.status_code == 200
    assert read.json()["price"] == "42.5"
    assert read.json()["financials"]["listing_type"] == "digital"


def test_financial_api_rejects_shop_mismatch_and_unmapped_product(tmp_path, monkeypatch):
    client = fixture_dashboard(tmp_path, monkeypatch)
    mismatch = client.get("/api/shops/unknown/financial-settings")
    assert mismatch.status_code == 404
    mismatch = client.get("/api/shops/shop1/products/product-99/financials")
    assert mismatch.status_code == 404


def test_calculate_endpoint_is_read_only(tmp_path, monkeypatch):
    client = fixture_dashboard(tmp_path, monkeypatch)
    headers = {dashboard._DASHBOARD_MUTATION_TOKEN_HEADER: dashboard._DASHBOARD_MUTATION_TOKEN}
    payload = {"financials": {"listing_type": "digital", "scenarios": []},
               "settings": {"listing_currency": "USD"}, "price": "42.5"}
    response = client.post("/api/shops/shop1/products/product-01/financials/calculate",
                           headers=headers, json=payload)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert not (tmp_path / "shops" / "shop1" / "product_financials.json").exists()
