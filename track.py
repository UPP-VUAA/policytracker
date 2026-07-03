# Regional Policy & Ordinance Tracker (VUAA / UPP)

Companion to the meetings tracker. The meetings tracker tells you **what's on the
agenda this week**; this one tells you **what legislation is actually moving through
the pipeline and where it stands** — across metro Phoenix, filtered to four
priorities: **Housing, Heat, Transit, Walkability**.

It's intentionally educational in framing (compiling public records), which keeps it
clean as a shared UPP/VUAA resource rather than something that has to live only on
the C4 side.

## What it does

- Pulls recent legislation (ordinances, resolutions, zoning cases) from every metro
  Phoenix city that publishes a machine-readable legislative feed.
- Flags items whose titles touch any of the four priority areas, using a keyword
  taxonomy you can edit at the top of `track.py`.
- Tags each item with its topic(s) and its **status** — adopted / pending / failed
  for Legistar cities, or upcoming / heard for agenda-portal cities — and links to the
  official municipal record.
- Outputs a branded `index.html`, a machine-readable `ordinances.json`, and a plain
  `digest.txt`.
- The page itself is interactive: filter by **topic**, **city**, or **type** (multi-select
  chips that stack with AND logic), and a **keyword search** box over titles and case
  numbers. Summary tiles, section counts, and a "showing N of M" readout update live.
  All client-side vanilla JS — nothing to host separately.
- Two headline panels sit up top:
  - **Act now** — items with a hearing in the next `ACTION_WINDOW_DAYS` (14) days, soonest
    first; these also float to the top of their topic sections with a "hearing in N days" badge.
  - **What moved** — new / advanced / adopted / failed / newly-scheduled items from the last
    `CHANGELOG_DAYS` (7) days. This is powered by `state.json`, a small committed ledger that
    diffs each daily run against the previous one. The first run establishes a baseline (no
    changes shown); movement appears from the second run on. The workflow commits
    `policy-tracker/state.json` so the history persists between runs.
- Every card carries a **Search news ↗** link — a one-tap Google News search pre-scoped to that
  item (distinctive title terms, with case numbers and agenda boilerplate stripped, plus the
  city + "Arizona" so results don't drift to Glendale CA or Peoria IL). It deliberately links to
  a *search* rather than asserting a specific article: local coverage is sparse, and keyless
  auto-matching is unreliable enough that it routinely pulls the wrong city's story. You glance
  and judge. (A future enhancement could auto-surface real headlines from Arizona-only outlet
  feeds — KJZZ, AZ Mirror, Rose Law Group Reporter — which sidesteps the wrong-city problem.)
- **Cross-city reform watch** — a panel that recognises when the *same reform* is moving in more
  than one Valley city, and whether a peer has already adopted it. It tags each item with a
  specific reform theme (code modernization, middle housing, ADUs, parking reform, TOD, complete
  streets, shade/heat, density bonus — `REFORMS`, editable), clusters across cities, and shows the
  adopted-vs-pending split per city. A yellow **"precedent set"** badge marks the leverage cases —
  a reform adopted in one city but still pending in another. It considers only *policy-level* items
  (text/code amendments, ordinances, plans) and excludes operational ones (procurement contracts,
  site-specific use permits) via `classify_kind`, so a striping contract or a shade-canopy permit
  never fakes "this reform is moving here." Every cluster is auditable — expand it to see the
  underlying items, each linked to its official record.
- Every card has a **Summary** expander — a plain-language gloss generated deterministically from
  the item's fields (what kind of action, where, which council district, where it sits in the
  pipeline, and why it's tracked). No LLM, so it works on the static site and never breaks the run.
- **By the numbers** — a longitudinal analytics section (inline SVG, no chart libraries): where
  items stand in the pipeline, adoption rate, activity by month, most active cities, and Phoenix
  rezonings by council district. A daily `archive.json` snapshot accumulates so month-over-month
  trend lines deepen over time.
- **Map view** (`map.html`) — a Leaflet map (OpenStreetMap basemap, no API key) with Phoenix's 8
  council districts shaded by tracked activity (popups name the councilmember + link to contact
  them), plus pins for every item whose title yields a geocodable intersection (Census geocoder,
  keyless, cached in `geocache.json`). The Vision Zero high-injury network, Valley Metro routes,
  and heat-vulnerability index are the next overlays to layer in — the data paths are confirmed.

No third-party Python packages — pure standard library.

## Coverage

Seven cities live now, across three platforms. Legistar cities carry full
adopted/pending/failed status; the agenda-based cities (Tempe, Glendale) show status
as *upcoming* vs. *heard*, since their portals publish agendas rather than a
status-tracked legislation database.

**Legistar API (full status, pulled automatically):**

| City | County | Notes |
|---|---|---|
| Phoenix | Maricopa | Full ordinance + rezoning stream |
| Mesa | Maricopa | Zoning cases (`ZON…`) + code amendments |
| Goodyear | Maricopa | Connected; surfaces items as they're filed |
| Apache Junction | Maricopa/Pinal | Planning/zoning cases |
| City of Maricopa | Pinal (greater region) | Adjacent; drop it if you want strict metro-only |

**Agenda portals (items parsed from published agendas):**

| City | Platform | Notes |
|---|---|---|
| Tempe | Granicus (`tempe.granicus.com`, view_id 2) | Council + Development Review + boards; ~90-day window |
| Glendale | DestinyHosted / AgendaQuick (portal id `45363`) | Council + Planning Commission + boards; current/upcoming agendas |

> Glendale was a maze: it ran **Legistar** until 2017, migrated to **Granicus**
> through 2020, and now publishes on **DestinyHosted**. The two older systems are
> still online as dead archives — the adapter targets the live DestinyHosted portal.

**Phase 2 (platform not yet identified — one adapter each):**
Scottsdale, Chandler, Gilbert, Peoria, Surprise, Buckeye, Avondale, Queen Creek, and
Maricopa County. The architecture is built so a new source is just a function that
yields the same record shape.

## Adding a city

1. **If it's on Legistar:** add a `(slug, "Name", "County")` row to `LEGISTAR_CITIES`.
   Test the slug first: `https://webapi.legistar.com/v1/SLUG/matters?$top=1` should
   return JSON (HTTP 200), not a 500.
2. **If it's on another platform:** write a `fetch_<platform>()` adapter that returns
   the same dict fields as `fetch_legistar()` (`city, county, topics, file, type,
   status, title, intro, agenda, enacted, last_modified, url, source`) and call it in
   `collect()`. Everything downstream (filtering, rendering) just works.

## Tuning the filter

The `TOPICS` dict in `track.py` is the whole filter. Plain words match at a left word
boundary (so `rezon` catches *rezoning*, and `tree` will **not** catch *street*);
anything with a space/hyphen matches as a phrase; a few short words live in `EXACT` so
`bus` doesn't catch *business*. Add or remove terms freely.

## Run it

Locally: `python3 track.py` → writes to `site/`.
Automated: the included `.github/workflows/track.yml` runs it daily at 6 AM Phoenix and
publishes to `policy/` (served at `https://upp-vuaa.github.io/policy/`). Adjust the
publish path if your repo layout differs.


## Council voting records (`council.py`)

Companion tool that builds `site/council.html` — per-member voting records on
topic-tagged items for **Phoenix** and **Tempe** (pilot cities).

**Sources.**
- Phoenix: Legistar Web API roll-call votes (City Council Formal Meetings +
  Transportation, Infrastructure & Planning Subcommittee), window since 2024-07-01.
- Tempe: the city's published Legal Action Summaries on Hyland Agenda Online,
  which record every motion with a tally and *named* For / Against / Abstain lists.

**How it works.** Votes are joined to the same topic tagger the tracker uses
(Housing / Heat / Transit / Walkability). The page shows per-topic stacked bars per
member and an expandable list of every **contested** vote (any motion with at least
one No, or that failed), each linked to the official record. Consent items pass
unanimously — the contested list is where positions show.

**Curated layer.** `members.json` is scaffolded on first run and **never
overwritten**. Fill in per member: seat/district, email (renders a one-tap Email
button), a short bio, affiliations and board seats (render as chips), and a
campaign-finance link. Entries prefilled from conversation carry a verify flag —
confirm before publishing.

**State.** `council_votes.json` (the accumulating vote ledger) and
`council_state.json` (which meetings are processed) are committed so daily runs are
incremental — the workflow runs `council.py` right after `track.py` and publishes
`policy/council.html`.

**Weighting.** Not every vote is equally substantial. `vote_weights.json` (scaffolded,
editable) assigns weights by item kind — citywide text amendments and plan changes high,
single-parcel permits/plats/licenses low — with boost keywords (e.g. "citywide") and
downweights (e.g. "hearing officer"). Contested lists sort by weight and show
major / minor badges.

**Re-tagging.** Topics are recomputed from the stored ledger on every run, so tagger
improvements (like the phrase-based Heat rules) apply retroactively. Tightening keyword
rules is safe; if rules are ever *broadened*, clear `council_state.json` to re-scrape.

**Links, tenure, websites.** Phoenix's InSite portal rejects per-item LegislationDetail
deep links, so vote records link to the official per-meeting page (MeetingDetail),
rewritten retroactively on each run. Member names hyperlink to their `website` field;
cards show district, first year in office, and tenure (`since` in members.json,
cross-checked against Legistar office records — verify flags apply).

**Org positions (positions.json).** A curated VUAA overlay marking specific votes the
org has a position on (matched by file number or title keyword). Members voting the
org-favored way get a green *org-aligned* pill; others a red *counter* mark, tallied
in an "Org priority" row per card. This is an advocacy layer — keep pages using it
under VUAA branding. `enabled:false` entries are drafts.

**Contested filter.** Every red "N contested" flag is a button: tap it to open that
member's vote list filtered to contested items on that topic (tap again to clear).
Each city header also has a **Contested only** toggle that hides members with no
contested or org-priority votes and opens the rest.
