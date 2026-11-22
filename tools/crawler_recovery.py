def is_page_crash(exc):
    """Return whether Playwright reported a closed/crashed page target."""
    return type(exc).__name__ == "TargetClosedError" or "page crashed" in str(exc).lower()


def should_recycle_page(processed_count, every):
    """Return whether a long-lived page should be replaced to bound renderer growth."""
    return every > 0 and processed_count > 0 and processed_count % every == 0


async def recover_page(context, page):
    """Replace a stale Playwright page without losing its context."""
    try:
        if page is not None and not page.is_closed():
            await page.close()
    except Exception:
        pass
    return await context.new_page()
