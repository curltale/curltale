# Design notes

How Curltale AI is put together and why. The README is the user-facing reference; this note records the design decisions behind it.

## 1. Goal

One product with two halves that share one login, one settings page, one activity log, one MCP endpoint and one assistant:

- **Ontology**: the centrally governed semantic model of the data. Drafted, reviewed, published, versioned.
- **Knowledge**: documents turned into a verified wiki of topics, organised in **collections**, each with its own access.
- **The bridge** between them: a topic can point at model objects; the model never points at collection content.

MIT licence for the whole product. Nothing shipped contains a particular customer's use case.

## 2. Principles

1. **Collections are the unit of isolation.** Every row derived from a document carries its `collection_id`. Nothing derived (chunks, wikis, topics, claims, links, vocabulary, jobs) crosses a collection.
2. **The model stays clean.** Nothing in the governed model references collection content. Links live inside the collection (topic to model object), so no access means no link, never a broken one.
3. **Configuration lives in Settings.** Environment variables are for bootstrap (database URL, secret key, first admin, port, data directory) and defaults. Everything else (AI endpoint, models, storage, embeddings, reranker, fetcher, limits) is a setting, stored encrypted where secret, audited, with the precedence page > environment > default.
4. **No unscoped query path.** Every Library function takes an explicit scope or collection handle. A missing scope is a programming error, not an empty result.
5. **Agents only see what the caller may see**: token or user entitlements for the model and the data, collection grants for documents.
6. **Verify before serving.** Documents are rewritten as verified pages before anything is built from them, and only approved and published content is ever searched by agents.

## 3. Roles and access

Users hold a **set of roles**: viewer (everyone signed in), modeller (edit the model draft), publisher (publish it; includes modeller), editor (ingest, edit and approve documents where granted write), admin (everything).

Roles say **what kind of work**; collection grants say **where**. Grants are subject (user or API token) x collection x level:

| Level | Can |
|---|---|
| read | search, read topics and sources, cite |
| write | ingest, edit, resolve reviews (users also need the editor role) |
| manage | grant and revoke, change collection settings, export, delete |

Admins create collections and appoint a manager. An admin does not automatically read content: they can grant themselves access, and that is audited.

## 4. The knowledge model

| Layer | What it is | Indexed as |
|---|---|---|
| Original text | The extracted text of a document, with its places (page, slide, sheet). | Passages: vectors and words |
| Source wiki | A standalone page written from one document, verified claim by claim against it; every version kept. | Every version chunked: vectors and words |
| Topic | A page by subject, written from the full verified wikis of every source about it, following its type's template. | Every version chunked; the topic itself embedded with its title, aliases, summary and claims |
| Claim | One atomic fact with its verbatim quote, the source, the wiki version and passage. Superseded, never deleted. | Embedded |
| Relationship | A typed link between two topics with why they relate. | Embedded with its context |

Search everywhere combines meaning and words by reciprocal rank fusion, filtered by collection. Agents only ever search the approved version of a source wiki and the published version of an active topic.

**Pipeline.** Extract and chunk (no AI needed) -> write the source wiki -> verify it (whole text when it fits, tools otherwise; a failure that names no problem is re-checked, never amended blindly; the checker reviews its own findings once) -> amend from the evidence -> approve or hold for a person -> analyze (create, update or complement up to 12 topics, extract claims whose quotes must be in the wiki) -> write each topic from the full wiki, with tools to read related topics and other approved wikis -> store claims with their wiki version -> link topics to the model.

**Change.** A source read again supersedes the claims only it supported; a topic it no longer mentions gets a surgical rewrite that checks each withdrawn claim against the remaining sources. A deleted source keeps its quotes (its id becomes empty) so the lint pass can audit what the topic used to rest on. A topic with no source left is archived.

**Review gate.** A change that contradicts a published claim, and a new topic very similar to an existing one, are written but held until a person approves, rejects or merges them.

## 5. Vocabulary and the bridge

Each collection has its **own topic and relation vocabulary**, seeded from a starter ontology (topic types with descriptions and page templates; relation types with inverses). New kinds proposed by the analyzer wait for the collection manager. The governed model is never touched by library vocabulary.

Example: a document describes a mobile product called *Business Mobile*. The Library creates a topic *Business Mobile* in that collection and links it to the model entity *Product*. The model gets no new entity and no new relationship. Only people (and tokens) with access to that collection see *Business Mobile* as linked to *Product*.

Topic-to-model links are `instance_of / about / governs / uses` with a confidence: 0.7 and above are confirmed automatically, lower ones are suggestions a writer confirms. On read, each link is resolved against the caller's entitlements; links to hidden or restricted model objects are dropped for that caller. The reverse view ("documents about this entity") is computed at query time from the caller's grants.

## 6. Platform

| Area | Decision |
|---|---|
| Database | Library tables in their own Postgres schema `library`, applied idempotently at startup |
| Embeddings | One embedding model per instance; the vector size follows it. HNSW index where pgvector allows it (vector up to 2000 dimensions, halfvec up to 4000, exact search beyond). Changing the size empties the vectors and a backfill recomputes them. A local model is the default |
| Languages | Depend on the embedding model; chunk size follows the model's input limit |
| LLM | One OpenAI-compatible endpoint (OpenRouter, LiteLLM, Ollama, vLLM). A model per role (writer, verifier, analyzer, topic writer, assistant, interviewer, OCR). Optional reranker |
| Storage | Driver interface: `local` by default, `s3` with endpoint, bucket and keys. Deleting a source deletes every stored version |
| Jobs | A Postgres queue; an optional worker container of the same image; the Library can be switched off |
| Fetching | Web capture with SSRF protection; an optional, isolated browser container for pages that need JavaScript |
| PDF | `pypdfium2` (permissive licence) |

## 7. The model holds ontology only

Rules, terms, processes and notes are not part of the model: they are Library content, retrieved when needed (`plan_query` lists the relevant topics briefly, capped at 25 with a note when there are more). The model keeps its ontology and an **Examples** sheet of example queries and filters.

## 8. Usage tag: public or internal

Each collection carries one tag, set by its manager: **public** (may be shared with customers) or **internal** (for the agent's own understanding; never quoted or referred to when talking to customers).

- The tag belongs to the collection only, so a collection never mixes the two.
- It is **guidance, not access control**. Real protection is collection grants and token entitlements; a customer-facing agent gets a token with grants on public collections only.
- The MCP carries the rule itself, in three layers generated by the server: the instructions sent at connection, one sentence in every document tool's description, and a `usage` field with its instruction on every result. Tests make sure no document tool returns a result without it.

## 9. Agents

- One MCP endpoint. Document tools appear only when the credential can read a collection; `library_context` is the primary one (whole topic pages, matched passage, linked topics, `answerable`).
- The instructions teach an agent to read a question: understand the words through the ontology, route to the documents, the data or both, answer with evidence. Every tool description stands on its own for clients that never show the instructions, and every result carries a `next` hint.
- Results are grouped per collection, never merged, each with its usage tag.
- Alfred uses the same tools under the signed-in user's access.

## 10. Isolation tests

Two collections A and B with a unique marker in each. For every entry point (REST, MCP, Alfred, graph, search, export, activity log, job payloads), as a user or token with a grant only on A, the marker of B must never appear in results, errors or counts. The suite fails on any leak.
