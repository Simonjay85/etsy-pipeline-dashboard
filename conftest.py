# conftest.py – pytest collection guard
# Exclude manual/exploratory scripts that are not real test modules.
collect_ignore = [
    "test_seo_fail.py",       # manual HTTP smoke script
    "test_tag_pills.py",      # Playwright interactive script
    "test_tags.py",           # Playwright interactive script
    "test_translate.py",      # manual translation check
]
