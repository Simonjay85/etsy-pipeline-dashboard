"""Temporary local stub for environment where iCloud file is dataless.
Used only for local verification on this branch when real module is unavailable."""

TRADEMARK_BLACKLIST = []
COMPATIBILITY_TERMS = []


def check_trademark_violations(text: str):
    return []


def check_field_violations(title: str, desc: str, tags: str):
    return {"errors": [], "warnings": []}
