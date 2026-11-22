from tools.pivotandpage_chatgpt_imagegen_batch import Product, build_manifest, validate_manifest


def _product(product_type: str = "Wooden Stump Stool") -> Product:
    return Product(
        row=4,
        folder="product-999",
        title=f"{product_type} | Walnut | Model W999",
        seed="seed title",
        before_count=3,
        product_type=product_type,
        wood="Walnut",
        model="W999",
        source={},
        selection_reason="replace_legacy_pillow_gallery",
    )


def test_manifest_is_ten_independent_chatgpt_image_jobs_without_branding():
    manifest = build_manifest(_product())
    validate_manifest(manifest)

    assert manifest["tool_mode"] == "chatgpt-built-in-imagegen"
    assert manifest["canva_used"] is False
    assert manifest["local_compositor_used"] is False
    assert manifest["contact_sheet_output"] is False
    assert manifest["branding_overlay"] is False
    assert manifest["logo_policy"] == "none"
    assert manifest["text_overlay_policy"] == "none"
    assert manifest["generation_count"] == 10
    assert manifest["one_generation_call_per_asset"] is True
    assert manifest["selection_reason"] == "replace_legacy_pillow_gallery"

    assets = manifest["assets"]
    assert len(assets) == 10
    assert len({asset["output"] for asset in assets}) == 10
    assert len({asset["prompt"] for asset in assets}) == 10
    assert all(asset["generation_mode"] == "independent_single_image" for asset in assets)
    assert all("PIVOTANDPAGE" not in asset["prompt"] for asset in assets)
    assert all("NO collage" in asset["prompt"] for asset in assets)
    assert all("NO logo" in asset["prompt"] for asset in assets)
    assert all("NO watermark" in asset["prompt"] for asset in assets)


def test_slot_07_uses_large_furniture_appropriate_scene_for_table_sets():
    manifest = build_manifest(_product("Wood Furniture Set"))
    prompt = manifest["assets"][6]["prompt"]
    assert "tea/lounge or dining-scale interior" in prompt
    assert "platform bed" not in prompt
