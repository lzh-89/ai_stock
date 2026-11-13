# Traceability manifest format

Use JSON so the audit has no third-party runtime dependency. Keep one manifest per coherent change.

## Minimal structure

```json
{
  "schemaVersion": 1,
  "changeId": "T1-RV",
  "title": "T+1 candidate review closure",
  "sourceDocuments": [
    "docs/source-plan.md"
  ],
  "requirements": [
    {
      "id": "T1-RV-005",
      "statement": "Expose unallocated leading-case diagnostics without losing any confirmed category.",
      "status": "confirmed",
      "sourceRefs": [
        {
          "id": "SRC-005",
          "path": "docs/source-plan.md",
          "locator": "section 10.2",
          "excerpt": "Split leading cases into five named categories and show where each was filtered out."
        }
      ],
      "decisionRefs": [
        "docs/decision-record.md#approved-scope"
      ],
      "requirementDocRefs": [
        "docs/primary-requirements.md#leading-cases"
      ],
      "implementationDocRefs": [
        "docs/implementation-plan.md#leading-cases"
      ],
      "releaseRefs": [],
      "rollbackRefs": [],
      "criteria": [
        {
          "id": "T1-RV-005-A",
          "statement": "Provide the opening-premium-leading filter.",
          "status": "confirmed",
          "sourceRefIds": ["SRC-005"],
          "appliesTo": ["backend", "frontend"],
          "evidence": {
            "artifacts": [],
            "tests": [],
            "uiAcceptance": [],
            "dataAcceptance": [],
            "reviews": []
          }
        },
        {
          "id": "T1-RV-005-B",
          "statement": "Show the terminal filtering stage and reason for every leading case.",
          "status": "confirmed",
          "sourceRefIds": ["SRC-005"],
          "appliesTo": ["backend", "frontend", "data"],
          "evidence": {
            "artifacts": [],
            "tests": [],
            "uiAcceptance": [],
            "dataAcceptance": [],
            "reviews": []
          }
        }
      ]
    }
  ]
}
```

The leading-case example is deliberately atomic. In a real manifest, create separate criteria for opening-premium leading, close/intraday rise, hard to buy, high risk, normally tradable miss, terminal stage, and filtering reason. A single criterion named “five categories” is insufficient.

## Evidence rules

- Local evidence uses a repository-relative path, optionally followed by `#anchor` or `::test_name`.
- External evidence may start with `http://`, `https://`, `git:`, `run:`, or `manual:`.
- `artifacts` points to implemented code, UI, schema, configuration, or completed document changes.
- `tests` points to automated tests that assert the criterion's semantics.
- `uiAcceptance` points to browser acceptance evidence for `frontend` criteria.
- `dataAcceptance` points to replay, migration, or real-data validation for `data` criteria.
- `reviews` records non-code acceptance, including the four-role document walkthrough for `docs` criteria.

For `cancelled`, add `cancellationReason` on the criterion and retain explicit `decisionRefs` on the parent requirement.

