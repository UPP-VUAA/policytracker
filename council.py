#!/usr/bin/env python3
"""
Council voting records — Phoenix + Tempe pilot.

Builds per-member voting records on topic-tagged items (Housing / Heat / Transit /
Walkability) from primary sources:

  * Phoenix — Legistar Web API roll-call votes (City Council Formal Meeting +
    Transportation, Infrastructure and Planning Subcommittee).
  * Tempe   — Hyland AgendaOnline "Legal Action Summary" documents, which record
    each motion with named For / Against / Abstain lists.

Outputs:
  site/council.html    branded report-card page (embedded data, no build step)
  council_votes.json   accumulated vote events (committed; incremental)
  council_state.json   which meetings have been processed (committed)
  members.json         curated member file — created as a scaffold if missing,
                       NEVER overwritten (bios/emails/affiliations are edited by hand)

Pure stdlib. Run after track.py in the same GitHub Action.
"""

import json, re, ssl, urllib.request, urllib.parse, http.cookiejar, time, datetime as dt
from pathlib import Path
from collections import defaultdict

import track  # reuse topic tagger + helpers + brand

HERE   = Path(__file__).parent
OUT    = HERE / "site"
VOTES  = HERE / "council_votes.json"
CSTATE = HERE / "council_state.json"
MEMBERS= HERE / "members.json"

SINCE  = "2024-07-01"          # backfill window start
PHX_BODIES = ["City Council Formal Meeting",
              "Transportation, Infrastructure, and Planning Subcommittee"]
TEMPE_TYPEIDS = ["109"]        # Regular City Council (extend with more type ids later)
PAUSE  = 0.06

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "UPP-VUAA council-records/1.0"})
    return urllib.request.urlopen(req, timeout=timeout, context=CTX).read().decode("utf-8", "ignore")

def _gjson(url):
    try:
        return json.loads(_get(url))
    except Exception:
        return None

def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

def _tidy_title(t):
    """Strip agenda boilerplate prefixes from item titles."""
    t = re.sub(r'^\s*(\*{2,}[^*]+\*{2,}\s*)+', '', t or '')
    t = re.sub(r'^\(CONTINUED FROM[^)]*\)\s*-?\s*', '', t, flags=re.I)
    return t.strip()

def topics_of(title):
    return track.topics_for(title or "")

# ---------------------------------------------------------------------------
# PHOENIX — Legistar roll calls
# ---------------------------------------------------------------------------
def phoenix_scrape(state, votes, positions):
    done = set(state.setdefault("phoenix_events", []))
    url = ("https://webapi.legistar.com/v1/phoenix/events?"
           f"$filter=EventDate+ge+datetime'{SINCE}'&$orderby=EventDate&$top=1000")
    events = _gjson(url) or []
    events = [e for e in events if e.get("EventBodyName") in PHX_BODIES
              and e.get("EventDate", "")[:10] <= dt.date.today().isoformat()]
    # Phoenix's InSite portal rejects LegislationDetail deep links ("Invalid parameters"),
    # but per-meeting MeetingDetail links from the API load fine — use those, and rewrite
    # any stale links already in the ledger.
    insite = {(e.get("EventDate", "")[:10], e.get("EventBodyName", "")): e.get("EventInSiteURL") or ""
              for e in events}
    fixed = 0
    for v in votes:
        if v.get("city") == "Phoenix" and "LegislationDetail" in v.get("url", ""):
            nu = insite.get((v.get("date"), v.get("body")))
            if nu:
                v["url"] = nu; fixed += 1
    if fixed:
        print(f"   phoenix: rewrote {fixed} stale item links to meeting pages")
    new = [e for e in events if e["EventId"] not in done]
    print(f"   phoenix: {len(events)} eligible meetings, {len(new)} new")
    for e in new:
        eid, edate, body = e["EventId"], e.get("EventDate", "")[:10], e.get("EventBodyName", "")
        items = _gjson(f"https://webapi.legistar.com/v1/phoenix/events/{eid}/eventitems?$top=1000") or []
        time.sleep(PAUSE)
        for it in items:
            title = it.get("EventItemTitle") or ""
            tps = topics_of(title)
            pos_hit = match_position({"city": "Phoenix", "title": title,
                                      "file": it.get("EventItemMatterFile") or ""}, positions)
            if (not tps and not pos_hit) or not it.get("EventItemMatterId"):
                continue
            vs = _gjson(f"https://webapi.legistar.com/v1/phoenix/eventitems/{it['EventItemId']}/votes") or []
            time.sleep(PAUSE)
            if not vs:
                continue
            link = insite.get((edate, body)) or "https://phoenix.legistar.com/Calendar.aspx"
            rec = {"city": "Phoenix", "date": edate, "body": body,
                   "title": _tidy_title(track._clean_subject(title))[:220] or _tidy_title(title)[:220],
                   "file": it.get("EventItemMatterFile") or "",
                   "url": link, "topics": tps,
                   "result": (it.get("EventItemActionName") or
                              ("Passed" if it.get("EventItemPassedFlag") == 1 else
                               "Failed" if it.get("EventItemPassedFlag") == 0 else "")),
                   "motion": "", "source": "rollcall", "votes": {}}
            for v in vs:
                nm = (v.get("VotePersonName") or "").strip()
                val = (v.get("VoteValueName") or "").strip()
                if nm:
                    rec["votes"][nm] = val
            if rec["votes"]:
                votes.append(rec)
        done.add(eid)
        state["phoenix_events"] = sorted(done)
        VOTES.write_text(json.dumps(votes, indent=0), encoding="utf-8")
        CSTATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
        print(f"     phx {edate} {body[:28]:28s} events-done={len(done)}", flush=True)
    state["phoenix_events"] = sorted(done)

# ---------------------------------------------------------------------------
# TEMPE — Hyland AgendaOnline Legal Action Summaries
# ---------------------------------------------------------------------------
def tempe_meetings():
    """Enumerate Regular City Council meetings via the AgendaOnline search."""
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=CTX))
    op.addheaders = [("User-Agent", "UPP-VUAA council-records/1.0")]
    sp = op.open("https://tempe.hylandcloud.com/Agendaonline/Meetings", timeout=30).read().decode("utf-8", "ignore")
    tok = re.search(r'__RequestVerificationToken[^>]*value="([^"]+)"', sp).group(1)
    end = dt.date.today()
    fields = [("__RequestVerificationToken", tok), ("Keywords", ""),
              ("DateRangeOptionID", "11"),
              ("DateRangeCustomStartDate", f"{int(SINCE[5:7])}/{int(SINCE[8:10])}/{SINCE[:4]}"),
              ("DateRangeCustomEndDate", f"{end.month}/{end.day}/{end.year}")]
    for t in TEMPE_TYPEIDS:
        fields.append(("MeetingTypeIDs", t))
    r = op.open(urllib.request.Request("https://tempe.hylandcloud.com/Agendaonline/Meetings",
                                       data=urllib.parse.urlencode(fields).encode()),
                timeout=45).read().decode("utf-8", "ignore")
    out = []
    for tr in r.split("<tr")[1:]:
        mid = re.search(r'ViewMeeting\?id=(\d+)', tr)
        md = re.search(r'(\d{1,2}/\d{1,2}/\d{4}) \d{1,2}:\d{2}', tr)
        if mid and md and "3" in set(re.findall(r'doctype=(\d)', tr)):
            d = dt.datetime.strptime(md.group(1), "%m/%d/%Y").date().isoformat()
            out.append((int(mid.group(1)), d))
    return sorted(set(out), key=lambda x: x[1])

_T_TITLE  = re.compile(r'^(Mayor|Vice Mayor|Councilmember|Councilwoman|Councilman)\s+', re.I)
_T_TALLY  = re.compile(r'Aye:\s*(\d+);\s*Nay:\s*(\d+);\s*Abstain:\s*(\d+);\s*Absent:\s*(\d+);\s*Recused:\s*(\d+)')
_T_ITEMNO = re.compile(r'^(\d{1,2}[A-Z]?\d{0,2})\.$')
_T_RANGE  = re.compile(r'Items?\s+(\d{1,2}[A-Z]\d{0,2})\s*[-–]\s*(\d{1,2}[A-Z]?)(\d{1,2})')
_T_SINGLE = re.compile(r'Items?\s+(\d{1,2}[A-Z]\d{0,2})\b')
_T_VOTELABEL = re.compile(r'(?:First|Second|Third|Fourth|Fifth|Sixth) vote:\s*', re.I)

def _t_norm_name(n):
    n = _T_TITLE.sub("", n.strip(" .;,")).strip()
    return n

def _t_names(seg):
    """Harvest member names from a For/Against/Abstain segment. Every name in the
    summaries carries a title prefix, so anchor on the title — prose can't fake that."""
    seg = re.split(r'For:|Against:|Abstain(?:ed)?:|Absent:|Recused:|\bMotion\b|\bSUPERSEDED\b'
                   r'|\bWITHDRAWN\b|\bPass\b|\bFail\b|\bPUBLIC\b|\bItem\b'
                   r'|Ordinance No\.|Resolution No\.', seg)[0]
    return re.findall(r"(?:Vice\s+Mayor|Mayor|Councilmember|Councilwoman|Councilman)\s+([A-Z][A-Za-z'\-]+(?:\.\s+[A-Z][A-Za-z'\-]+)?)", seg)

def _expand_items(motion, item_titles):
    """Map a motion's 'Items 4B1 - 4B6' style references to concrete item numbers."""
    nos = set()
    for m in _T_RANGE.finditer(motion):
        start, pfx2, endn = m.group(1), m.group(2), int(m.group(3))
        pm = re.match(r'(\d{1,2}[A-Z])(\d{1,2})', start)
        if pm:
            pfx, s = pm.group(1), int(pm.group(2))
            for i in range(s, endn + 1):
                nos.add(f"{pfx}{i}")
    for m in _T_SINGLE.finditer(motion):
        nos.add(m.group(1))
    return [n for n in nos if n in item_titles]

def tempe_parse_summary(html, mid, mdate):
    """Parse one Legal Action Summary into vote events (one per covered item)."""
    txt = re.sub(r'<[^>]+>', '\n', html)
    txt = txt.replace('&#xa0;', ' ').replace('&amp;', '&').replace('&#39;', "'")
    txt = re.sub(r'[ \t]+', ' ', txt)
    lines = [l.strip() for l in txt.split('\n')]
    lines = [l for l in lines if l]

    # pass 1: item number -> title (title = first substantial line after the number)
    item_titles, cur = {}, None
    for i, l in enumerate(lines):
        m = _T_ITEMNO.match(l)
        if m:
            cur = m.group(1)
            for j in range(i + 1, min(i + 4, len(lines))):
                if len(lines[j]) > 8 and not _T_ITEMNO.match(lines[j]) and not lines[j].startswith("Motion"):
                    item_titles[cur] = lines[j][:240]
                    break
            continue

    # pass 2: walk vote records. Tally lines can wrap; work on a flattened string
    # with explicit line separators so we can find block boundaries.
    flat = ' \u2029 '.join(lines)
    flat = re.sub(r'Recused:\s*\u2029\s*(\d+);', r'Recused: \1;', flat)
    events = []
    link = f"https://tempe.hylandcloud.com/Agendaonline/Meetings/ViewMeeting?id={mid}&doctype=3"
    boundary = re.compile(r'\u2029 (?:Pass|Fail) \u2029|\u2029 \d{1,2}[A-Z]?\d{0,2}\. \u2029')

    for m in _T_TALLY.finditer(flat):
        aye, nay, abst, absn, rec = (int(m.group(k)) for k in range(1, 6))
        pre = flat[max(0, m.start() - 2200):m.start()]
        post = flat[m.end():m.end() + 700]

        mm = list(re.finditer(r'Motion[^\u2029]{5,300}', pre))
        motion = mm[-1].group(0).strip() if mm else ""
        m_at = mm[-1].start() if mm else len(pre)

        # the description block between the previous record's end (Pass/Fail token
        # or item header) and the Motion line is the actual subject of this vote —
        # e.g. "Second vote: Amend Resolution No. R2026.48 in Item 8A1 ... 0.1% for transit ..."
        bnds = list(boundary.finditer(pre[:m_at]))
        d_from = bnds[-1].end() if bnds else max(0, m_at - 600)
        desc = pre[d_from:m_at].replace('\u2029', ' ')
        desc = _T_VOTELABEL.sub('', desc)
        desc = re.sub(r'\s+', ' ', desc).strip(' ;·-')
        desc_ok = len(desc) > 40

        hm = list(re.finditer(r'\u2029 (\d{1,2}[A-Z]?\d{0,2})\. \u2029', pre))
        ctx_no = hm[-1].group(1) if hm else None

        fr, ag, ab = [], [], []
        fm = re.search(r'For:\s*(.{0,400})', post)
        if fm: fr = _t_names(fm.group(1).replace('\u2029', ' '))
        am = re.search(r'Against:\s*(.{0,400})', post)
        if am: ag = _t_names(am.group(1).replace('\u2029', ' '))
        bm = re.search(r'Abstain(?:ed)?:\s*(.{0,300})', post)
        if bm: ab = _t_names(bm.group(1).replace('\u2029', ' '))

        covered = _expand_items(desc + " " + motion, item_titles)
        if not covered and ctx_no in item_titles:
            covered = [ctx_no]

        result = "Passed" if aye > nay else "Failed"
        vmap = {}
        for n in fr: vmap[n] = "Yes"
        for n in ag: vmap[n] = "No"
        for n in ab: vmap[n] = "Abstain"
        if not vmap:
            continue

        def emit(no, title, tps):
            if not title:
                return
            if not tps and not match_position({"city": "Tempe", "title": title, "file": no or ""}, _POSITIONS):
                return
            events.append({"city": "Tempe", "date": mdate, "body": "Regular City Council",
                           "title": track._clean_subject(title)[:220] or title[:220],
                           "file": no or "", "url": link, "topics": tps,
                           "result": result, "motion": motion[:260],
                           "source": "action-summary", "votes": dict(vmap),
                           "tally": {"aye": aye, "nay": nay, "abstain": abst,
                                     "absent": absn, "recused": rec}})

        if len(covered) > 1:
            # consent bundle — one event per covered item, tagged by its own title
            for no in covered:
                t = item_titles.get(no, "")
                emit(no, t, topics_of(t + " " + motion))
        elif len(covered) == 1:
            no = covered[0]
            base = item_titles.get(no, "")
            title = desc[:220] if desc_ok else base
            emit(no, title or base or motion[:220],
                 topics_of(base + " " + desc + " " + motion))
        else:
            title = desc[:220] if desc_ok else (motion[:220] if motion else "")
            emit(None, title, topics_of(desc + " " + motion))
    return events

_POSITIONS = []

def tempe_scrape(state, votes):
    done = set(state.setdefault("tempe_meetings", []))
    try:
        mts = tempe_meetings()
    except Exception as ex:
        print("   tempe: meeting search failed:", str(ex)[:80]); return
    new = [(i, d) for i, d in mts if i not in done]
    print(f"   tempe: {len(mts)} council meetings w/ summaries, {len(new)} new")
    for mid, mdate in new:
        try:
            h = _get(f"https://tempe.hylandcloud.com/Agendaonline/Documents/ViewAgenda?meetingId={mid}&doctype=3&type=")
            evs = tempe_parse_summary(h, mid, mdate)
            votes.extend(evs)
        except Exception as ex:
            print(f"   tempe meeting {mid}: {str(ex)[:70]}")
        done.add(mid); time.sleep(PAUSE)
        state["tempe_meetings"] = sorted(done)
        VOTES.write_text(json.dumps(votes, indent=0), encoding="utf-8")
        CSTATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
        print(f"     tempe {mdate} meetings-done={len(done)}", flush=True)
    state["tempe_meetings"] = sorted(done)

# ---------------------------------------------------------------------------
# members.json scaffold (created once; never overwritten)
# ---------------------------------------------------------------------------
def scaffold_members(votes):
    if MEMBERS.exists():
        return _load(MEMBERS, {})
    def blank(name):
        return {"name": name, "seat": "", "district": "", "email": "",
                "bio": "", "affiliations": [], "boards": [], "donors_url": "",
                "website": "", "verify": True}
    phx_names = []
    o = _gjson("https://webapi.legistar.com/v1/phoenix/officerecords?$top=60"
               "&$filter=OfficeRecordEndDate+gt+datetime'" + dt.date.today().isoformat() + "'")
    if o:
        seen = set()
        for r in o:
            if r.get("OfficeRecordBodyName") == "City Council Formal Meeting":
                n = r.get("OfficeRecordFullName", "").strip()
                if n and n not in seen:
                    seen.add(n); phx_names.append((n, r.get("OfficeRecordTitle", "")))
    tempe_names = sorted({n for v in votes if v["city"] == "Tempe" for n in v["votes"]})
    data = {"_note": ("Curated file — edited by the org, never overwritten by the scraper. "
                      "Fill in seat/district, email, short bio, affiliations, board seats, and a "
                      "campaign-finance link per member. Entries marked verify:true were prefilled "
                      "from conversation or scraped rosters and should be confirmed before publishing."),
            "Phoenix": {"members": []}, "Tempe": {"members": []}}
    for n, t in phx_names:
        m = blank(n); m["seat"] = t
        if n == "Laura Pastor":
            m["boards"] = ["Valley Metro Board (verify current membership)"]
        if n == "Ann O'Brien":
            m["boards"] = ["Valley Metro Board (unconfirmed — verify)"]
        if n == "Anna Hernandez":
            m["affiliations"] = ["Abundance Network (verify)"]
        data["Phoenix"]["members"].append(m)
    for n in tempe_names:
        m = blank(n)
        m["seat"] = "Mayor" if n == "Woods" else ""
        if n == "Woods":
            m["name"] = "Corey Woods"
            m["affiliations"] = ["Abundance Network (verify)"]
        data["Tempe"]["members"].append(m)
    MEMBERS.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"   members.json scaffolded ({len(phx_names)} Phoenix, {len(tempe_names)} Tempe) — fill in and verify")
    return data

# ---------------------------------------------------------------------------
# Aggregation + page
# ---------------------------------------------------------------------------
POSITIONS = HERE / "positions.json"

_DEFAULT_POSITIONS = {
    "_note": ("Org position overlay — an advocacy layer; publish pages using it under VUAA "
              "branding. Each entry marks specific votes by city + exact file number and/or a "
              "title keyword. aligned_vote is the vote the org favors; members who voted "
              "otherwise get a counter marker (severity 2 = red, 1 = amber). enabled:false "
              "entries are drafts and are ignored."),
    "positions": [
        {"id": "phx-parks-medical-permits", "enabled": True, "city": "Phoenix",
         "match_file": "25-2602", "match_title": "Safe Medical Care in City Parks",
         "aligned_vote": "No", "severity": 2,
         "label": "Permit requirement for medical care providers in city parks (G-7467)"},
        {"id": "phx-scooter-min-age", "enabled": True, "city": "Phoenix",
         "match_file": "24-2511", "match_title": "Minimum Age Requirement for Electric Scooters",
         "aligned_vote": "No", "severity": 2,
         "label": "E-scooter / e-bike minimum age set at 16 (citywide)",
         "note": ("Council adopted a 16-year minimum age. The Vision Zero Community Advisory "
                  "Board recommended 12\u201313 and Councilmember O'Brien proposed 14; neither "
                  "was incorporated.")},
        {"id": "phx-middle-housing-study", "enabled": False, "city": "Phoenix",
         "match_file": "26-0399", "match_title": "middle housing",
         "aligned_vote": "No", "severity": 1,
         "label": ("DRAFT — study-first vs. citywide middle housing. Roll calls show "
                   "Z-TA-1-25-Y adopted unanimously and the ASU study agreement unanimous; "
                   "confirm which vote to score before enabling.")},
    ],
}

def load_positions():
    if not POSITIONS.exists():
        POSITIONS.write_text(json.dumps(_DEFAULT_POSITIONS, indent=1), encoding="utf-8")
        print("   positions.json scaffolded — org-position overlay (VUAA layer)")
    cfg = _load(POSITIONS, _DEFAULT_POSITIONS)
    return [p for p in cfg.get("positions", []) if p.get("enabled")]

def match_position(ev, positions):
    for p in positions:
        if p.get("city") and p["city"] != ev.get("city"):
            continue
        f_ok = p.get("match_file") and p["match_file"] == (ev.get("file") or "")
        t_ok = p.get("match_title") and p["match_title"].lower() in (ev.get("title") or "").lower()
        if f_ok or t_ok:
            return p
    return None

WEIGHTS = HERE / "vote_weights.json"

_DEFAULT_WEIGHTS = {
    "_note": ("Editable weighting config. Not every vote is equally substantial: a citywide "
              "text amendment says far more about a member's positions than a single-parcel "
              "use permit or a liquor license. Weight = kind weight, raised by any matching "
              "boost keyword, then lowered by any matching downweight keyword. Weights >= 2.5 "
              "show a 'major' badge; <= 0.5 show 'minor'; contested lists sort by weight."),
    "kind_weights": {"Code/Text amend": 3.0, "Plan amend": 2.5, "Ordinance": 1.5,
                     "Plan/Study": 1.2, "Rezoning": 1.0, "Resolution": 1.0,
                     "Annexation": 1.0, "Grant/Funding": 0.8, "Contract/IGA": 0.8,
                     "Dev/Site plan": 0.5, "Use permit": 0.3, "Variance": 0.3,
                     "Plat/Subdiv": 0.3, "ROW/Easement": 0.3, "Other": 1.0},
    "boost_keywords": {"citywide": 3.0, "city-wide": 3.0, "text amendment": 3.0,
                       "general plan": 3.0, "all council districts": 3.0},
    "downweight_keywords": {"hearing officer": 0.3, "pho-": 0.3, "liquor license": 0.2,
                            "abandonment": 0.3, "final plat": 0.3},
    "default": 1.0,
}

def load_weights():
    if not WEIGHTS.exists():
        WEIGHTS.write_text(json.dumps(_DEFAULT_WEIGHTS, indent=1), encoding="utf-8")
        print("   vote_weights.json scaffolded — tune kind/keyword weights there")
    return _load(WEIGHTS, _DEFAULT_WEIGHTS)

def event_weight(ev, cfg):
    hay = (ev.get("title", "") + " " + ev.get("motion", "")).lower()
    kind = track.classify_kind({"title": ev.get("title", ""), "type": ""})
    w = cfg.get("kind_weights", {}).get(kind, cfg.get("default", 1.0))
    for kw, bv in cfg.get("boost_keywords", {}).items():
        if kw in hay:
            w = max(w, bv)
    for kw, dv in cfg.get("downweight_keywords", {}).items():
        if kw in hay:
            w = min(w, dv)
    return round(w, 2), kind

YES = {"yes", "aye", "yea"}
NO  = {"no", "nay"}

def _match_member(vote_name, member_name):
    vn, mn = vote_name.lower().strip(), member_name.lower().strip()
    if vn == mn:
        return True
    return vn and (vn == mn.split()[-1] or mn.endswith(" " + vn))

def aggregate(votes, members, cfg, positions):
    """per city -> per member -> per topic tallies + contested list + org alignment."""
    out = {}
    for city in ("Phoenix", "Tempe"):
        evs = [v for v in votes if v["city"] == city and
               (v.get("topics") or match_position(v, positions))]
        # dedupe (same date+title+votes signature)
        seen, uniq = set(), []
        for v in evs:
            k = (v["date"], v["title"][:80], tuple(sorted(v["votes"].items())))
            if k in seen: continue
            seen.add(k); uniq.append(v)
        for v in uniq:
            v["_w"], v["_kind"] = event_weight(v, cfg)
        roster = [m["name"] for m in members.get(city, {}).get("members", [])]
        agg = {}
        for mname in roster:
            per = {t: {"yes": 0, "no": 0, "other": 0, "events": []} for t in track.TOPIC_ORDER}
            org = []
            for v in uniq:
                val = None
                for vn, vv in v["votes"].items():
                    if _match_member(vn, mname):
                        val = vv.lower(); break
                if val is None:
                    continue
                contested = (v.get("tally", {}).get("nay", 0) > 0 or
                             any(x.lower() in NO for x in v["votes"].values()) or
                             v.get("result", "").lower().startswith("fail"))
                bucket = "yes" if val in YES else "no" if val in NO else "other"
                pos = match_position(v, positions)
                if pos:
                    myvote = next((vv for vn, vv in v["votes"].items() if _match_member(vn, mname)), "")
                    aligned = myvote.lower() in (YES if pos["aligned_vote"].lower() in YES else NO)
                    org.append({"date": v["date"], "title": v["title"], "vote": myvote,
                                "url": v["url"], "result": v.get("result", ""),
                                "tally": v.get("tally"), "w": v.get("_w", 1.0),
                                "pos_label": pos.get("label", ""),
                                "pos_note": pos.get("note", ""), "aligned": aligned,
                                "sev": pos.get("severity", 2)})
                for t in v["topics"]:
                    per[t][bucket] += 1
                    if contested or bucket == "no":
                        per[t]["events"].append({"date": v["date"], "title": v["title"],
                                                 "vote": v["votes"].get(mname) or
                                                         next((vv for vn, vv in v["votes"].items()
                                                               if _match_member(vn, mname)), ""),
                                                 "url": v["url"], "file": v.get("file", ""),
                                                 "result": v.get("result", ""),
                                                 "tally": v.get("tally"),
                                                 "w": v.get("_w", 1.0), "kind": v.get("_kind", "")})
            agg[mname] = {"topics": per, "org": org}
        out[city] = {"events": uniq, "members": agg,
                     "n_meetings": len({v["date"] for v in uniq}),
                     "n_events": len(uniq)}
    return out

_TOPIC_HEX = {"Housing": "#1c6b2e", "Transit": "#3a3a8a",
              "Walkability": "#b3361f", "Heat": "#c98a00"}

def render_council(agg, members, generated):
    esc = track.esc
    css = """
:root{--yellow:#F6DC31;--ink:#0b0b0a;--paper:#FFFFF0;--mute:#78776c;--hair:#e7e5d4;--hair2:#f1f0e4;
 --green:#1c6b2e;--indigo:#3a3a8a;--red:#b3361f;--amber:#a9760a;}
*{box-sizing:border-box;} html{scroll-behavior:smooth;}
body{margin:0;background:var(--paper);color:var(--ink);font-family:Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased;line-height:1.5;}
a{color:inherit;} .wrap{max-width:1000px;margin:0 auto;padding:0 22px 90px;}
.mast{background:var(--ink);color:#fff;} .mast .wrap{padding:22px 22px 24px;}
.brandbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:4px;}
.logo{display:flex;align-items:center;gap:10px;}
.logomark{width:30px;height:30px;flex:none;border-radius:5px;}
.lm-upp{background:var(--yellow);} .lm-vuaa{background:var(--ink);border:3px solid var(--yellow);}
.logotext{font-weight:800;font-size:13px;line-height:1.15;letter-spacing:.02em;text-transform:uppercase;color:#fff;}
.branddiv{width:1px;height:28px;background:#39392f;flex:none;}
.mast h1{font-size:clamp(24px,4.2vw,38px);line-height:1.06;margin:14px 0 8px;font-weight:800;letter-spacing:-.022em;}
.mast h1 .hl{color:var(--yellow);}
.tag{font-size:14px;color:#c8c8bd;max-width:70ch;line-height:1.5;}
.runinfo{margin-top:14px;font-size:12px;color:#a9a99d;display:flex;flex-wrap:wrap;gap:20px;}
.runinfo b{color:#fff;}
.topnav{margin-top:15px;display:flex;gap:20px;flex-wrap:wrap;}
.topnav a{font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--yellow);
 text-decoration:none;padding-bottom:3px;border-bottom:2px solid transparent;}
.topnav a:hover{border-bottom-color:var(--yellow);}
.citysec{margin-top:34px;} .chead{padding:0 0 11px;border-bottom:2px solid var(--ink);
 display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;}
.chead h2{margin:0;font-size:24px;font-weight:800;letter-spacing:-.018em;}
.csub{font-size:12.5px;color:var(--mute);}
.note{margin:14px 0 4px;font-size:12.5px;color:var(--mute);max-width:86ch;line-height:1.55;}
.mgridc{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:26px;margin-top:18px;}
@media(max-width:520px){.mgridc{grid-template-columns:1fr;}}
.mcard{border:1px solid var(--hair);border-radius:12px;background:#fff;padding:17px 19px;}
.mtop{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
.mname{font-size:17px;font-weight:800;letter-spacing:-.01em;}
.mseat{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);}
.mmail{margin-left:auto;font-size:11px;font-weight:700;color:var(--ink);text-decoration:none;
 border:1px solid var(--hair);border-radius:999px;padding:3px 11px;white-space:nowrap;}
.mmail:hover{border-color:var(--ink);}
.mbio{margin:7px 0 2px;font-size:12.5px;color:#57574e;line-height:1.5;}
.mchips{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 4px;}
.mch{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:2px 9px;
 border-radius:999px;border:1px solid #d8d6c6;color:#57574e;background:#fff;}
.mch.aff{background:var(--yellow);border-color:var(--yellow);color:var(--ink);}
.trow{display:flex;align-items:center;gap:10px;margin:9px 0 0;font-size:12px;}
.tlab{flex:0 0 92px;font-weight:800;font-size:11px;text-transform:uppercase;letter-spacing:.04em;}
.tl-housing{color:var(--green);} .tl-transit{color:var(--indigo);}
.tl-walkability{color:var(--red);} .tl-heat{color:var(--amber);}
.tbar{flex:1;height:9px;border-radius:999px;background:var(--hair2);overflow:hidden;display:flex;}
.tbar i{display:block;height:100%;}
.tnum{flex:0 0 auto;color:var(--mute);white-space:nowrap;}
.tnum b{color:var(--ink);}
.cflag{font-size:10px;font-weight:800;color:#fff;background:var(--red);border-radius:999px;padding:1px 8px;
 border:2px solid var(--red);cursor:pointer;font-family:inherit;line-height:1.4;}
.cflag:hover{background:#8f2b18;border-color:#8f2b18;}
.cflag.on{background:#fff;color:var(--red);}
.conly{margin-left:auto;font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.05em;
 color:var(--mute);background:none;border:1.5px solid var(--hair);border-radius:999px;padding:4px 13px;
 cursor:pointer;font-family:inherit;}
.conly:hover{border-color:var(--ink);color:var(--ink);}
.conly.on{background:var(--ink);border-color:var(--ink);color:var(--yellow);}
.citysec.conly-on .mcard:not(.has-notes){display:none;}
.mcard[data-filter="Housing"] .mnotes li:not([data-t="Housing"]),
.mcard[data-filter="Transit"] .mnotes li:not([data-t="Transit"]),
.mcard[data-filter="Walkability"] .mnotes li:not([data-t="Walkability"]),
.mcard[data-filter="Heat"] .mnotes li:not([data-t="Heat"]),
.mcard[data-filter="org"] .mnotes li:not([data-t="org"]){display:none;}
.mcard[data-filter] details.mnotes>summary::before{content:"filtered · ";color:var(--red);}
.wmaj{background:var(--yellow);color:var(--ink);font-size:9px;font-weight:800;padding:1px 7px;border-radius:999px;
 margin-left:7px;text-transform:uppercase;letter-spacing:.05em;vertical-align:1px;}
.wmin{background:#e8e7d8;color:#6b6a5e;font-size:9px;font-weight:800;padding:1px 7px;border-radius:999px;
 margin-left:7px;text-transform:uppercase;letter-spacing:.05em;vertical-align:1px;}
.mch.st{background:#efeee0;border-color:#dcdaca;}
.mlink{color:inherit;text-decoration:none;border-bottom:2px solid var(--yellow);}
.mlink:hover{border-bottom-color:var(--ink);}
.tl-org{color:var(--ink);} .oal{color:var(--green);font-weight:800;} .oct{color:var(--red);font-weight:800;}
.porg-y{border:1.5px solid var(--green);color:var(--green);font-size:9px;font-weight:800;padding:0 7px;
 border-radius:999px;margin-left:7px;text-transform:uppercase;letter-spacing:.04em;vertical-align:1px;}
.porg-n{background:var(--red);color:#fff;font-size:9px;font-weight:800;padding:1px 7px;border-radius:999px;
 margin-left:7px;text-transform:uppercase;letter-spacing:.04em;vertical-align:1px;}
.pnote{margin-top:4px;font-size:11.5px;line-height:1.5;color:#6b6a5e;font-style:italic;
 border-left:2px solid var(--yellow);padding-left:9px;}
.porg-s{background:#a9760a;color:#fff;font-size:9px;font-weight:800;padding:1px 7px;border-radius:999px;
 margin-left:7px;text-transform:uppercase;letter-spacing:.04em;vertical-align:1px;}
.mnotes{margin-top:11px;border-top:1px solid var(--hair);padding-top:9px;}
.mnotes>summary{cursor:pointer;list-style:none;font-size:10.5px;font-weight:700;text-transform:uppercase;
 letter-spacing:.04em;color:var(--mute);}
.mnotes>summary::-webkit-details-marker{display:none;}
.mnotes>summary::after{content:" \\25be";} .mnotes[open]>summary::after{content:" \\25b4";}
.mnotes ul{margin:9px 0 2px;padding:0;list-style:none;}
.mnotes li{font-size:12px;line-height:1.5;margin:7px 0;color:#4a4a42;}
.mnotes .vd{color:var(--mute);font-size:11px;margin-right:6px;}
.mnotes .vv{font-weight:800;margin-right:5px;}
.vv-no{color:var(--red);} .vv-yes{color:var(--green);}
.mnotes a{color:var(--ink);}
.mzero{font-size:12px;color:var(--mute);font-style:italic;margin-top:8px;}
.legendc{margin:16px 0 0;font-size:11.5px;color:var(--mute);display:flex;gap:16px;flex-wrap:wrap;}
.legendc i{width:10px;height:10px;border-radius:999px;display:inline-block;margin-right:5px;vertical-align:-1px;}
footer{margin-top:54px;border-top:1px solid var(--hair);padding-top:18px;font-size:12px;color:var(--mute);line-height:1.6;}
footer b{color:var(--ink);}
.totop{position:fixed;right:16px;bottom:16px;width:46px;height:46px;border-radius:999px;background:var(--ink);
 color:var(--yellow);display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:800;
 text-decoration:none;box-shadow:0 2px 10px rgba(0,0,0,.28);opacity:0;pointer-events:none;transform:translateY(8px);
 transition:opacity .18s,transform .18s;z-index:1000;}
.totop.show{opacity:1;pointer-events:auto;transform:none;} .totop:hover{background:#26261f;}
"""
    def bar(p, t):
        tot = p["yes"] + p["no"] + p["other"]
        if not tot:
            return '<span class="tbar"></span><span class="tnum">no tagged votes</span>'
        wy, wn = round(100 * p["yes"] / tot), round(100 * p["no"] / tot)
        seg = (f'<i style="width:{wy}%;background:var(--green)"></i>'
               f'<i style="width:{wn}%;background:var(--red)"></i>'
               f'<i style="width:{max(0,100-wy-wn)}%;background:#c9c8b8"></i>')
        n_c = len(p["events"])
        cf = (f' <button type="button" class="cflag" data-t="{t}" '
              f'title="Filter this member\'s vote list to contested {t} items">{n_c} contested</button>') if n_c else ""
        return (f'<span class="tbar">{seg}</span>'
                f'<span class="tnum"><b>{p["yes"]}</b> yes · {p["no"]} no · {p["other"]} other</span>{cf}')

    def member_card(city, m, data):
        per = data.get("topics", {})
        org = data.get("org", [])
        chips = (f'<span class="mch st">{esc(m["status"])}</span>' if m.get("status") else "")
        chips += "".join(f'<span class="mch aff">{esc(a)}</span>' for a in m.get("affiliations", []))
        chips += "".join(f'<span class="mch">{esc(b)}</span>' for b in m.get("boards", []))
        mail = (f'<a class="mmail" href="mailto:{esc(m["email"])}">Email</a>'
                if m.get("email") else "")
        donors = (f'<a class="mmail" href="{esc(m["donors_url"])}" target="_blank" rel="noopener">Campaign finance ↗</a>'
                  if m.get("donors_url") else "")
        bio = f'<p class="mbio">{esc(m["bio"])}</p>' if m.get("bio") else ""
        orgrow = ""
        if org:
            na = sum(1 for e in org if e["aligned"]); nc = len(org) - na
            parts = []
            if na: parts.append(f'<b class="oal">{na} aligned</b>')
            if nc: parts.append(f'<b class="oct">{nc} counter</b>')
            orgrow = (f'<div class="trow"><span class="tlab tl-org">Org priority</span>'
                      f'<span class="tnum">{" · ".join(parts)} — curated positions</span></div>')
        rows, any_votes, notes = "", False, []
        for t in track.TOPIC_ORDER:
            p = per.get(t, {"yes": 0, "no": 0, "other": 0, "events": []})
            if p["yes"] + p["no"] + p["other"]:
                any_votes = True
            rows += (f'<div class="trow"><span class="tlab tl-{t.lower()}">{t}</span>{bar(p, t)}</div>')
            for e in p["events"]:
                notes.append((e, t))
        seen_n, lis = set(), ""
        notes.sort(key=lambda x: x[0]["date"], reverse=True)
        notes.sort(key=lambda x: -x[0].get("w", 1.0))   # stable: weight desc, then date desc
        notes = [(e, "org") for e in sorted(org, key=lambda e: (-e["sev"], e["date"]), reverse=False)] + notes
        for e, t in notes:
            k = (e["date"], e["title"][:60])
            if k in seen_n: continue
            seen_n.add(k)
            vcls = "vv-no" if (e["vote"] or "").lower() in NO else "vv-yes"
            tal = e.get("tally")
            tal_s = f' ({tal["aye"]}–{tal["nay"]})' if tal else ""
            w = e.get("w", 1.0)
            wp = ('<span class="wmaj">major</span>' if w >= 2.5 else
                  '<span class="wmin">minor</span>' if w <= 0.5 else "")
            if "pos_label" in e:
                wp = (f'<span class="porg-y" title="{esc(e["pos_label"])}">org-aligned ✓</span>' if e["aligned"]
                      else (f'<span class="porg-n" title="{esc(e["pos_label"])}">counter ✕</span>' if e.get("sev", 2) >= 2
                            else f'<span class="porg-s" title="{esc(e["pos_label"])}">soft counter</span>'))
            lis += (f'<li data-t="{esc(t)}"><span class="vd">{esc(e["date"])}</span>'
                    f'<span class="vv {vcls}">{esc(e["vote"] or "?")}</span>'
                    f'<a href="{esc(e["url"])}" target="_blank" rel="noopener">'
                    f'{esc(track._trunc(e["title"], 88))}</a>'
                    f' <span class="vd">{esc(e.get("result",""))}{tal_s}</span>{wp}'
                    + (f'<div class="pnote">{esc(e["pos_note"])}</div>' if e.get("pos_note") else "")
                    + '</li>')
        notes_html = (f'<details class="mnotes"><summary>{len(seen_n)} contested / dissenting vote(s)</summary>'
                      f'<ul>{lis}</ul></details>') if lis else ""
        zero = '' if any_votes else '<p class="mzero">No topic-tagged roll calls recorded for this member in the window (most items pass on consent).</p>'
        disp = f'District {m["district"]}' if m.get("district") else (m.get("seat") or "")
        since = m.get("since")
        if since:
            yrs = max(0, 2026 - int(since))
            ended = "Term ended" in (m.get("status") or "")
            disp += (f' · {since}–2026 · {yrs} yrs' if ended else
                     f' · since {since} · {yrs if yrs else "<1"} yr{"s" if yrs != 1 else ""}')
        nm = (f'<a class="mlink" href="{esc(m["website"])}" target="_blank" rel="noopener">{esc(m["name"])}</a>'
              if m.get("website") else esc(m["name"]))
        hasn = " has-notes" if lis else ""
        return (f'<div class="mcard{hasn}"><div class="mtop"><span class="mname">{nm}</span>'
                f'<span class="mseat">{esc(disp)}</span>{mail}{donors}</div>'
                f'{bio}<div class="mchips">{chips}</div>{orgrow}{rows}{zero}{notes_html}</div>')

    secs = ""
    for city in ("Phoenix", "Tempe"):
        a = agg[city]
        cards = ""
        for m in members.get(city, {}).get("members", []):
            cards += member_card(city, m, a["members"].get(m["name"], {"topics": {}, "org": []}))
        src = ("Legistar roll-call votes (Formal Meetings + Transportation, Infrastructure & Planning Subcommittee)"
               if city == "Phoenix" else
               "Legal Action Summaries (named For / Against lists) from Tempe Agenda Online")
        secs += (f'<section class="citysec" id="{city.lower()}">'
                 f'<header class="chead"><h2>{city}</h2>'
                 f'<span class="csub">{a["n_events"]} topic-tagged vote records · {a["n_meetings"]} meeting dates · source: {src}</span>'
                 f'<button type="button" class="conly">Contested only</button></header>'
                 f'<p class="note">Bars show each member\'s recorded votes on items this tracker tags as Housing, Heat, '
                 f'Transit, or Walkability since {SINCE}. Most items pass unanimously on consent, so the signal is in '
                 f'the <b>contested</b> list — expand it to see every split or dissenting vote, linked to the official record. '
                 f'Contested lists rank substance first: citywide text amendments and plan changes carry a <b>major</b> badge; '
                 f'single-parcel permits, plats, and licenses are marked <b>minor</b> (weights are tunable in vote_weights.json).</p>'
                 f'<div class="mgridc">{cards}</div></section>')

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Council Voting Records — Valley Urban Action Alliance / Urban Phoenix Project</title>
<style>{css}</style></head>
<body>
<div class="mast"><div class="wrap">
  <div class="brandbar">
    <span class="logo"><span class="logomark lm-upp"></span><span class="logotext">Urban Phoenix Project</span></span>
    <span class="branddiv"></span>
    <span class="logo"><span class="logomark lm-vuaa"></span><span class="logotext">Valley Urban Action Alliance</span></span>
  </div>
  <h1>Council <span class="hl">Voting Records</span></h1>
  <p class="tag">How each councilmember has voted on housing, heat, transit, and walkability items,
     compiled from official roll calls and legal action summaries. Companion to the
     Regional Policy &amp; Ordinance Tracker.</p>
  <div class="runinfo"><span>Updated <b>{generated}</b></span>
    <span>Window: <b>since {SINCE}</b></span>
    <span>Cities: <b>Phoenix · Tempe</b> (pilot)</span></div>
  <nav class="topnav"><span></span><a href="#phoenix">Phoenix</a><a href="#tempe">Tempe</a><a href="index.html">← Policy tracker</a></nav>
</div></div>
<div class="wrap">
{secs}
<div class="legendc"><span><i style="background:var(--green)"></i>Yes</span>
<span><i style="background:var(--red)"></i>No</span>
<span><i style="background:#c9c8b8"></i>Abstain / other</span></div>
<footer><b>How to read this.</b> A "contested" vote is any motion with at least one No (or that failed).
Consent-calendar items pass unanimously and carry little signal on their own; splits are where positions show.
Phoenix records are machine roll calls from the Legistar API; Tempe records are parsed from the city's published
Legal Action Summaries, which name each member For or Against. Every vote links to the official source.
Bios, affiliations, board seats, and campaign-finance links are curated by the organizations (members.json) and
should be independently verified. Org-priority markers reflect positions curated by Valley Urban Action Alliance
(positions.json) — an advocacy overlay, distinct from the neutral voting record. Educational resource compiled from public government records.
Generated {generated}.</footer>
</div>
<a class="totop" id="totop" href="#" aria-label="Back to top">&#8593;</a>
<script>
(function(){{
 document.querySelectorAll('.cflag').forEach(function(btn){{
   btn.addEventListener('click', function(){{
     var card=btn.closest('.mcard'); var t=btn.getAttribute('data-t');
     var det=card.querySelector('details.mnotes');
     if(card.getAttribute('data-filter')===t){{
       card.removeAttribute('data-filter'); btn.classList.remove('on');
     }} else {{
       card.setAttribute('data-filter', t);
       card.querySelectorAll('.cflag.on').forEach(function(x){{x.classList.remove('on');}});
       btn.classList.add('on');
       if(det){{ det.setAttribute('open',''); }}
     }}
   }});
 }});
 document.querySelectorAll('.conly').forEach(function(btn){{
   btn.addEventListener('click', function(){{
     var sec=btn.closest('.citysec'); var on=sec.classList.toggle('conly-on');
     btn.classList.toggle('on', on);
     if(on) sec.querySelectorAll('.mcard.has-notes details.mnotes').forEach(function(d){{d.setAttribute('open','');}});
   }});
 }});
}})();
</script>
<script>
(function(){{var t=document.getElementById('totop');
 function u(){{t.classList.toggle('show',window.scrollY>500);}}
 window.addEventListener('scroll',u,{{passive:true}});u();
 t.addEventListener('click',function(e){{e.preventDefault();window.scrollTo({{top:0,behavior:'smooth'}});}});}})();
</script>
</body></html>"""
    return html

def main():
    global _POSITIONS
    state = _load(CSTATE, {})
    votes = _load(VOTES, [])
    n0 = len(votes)
    positions = load_positions()
    _POSITIONS = positions
    phoenix_scrape(state, votes, positions)
    tempe_scrape(state, votes)
    # re-tag every event under the current topic rules so tagger improvements
    # apply retroactively (safe for tightening changes; broadened keywords need a re-scrape)
    for e in votes:
        e["title"] = _tidy_title(e.get("title", ""))
        e["topics"] = topics_of(e.get("title", "") + " " + e.get("motion", ""))
    print(f"   vote events: {n0} -> {len(votes)} (+{len(votes)-n0})")
    VOTES.write_text(json.dumps(votes, indent=0), encoding="utf-8")
    CSTATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    members = scaffold_members(votes)
    cfg = load_weights()
    agg = aggregate(votes, members, cfg, positions)
    gen = dt.datetime.now(track.PHX).strftime("%b %-d, %Y · %-I:%M %p MST")
    OUT.mkdir(exist_ok=True)
    (OUT / "council.html").write_text(render_council(agg, members, gen), encoding="utf-8")
    for c in ("Phoenix", "Tempe"):
        print(f"   {c}: {agg[c]['n_events']} tagged vote records across {agg[c]['n_meetings']} meeting dates")
    print("   wrote site/council.html")

if __name__ == "__main__":
    main()
