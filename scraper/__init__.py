"""LinkedIn company-home scraper package.

Layered, learning-oriented design:
    Tier 1 (truth)    : Voyager/GraphQL API responses captured by `interceptor`
    Tier 2 (patch)    : Stable DOM hooks (data-urn, data-test-*, aria-label, /in/ links)
    Tier 3 (give-up)  : Emit author URN + `name_resolution: 'failed'` (never the
                        string "Unknown User").

Entry point: `scraper.main.scrape_company(url, ...)`.
"""
