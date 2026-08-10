import json
import multiprocessing
import threading
from pathlib import Path

from social_post_store import (
    social_post_store_path,
    get_product_social_statuses,
    load_social_post_records,
    record_social_post,
)

def _social_post_worker(
    base_dir: str,
    shop_id: str,
    folder: str,
    row: int,
    channel: str,
    url: str,
    posted_at: str,
) -> None:
    record_social_post(
        Path(base_dir),
        shop_id,
        folder,
        row,
        channel,
        url=url,
        posted_at=posted_at,
    )


def test_record_social_post_uses_stable_lock_file(tmp_path, monkeypatch):
    opened = []
    original_open = Path.open

    def capture_open(self, *args, **kwargs):
        opened.append(str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", capture_open)

    record_social_post(
        tmp_path,
        "templystudios",
        "product-414",
        111,
        "pinterest",
        url="https://ca.pinterest.com/pin/888475832769175963/",
    )

    lock_path = social_post_store_path(tmp_path, "templystudios").with_suffix(".json.lock")
    assert any(path.endswith(lock_path.name) for path in opened), opened


def test_record_social_post_is_safe_for_multi_process_updates(tmp_path):
    channels = {
        "instagram": "https://example.com/instagram",
        "facebook": "https://example.com/facebook",
        "twitter": "https://example.com/twitter",
        "medium": "https://example.com/medium",
        "reddit": "https://example.com/reddit",
    }

    ctx = multiprocessing.get_context("spawn")
    processes = []
    for idx, (platform, url) in enumerate(channels.items()):
        proc = ctx.Process(
            target=_social_post_worker,
            args=(str(tmp_path), "templystudios", "product-414", 111, platform, url, f"2026-07-{30-idx:02d}T00:00:00Z"),
        )
        proc.start()
        processes.append(proc)

    for proc in processes:
        proc.join(15)
        assert proc.exitcode == 0, f"Child process failed for {proc.pid}"

    store = get_product_social_statuses(
        tmp_path,
        "templystudios",
        "product-414",
        111,
    )
    assert set(store.keys()) == set(channels.keys())
    for platform, expected_url in channels.items():
        assert store[platform]["url"] == expected_url


def test_records_are_isolated_by_shop_and_channel(tmp_path):
    record_social_post(
        tmp_path,
        "templystudios",
        "product-414",
        111,
        "pinterest",
        url="https://ca.pinterest.com/pin/888475832769175963/",
        posted_at="2026-07-31T00:00:00Z",
    )
    record_social_post(
        tmp_path,
        "daisyflowdigital",
        "product-414",
        9,
        "instagram",
        detail="confirmed",
    )

    temply = get_product_social_statuses(
        tmp_path, "templystudios", "product-414", 111
    )
    daisy = get_product_social_statuses(
        tmp_path, "daisyflowdigital", "product-414", 9
    )
    assert set(temply) == {"pinterest"}
    assert set(daisy) == {"instagram"}


def test_folder_identity_survives_row_change(tmp_path):
    record_social_post(
        tmp_path,
        "templystudios",
        "product-414",
        111,
        "pinterest",
        posted_at="2026-07-31T00:00:00Z",
    )
    statuses = get_product_social_statuses(
        tmp_path, "templystudios", "product-414", 999
    )
    assert statuses["pinterest"]["status"] == "posted"


def test_failed_or_missing_record_does_not_create_false_status(tmp_path):
    assert get_product_social_statuses(
        tmp_path, "templystudios", "product-999", 999
    ) == {}
    assert not (
        tmp_path / "shops" / "templystudios" / "social_post_status.json"
    ).exists()


def test_corrupt_store_file_is_tolerated_and_read_returns_empty(tmp_path):
    store_path = tmp_path / "shops" / "templystudios" / "social_post_status.json"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text("{not-json}", encoding="utf-8")

    assert get_product_social_statuses(
        tmp_path, "templystudios", "product-414", 111
    ) == {}
    raw = load_social_post_records(tmp_path, "templystudios")
    assert raw == {
        "version": 1,
        "shop_id": "templystudios",
        "products": {},
    }


def test_merge_updates_preserve_other_channels_and_posts_are_atomic(tmp_path):
    record_social_post(
        tmp_path,
        "templystudios",
        "product-414",
        111,
        "pinterest",
        url="https://example.com/old",
        posted_at="2026-07-30T00:00:00Z",
    )

    errors: list[str] = []

    def writer(channel: str, url: str, posted_at: str):
        try:
            record_social_post(
                tmp_path,
                "templystudios",
                "product-414",
                111,
                channel,
                url=url,
                posted_at=posted_at,
            )
        except Exception as exc:  # pragma: no cover
            errors.append(f"{channel}: {exc}")

    threads = [
        threading.Thread(target=writer, args=(channel, f"https://example.com/{channel}", f"2026-07-{day:02d}T00:00:00Z"))
        for day, channel in [(31, "instagram"), (30, "facebook")]
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors

    store = load_social_post_records(tmp_path, "templystudios")
    raw = json.loads(
        (
            tmp_path / "shops" / "templystudios" / "social_post_status.json"
        ).read_text(encoding="utf-8")
    )
    assert raw == store
    product_record = store["products"]["product-414"]
    channels = product_record["channels"]
    assert set(channels) == {"pinterest", "instagram", "facebook"}
    assert channels["instagram"]["url"] == "https://example.com/instagram"
    assert channels["facebook"]["url"] == "https://example.com/facebook"
    assert product_record["row"] == 111
    temp_leftovers = [
        p
        for p in store_path_iter(tmp_path / "shops" / "templystudios")
        if p.name.startswith(".social_post_status.json.") and p.suffix == ".tmp"
    ]
    assert not temp_leftovers


def test_record_posts_to_legacy_file_still_readable(tmp_path):
    legacy = tmp_path / "shops" / "templystudios" / "social_posts.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        '{"version": 1, "shop_id": "templystudios", "products": {"product-414": {"folder": "product-414", "row": 111, "channels": {"pinterest": {"status": "posted", "posted_at": "2026-07-31T00:00:00Z", "url": "https://ca.pinterest.com/pin/888475832769175963/", "detail": "", "source": "social_auto_post"}}}}}',
        encoding="utf-8",
    )

    statuses = get_product_social_statuses(
        tmp_path, "templystudios", "product-414", 111
    )
    assert statuses["pinterest"]["url"] == "https://ca.pinterest.com/pin/888475832769175963/"


def store_path_iter(directory):
    if not directory.exists():
        return []
    return list(directory.iterdir())
