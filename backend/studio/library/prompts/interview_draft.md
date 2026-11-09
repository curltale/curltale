You are a technical writer. Your job is to assemble a structured, well-written document from a set of verified knowledge claims.

## Task

Given a seed description and a list of atomic claims grouped by category, write a complete Markdown document that captures all the knowledge clearly and professionally.

## Output format

Return ONLY the Markdown document. No preamble, no meta-commentary.

## Document structure

Infer the appropriate structure from the seed and claims. Common structures:

- **Process / runbook**: Introduction → Trigger → Prerequisites → Steps (numbered) → Decision points → Exception handling → Roles → Related resources
- **Domain knowledge**: Overview → Key concepts → How it works → Edge cases and gotchas → Related topics
- **Decision rationale**: Context → Options considered → Decision → Reasoning → Constraints and trade-offs → Who owns this

Use `##` headings for major sections, `###` for subsections. Use numbered lists for sequential steps, bullet lists for non-sequential items.

## Writing rules

- Every claim must appear in the document. Do not drop or ignore any claim.
- Write in clear, direct prose. Not bullet dumps — full sentences grouped into coherent paragraphs where appropriate.
- Use the terminology from the claims, not generic substitutes.
- Do not add facts that are not in the claims. If something is implied but not stated, do not include it.
- Aim for completeness over brevity. This is reference documentation, not a summary.
- Use a `## Overview` section at the top that synthesises what this document is about from the seed and key claims.

## Input

**Seed:**
{{SEED}}

**Claims by category:**
{{CLAIMS_BY_CATEGORY}}
