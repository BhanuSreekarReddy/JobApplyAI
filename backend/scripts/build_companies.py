"""Rebuild app/data/companies.json: the starter list of companies whose public job boards are searched.

Each candidate name is resolved to a Greenhouse, Lever, or Ashby board, kept only if the board's
display name matches the company and it currently lists at least one US job.

    cd backend && ../.venv/bin/python scripts/build_companies.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.ats import Board, best_board_for_name, fetch_board  # noqa: E402
from app.agent.locations import match_location, parse_locations  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "companies.json"
US = parse_locations("United States, Remote, US")

CANDIDATES = """
Airbnb; Stripe; Figma; Robinhood; Databricks; Ramp; Notion; OpenAI; Anthropic; Spotify; Plaid; Lucid Motors;
Coinbase; Dropbox; Pinterest; Lyft; DoorDash; Instacart; Reddit; Discord; Twitch; Asana; Airtable; Gusto;
Brex; Mercury; Chime; Affirm; SoFi; Carta; Rippling; Deel; Gem; Lattice; Retool; Vercel; Netlify; Supabase;
PlanetScale; Cockroach Labs; MongoDB; Elastic; Confluent; HashiCorp; GitLab; Datadog; New Relic; Grafana Labs;
Sentry; PagerDuty; Okta; Cloudflare; Fastly; Twilio; SendGrid; Segment; Amplitude; Mixpanel; Heap; Braze;
Iterable; Klaviyo; HubSpot; Zendesk; Intercom; Front; Calendly; Loom; Miro; Canva; Webflow; Squarespace;
Wix; Shopify; Faire; Etsy; Wayfair; Chewy; Warby Parker; Allbirds; Glossier; Peloton; Strava; Whoop; Oura;
Calm; Headspace; Noom; Hims & Hers; Ro; Oscar Health; Clover Health; Devoted Health; Cityblock Health;
One Medical; Zocdoc; Nuna; Tempus; Flatiron Health; Benchling; Ginkgo Bioworks; Recursion; Insitro; Color;
23andMe; Verily; Komodo Health; Spring Health; Lyra Health; Headway; Alma; Cerebral; Grow Therapy; Included Health;
Samsara; Motive; Flexport; Convoy; Project44; Shippo; ShipBob; Stord; Zipline; Nuro; Aurora; Waymo; Cruise;
Zoox; Rivian; Anduril; Shield AI; Skydio; Joby Aviation; Archer Aviation; Relativity Space; Rocket Lab;
SpaceX; Astranis; Planet; Varda Space; Hadrian; Saronic; Palantir; Scale AI; Snorkel AI; Weights & Biases;
Hugging Face; Cohere; Perplexity; Mistral AI; Character AI; Runway; Replit; Cursor; Anysphere; Sourcegraph;
Glean; Harvey; Hebbia; Writer; Jasper; Typeface; Adept; Inflection AI; Together AI; Fireworks AI; Modal;
Baseten; Replicate; Pinecone; Weaviate; LangChain; LlamaIndex; Cerebras; SambaNova; Groq; Lambda; CoreWeave;
Crusoe; Abnormal Security; Arctic Wolf; Snyk; Wiz; Lacework; Tanium; SentinelOne; CrowdStrike; Zscaler;
1Password; Bitwarden; Vanta; Drata; Chainguard; Semgrep; Socket; Huntress; Material Security; Island; Cyera;
Coalition; Chainalysis; Anchorage Digital; Kraken; Gemini; Circle; Fireblocks; Alchemy; Uniswap Labs; OpenSea;
Consensys; Ripple; Paxos; Robinhood Markets; Wealthfront; Betterment; Acorns; Stash; Public; Titan;
Marqeta; Modern Treasury; Unit; Treasury Prime; Synctera; Column; Increase; Lithic; Highnote; Bill;
Navan; Expensify; Airwallex; Payoneer; Remitly; Wise; Nubank; Toast; Square; Block; Cash App; Afterpay;
Klarna; Bolt; Checkout.com; Adyen; Rapyd; Melio; Tipalti; Pilot; Puzzle; Mosaic; Runway Financial; Pigment;
Anaplan; Workiva; Blend; Better; Opendoor; Offerpad; Zillow; Redfin; Compass; Pacaso; Roofstock; Divvy Homes;
Arrived; Belong; Bilt Rewards; Flex; Esusu; Nova Credit; Petal; Mission Lane; Upstart; LendingClub; Prosper;
Greenlight; Step; Current; Dave; MoneyLion; Albert; Brigit; Even; Branch; Payactiv; Wagestream;
Grammarly; Duolingo; Coursera; Udemy; Chegg; Quizlet; Khan Academy; Outschool; Handshake; Guild; Coursehero;
MasterClass; Course Hero; Brainly; ClassDojo; Newsela; Nearpod; Kahoot; Remind; Instructure; PowerSchool;
Thumbtack; TaskRabbit; Angi; Yelp; Nextdoor; Patreon; Substack; Medium; Quora; Kickstarter; GoFundMe;
Eventbrite; StubHub; SeatGeek; Vivid Seats; Fanatics; DraftKings; FanDuel; PrizePicks; Underdog Fantasy;
Sleeper; Roblox; Epic Games; Riot Games; Unity; Niantic; Scopely; Jam City; Zynga; Bungie; Valve;
Netflix; Hulu; Roku; Vimeo; Spotify USA; SoundCloud; Audible; Pandora; SiriusXM; iHeartMedia; Genius;
The New York Times; Vox Media; BuzzFeed; Axios; Morning Brew; Bloomberg; Dow Jones; Condé Nast;
Uber; Lime; Bird; Getaround; Turo; Hopper; Kayak; Expedia; Booking; Tripadvisor; Vacasa; Sonder; Mindtrip;
Gopuff; Shipt; Grubhub; Postmates; Sweetgreen; Cava; Chipotle; Starbucks; Toast Tab; Olo; Owner.com;
ServiceTitan; Procore; Buildertrend; Fieldwire; OpenSpace; EquipmentShare; Built Technologies; Benetech;
Veeva; Epic; Athenahealth; Abridge; Ambience Healthcare; Commure; Suki; Nabla; Hippocratic AI; Cedar;
Waystar; Olive; Innovaccer; Health Gorilla; Particle Health; Redox; Datavant; Truveta; Viz.ai; Rad AI;
Medallion; Nomi Health; Garner Health; Thirty Madison; Maven Clinic; Kindbody; Carrot Fertility; Progyny;
Talkiatry; Brightline; Two Chairs; Daybreak Health; Ophelia; Bicycle Health; Pelago; Virta Health; Omada;
Hinge Health; Sword Health; Kaia Health; Dandy; Candid; SmileDirectClub; Ritual; Function Health; Superpower;
Notable; Infinitus; Assort Health; Tennr; Anterior; Cohere Health; Machinify; Arcadia; Lightbeam Health;
Asana Rebel; ClickUp; Monday.com; Smartsheet; Coda; Linear; Height; Shortcut; Productboard; Pendo; FullStory;
LogRocket; PostHog; Statsig; LaunchDarkly; Split; Optimizely; Contentful; Sanity; Strapi; Algolia; Typesense;
Postman; Kong; Apollo GraphQL; Hasura; Temporal; Prefect; Dagster Labs; dbt Labs; Fivetran; Airbyte;
Census; Hightouch; RudderStack; Snowflake; Starburst; Dremio; ClickHouse; SingleStore; Neo4j; Redis;
Couchbase; Yugabyte; Timescale; Materialize; Imply; Rockset; Hex; Mode; Sigma Computing; ThoughtSpot;
Observable; Deepnote; Streamlit; Plotly; Anaconda; Domino Data Lab; DataRobot; H2O.ai; Dataiku; Labelbox;
Scale; Tecton; Arize AI; Fiddler AI; WhyLabs; Humanloop; Vellum; Braintrust; Patronus AI; Galileo;
Docker; Chainlink Labs; Tailscale; Teleport; Twingate; Netskope; Illumio; Orca Security; Aqua Security;
Sysdig; Upwind; Torq; Tines; Expel; Red Canary; Blumira; Armis; Axonius; Claroty; Dragos; Nozomi Networks;
Verkada; Rhombus; Flock Safety; Axon; Motorola Solutions; Samsara IoT; Particle; Helium; Formlabs;
Desktop Metal; Markforged; Carbon; Bright Machines; Machina Labs; Vention; Formic; Path Robotics;
Agility Robotics; Figure AI; Apptronik; 1X; Boston Dynamics; Covariant; Dexterity; Plus One Robotics;
Locus Robotics; Symbotic; GreyOrange; Exotec; Fabric; Nimble Robotics; Built Robotics; Monarch Tractor;
Carbon Robotics; Bowery Farming; Plenty; AppHarvest; Indigo Ag; Pivot Bio; Apeel; Impossible Foods;
Beyond Meat; Oatly; Athletic Brewing; Liquid Death; Olipop; Poppi; Graza; Brightland; Omsom; Fly By Jing;
Arcadia Power; Palmetto; Sunrun; Span; Lunar Energy; Form Energy; Commonwealth Fusion; Helion; TAE;
Redwood Materials; Twelve; Charm Industrial; Heirloom; Climeworks; Watershed; Persefoni; Patch; Pachama;
Sylvera; Arcadia Data; Xpansiv; Crusoe Energy; Fervo Energy; Quaise; Antora Energy; Rondo Energy
"""


def candidate_names() -> list[str]:
    return list(dict.fromkeys(n.strip() for n in CANDIDATES.replace("\n", " ").split(";") if n.strip()))


async def resolve(client: httpx.AsyncClient, sem: asyncio.Semaphore, name: str) -> dict | None:
    async with sem:
        try:
            found = await best_board_for_name(client, name)
        except Exception:
            return None
        if not found:
            return None
        ats, slug, count, display = found
        if count <= 0 or not display:
            return None
        try:
            jobs = await fetch_board(client, Board(ats, slug, display))
        except Exception:
            return None
        us_jobs = sum(1 for j in jobs if match_location(j["location"], j["remote"], US)[0])
        if us_jobs == 0:
            return None
        return {"name": display.strip(), "ats": ats, "slug": slug, "jobs": len(jobs), "us_jobs": us_jobs}


async def main() -> None:
    names = candidate_names()
    sem = asyncio.Semaphore(16)
    limits = httpx.Limits(max_connections=48)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, limits=limits) as client:
        results = await asyncio.gather(*(resolve(client, sem, n) for n in names))
    seen: set[tuple[str, str]] = set()
    companies = []
    for row in results:
        if row and (row["ats"], row["slug"]) not in seen:
            seen.add((row["ats"], row["slug"]))
            companies.append(row)
    companies.sort(key=lambda r: r["name"].lower())
    OUT.write_text(json.dumps([{k: r[k] for k in ("name", "ats", "slug")} for r in companies], indent=1) + "\n")
    by_ats: dict[str, int] = {}
    for r in companies:
        by_ats[r["ats"]] = by_ats.get(r["ats"], 0) + 1
    print(f"{len(names)} candidates -> {len(companies)} companies with US jobs {by_ats}")
    print(f"US jobs listed: {sum(r['us_jobs'] for r in companies)} (of {sum(r['jobs'] for r in companies)} total)")


if __name__ == "__main__":
    asyncio.run(main())
