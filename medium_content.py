"""Shared, product-grounded Medium research-note content."""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit


_HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:br|div|li|ol|p|section|h[1-6]|ul)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BRACKETED_TEXT_RE = re.compile(r"\[[^\]]*\]")
_MARKETING_MARKERS = (
    "buy now",
    "critical",
    "download today",
    "get it now",
    "google-optimized",
    "instant digital download",
    "instant download",
    "limited time",
    "link in bio",
    "must-have",
    "no waiting",
    "perfect for",
    "perfect gift",
    "shop now",
    "seo description",
    "seo title",
    "you'll receive",
)
_MAX_CONTEXT_SENTENCES = 2
_MAX_CONTEXT_SENTENCE_CHARS = 180
_MAX_CONTEXT_CHARS = 320
_MAX_CORE_TITLE_CHARS = 120
_CORE_TITLE_SEPARATOR_RE = re.compile(
    r"\s*(?:\||•|·|—|–|::)\s*|\s+-\s+|\s+/\s+"
)
_STANDALONE_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TOPIC_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TOPIC_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
        "in", "is", "it", "of", "on", "or", "the", "this", "to", "with",
        "your", "you", "use", "using", "includes", "include", "one", "way",
    }
)
_TOPIC_FORMAT_TERMS = frozenset(
    {
        "a4", "bundle", "digital", "download", "editable", "file", "files",
        "format", "instant", "jpg", "jpeg", "letter", "page", "pages", "pdf",
        "png", "printable", "size", "svg", "template",
    }
)


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_description(value: object) -> str:
    """Normalize description markup and retain only factual-looking text."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>", " ", text, flags=re.IGNORECASE)
    text = _HTML_BLOCK_TAG_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _BRACKETED_TEXT_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()

    parts = re.split(r"(?<=[.!?])\s+|\s*\n+\s*", text)
    factual_parts: list[str] = []
    for part in parts:
        normalized = re.sub(r"^[-*•\s]+", "", part).strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if any(marker in lowered for marker in _MARKETING_MARKERS):
            continue
        factual_parts.append(normalized)
    return " ".join(factual_parts)


def _bounded_description_context(cleaned_description: str) -> str:
    if not cleaned_description:
        return ""

    parts = re.split(r"(?<=[.!?])\s+", cleaned_description)
    bounded: list[str] = []
    remaining = _MAX_CONTEXT_CHARS
    for part in parts:
        if len(bounded) >= _MAX_CONTEXT_SENTENCES or remaining <= 0:
            break
        sentence = part.strip()
        if not sentence:
            continue
        sentence = _truncate_words(sentence, min(_MAX_CONTEXT_SENTENCE_CHARS, remaining))
        if not sentence:
            continue
        if bounded:
            remaining -= 1
        bounded.append(sentence)
        remaining -= len(sentence)
    return " ".join(bounded)


def _normalize_topic_token(token: str) -> str:
    if token in {"planned", "planning"}:
        return "plan"
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith("ss") and len(token) > 4:
        return token[:-1]
    return token


def _meaningful_topic_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in _TOPIC_TOKEN_RE.findall(text.lower()):
        if raw_token.isdigit() or raw_token in _TOPIC_STOPWORDS or raw_token in _TOPIC_FORMAT_TERMS:
            continue
        tokens.add(_normalize_topic_token(raw_token))
    return tokens


def _description_with_source_overlap(
    cleaned_description: str, title: str, tags: list[str]
) -> str:
    source_tokens = _meaningful_topic_tokens(" ".join([title, *tags]))
    if not source_tokens:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned_description)
    matching_sentences = [
        sentence
        for sentence in sentences
        if _meaningful_topic_tokens(sentence) & source_tokens
    ]
    return " ".join(matching_sentences)


def _truncate_words(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    words = text.split()
    result: list[str] = []
    length = 0
    for word in words:
        next_length = length + len(word) + (1 if result else 0)
        if next_length > max_length:
            break
        result.append(word)
        length = next_length
    return " ".join(result) or text[:max_length].rstrip()


def _extract_core_title(title: object) -> str:
    """Keep the meaningful product phrase for article-facing text."""
    clean_title = _clean_text(title)
    first_segment = _CORE_TITLE_SEPARATOR_RE.split(clean_title, maxsplit=1)[0]
    without_year = _STANDALONE_YEAR_RE.sub(" ", first_segment)
    core_title = re.sub(r"\s+", " ", without_year).strip(" -–—|•·:").strip()
    return _truncate_words(core_title, _MAX_CORE_TITLE_CHARS) or "Planning resource"


def render_medium_plain_text(markdown: object) -> str:
    """Render the article body as readable plain text for the Medium editor."""
    text = str(markdown or "")
    text = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*", "", text)
    text = re.sub(
        r"!\[([^\]]*)\]\((?:<[^>]*>|[^)]*)\)",
        r"\1",
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\((<[^>]*>|[^)]*)\)",
        _render_medium_link,
        text,
    )
    text = re.sub(r"`([^`\r\n]*)`", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text


def _render_medium_link(match: re.Match[str]) -> str:
    label = match.group(1)
    target = match.group(2).strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    valid_target = _validated_etsy_listing_url(target)
    return f"{label} ({valid_target})" if valid_target else label


def _tag_list(tags: object) -> list[str]:
    if isinstance(tags, str):
        values = tags.split(",")
    elif tags:
        values = list(tags)  # type: ignore[arg-type]
    else:
        values = []
    return [_clean_text(tag) for tag in values if _clean_text(tag)]


def _topic_profile(
    title: str, cleaned_description: str, tags: list[str]
) -> tuple[str, str, list[str], str]:
    signal = " ".join([title, cleaned_description, *tags]).lower()
    title_tags_signal = " ".join([title, *tags]).lower()

    if (
        any(word in signal for word in ("nursing", "nurse", "clinical"))
        or re.search(r"\brn\b", signal)
    ):
        return (
            "nursing study and clinical planning",
            "coordinate lectures, clinical blocks, and review work",
            [
                "Separate lecture topics, clinical blocks, and review sessions so the next action is visible.",
                "After a clinical day, note the follow-up topic or question while the context is still fresh.",
                "Use the weekly review to move unfinished study tasks instead of hiding them in a long list.",
            ],
            "one lecture-and-clinical week",
        )

    wedding = any(word in signal for word in ("wedding", "bridal", "bride"))
    if wedding and any(word in signal for word in ("budget", "payment", "vendor", "expense")):
        return (
            "wedding budget and vendor planning",
            "track vendor payments, budget categories, and the next weekly decision",
            [
                "Give each vendor payment a category, due date, and status so commitments are easy to review.",
                "Keep open vendor questions beside the budget item they affect rather than in a separate memory list.",
                "Use a weekly review to compare planned spending with confirmed payments and reschedule the next decision.",
            ],
            "one vendor-payment review cycle",
        )
    if wedding and any(word in signal for word in ("invitation", "invite", "card", "thank you")):
        return (
            "wedding invitation and card design",
            "organize wording, proofing, and delivery decisions",
            [
                "Keep the event details and the current wording together while reviewing each version.",
                "Compare names, dates, and addresses against the source details before sharing or printing.",
                "Keep a short revision note so an old card version is not mistaken for the current one.",
            ],
            "one wording-and-proofing cycle",
        )

    if any(word in signal for word in ("svg", "cut file", "cricut", "silhouette", "dxf")):
        return (
            "SVG and cut-file preparation",
            "move from a design choice to a checked cutting plan",
            [
                "Write down the material, intended size, and important design choices before preparing the cut.",
                "Keep a small note of any detail that needs checking at the next preparation step.",
                "After the cycle, record what was ready, what needed adjustment, and what should be checked first next time.",
            ],
            "one design-preparation cycle",
        )

    if any(word in signal for word in ("wall art", "printable decor", "home decor", "poster", "gallery wall")):
        return (
            "wall-art and printable-decor planning",
            "choose a placement, size, and review plan for a decor project",
            [
                "Record the intended room, placement, and size before making the final choice.",
                "Compare the selected wording or artwork against the space and surrounding colors.",
                "Keep a short decision note so the next decor change starts from the current plan.",
            ],
            "one placement-and-review cycle",
        )

    if any(word in signal for word in ("canva", "editable template", "editable design")):
        return (
            "Canva and editable-template workflow",
            "separate content decisions from visual revisions",
            [
                "Keep the factual wording and source details together before making visual changes.",
                "Review text, names, dates, and measurements as a separate pass from visual choices.",
                "Record a simple version name or date so the current draft is easy to identify.",
            ],
            "one content-and-revision cycle",
        )

    if any(word in title_tags_signal for word in ("planner", "agenda", "schedule", "organizer", "time block")):
        return (
            "personal planning and time-blocking",
            "turn tasks into visible time blocks and a repeatable weekly review",
            [
                "Write the task next to the time block or planning area where you expect to do it.",
                "Leave a small amount of open space for tasks that take longer than expected or need to move.",
                "End the week by marking completed, moved, and intentionally dropped items before planning again.",
            ],
            "one workweek",
        )

    if any(word in signal for word in ("journal", "workbook", "reflection", "guided prompts")):
        return (
            "journal and workbook reflection",
            "turn prompts into a consistent reflection or practice cycle",
            [
                "Date each entry or exercise so the sequence is easy to revisit.",
                "Choose one prompt or exercise for the cycle instead of trying to complete everything at once.",
                "Review your own notes for recurring questions, then choose one next prompt without treating it as a measured finding.",
            ],
            "one reflection or practice cycle",
        )

    if any(word in signal for word in ("spreadsheet", "google sheets", "excel", "inventory", "dashboard")):
        return (
            "spreadsheet and tracking workflow",
            "turn repeated entries into a consistent record and review routine",
            [
                "Define what each field or column means before adding a new entry.",
                "Use one consistent status or category vocabulary so entries can be reviewed together.",
                "Review the record at a fixed interval and note which next action follows from it.",
            ],
            "one reporting or review period",
        )

    if any(word in signal for word in ("checklist", "check list", "to-do list")):
        return (
            "printable checklist workflow",
            "turn a repeatable sequence into visible check-offs and a short review",
            [
                "Read the checklist once and mark the order that matches the real task sequence.",
                "Add a short note beside an item when the next attempt needs a reminder.",
                "Review unchecked items at the end of the cycle and decide whether to do, move, or drop them.",
            ],
            "one repeatable task cycle",
        )

    if any(word in signal for word in ("budget", "finance", "expense", "money")):
        return (
            "budget and expense review",
            "organize categories, due dates, and a regular review of recorded expenses",
            [
                "Record each expense with the category and date that will make the next review easier.",
                "Keep planned costs separate from confirmed payments so the two states are not confused.",
                "Review one period at a time and note which category needs an intentional adjustment.",
            ],
            "one budget-review period",
        )

    return (
        "product-context workflow planning",
        "turn the supplied product context into a small, observable workflow",
        [
            "Start by naming the one outcome the product context is meant to support.",
            "Break that outcome into the smallest actions that can be written, scheduled, or checked.",
            "Review the next cycle and keep only the prompts that help you decide what to do next.",
        ],
        "one repeatable planning cycle",
    )


def _article_context(
    title: object, desc: object, tags: object
) -> tuple[str, str, str, list[str], str, str, list[str], str, str]:
    clean_title = _clean_text(title) or "Untitled planning resource"
    core_title = _extract_core_title(clean_title)
    cleaned_description = _clean_description(desc)
    clean_tags = _tag_list(tags)
    topic, goal, tips, cycle = _topic_profile(
        clean_title, cleaned_description, clean_tags
    )
    description_for_context = _description_with_source_overlap(
        cleaned_description, clean_title, clean_tags
    )
    description_context = _bounded_description_context(description_for_context)
    return (
        clean_title,
        core_title,
        cleaned_description,
        clean_tags,
        topic,
        goal,
        tips,
        cycle,
        description_context,
    )


def _validated_etsy_listing_url(value: object) -> str:
    candidate = _clean_text(value)
    if not candidate or re.search(r"\s", candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or hostname not in {"etsy.com", "www.etsy.com"}:
            return ""
        if parsed.port is not None:
            return ""
    except ValueError:
        return ""
    if not re.fullmatch(r"/listing/(\d+)(?:/[^/]*)*", parsed.path.rstrip("/")):
        return ""
    return candidate


def make_medium_article_title(title: object, desc: object, tags: object) -> str:
    """Return the shared article title used by previews and the Medium poster."""
    _, core_title, _, _, topic, _, _, _, _ = _article_context(title, desc, tags)
    return f"A Research-Style How-To for {topic.title()}: {core_title}"


def make_medium_research_article(
    title: object,
    desc: object,
    tags: object,
    etsy_url: object,
    *,
    include_heading: bool = True,
) -> str:
    """Build the shared Markdown article used by every Medium entry point.

    The default output is a preview-ready article with one H1. Passing
    ``include_heading=False`` produces a body for a poster whose title field is
    filled separately; it also avoids repeating the product title in the body.
    """
    (
        _clean_title,
        core_title,
        _cleaned_description,
        clean_tags,
        topic,
        goal,
        tips,
        cycle,
        description_context,
    ) = _article_context(title, desc, tags)
    tag_context = ", ".join(clean_tags[:5]) or "no additional tags supplied"
    description_context = description_context or "No usable factual product context was supplied."
    product_reference = f'“{core_title}”' if include_heading else "the supplied product context"
    related_url = _validated_etsy_listing_url(etsy_url)

    heading = (
        f"# {make_medium_article_title(title, desc, tags)}\n\n"
        if include_heading
        else ""
    )
    related_section = ""
    if related_url:
        resource_label = core_title if include_heading else "Open the related Etsy resource"
        related_section = (
            "\n## Related Resource\n\n"
            f"For reference, the related Etsy resource is available here: "
            f"[{resource_label}](<{related_url}>).\n"
        )

    return f"""{heading}## Abstract

This practical research note uses {product_reference} as a starting point for {topic}. The short factual context is: “{description_context}” The supplied topic signals are {tag_context}. The aim is to make {goal} easier to observe without assuming features or outcomes that were not provided.

## Research Question

How might someone use {product_reference} to make {goal} more consistent from one planning cycle to the next?

## Practical Method: A Small Workflow

1. **Define the cycle.** Choose {cycle} that fits the product context.
2. **Capture the next actions.** Write down the decisions, tasks, or follow-ups that are visible from the title, cleaned description, and tags.
3. **Plan before doing.** Use the product context to keep each action in one visible place, without assuming undocumented sections or features.
4. **Review at the end.** Mark what was completed, moved, or intentionally dropped, and use that record to shape the next cycle.

## Product-Specific Tips

The supplied context points to **{topic}**, so these prompts stay close to that use case:

- {tips[0]}
- {tips[1]}
- {tips[2]}

These are practical prompts inferred from the supplied title, cleaned description, and tags, not claims about undocumented product features.

## A Simple Way to Measure Whether It Helps

For two comparable cycles, record the number of planned items, completed items, moved items, and times you had to search for the next action. Compare your own notes at the end of each cycle. This is a lightweight self-check, not proof of a general effect.

## Limitations

This note contains no clinical or scientific study, external evidence, benchmark, or measured product result. It does not claim that the workflow will improve performance for every person. The useful sections, writing space, and level of detail may differ from the assumptions in this note, so adapt the prompts to what is actually present.

## Conclusion

A small, repeatable capture-and-review loop can make {topic} easier to inspect. Start with one cycle, keep the record simple, and revise the prompts based on your own observations rather than on an invented finding.
{related_section}"""
