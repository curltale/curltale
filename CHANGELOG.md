# Changelog

All notable changes are listed here. The project follows semantic versioning; before 1.0, minor versions may change behaviour.

## [0.4.2] - 2026-11-19

### Fixed
- **Opening a review item, a source, a topic or an interview no longer crashes the page** (*rememberCollection is not defined*). Since 0.4.0 the app's frame remembers the collection for every Knowledge page, and these pages still called the helper they no longer imported.

## [0.4.1] - 2026-11-19

### Changed
- **Web pages are read as Markdown.** A captured web page or an uploaded HTML file keeps its structure: headings, bulleted and numbered lists (nested ones indented), code blocks with their spacing, bold, italics and quotes. A link keeps its words, not its address, and nothing is escaped (*snake_case* stays as written). New dependency `markdownify` (MIT).
- **Web page tables keep their columns.** A data table now reads like a Word or PowerPoint table: each row lists its values next to their column names (*Row 2: Plan: Plus; Price: 12; Billing: monthly*), so a row still reads correctly when it is quoted or cut from its header. A cell merged across columns names each of them in the header, empty cells move nothing, and a caption comes first. Tables that only arrange the page (marked as presentation, holding other tables, or with several paragraphs in a cell) stay plain text.
- **Heading anchors are dropped.** The small `#`, `¶` or `§` links many sites put next to headings no longer end up in the text.
- Pages already in a collection keep their text until they are read again.

## [0.4.0] - 2026-11-19

### Added
- **Figures in documents.** Screenshots, diagrams, charts and pictures are now read where they sit, in PDF, Word, PowerPoint and web pages.
  - With an AI model that reads images, each figure that carries information is described for someone who cannot see it, using the text around it for names and context. The description goes into the document's text where the figure sits, marked as the model's words: `[Figure 3, page 4, described by AI: …]`. The source page, topics and claims then use it like any other text.
  - Charts in Word and PowerPoint are read from the values stored in the file, with no AI: `[Chart 1, slide 5: Revenue]` followed by their rows.
  - Without AI, or over the limit, a figure keeps its caption or alt text (placeholders such as *image1.png* are ignored).
  - Logos, icons, dividers, pictures marked decorative and images repeated on many pages are skipped before any model call. A screenshot used twice is described once, and the model can answer that a picture carries nothing.
  - Figures with a caption, or that the text points to, are described first. *Most figures described per document* (`FIGURES_MAX`, 30) caps the cost; *Describe figures with the AI model* (`FIGURES_DESCRIBE`, on) turns it off.
  - The source says what happened, for example *14 figure(s): 9 described by AI, 5 skipped as decorative, repeated or too small*.
- **Scanned pages describe their figures too.** The image model returns the page's exact text and, apart from it, what its photos, diagrams, signatures or stamps show. The text stays exactly as read. Local OCR still reads text only.
- **Figures are kept with their source.** The images (except scanned pages, which cannot be cut up reliably) are stored with the document. They are listed on its page with what the text says about each, so a description can be checked against the picture. They are deleted with the source and replaced with a new version.
- **`library_figure` for agents.** `library_source` lists a document's figures, each with the passage it sits in. The new `library_figure` tool returns one figure's details and its image as an MCP image block, for agents that read images; the others use the description in the text. Both are only for credentials that may see sources.
- **Optional document converter** (`docker-compose.converter.yml`: LibreOffice in the Gotenberg image, on an internal network with no internet access). It draws PowerPoint slides whose diagram is built from shapes, and EMF or WMF pictures. Without it those figures are counted as *need the document converter*. New settings: *Document converter address* (`CONVERTER_URL`) and *password* (`CONVERTER_TOKEN`), with a test button on Settings > Library.
- A captured web page's own images are downloaded when it is read, through the same address guard as web capture (at most 60, 10 MB each). An uploaded HTML file's linked images are not fetched.
- SVG drawings are drawn on the server with resvg (new dependency `resvg-py`, MIT), after removing every reference to other files or addresses.
- **Pictures that carry nothing never reach the model.** Empty pictures, flat colours and gradient banners are dropped before any call, and so are a web page's small icons. A light drawing on a transparent background (a white icon) is shown on dark grey so it is visible, and inline SVG drawings get the colours the page defines for them (CSS custom properties) and a font the server has.
- **Descriptions are checked against the picture.** Before a description is kept, the picture is read on the server with Tesseract. A description that quotes labels the picture does not show was made up from the text around it, and is left out with a note (*N left out: their description named things the picture does not show*). The image model is also told to describe only what it can see and to answer NONE for icons and empty pictures.
- **The collection stays chosen.** One *Collection* selector sits at the top of every Knowledge page, as *Ontology version* does for the model. The choice is remembered in the browser and used by every page; a link that names a collection (`?c=`) still opens it. Switching collection on a single item goes to that section's list in the new collection. Review and Interview say plainly when the chosen collection does not allow them.
- **Review counts per collection.** The *Review* badge counts what waits in the collection you are working in. The selector shows each collection's count, and the Review page lists the other collections with items waiting. `/api/status` gains `library_review_by` (collection id -> count).

### Changed
- *Model that reads images and scanned pages (OCR)* is now *Model that reads images, scanned pages and figures*.
- Documents already in a collection get their figures when they are replaced with a new version or uploaded again.
- A captured web page downloads each image address once (pages repeat the same picture in every card).

### Fixed
- **The test suite no longer touches the app's data roles.** Login roles belong to the whole PostgreSQL server: tests run against the same server reset the passwords of the app's read-only data roles (`sd_…`) with the test key, and the app then could not read its data (*couldn't get a connection*). Tests now use their own roles (`SYNTH_ROLE_PREFIX`, `sdtest_` in the suite). If it happened to you, building the demo data again sets the passwords back.

## [0.3.2] - 2026-11-18

### Added
- **Replace a document with a new version.** *Replace with a new version* on a source's page (and `POST /api/library/collections/{ref}/sources/{id}/replace`) uploads a newer version of an uploaded file. It stays the same source: it is read again, and the topics it feeds are updated in place, keeping their names and links. Newer evidence wins, what the new version no longer says is withdrawn, and a topic it no longer covers is revised, archived only if no other source supports it. The topic analyzer is told which topics the previous version fed, so it updates them rather than creating new ones. Deleting the source and uploading the new file still works, but then the topics are written again from scratch. Refused, with the reason, for a web page (use *Refresh*), an interview, a document still being read, the same file again, or a file already in the collection. The previous file is removed from storage; an S3 bucket with versioning keeps it.

## [0.3.1] - 2026-11-18

### Fixed
- **An approved change now always reaches readers.** When a topic page was updated while a change waited for review, approving the change published it into the older version, so readers kept seeing the newer page without it. The approved change is now written on top of the page readers see and published as a new version; the held version stays as the record of what was reviewed.
- **A failed approval changes nothing.** The page is written before any statement changes, and everything is then saved at once. If the AI model fails, the change stays open with the reason, instead of the page becoming a plain list of facts.

### Changed
- **Approvals that need the AI model run in the background**, with feedback. The review page says when the page changed since the change was held, the buttons show what they are doing, the change is marked *approving…* in the queue while the page is written, and the page confirms when it is published or says why it failed. Approvals that need no model are published at once, with a confirmation. Without an AI model for the collection, such an approval is refused with the reason.

## [0.3.0] - 2026-11-18

### Added
- **Local OCR.** Scanned pages and images that no AI model reads (none is set, the collection does not allow AI, or the model fails on a page) are now read by Tesseract, included in the image. It runs on the server and sends nothing anywhere. Pages it reads are marked *local OCR*, and the source says how many. A page read with too little confidence is left out with a note instead of becoming a source, because a misread figure would otherwise pass the check against the document. New settings: *Local OCR when no AI model reads a page* (`OCR_LOCAL`, on), *Languages for local OCR* (`OCR_LOCAL_LANGUAGES`, `eng`), *Least confidence for a page read by local OCR* (`OCR_LOCAL_MIN_CONFIDENCE`, 70%). Other languages are added at build time with `--build-arg OCR_LANGS="ita fra"`.

### Changed
- **Word and PowerPoint tables are read in place.** A table now appears where it is in the document, not after all the text, and each row keeps its values with their column names (*Row 2: Tier: Tier 2; Audit: Every 24 months*), as spreadsheets already did. An empty cell no longer shifts the values after it into the wrong column, and a header merged across columns names each of them. Documents already in a collection keep their text until they are uploaded or refreshed again.
- A note now says when a scanned page gave no text when read.

## [0.2.5] - 2026-11-16

### Changed
- **Newer evidence wins on topic pages.** The topic writer now knows when each source was added. When sources disagree about something that changes over time (a version, a price, a limit, a date, a policy), the page states the value from the most recently added source as current and keeps an older one only as history, instead of describing the evidence as conflicting.
- **Disagreements the dates do not settle wait for a person.** When the writer or the revision pass finds sources that disagree and the newer one does not simply replace the older, the change is held on **Knowledge > Review** with the disagreeing statements, like other contradictions. A new topic in that case waits as a draft. A held change's summary and source link are recorded only when it is approved.

### Security
- Dependencies updated: `opentelemetry-api` 1.45.2.

## [0.2.4] - 2026-11-11

### Fixed
- Topic pages no longer keep statements from a document that has been removed or replaced by a newer upload. When a page still cites such a document, the revision pass reviews every withdrawn statement it may rest on, not only the most recent ones, and the writer is told which cited documents no longer exist. *Revise* on a topic now also cleans up pages in this state.

## [0.2.3] - 2026-11-11

### Fixed
- A topic page is revised even when the AI model writes a short note before the page. Such revisions used to fail with "the writer did not return a page", leaving the topic on its previous version and flagged for revision.

## [0.2.2] - 2026-11-11

### Added
- **Account administration, with no email service.** On **Users & access > Users** an administrator can **reset a password** (give one, or have a strong temporary one generated; it is shown once, the user's sessions end and they choose their own at the next sign-in), **disable** and re-enable an account (it cannot sign in, its grants and history stay), and **delete** a disabled account (refused while the user is the only manager of a collection). The last administrator who can sign in cannot be disabled, deleted or demoted.
- **`python -m studio.passwd`**, to run in the app container (`docker compose exec app python -m studio.passwd <username>`): a way back in when nobody can sign in. It prints a new temporary password and enables the account if it was disabled; `--list` shows the users. Each use is written to the audit log.
- The audit log records user administration (create, reset, disable, enable, delete) and the deletion of a token. Passwords are never recorded.

### Changed
- Adding a user whose name already exists is refused (HTTP 409) instead of silently replacing that account. The button reads *Add user*.

## [0.2.1] - 2026-11-10

### Security
This release hardens several parts of the platform. Upgrading is recommended for every installation, and in particular for those that connect to their own database or run the browser service.
- **Data queries:** stricter checks on what a query may contain. Every column must be named, and only standard functions that compute from their arguments can be called. Add your own safe database functions with `SQL_ALLOWED_FUNCTIONS` in `.env`.
- **Masked columns:** tighter rules. A masked column joins only to another masked column, and ordering by one orders by its hash.
- **Request size:** requests to `/api` and `/mcp` have a size limit (`MAX_BODY_MB`, default 4; uploads and Excel models keep their own limits), and larger requests are accepted only from a signed-in caller or a valid token.
- **Secrets:** the app does not start with a missing, placeholder or short (under 32 characters) `SECRET_KEY`, nor with the placeholder passwords of `.env.example`.
- **Browser service:** a current browser (Playwright 1.64.1) that runs inside its own sandbox. The container needs the seccomp profile and settings of the new `docker-compose.fetcher.yml`: rebuild it with `--build`. On a host without user namespaces, set `FETCH_NO_SANDBOX=1`.

### Changed
- A query that joins a masked column to a readable column, or calls a database function that is not a standard one, is now refused with an explanation.

### Added
- **Show sources**, a fifth entitlement on tokens and users. Off, the document tools return the content without the trail: no `[s1]` citation markers, no `sources` list, no links into the app, no collection ids, claims as their exact quotes, and `library_topic_sources` is not offered. The tool descriptions follow, so an agent is not told about what it cannot get. On by default, so nothing changes for existing credentials; Alfred uses the signed-in user's switch. Set it on **Users & access**.

## [0.2.0] - 2026-11-09

First public release.

### Ontology and data
- Governed semantic model: entities, fields, calculated fields, relationships, named filters, checks, query examples, custom properties; drafts, review, versioned publishing with an audit trail and optional four-eyes rule.
- Start from the Excel template or generate a first draft from a database (PostgreSQL, MySQL, MariaDB, ClickHouse, SQLite, DuckDB).
- Model check against the data, synthetic demo database, read-only SQL console.
- Access control by sensitivity class (confidential, personal, restricted) for users and API tokens; personal columns hashed, restricted ones never returned; every statement parsed, checked and rebuilt under the caller's entitlements.

### Knowledge base
- Collections with grants, a public or internal usage tag, and switches for AI and agent access.
- Files, web pages (with an optional browser service) and expert interviews.
- Verified source wiki per document (written, checked claim by claim, amended, reviewed when doubtful), topics written from the full wikis, claims with verbatim quotes and their wiki version, typed relationships, surgical revision when sources change.
- Every layer chunked, embedded and searchable by meaning and words.
- Review queue for contradictions, probable duplicates, doubtful pages, model links and vocabulary proposals; health checks; collection export.
- Bridge from topics to the entities of the model.

### Agents
- MCP endpoint with sixteen read-only tools, self-explaining instructions, `next` hints and ready prompts; Alfred, the built-in assistant.
- Agents read less for the same answer: compact tool results, short source references, leaner tool definitions, and prompt caching for the assistant and for the source checker.
- MCP instructions teach agents to read a question in three steps (understand the words, route to documents, data or both, answer with evidence); every tool description stands on its own and every result carries a `next` hint.

### Interface
- A web app with the brand typeface and the logo's teal accent, in light and dark themes.

### Operations
- Docker images (non-root, health checks), optional worker, browser service and S3 storage overlays, any OpenAI-compatible AI endpoint, local embeddings by default, activity log with tokens and cost.
