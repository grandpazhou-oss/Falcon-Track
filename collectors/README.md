# Public-source collectors

This package runs Scrapy in a child process so the Twisted reactor is never
restarted inside a Django request or Prefect task.

## Interface

```sh
python -m collectors.cli --source ysczw --output /absolute/path/result.json \
  --cursor 1 --max-pages 1 --max-items 5 --seen-file /absolute/path/seen.json
```

Registered announcement sources are listed in `collectors/sources.py` and `collectors/extra_sources.py` (13 adapters in v0.2). Enterprise directories run through `radar/discovery.py` and are counted separately. No arbitrary URL is accepted.
`seen.json` is an array of stable source IDs as strings, for example
`["71635", "71636"]`. It should contain successfully persisted documents only.
`--refresh-items N` optionally refreshes up to N known rows per scanned page;
new rows are prioritized so refreshes cannot consume their whole budget.

The JSON result contains `source_key`, `status`, `items`, `next_cursor`, `stats`
and `error`. `partial` usually means a bounded batch intentionally stopped, not
that every request failed. Inspect `stats.error_count`, `budget_limited` and
the per-document `access_level` separately.

- If unprocessed eligible rows remain on a page, `next_cursor` stays on that page.
- Supply updated seen IDs on the next call to continue with the remaining rows.
- If all rows on a page have been considered, the cursor advances to the next page.
- At the verified end of the source, the cursor resets to page 1.
- Header refresh/overlap/backfill frequency is the application's responsibility.
- Run status `success` means the bounded traversal completed; it does not prove
  that a procurement's buyer, membership-only body or attachments were obtained.
- A failed detail keeps the current page for retry. Successfully returned
  documents may still be persisted by the caller.
- Source layout changes, access challenges and HTTP failures are not reported as
  an empty successful scan. Exit status is 2 for `blocked`/`failed`.

## Content boundaries

`ysczw` public summaries are typically partial. Membership-only body/attachments
are marked in metadata; no membership login, credential reuse or bypass is done.
The listing explicitly labels dates as registration start/end. Start date is
not copied to publication date, and detail update date is separately preserved.

`suzhou_epia` is an association's project-publicity site. Its records are
`early_signal`, never confirmed logistics procurement. PDF links are recorded during collection. The explicit background enrichment task can fetch and parse bounded public PDFs with identity checks. Contact information for environmental publicity
must not be assumed to be a procurement contact.

Every request stays on the source's registered hosts, follows robots.txt and uses
TLS certificate verification. Each source has one concurrent request with a
nominal one-second delay and jitter. HTTP rate limiting is surfaced rather than
aggressively retried. Redirects cannot move collection to arbitrary hosts.

## Verification

v0.2 Mac runs, A–G plans, source quality and limits are in `SOURCE_VALIDATION.md` and `docs/verification/v02/`. Eight demand sites qualify; the third business category remains unmet. The historical v0.1 Linux result below is retained as context only.

`tests/test_collectors.py` uses small, explicitly synthetic fixtures. Live
response HTML and raw result JSON are under `artifacts/live` during development
and are excluded from the distribution. `tests/fixtures/collector_verification.json`
records source URLs, content hashes and live-run counts without distributing
third-party full text.

On 2026-11-13, the Linux/Python 3.12/Scrapy 2.20 environment collected three
public procurement summaries, two early signals and then two different
procurement summaries with the saved seen-ID list. The resumed run had zero
overlap with its first three IDs. This is a small live smoke test, not proof of
continuous source availability or native Mac/Windows execution.
