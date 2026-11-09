# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub's **Report a vulnerability** button (Security tab > Advisories), with:

- what an attacker can do, and under which role or credential (anonymous, signed-in user, API token, a document in a collection);
- the steps or a request that shows it;
- the version or commit you tested.

You will get an acknowledgement within 5 working days. Fixes are released as soon as they are ready, with an advisory that credits you unless you prefer otherwise.

## Supported versions

Security fixes go into the latest release. Curltale AI is pre-1.0: upgrade to the newest release to receive them.

## Scope

In scope: the app (API, MCP endpoint, web UI), the background worker, the optional browser service and the example deployment files.
Out of scope: issues that need an administrator account to misuse administrator features, denial of service by volume, and vulnerabilities in the AI model or database you connect (report those to their vendors).

## What the product already enforces

The README section "Security" lists the protections in place (read-only data connections, SQL parsed and rebuilt under each caller's entitlements, collection isolation, SSRF protection for web capture, encrypted secrets, rate limits). A report that one of them can be bypassed is exactly what we want to hear about.
