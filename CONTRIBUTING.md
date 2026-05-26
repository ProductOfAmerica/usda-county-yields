# Contributing

This repository publishes a static JSON cache of USDA NASS county-level crop yield data. Contributions should keep the public data contract stable unless a breaking change is discussed first.

## Good first contributions

- Improve documentation, examples, or search/discovery pages.
- Report missing or surprising county/crop coverage with a source example.
- Add tests for refresh behavior or schema validation.
- Improve producer safety checks without changing existing JSON shapes.

## Local checks

```bash
python -m unittest tests.test_refresh
```

Running the full refresh downloads roughly 1 GB from NASS:

```bash
python scripts/refresh.py
```

## Pull requests

- Explain the user-facing reason for the change.
- Note whether the JSON API shape changes.
- Include tests for producer behavior changes.
- Do not commit local refresh artifacts, caches, or private monitoring URLs.
