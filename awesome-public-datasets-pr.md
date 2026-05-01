# Draft PR for awesome-public-datasets

Target repo: https://github.com/awesomedata/awesome-public-datasets
Target file: `README.rst` (it's reStructuredText, not Markdown — verify before submitting)

## What to add

Find the `Agriculture` section. Insert alphabetically. The list is mostly U.S. agencies and academic data sources. Format used elsewhere in that file (RST list item):

```rst
- `USDA NASS County Crop Yields <https://github.com/ProductOfAmerica/usda-county-yields>`_ - Free static JSON API serving USDA NASS county-level corn, soybean, and wheat yield data. Refreshed weekly from the NASS bulk file. No API key, no rate limits.
```

If it turns out the file is Markdown after all (the repo has rotated formats over the years), use:

```markdown
- [USDA NASS County Crop Yields](https://github.com/ProductOfAmerica/usda-county-yields) - Free static JSON API serving USDA NASS county-level corn, soybean, and wheat yield data. Refreshed weekly from the NASS bulk file. No API key, no rate limits.
```

Verify before opening the PR by `cat`-ing the file or browsing it on github.com.

## PR title

```
Add USDA NASS County Crop Yields under Agriculture
```

## PR body

```
Adds USDA NASS County Crop Yields to the Agriculture section.

What it is:
- Free static JSON API serving USDA NASS county-level corn, soybean, and wheat yield data
- Refreshed weekly from the NASS bulk file (`qs.crops_*.txt.gz`) via GitHub Actions
- Per-county point lookups in 2-22 KB; no API key, no rate limits, no auth
- Hosted on jsDelivr's CDN so reads are free and globally cached
- Public-domain data (USDA NASS); cache code is MIT-licensed
- JSON Schema 2020-12 contract published at `data/_schema/leaf.json`

Useful for:
- Data scientists doing agricultural ML
- Notebooks / dashboards that need yield series without setting up a NASS Quick Stats API key
- Embedded clients (browser, mobile) that can't hit Quick Stats directly

Inserted alphabetically in the Agriculture section.
```

## How to push (run when ready)

```bash
gh repo fork awesomedata/awesome-public-datasets --clone=true
cd awesome-public-datasets
git checkout -b add-usda-county-yields

# Edit README.rst (or README.md) and insert the list item per above.
# Verify the diff is one line in the right section:
git diff

git add README.rst   # or README.md
git commit -m "Add USDA NASS County Crop Yields under Agriculture"
git push origin add-usda-county-yields

gh pr create \
  --repo awesomedata/awesome-public-datasets \
  --title "Add USDA NASS County Crop Yields under Agriculture" \
  --body-file ../usda-county-yields/awesome-public-datasets-pr.md
```

## Caveats

- This list has had AI-generated PRs in the past, and reception varies. Keep the PR body human-written-feeling. If the maintainers' contribution guide has specific etiquette, follow it (some lists require a justification of why the entry meets their inclusion criteria; some auto-close PRs that don't match a template).
- The list's last big curation pass may have been a while ago; PRs sometimes sit. Don't take silence personally.
- Other curated lists worth considering after this one lands: `awesome-data` variants, `awesome-static-website` (for the CDN-API angle), `awesome-jsdelivr` if it exists.
