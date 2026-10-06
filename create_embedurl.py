import requests
import json

H = {"User-Agent": "Mozilla/5.0"}

NBA_TEAMS = {
    "atlanta hawks", "boston celtics", "brooklyn nets",
    "charlotte hornets", "chicago bulls", "cleveland cavaliers",
    "dallas mavericks", "denver nuggets", "detroit pistons",
    "golden state warriors", "houston rockets", "indiana pacers",
    "la clippers", "los angeles clippers", "los angeles lakers",
    "memphis grizzlies", "miami heat", "milwaukee bucks",
    "minnesota timberwolves", "new orleans pelicans", "new york knicks",
    "oklahoma city thunder", "orlando magic", "philadelphia 76ers",
    "phoenix suns", "portland trail blazers", "sacramento kings",
    "san antonio spurs", "toronto raptors", "utah jazz",
    "washington wizards"
}

def get_json(url):
    response = requests.get(url, headers=H, timeout=20)
    response.raise_for_status()
    return response.json()

matches = get_json("https://streamed.pk/api/matches/basketball")

# Filter for games involving NBA teams
matches = [
    game for game in matches
    if any(team in game["title"].lower() for team in NBA_TEAMS)
]

output = []

for game in matches:
    game_output = {"title": game["title"], "streams": []}

    for s in game["sources"]:
        streams = get_json(
            f"https://streamed.pk/api/stream/{s['source']}/{s['id']}"
        )

        for st in streams:
            game_output["streams"].append({
                "source": s["source"],
                "language": st["language"],
                "quality": "HD" if st["hd"] else "SD",
                "embedUrl": st["embedUrl"]
            })

    output.append(game_output)

with open("nba_streams.json", "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"Saved {len(output)} matches to nba_streams.json")