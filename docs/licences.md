# Dependency licence audit

Purpose: Curltale AI is MIT. Every dependency must be usable in an MIT-licensed product, and none may force its licence onto the product.
Licences were read from the installed packages' metadata and the pinned lock files. Re-run this audit whenever a dependency is added and before each release.

## Verdict

| Item | Licence | Verdict |
|---|---|---|
| All Python packages in `backend/requirements.lock` not listed below | MIT, BSD, Apache-2.0, ISC, PSF, MIT-CMU | OK |
| `psycopg`, `psycopg-pool`, `psycopg-binary` | LGPL-3.0 | OK as an unmodified library imported by the app. Do not patch and vendor it. Keep the notice in the image |
| `certifi`, `tqdm` | MPL-2.0 (tqdm: MPL-2.0 AND MIT) | OK; file-level copyleft, unmodified |
| `numpy`, `py-rust-stemmers` | not in package metadata | BSD-3 (numpy) and MIT/Apache-2.0 (stemmers) per their repositories: OK |
| Frontend: react, react-dom, react-markdown, react-router-dom, remark-gfm, vite, d3-* | MIT / ISC | OK |
| Font: Bricolage Grotesque (`@fontsource-variable/bricolage-grotesque`, the wordmark) | SIL Open Font License 1.1 | OK; bundled in the image, its licence ships in the package. The app never loads fonts from a font host |
| `boto3`, `botocore`, `s3transfer` (S3 storage; now in the image) | Apache-2.0 | OK |
| `jmespath`, `python-dateutil`, `six` (boto3's dependencies) | MIT / Apache-2.0 + BSD / MIT | OK |
| `moto` (tests only, not in the image) | Apache-2.0 | OK |
| `pypdf` (BSD-3), `pypdfium2` (BSD-3 / Apache-2.0, bundles PDFium), `beautifulsoup4`, `soupsieve` (MIT), `python-docx`, `python-pptx` (MIT), `lxml` (BSD-3), `xlsxwriter` (BSD-2) | in the image for reading documents | OK. `pypdfium2` is used for PDFs instead of the AGPL `pymupdf` |
| Playwright's bundled Chromium (fetcher sidecar) | BSD-style plus third-party notices | OK; ship as a separate optional image |
| Image `pgvector/pgvector` (Postgres + pgvector) | PostgreSQL licence | OK |
| Playwright (Python package and the `mcr.microsoft.com/playwright/python` image used for the optional browser container; Chromium inside) | Apache-2.0; Chromium BSD-3 plus third-party notices; Ubuntu base | OK; separate optional image, nothing in the main image |
| `fetcher-seccomp.json` (the seccomp profile of the browser container: Docker's default profile plus user namespaces, copied from Playwright's `utils/docker/seccomp_profile.json`) | Apache-2.0 | OK; unmodified copy |
| MinIO | **AGPL-3.0** | Never bundled or required. S3 is configured as an endpoint; an example for MinIO may be documented as a user's own choice |
| LiteLLM | MIT (core) | Never required; any OpenAI-compatible endpoint works |
| Embedding model `BAAI/bge-base-en-v1.5` | MIT | OK. A chosen external or multilingual model brings its own licence; Manage shows it |

## Rules going forward

1. A new dependency needs its licence recorded here in the same change.
2. No GPL, AGPL, SSPL, BUSL, PolyForm or "non-commercial" code in the image or as a required service.
3. LGPL and MPL only as unmodified libraries.
4. Model weights are dependencies too: record their licence.
