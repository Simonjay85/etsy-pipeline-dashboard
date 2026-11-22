from urllib.parse import parse_qs, urlparse


def select_page_indexes(page_urls, listing_url):
    expected = urlparse(listing_url)
    expected_query = parse_qs(expected.query, keep_blank_values=True)
    fixed_filters = {key: values for key, values in expected_query.items() if key != "pg"}

    listing_candidates = []
    profile_candidates = []
    for index, page_url in enumerate(page_urls):
        parsed = urlparse(page_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.netloc.lower() != expected.netloc.lower():
            continue
        if parsed.path == expected.path:
            if all(query.get(key) == values for key, values in fixed_filters.items()):
                page_number = query.get("pg", ["1"])[0]
                try:
                    page_number = int(page_number)
                except (TypeError, ValueError):
                    page_number = 1
                listing_candidates.append((page_number, index))
            continue
        if parsed.path.rstrip("/").lower() not in {"/login", "/signup", "/register"}:
            profile_candidates.append(index)

    listing_index = max(listing_candidates)[1] if listing_candidates else None
    profile_index = profile_candidates[0] if profile_candidates else None
    return listing_index, profile_index
