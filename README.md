# Regional Policy & Ordinance Tracker — automated daily run
# Drop this in .github/workflows/ of the upp-vuaa.github.io repo (or wherever
# the meetings tracker lives). It needs no Python packages — track.py is pure
# standard library — so the run is fast and dependency-free.
#
# Output lands in policy/ (mirroring the meetings/ convention), so the page is
# served at  https://upp-vuaa.github.io/policy/

name: policy-tracker

on:
  schedule:
    - cron: "0 13 * * *"      # 13:00 UTC = 6:00 AM Phoenix (MST, no DST)
  workflow_dispatch: {}        # lets you trigger a run by hand from the Actions tab

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run tracker
        run: python3 policy-tracker/track.py

      - name: Run council voting records
        run: python3 policy-tracker/council.py

      - name: Publish to policy/
        run: |
          mkdir -p policy
          cp policy-tracker/site/index.html      policy/index.html
          cp policy-tracker/site/map.html        policy/map.html
          cp policy-tracker/site/ordinances.json policy/ordinances.json
          cp policy-tracker/site/digest.txt      policy/digest.txt
          cp policy-tracker/site/council.html    policy/council.html

      - name: Commit if changed
        run: |
          git config user.name  "policy-tracker-bot"
          git config user.email "actions@users.noreply.github.com"
          git add policy/ policy-tracker/state.json policy-tracker/archive.json policy-tracker/geocache.json policy-tracker/districts.geojson policy-tracker/council_votes.json policy-tracker/council_state.json policy-tracker/members.json
          git diff --staged --quiet || git commit -m "Update policy tracker $(date -u +%Y-%m-%d)"
          git push
