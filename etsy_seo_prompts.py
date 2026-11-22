"""Shared, evidence-grounded Etsy copy guidelines for every generation mode.

References (checked 2026-09-22):
https://www.etsy.com/seller-handbook/article/1399426136697
https://www.etsy.com/seller-handbook/article/382774281517
https://www.etsy.com/seller-handbook/article/375461474487

Description length is an editorial target, not an Etsy ranking requirement.
These instructions do not provide keyword-volume data or promise a ranking.
"""

SEO_TITLE_PROMPT_RULES = """TITLE QUALITY RULES:
- Hard limit: write no more than 140 characters total in the finished title text, including all spaces, punctuation and symbols. Count only the title text before returning it; do not count the XML tag wrappers.
- If needed, shorten at a natural word boundary by removing the least useful phrase until the title meets the hard limit. Never rely on downstream truncation to make it fit.
- Write a clear, buyer-friendly title. Prefer fewer than 15 words when the item can be described accurately that way.
- Lead with the most specific, factual primary product phrase a buyer would search for, then add two or three useful differentiators only when supported (such as material, style, size or intended use).
- Prioritize relevance and readability, not using all 140 characters. Never pad a title to reach the character limit.
- Avoid keyword stuffing, repeated words or near-synonyms, and chains of pipe-separated fragments. Do not list every variant in the title.
- Omit internal SKU/catalog codes, supplier names, promotional claims, subjective superlatives and generic gift phrases unless essential to identifying the actual item.
- Use only product facts allowed by the active source-of-truth rules. Do not invent popularity, search volume, best-seller status, ranking or guaranteed results."""

SEO_TAG_PROMPT_RULES = """TAG QUALITY RULES:
- Return exactly 13 unique, comma-separated tags; every tag must be 20 characters or fewer, including spaces.
- Prefer natural multi-word search phrases over isolated words. Choose specific purchase-intent phrases, not broad traffic unrelated to the actual item.
- Cover complementary evidence-supported angles: precise item type, natural buyer synonyms, material or technique, style, practical use and relevant setting. Do not force an angle if the source does not support it.
- Keep one coherent product topic across title, tags and description. A relevant primary phrase may appear in both title and tags; other tags should broaden relevant coverage.
- Avoid near-identical tags, singular/plural duplicates, word-order swaps and generic filler. If a useful phrase exceeds 20 characters, use shorter meaningful phrases, never cut a word mid-way.
- Do not invent audiences, gift occasions, formats, compatibility, customization, brands, materials or features just to fill 13 slots. Use factual synonyms and supported uses instead.
- Do not claim keyword research, low competition, search volume or a top ranking without supplied data. Check tag count, uniqueness and character limits before responding."""

SEO_DESCRIPTION_PROMPT_TEMPLATE = """DESCRIPTION QUALITY AND LENGTH:
- Aim for 500-800 words (roughly 3,500-6,000 characters) when the source has enough distinct facts. This is a soft editorial target, not a ranking rule. When evidence is sparse, write a shorter useful description rather than inventing facts or padding the text.
- Start with one or two natural sentences that name the specific product, include the primary buyer search phrase and communicate its evidenced purpose or distinguishing feature. Put useful information early; do not force an exact snippet character count or copy the entire title.
- Expand into short, specific paragraphs that explain benefits through confirmed features and practical use. Add relevant secondary phrases naturally where they answer a buyer's question. Never append a keyword list or mechanically repeat phrases.
- Write finished English buyer-facing copy with clear headings and selective bullets. No bracketed instructions, keyword strategy, word counts, or commentary about this prompt.
- Every claim must obey the active source-of-truth rules. Do not turn a material option into the material of every variant, invent dimension units/conversions, infer solid wood from a photo, or assert handmade/sustainable/custom-made status without evidence.
- Exclude internal supplier prices, cost/margin notes, catalog reference prices and unrelated catalog metadata. Do not copy a supplier's policies into the shop's promises.

BUYER-FOCUSED STRUCTURE (include only sections that add supported information):
1. OPENING: identify the product and its most useful evidenced value in one or two sentences.
2. DESIGN AND PRACTICAL USE: one or two short paragraphs explaining confirmed features and relevant everyday uses; no generic luxury filler or repeated opening.
3. PRODUCT DETAILS: scannable facts about materials, dimensions with confirmed units, colors, variants, quantities or formats, only as evidenced. Distinguish options from what is included.
4. KEY FEATURES / HOW TO USE: explain useful confirmed features, setup or usage where relevant. Do not force a fixed number of bullets or invent care/assembly instructions.
5. WHAT'S INCLUDED: exactly what the buyer receives when that is confirmed. Do not count scene props, preview images or other catalog products as included items.
6. BEFORE ORDERING: relevant confirmed options and buyer choices. Omit unsupported details; for one essential unresolved purchase detail, invite the buyer to message the shop instead of inventing an answer or listing many 'not specified' lines.
7. FAQ: three to five genuinely useful questions when the evidence supports their answers. Use fewer or omit this section if it would repeat prior text or invent policies/specifications.
8. HELP AND SHOP: a brief invitation to ask product questions, followed only by the supplied store/social links. No fake urgency or ranking promises.

PRODUCT TYPE AND POLICY SAFETY:
- Determine physical versus digital only from the authoritative product evidence. Photos of an object or the presence of local PDF/image files do not by themselves prove what the buyer receives. If fulfillment type is unclear, use neutral wording and omit delivery/download assertions.
- For confirmed physical goods, describe the actual item; never include digital-download boilerplate. Shipping, production time, assembly, returns, warranties, customization and care instructions require explicit evidence.
- For confirmed digital goods, clearly identify the digital deliverable and that no physical item is shipped only when the evidence supports that. State formats, app compatibility and download steps only if confirmed. Do not equate digital with instant delivery; made-to-order digital products may not be instant.
- Include licensing, personal/commercial-use permissions, no-resale terms, refund rules or delivery promises only when explicitly supplied by the authoritative source. Never manufacture shop policies.
- Do not promise top 1, a ranking deadline, guaranteed sales or unsupported performance. Prioritize accurate buyer understanding and useful detail."""
