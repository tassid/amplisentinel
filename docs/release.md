# Release guide (GitHub Actions, Zenodo DOI, and PyPI)

## 1. GitHub Actions CI

The workflow in [.github/workflows/ci.yml](../.github/workflows/ci.yml) runs
`ruff check sentinel tests` and `pytest` on `ubuntu-latest` and `macos-latest`
across Python 3.10, 3.12, and 3.13 without network access.

## 2. Zenodo DOI via GitHub Release

1. Sign in to Zenodo (`https://zenodo.org`) with your GitHub account.
2. Under GitHub settings in Zenodo, enable the repository `tassid/sentinel`.
3. Create and publish a tagged GitHub release matching the semantic version in
   `pyproject.toml` and `CITATION.cff` (for example `v0.2.0`).
4. Zenodo reads `.zenodo.json` and `CITATION.cff`, mints a persistent DOI, and
   assigns a versioned archive. Record the DOI in `CITATION.cff` (`doi:`) for
   subsequent releases.

## 3. PyPI package (`amplisentinel`)

Checked on 2026-11-19: `https://pypi.org/pypi/amplisentinel/json` returned HTTP
404, so the `amplisentinel` distribution name was unclaimed on PyPI.

Build and verify locally before publishing:

```bash
pip install build twine
python -m build
twine check dist/*
```

Publish with PyPI Trusted Publishing (OpenID Connect) or `twine upload dist/*`
after merging all stage PRs to `main` and tagging the release.
