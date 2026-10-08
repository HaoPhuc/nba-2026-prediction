"""Fetch NBA matches from the streamed.pk API (https://streamed.pk/docs/matches).

The API only lists upcoming and live matches, never past ones. To build up a
history, run this script regularly: each run merges the new matches into
data/nba_matches.csv (deduplicated by date + teams), and only matches from the
previous and current NBA seasons are kept.

Usage:
    python fetch_streamed.py
"""

import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

API_URL = "https://streamed.pk/api/matches/basketball"
OUT_PATH = Path(__file__).parent / "data" / "nba_matches.csv"

NBA_TEAMS = {
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
    "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
    "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
    "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
    "New York Knicks", "Oklahoma City Thunder", "Orlando Magic",
    "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers",
    "Sacramento Kings", "San Antonio Spurs", "Toronto Raptors", "Utah Jazz",
    "Washington Wizards",
}


def season_start_year(dt: datetime) -> int:
    """NBA seasons start in October, so Aug-Dec belongs to the season starting that year."""
    return dt.year if dt.month >= 8 else dt.year - 1


def season_label(start_year: int) -> str:
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def split_title(title: str) -> tuple[str, str]:
    """'Team A vs. Team B' -> ('Team A', 'Team B'); returns empty strings if no 'vs'."""
    parts = re.split(r"\s+vs\.?\s+", title.strip(), maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", "")


def fetch_matches() -> list[dict]:
    resp = requests.get(API_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def to_nba_rows(matches: list[dict]) -> list[dict]:
    rows = []
    for m in matches:
        if not m.get("date"):  # placeholder entries like "NFL Streams Schedule" have date 0
            continue
        teams = m.get("teams") or {}
        home = teams.get("home", {}).get("name")
        away = teams.get("away", {}).get("name")
        if not (home and away):
            home, away = split_title(m["title"])
        if home not in NBA_TEAMS or away not in NBA_TEAMS:
            continue
        dt = datetime.fromtimestamp(m["date"] / 1000, tz=timezone.utc)
        rows.append({
            "id": m["id"],
            "date_utc": dt.isoformat(),
            "season": season_label(season_start_year(dt)),
            "home_team": home,
            "away_team": away,
            "title": m["title"],
            "popular": m.get("popular", False),
            "sources": ";".join(f"{s['source']}:{s['id']}" for s in m.get("sources", [])),
        })
    return rows


def main() -> None:
    new = pd.DataFrame(to_nba_rows(fetch_matches()))

    if OUT_PATH.exists():
        df = pd.concat([pd.read_csv(OUT_PATH), new], ignore_index=True)
    else:
        df = new
    if df.empty:
        print("No NBA matches found.")
        return

    # The API's ids and home/away order change between fetches for the same game,
    # so dedupe on date + team pair instead. Newer fetches win.
    pair = df[["home_team", "away_team"]].apply(lambda r: "|".join(sorted(r)), axis=1)
    df = df.assign(_key=df["date_utc"] + "|" + pair)
    df = df.drop_duplicates(subset="_key", keep="last").drop(columns="_key")

    current = season_start_year(datetime.now(timezone.utc))
    keep = {season_label(current - 1), season_label(current)}
    df = df[df["season"].isin(keep)].sort_values("date_utc").reset_index(drop=True)

    OUT_PATH.parent.mkdir(exist_ok=True)
    df.to_csv(OUT_PATH, index=False)
    print(f"Fetched {len(new)} NBA matches; {len(df)} total in {OUT_PATH} (seasons: {', '.join(sorted(keep))})")


if __name__ == "__main__":
    main()
