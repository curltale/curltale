"""The starter ontology every collection begins with (topic types with their description and page template,
relation types with their inverse and description). A collection then goes its own way; nothing it adds reaches another collection."""

TOPIC_TYPES = [('Concept',
  "An abstract business or technical idea, methodology, or principle that explains HOW something works or WHY it matters — not a concrete artifact you can click, configure, or measure. E.g. 'Lead "
  "Scoring', 'Revenue Recognition', 'Account Segmentation'. NOT a Term (which is a vocabulary definition), NOT a Feature (which is a product capability the user interacts with), NOT a Metric (which "
  'has a formula and is measured).',
  '## Overview\n'
  '[1–2 sentence definition of the concept and the domain it belongs to.]\n'
  '\n'
  '## How It Works\n'
  '[Explain the mechanism, approach, or logic behind this concept.]\n'
  '\n'
  '## Business Value\n'
  '[Why this concept matters operationally or strategically.]\n'
  '\n'
  '## Key Considerations\n'
  '[Prerequisites, caveats, or important nuances practitioners must know.]\n'
  '\n'
  '## Related Topics\n'
  '[Links to related concepts, metrics, features, or processes.]\n'),
 ('Data Entity',
  "A named record type, domain object, or data model entity that exists as a structured record in a system. E.g. 'Lead', 'Opportunity', 'Invoice', 'Purchase Order', 'Contact', 'Account', 'Case'. The "
  'structural building blocks of CRM/ERP data models. NOT a Product (a commercial offering), NOT a Module (a software component), NOT a Feature (a UI capability). If the topic describes a type of '
  'database record — use this.',
  '## Definition\n'
  '[What this record type represents and in which system or module it lives.]\n'
  '\n'
  '## Key Fields\n'
  '[The most important attributes, their data types, and purpose.]\n'
  '\n'
  '## Lifecycle\n'
  '[How records of this type are created, updated, and closed or archived.]\n'
  '\n'
  '## Relationships\n'
  '[How this entity relates to other data entities — parent/child, lookup fields, etc.]\n'
  '\n'
  '## Business Rules\n'
  '[Validation constraints, mandatory fields, or business logic that governs this record.]\n'
  '\n'
  '## Access & Permissions\n'
  '[Which roles can create, read, update, or delete this record type.]\n'),
 ('Error',
  "A known error condition, fault code, exception class, or documented failure mode with a specific identity. E.g. 'HTTP 429 Rate Limit Error', 'Sync Conflict Error', 'Batch Job Timeout', "
  "'ERR_INVALID_TOKEN'. The topic IS the error itself — not the fix workflow (use Support Procedure) and not a Q&A about the error (use FAQ). Use this when the error has a name or code worth "
  'indexing.',
  '## Description\n'
  '[What this error means, what system or component raises it, and when.]\n'
  '\n'
  '## Common Causes\n'
  '[The typical root causes or conditions that trigger this error.]\n'
  '\n'
  '## Resolution\n'
  '[Step-by-step fix or the recommended workaround.]\n'
  '\n'
  '## Prevention\n'
  '[Configuration changes, best practices, or guards that prevent this error.]\n'
  '\n'
  '## Related Errors\n'
  '[Similar or related error conditions that may be confused with this one.]\n'),
 ('Event',
  "A significant, named point-in-time occurrence: a scheduled business milestone, a system event, a lifecycle moment, or a named incident. E.g. 'Annual Contract Renewal', 'System Maintenance "
  "Window', 'Q4 Business Review', 'Data Migration Cutover'. NOT a Release (which is specifically a software version with a version number — use Release for those). NOT a Process (which is a "
  'repeatable workflow, not a one-time occurrence).',
  '## Summary\n'
  '[Brief description of the event and its business significance.]\n'
  '\n'
  '## Date / Schedule\n'
  '[When it occurred, recurs, or is scheduled — include timezone if relevant.]\n'
  '\n'
  '## Participants\n'
  '[Who is involved: teams, roles, stakeholders, or systems.]\n'
  '\n'
  '## Impact\n'
  '[What systems, data, or users are affected during or after the event.]\n'
  '\n'
  '## Actions Required\n'
  '[What users, administrators, or teams need to do before, during, or after.]\n'
  '\n'
  '## Related Processes\n'
  '[Links to runbooks or processes triggered by this event.]\n'),
 ('FAQ',
  "A discrete, self-contained question-and-answer pair where the question is explicit and the answer is definitive. E.g. 'Can I export data in bulk?', 'What is the maximum file upload size?'. NOT a "
  'Support Procedure (which is a multi-step remediation guide for a problem — use that instead). NOT a Concept (which explains an idea without Q&A structure). The question must be answerable in one '
  'or a few sentences; if it requires a numbered steps list, use Support Procedure.',
  '## Question\n'
  '[The exact question being answered, phrased as a user would ask it.]\n'
  '\n'
  '## Answer\n'
  '[Direct, definitive answer to the question.]\n'
  '\n'
  '## Context\n'
  '[Additional background, conditions, or plan/role restrictions that affect the answer.]\n'
  '\n'
  '## Related Questions\n'
  '[Links to related FAQs or topics.]\n'),
 ('Feature',
  "A discrete, named capability of a product or module that a user can directly interact with, enable, or configure. E.g. 'Two-Factor Authentication', 'Bulk Export', 'Real-Time Sync Dashboard', "
  "'Custom Field Builder'. Use Module for a major software component containing many features. Use Integration for a system connector. Use Service for a managed/delivered capability (not "
  'self-serve). NOT a Process (which describes a workflow for using it). If you can click it in a UI or toggle it in settings, it is likely a Feature.',
  '## Overview\n'
  '[What this feature does, which module or product it belongs to, and who uses it.]\n'
  '\n'
  '## How to Use\n'
  '[Key usage steps or configuration instructions.]\n'
  '\n'
  '## Requirements\n'
  '[Minimum plan, license, role, or configuration prerequisites to access this feature.]\n'
  '\n'
  '## Limitations\n'
  '[Known constraints, quotas, edge cases, or unsupported scenarios.]\n'
  '\n'
  '## Related Features\n'
  '[Connected, dependent, or complementary features.]\n'),
 ('Integration',
  "A named connection, API endpoint, connector, webhook, or interoperability bridge between two or more systems. E.g. 'Salesforce-SAP Bidirectional Sync', 'Stripe Payment Webhook', 'Google Workspace "
  "SSO', 'REST API v3'. NOT a Feature (which is a standalone product capability the user interacts with directly). NOT a Process (which describes how to set up the integration). If the topic "
  'describes HOW two systems exchange data or authenticate — it is an Integration.',
  '## Overview\n'
  '[What systems are connected, what data flows between them, and in which direction.]\n'
  '\n'
  '## Authentication & Setup\n'
  '[Authentication method, credentials required, and configuration steps.]\n'
  '\n'
  '## Data Mapping\n'
  '[What objects, fields, or events are synchronized or exchanged.]\n'
  '\n'
  '## Sync Behavior\n'
  '[Frequency, triggers, conflict resolution, and error handling.]\n'
  '\n'
  '## Limitations & Constraints\n'
  '[Rate limits, supported API versions, known gaps, or unsupported scenarios.]\n'
  '\n'
  '## Troubleshooting\n'
  '[Common integration issues, error codes, and fixes.]\n'),
 ('Location',
  "A physical place, geographic region, legal jurisdiction, or data residency zone relevant to operations, compliance, or service delivery. E.g. 'EMEA Region', 'Frankfurt Data Centre', 'State of "
  "California', 'EU Data Residency Zone', 'APAC'. NOT an Organization (which is an entity operating in a location). Use this when the location itself is the subject — for compliance scope, data "
  'sovereignty, or operational coverage.',
  '## Description\n'
  '[What this location is and its relevance to operations or compliance.]\n'
  '\n'
  '## Regulatory Context\n'
  '[Applicable laws, directives, or compliance frameworks for this location.]\n'
  '\n'
  '## Data Residency\n'
  '[What categories of data are stored or processed here.]\n'
  '\n'
  '## Service Coverage\n'
  '[Support hours, SLA variations, or operational specifics for this location.]\n'
  '\n'
  '## Restrictions\n'
  '[Any data transfer restrictions or special requirements.]\n'),
 ('Metric',
  "A named, measurable indicator, KPI, SLA threshold, or business performance figure with a defined formula or measurement method. E.g. 'Customer Acquisition Cost (CAC)', 'First Response Time SLA', "
  "'Gross Revenue Retention (GRR)', 'Monthly Recurring Revenue (MRR)'. NOT a Concept (a concept is an idea; a metric is measured with numbers). NOT a Term (a term is a vocabulary definition; a "
  'metric has a formula and a target). If it has a formula, a unit, and is tracked over time — it is a Metric.',
  '## Definition\n'
  '[Precise definition of what is measured and what it represents.]\n'
  '\n'
  '## Formula\n'
  '[How it is calculated, including numerator, denominator, and any exclusions.]\n'
  '\n'
  '## Unit & Range\n'
  '[The unit of measurement and a typical healthy range or benchmark.]\n'
  '\n'
  '## Target / SLA\n'
  '[The expected threshold, target, or contractual commitment.]\n'
  '\n'
  '## Data Source\n'
  '[Which system, report, or data entity provides the underlying data.]\n'
  '\n'
  '## Interpretation\n'
  '[How to read this metric and what actions different values should trigger.]\n'),
 ('Module',
  "A major, discrete software component, application module, or self-contained functional area within a platform. E.g. 'Sales Cloud', 'Financial Management Module', 'CRM Analytics', 'Inventory "
  "Management', 'HR Core'. Larger than a Feature (a single capability); a Product is composed of Modules, a Module is composed of Features. NOT a Product (the top-level commercial offering). NOT an "
  'Integration (a connector between systems). If it represents a distinct section of the product with its own navigation, licensing, or configuration — it is a Module.',
  '## Overview\n'
  '[What this module does, which product it belongs to, and which user personas use it.]\n'
  '\n'
  '## Key Features\n'
  '[The main capabilities and screens within this module.]\n'
  '\n'
  '## Configuration & Setup\n'
  '[Required initial configuration, roles, or dependencies.]\n'
  '\n'
  '## Integrations\n'
  '[Systems or other modules this module connects with.]\n'
  '\n'
  '## Licensing & Requirements\n'
  '[Which plan or license is required to access this module.]\n'
  '\n'
  '## Limitations\n'
  '[Known constraints or unsupported scenarios.]\n'),
 ('Organization',
  "A named company, business unit, department, team, partner, vendor, or institution. E.g. 'Acme Corp', 'Finance Department', 'AWS Professional Services', 'EMEA Sales Team'. NOT a Role (which is a "
  'job function without a named entity). NOT a Person (which is a named individual). Use this for any named organizational entity — internal or external — that acts as a stakeholder, owner, or '
  'participant.',
  '## Overview\n'
  '[What this organization is, its purpose, and its place in the broader structure.]\n'
  '\n'
  '## Responsibilities\n'
  '[Key functions, domains of ownership, or services provided.]\n'
  '\n'
  '## Key Contacts\n'
  '[Primary roles or named contacts (link to Person topics where applicable).]\n'
  '\n'
  '## Relationships\n'
  '[Parent organizations, subsidiaries, partners, or customer relationships.]\n'),
 ('Person',
  "A named, specific individual — a human with an identity. E.g. 'Jane Smith, CFO', 'John Doe — Lead Architect'. Use Role for unnamed job functions or personas. NOT an Organization. Only create a "
  'Person topic when the individual is directly relevant to the knowledge base (author, key contact, decision-maker) — not for incidental mentions.',
  '## Role & Title\n'
  "[This person's current title and organizational unit.]\n"
  '\n'
  '## Key Responsibilities\n'
  '[What they own, lead, or are accountable for.]\n'
  '\n'
  '## Contact\n'
  '[How to reach them — email, Slack, or preferred channel.]\n'
  '\n'
  '## Related Topics\n'
  '[Processes, projects, or organizations this person is associated with.]\n'),
 ('Plan',
  "A named commercial pricing tier, subscription package, license type, or service level agreement tier. E.g. 'Enterprise Plan', 'Starter Tier', 'Professional Annual License', 'Premium Support SLA'. "
  'NOT a Product (which is the overall offering — a Plan defines terms and pricing for accessing a Product or Service). NOT a Service (which is a delivered capability — a Plan governs what is '
  'included in that service). If the topic describes what is included at a given price point or contract tier — it is a Plan.',
  '## Overview\n'
  '[What this plan includes, who it is designed for, and at what level.]\n'
  '\n'
  '## Included Features & Modules\n'
  '[Capabilities, modules, and entitlements included in this plan.]\n'
  '\n'
  '## Limits & Quotas\n'
  '[Usage caps, seat limits, storage, or API call limits.]\n'
  '\n'
  '## Pricing\n'
  '[Cost structure — list price, billing cycle, or enterprise negotiation note.]\n'
  '\n'
  '## Excluded or Upgrade Items\n'
  '[What requires add-ons or a higher plan tier.]\n'
  '\n'
  '## Upgrade Path\n'
  '[Which plans precede or follow this one in the tier structure.]\n'),
 ('Policy',
  "An internally authored rule set, governance document, operational standard, or guidance document. E.g. 'Data Retention Policy', 'Acceptable Use Policy', 'Expense Approval Policy', 'Change "
  "Management Policy'. NOT a Regulation (which is externally mandated by law or a standards body — use Regulation for those). NOT a Process (which is a workflow for doing something — a Policy states "
  "the rules, a Process describes how to follow them). If the document says 'you must' or 'it is prohibited' and was authored internally — it is a Policy.",
  '## Purpose\n'
  '[Why this policy exists and what risk or obligation it addresses.]\n'
  '\n'
  '## Scope\n'
  '[Who this policy applies to — roles, regions, systems, or data types.]\n'
  '\n'
  '## Rules & Requirements\n'
  '[The specific obligations, prohibitions, or standards.]\n'
  '\n'
  '## Exceptions & Escalations\n'
  '[Approved exceptions, waiver process, or escalation contacts.]\n'
  '\n'
  '## Enforcement\n'
  '[How compliance is monitored and what happens on violation.]\n'
  '\n'
  '## Review Cycle\n'
  '[How often this policy is reviewed and who owns it.]\n'),
 ('Process',
  "A named, repeatable, multi-step operational or business workflow that produces a defined outcome. E.g. 'Quote-to-Cash', 'Employee Onboarding', 'Incident Management', 'Lead Qualification', "
  "'Monthly Close'. NOT a Support Procedure (which is reactive troubleshooting for a specific problem — a Process is proactive and routine). NOT a Feature (which is a product capability, not a "
  'workflow). If it has defined steps, roles, inputs, and outputs and runs repeatedly — it is a Process.',
  '## Purpose\n'
  '[What business outcome this process achieves and why it exists.]\n'
  '\n'
  '## Scope & Trigger\n'
  '[Who performs this process, when it is initiated, and what triggers it.]\n'
  '\n'
  '## Steps\n'
  '[Ordered list of steps with the responsible role for each.]\n'
  '\n'
  '## Inputs & Outputs\n'
  '[What data, documents, or states are required as inputs and produced as outputs.]\n'
  '\n'
  '## Roles & Responsibilities\n'
  '[RACI or ownership summary for key steps.]\n'
  '\n'
  '## Exceptions & Escalations\n'
  '[Known edge cases, failure modes, and escalation paths.]\n'),
 ('Product',
  "A named top-level commercial product, platform, or product family sold or delivered as a whole. E.g. 'Salesforce CRM', 'SAP S/4HANA', 'Microsoft 365', 'Oracle NetSuite', 'Zendesk Support'. "
  'Broader than a Module (a component within the product) or Feature (a single capability). Use Plan for a pricing tier of a product. If the topic describes something you can buy or subscribe to as '
  'a named offering — it is a Product.',
  '## Overview\n'
  '[What this product does, which market it serves, and who uses it.]\n'
  '\n'
  '## Key Modules\n'
  '[The major functional areas or modules that compose this product.]\n'
  '\n'
  '## Target Users\n'
  '[Primary user personas, departments, or industries.]\n'
  '\n'
  '## Deployment Options\n'
  '[Cloud, on-premise, hybrid, or multi-tenant specifics.]\n'
  '\n'
  '## Available Plans\n'
  '[Pricing tiers or editions — link to Plan topics.]\n'
  '\n'
  '## Key Integrations\n'
  '[Commonly used integrations — link to Integration topics.]\n'),
 ('Regulation',
  "An externally mandated law, standard, certification, directive, or regulatory framework authored by a government, standards body, or industry authority. E.g. 'GDPR', 'SOC 2 Type II', 'ISO 27001', "
  "'PCI-DSS', 'HIPAA', 'SOX'. NOT a Policy (which is internally authored — use Policy for those). NOT a Process (which is the workflow for achieving compliance). If the source is external to the "
  'organization and carries legal or certification consequences — it is a Regulation.',
  '## Overview\n'
  '[What this regulation requires, who authored it, and when it came into force.]\n'
  '\n'
  '## Scope & Applicability\n'
  '[Which industries, geographies, data types, or organizations this applies to.]\n'
  '\n'
  '## Key Requirements\n'
  '[The main compliance obligations — summarized as actionable items.]\n'
  '\n'
  '## Penalties\n'
  '[Consequences of non-compliance: fines, sanctions, or loss of certification.]\n'
  '\n'
  '## Relevant Internal Controls\n'
  '[Policies, processes, or features that implement compliance with this regulation.]\n'
  '\n'
  '## Certification / Audit Process\n'
  '[How compliance is assessed, certified, or audited.]\n'),
 ('Release',
  "A named software version, product release, patch, hotfix, or changelog entry with a version identifier. E.g. 'v4.2.1 Hotfix', 'Spring 2024 Platform Update', 'API v3 Deprecation', '2024.Q1 "
  "Release'. NOT an Event (which is a business or operational milestone without a version number — use Event for those). If the topic describes what changed in a specific version of a software "
  'product — it is a Release.',
  '## Summary\n'
  '[High-level summary of what changed and why this release is significant.]\n'
  '\n'
  '## Version & Date\n'
  '[Version number, release date, and affected products or modules.]\n'
  '\n'
  '## New Features\n'
  '[New capabilities introduced in this release — link to Feature topics.]\n'
  '\n'
  '## Changes & Improvements\n'
  '[Modifications to existing behavior or performance improvements.]\n'
  '\n'
  '## Bug Fixes\n'
  '[Resolved issues — reference error codes or support tickets where relevant.]\n'
  '\n'
  '## Deprecations & Removals\n'
  '[Capabilities removed or scheduled for removal.]\n'
  '\n'
  '## Breaking Changes & Upgrade Notes\n'
  '[Migration steps, API changes, or configuration updates required.]\n'
  '\n'
  '## Known Issues\n'
  '[Outstanding problems with workarounds if available.]\n'),
 ('Role',
  "A named job function, user persona, access profile, or system actor — without reference to a specific named individual. E.g. 'Sales Manager', 'System Administrator', 'Read-Only Viewer', "
  "'Procurement Officer', 'Finance Controller'. NOT a Person (which is a named individual — use Person when a specific human is the subject). NOT an Organization (which is a named entity). If the "
  'topic describes a function or permission set that any person could hold — it is a Role.',
  '## Description\n'
  '[What this role does, which team or domain it belongs to, and its organizational level.]\n'
  '\n'
  '## Key Responsibilities\n'
  '[Primary duties, decisions owned, and accountability areas.]\n'
  '\n'
  '## Required Permissions & Access\n'
  '[System permissions, license type, or modules required for this role.]\n'
  '\n'
  '## Collaboration\n'
  '[Roles this role regularly works with or hands off to.]\n'
  '\n'
  '## Related Processes\n'
  '[Processes where this role has a defined step or ownership.]\n'),
 ('Service',
  "A delivered, managed, or professional service offering provided to customers or internal users — involving human delivery or ongoing managed operations. E.g. '24/7 Premium Support', "
  "'Implementation & Onboarding Services', 'Managed Backup', 'Consulting Engagement'. NOT a Feature (which is a self-serve product capability the user operates). NOT a Product (which is a software "
  'offering, not a delivered service). If someone delivers it as a service (not a UI button the user clicks) — it is a Service.',
  '## Overview\n'
  '[What this service delivers, to whom, and in what context.]\n'
  '\n'
  '## Scope of Delivery\n'
  '[What is explicitly included and excluded from this service.]\n'
  '\n'
  '## SLA & Commitments\n'
  '[Response time, availability, quality guarantees, or escalation commitments.]\n'
  '\n'
  '## Engagement Model\n'
  '[How to request, onboard, or contract for this service.]\n'
  '\n'
  '## Pricing\n'
  '[Cost structure or how pricing is determined — link to Plan topics.]\n'
  '\n'
  '## Key Contacts\n'
  '[Who to contact — link to Role or Person topics.]\n'),
 ('Support Procedure',
  "A reactive, step-by-step troubleshooting guide, runbook, or remediation workflow for a specific known issue, error condition, or failure scenario. E.g. 'Resetting a Locked Account', 'Clearing the "
  "Sync Queue', 'Emergency Rollback Procedure'. NOT a Process (which is a proactive, routine operational workflow — not triggered by a problem). NOT a FAQ (which is Q&A format without numbered "
  'remediation steps). NOT an Error topic (which describes the error itself — a Support Procedure describes how to fix it). If the document exists because something went wrong — it is a Support '
  'Procedure.',
  '## Problem Description\n'
  '[The specific issue, error condition, or failure scenario this procedure addresses.]\n'
  '\n'
  '## Affected Systems & Versions\n'
  '[Which products, modules, or versions are affected.]\n'
  '\n'
  '## Prerequisites\n'
  '[What must be in place or verified before starting — permissions, backups, etc.]\n'
  '\n'
  '## Steps\n'
  '[Numbered remediation steps with expected outcomes for each.]\n'
  '\n'
  '## Verification\n'
  '[How to confirm the issue is resolved and the system is healthy.]\n'
  '\n'
  '## Escalation\n'
  '[When this procedure is insufficient and how to escalate — include contacts or runbooks.]\n'),
 ('Term',
  "A domain vocabulary entry, glossary definition, or technical term whose primary value is its definition. E.g. 'ARR (Annual Recurring Revenue)', 'Churn Rate', 'SKU', 'Idempotency', 'Upsell'. NOT a "
  "Concept (which explains a methodology or approach in depth — a Term is a definition, not an explanation). NOT a Metric (which is measured with a formula and a target). If the topic's entire value "
  "is 'this word means X' — it is a Term.",
  '## Definition\n'
  '[Precise definition of the term in one or two sentences.]\n'
  '\n'
  '## Context\n'
  '[Domain, system, or industry context where this term is used — and how usage may vary.]\n'
  '\n'
  '## Related Terms\n'
  '[Synonyms, antonyms, parent concepts, or closely related vocabulary.]\n'
  '\n'
  '## Example\n'
  '[A concrete usage example showing the term in context.]\n')]

RELATION_TYPES = [('has_part',
  'part_of',
  'Structural, hierarchical containment: the subject contains the object as a component. E.g. Product has_part Module; Module has_part Feature; Process has_part Process (sub-process). Use for '
  'whole-part composition where the part is meaningless outside the whole. NOT governs (authority relationship). NOT depends_on (dependency, not structural).'),
 ('part_of',
  'has_part',
  'Inverse of has_part. The subject is a structural component of the object. E.g. Feature part_of Module; Module part_of Product. Use when writing from the component perspective.'),
 ('governs',
  'governed_by',
  'An authoritative entity mandates, constrains, or regulates the object. E.g. GDPR governs Data Entity; Expense Policy governs Process; Enterprise Plan governs Feature (entitlement). NOT part_of '
  '(structural). Use when a Regulation, Policy, or Plan sets binding rules for another topic.'),
 ('governed_by',
  'governs',
  'Inverse of governs. The subject is mandated, constrained, or regulated by the object. E.g. Data Entity governed_by GDPR; Process governed_by Change Management Policy. Use when writing from the '
  'governed entity perspective.'),
 ('implements',
  'implemented_by',
  'The subject concretely realizes, enacts, or gives form to the object. E.g. Feature implements Concept; Service implements Process; Product implements Regulation. NOT depends_on (dependency, not '
  'realization). Use when the subject is the concrete expression of an abstract object.'),
 ('implemented_by',
  'implements',
  'Inverse of implements. The abstract subject is realized or enacted by the object. E.g. Concept implemented_by Feature; Regulation implemented_by Policy. Use when writing from the abstract topic '
  'perspective.'),
 ('depends_on',
  'required_by',
  'The subject cannot operate or function correctly without the object — a hard dependency. E.g. Integration depends_on Feature; Process depends_on Data Entity. NOT uses (softer). NOT part_of '
  '(structural). Use when absence of the object breaks the subject.'),
 ('required_by', 'depends_on', 'Inverse of depends_on. The object cannot function without the subject. E.g. Feature required_by Integration. Use when writing from the dependency perspective.'),
 ('uses',
  'used_by',
  'The subject consumes, references, or relies on the object — a soft dependency. E.g. Process uses Data Entity; Role uses Feature. Softer than depends_on: the subject can exist without the object. '
  'Use for optional or contextual consumption relationships.'),
 ('used_by', 'uses', 'Inverse of uses. The subject is consumed or referenced by the object. E.g. Data Entity used_by Process. Use when writing from the consumed resource perspective.'),
 ('integrates_with',
  'integrates_with',
  'Two systems, products, or modules have a defined technical integration or data exchange. Symmetric. E.g. Salesforce CRM integrates_with SAP S/4HANA. Use for technical interoperability — not '
  'general relationships.'),
 ('replaces',
  'replaced_by',
  "The subject functionally supersedes or deprecates the object. E.g. Feature 'New Dashboard' replaces Feature 'Legacy Dashboard'. NOT successor_of (versioning lineage). Use for functional "
  'substitution where the old item is being retired.'),
 ('replaced_by', 'replaces', 'Inverse of replaces. The subject has been superseded or deprecated by the object. Use when writing from the deprecated item perspective.'),
 ('successor_of',
  'predecessor_of',
  'The subject is the next version, iteration, or evolution of the object in a defined series. E.g. Release v5.0 successor_of Release v4.x. NOT replaces (functional substitution). Use for versioned '
  'lineage.'),
 ('predecessor_of',
  'successor_of',
  'Inverse of successor_of. The subject is the earlier version or iteration of the object. E.g. Release v4.x predecessor_of Release v5.0. Use when writing from the earlier version perspective.'),
 ('triggers',
  'triggered_by',
  'The subject causally initiates, activates, or starts the object. E.g. Event triggers Process; Error triggers Support Procedure; Data Entity change triggers Integration. Implies causal activation, '
  'not merely sequential order.'),
 ('triggered_by',
  'triggers',
  'Inverse of triggers. The subject is initiated or activated by the object. E.g. Process triggered_by Event; Support Procedure triggered_by Error. Use when writing from the activated topic '
  'perspective.'),
 ('conflicts_with',
  'conflicts_with',
  'A known incompatibility, contradiction, or mutual exclusion between two topics. Symmetric. E.g. Feature A conflicts_with Feature B (cannot both be enabled); Policy X conflicts_with Regulation Y. '
  'Use when co-existence causes problems.'),
 ('same_as',
  'same_as',
  "Two topics refer to the same real-world concept, entity, or artifact and are aliases or synonyms. Symmetric. E.g. 'MRR' same_as 'Monthly Recurring Revenue'. Use for deduplication and "
  'canonicalization — NOT related_to.'),
 ('owned_by',
  'owns',
  'The subject is under the organizational or system ownership, stewardship, or accountability of the object. E.g. Module owned_by Organization; Process owned_by Role. NOT governed_by (regulatory '
  'authority — owned_by is operational accountability).'),
 ('owns',
  'owned_by',
  'Inverse of owned_by. The subject has organizational ownership or accountability for the object. E.g. Organization owns Module; Role owns Process. Use when writing from the owner perspective.'),
 ('related_to',
  'related_to',
  'A general, undifferentiated association between two topics. Symmetric. LAST RESORT — only use when no other relation type fits. Before using this, ask: does governs, implements, depends_on, uses, '
  'triggers, integrates_with, has_part, same_as, replaces, or conflicts_with fit better? Overuse degrades graph quality.')]  # (name, inverse_name, description)
