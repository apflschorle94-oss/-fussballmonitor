"""Live-Fußballmonitor: neutrale Spielstatistiken, keine Wettfunktion.

Start: streamlit run app.py
API: API-FOOTBALL (v3), eigener gültiger Schlüssel nötig.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import pandas as pd
import requests
import streamlit as st

BASE_URL = "https://v3.football.api-sports.io"
STATS = {
    "Torschüsse gesamt": "Total Shots",
    "Schüsse aufs Tor": "Shots on Goal",
    "Schüsse vorbei": "Shots off Goal",
    "Ecken": "Corner Kicks",
    "Ballbesitz": "Ball Possession",
    "xG (falls verfügbar)": "expected goals",
}

st.set_page_config(page_title="Live-Fußballmonitor", page_icon="⚽", layout="wide")


def api_error(response: requests.Response) -> str:
    if response.status_code in (401, 403):
        return "Ungültiger Schlüssel oder Endpunkt im API-Tarif nicht verfügbar."
    if response.status_code == 429:
        return "Das API-Limit ist erreicht. Bitte später erneut aktualisieren."
    return f"API-Fehler: HTTP {response.status_code}."


@st.cache_data(ttl=55, show_spinner=False)
def fetch(path: str, query: tuple[tuple[str, str], ...], key: str) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            f"{BASE_URL}/{path}",
            params=dict(query),
            headers={"x-apisports-key": key},
            timeout=12,
        )
        if not response.ok:
            raise RuntimeError(api_error(response))
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Verbindungsfehler: {exc}") from exc
    if payload.get("errors"):
        errors = payload["errors"]
        raise RuntimeError(f"API-Rückmeldung: {errors}")
    return payload.get("response") or []


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_standings(league: int, season: int, key: str) -> list[dict[str, Any]]:
    return fetch("standings", (("league", str(league)), ("season", str(season))), key)


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_h2h(a: int, b: int, key: str) -> list[dict[str, Any]]:
    return fetch("fixtures/headtohead", (("h2h", f"{a}-{b}"), ("last", "5")), key)


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_form(team: int, key: str) -> list[dict[str, Any]]:
    return fetch("fixtures", (("team", str(team)), ("last", "5")), key)


def as_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def raw_stats(item: dict[str, Any]) -> dict[str, Any]:
    return {str(entry.get("type", "")).lower(): entry.get("value") for entry in item.get("statistics", [])}


def one_stat(raw: dict[str, Any], name: str) -> float | None:
    return as_number(raw.get(name.lower()))


def display_num(value: float | None, as_percent: bool = False) -> str:
    if value is None:
        return "k. A."
    text = str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")
    return text + (" %" if as_percent else "")


def standings_row(data: list[dict[str, Any]], team_id: int) -> dict[str, Any] | None:
    for league_data in data:
        for table in league_data.get("league", {}).get("standings", []):
            for team in table:
                if team.get("team", {}).get("id") == team_id:
                    return team
    return None


def record_snapshot(fixture_id: int, home_id: int, away_id: int, responses: list[dict[str, Any]]) -> None:
    now = time.time()
    captured = {}
    for item in responses:
        team_id = item.get("team", {}).get("id")
        if team_id not in (home_id, away_id):
            continue
        raw = raw_stats(item)
        captured[team_id] = {
            "shots": one_stat(raw, "Total Shots"),
            "on_target": one_stat(raw, "Shots on Goal"),
            "corners": one_stat(raw, "Corner Kicks"),
        }
    if not captured:
        return
    snapshots = st.session_state.setdefault("snapshots", {})
    history = snapshots.setdefault(fixture_id, [])
    # Cache/UI-Reruns können identische Werte liefern; nur Messungen mit >=30s Abstand sammeln.
    if not history or now - history[-1][0] >= 30:
        history.append((now, captured))
    snapshots[fixture_id] = [(t, vals) for t, vals in history if t >= now - 3600]


def last_ten_minutes(fixture_id: int, team_id: int) -> dict[str, str] | None:
    history = st.session_state.get("snapshots", {}).get(fixture_id, [])
    if len(history) < 2:
        return None
    current_time, current = history[-1]
    # The earliest snapshot within the past 10 minutes becomes the baseline.
    candidates = [(t, vals) for t, vals in history[:-1] if t >= current_time - 600]
    if not candidates:
        return None
    baseline = candidates[0][1]
    start, end = baseline.get(team_id, {}), current.get(team_id, {})
    result: dict[str, str] = {}
    for label, code in (("Neue Schüsse", "shots"), ("Davon aufs Tor", "on_target"), ("Neue Ecken", "corners")):
        a, b = start.get(code), end.get(code)
        result[label] = display_num(max(0, b - a)) if a is not None and b is not None else "k. A."
    return result


def format_fixture(item: dict[str, Any]) -> str:
    a = item.get("teams", {}).get("home", {}).get("name", "Heim")
    b = item.get("teams", {}).get("away", {}).get("name", "Gast")
    league = item.get("league", {}).get("name", "Liga")
    return f"{a} – {b}  |  {league}  |  #{item['fixture']['id']}"


def get_score(item: dict[str, Any]) -> str:
    goal = item.get("goals", {})
    a = goal.get("home")
    b = goal.get("away")
    return f"{a if a is not None else '–'} : {b if b is not None else '–'}"


def show_h2h(items: list[dict[str, Any]]) -> None:
    if not items:
        st.info("Keine direkten Duelle in diesem API-Datensatz vorhanden.")
        return
    rows = []
    for game in items:
        rows.append({
            "Datum": str(game.get("fixture", {}).get("date", ""))[:10],
            "Heim": game.get("teams", {}).get("home", {}).get("name"),
            "Gast": game.get("teams", {}).get("away", {}).get("name"),
            "Ergebnis": get_score(game),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def last_five_rows(items: list[dict[str, Any]], team_id: int) -> pd.DataFrame:
    rows = []
    for game in items:
        a = game.get("teams", {}).get("home", {}).get("name", "")
        b = game.get("teams", {}).get("away", {}).get("name", "")
        rows.append({
            "Datum": str(game.get("fixture", {}).get("date", ""))[:10],
            "Spiel": f"{a} – {b}",
            "Ergebnis": get_score(game),
        })
    return pd.DataFrame(rows)


def view_fixture(game: dict[str, Any], key: str) -> None:
    fixture_id = int(game["fixture"]["id"])
    a = game["teams"]["home"]
    b = game["teams"]["away"]
    a_id, b_id = int(a["id"]), int(b["id"])
    minute = game.get("fixture", {}).get("status", {}).get("elapsed")
    status = game.get("fixture", {}).get("status", {}).get("short", "")
    st.subheader(f"{a['name']}  {get_score(game)}  {b['name']}")
    st.caption(f"{game.get('league', {}).get('name', 'Liga')} · {str(minute) + '. Minute' if minute is not None else status}")

    try:
        details = fetch("fixtures/statistics", (("fixture", str(fixture_id)),), key)
    except RuntimeError as exc:
        st.error(str(exc))
        details = []

    team_stats = {row.get("team", {}).get("id"): raw_stats(row) for row in details}
    home, away = team_stats.get(a_id, {}), team_stats.get(b_id, {})
    st.markdown("#### Aktuelle Spielstatistik")
    rows = []
    for label, code in STATS.items():
        rows.append({
            "Kategorie": label,
            a["name"]: display_num(one_stat(home, code), code == "Ball Possession"),
            b["name"]: display_num(one_stat(away, code), code == "Ball Possession"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    if not details:
        st.caption("Live-Detaildaten werden nicht für jede Liga oder jeden API-Tarif geliefert.")

    if details:
        record_snapshot(fixture_id, a_id, b_id, details)
    st.markdown("#### Spielaktivität im beobachteten Zeitraum")
    col1, col2 = st.columns(2)
    for col, team in ((col1, a), (col2, b)):
        with col:
            st.markdown(f"**{team['name']}**")
            diff = last_ten_minutes(fixture_id, int(team["id"]))
            if diff is None:
                st.caption("Noch keine zwei zeitlich getrennten Messpunkte. Monitor geöffnet lassen.")
            else:
                st.dataframe(pd.DataFrame([diff]), hide_index=True, use_container_width=True)
    st.caption("Diese Werte zeigen Änderungen zwischen gespeicherten Messpunkten der letzten 10 Minuten, keine prognostizierte Torwahrscheinlichkeit. Nach einem Neustart beginnt die Erfassung erneut.")

    with st.expander("Tabellenstand und Form", expanded=True):
        league = game.get("league", {})
        league_id, season = league.get("id"), league.get("season")
        if not league_id or not season:
            st.info("Liga- oder Saisondaten fehlen.")
        else:
            try:
                tables = fetch_standings(int(league_id), int(season), key)
                items = []
                for team in (a, b):
                    row = standings_row(tables, int(team["id"]))
                    items.append({
                        "Team": team["name"],
                        "Platz": row.get("rank", "k. A.") if row else "k. A.",
                        "Punkte": row.get("points", "k. A.") if row else "k. A.",
                        "Form (5 Spiele)": row.get("form", "k. A.") if row else "k. A.",
                    })
                st.dataframe(pd.DataFrame(items), hide_index=True, use_container_width=True)
            except RuntimeError as exc:
                st.info(f"Tabellendaten nicht abrufbar: {exc}")

    with st.expander("Letzte fünf direkte Duelle"):
        try:
            show_h2h(fetch_h2h(a_id, b_id, key))
        except RuntimeError as exc:
            st.info(f"Direkte Duelle nicht abrufbar: {exc}")

    with st.expander("Form: letzte fünf absolvierte Spiele je Team"):
        for team in (a, b):
            st.markdown(f"**{team['name']}**")
            try:
                recent = fetch_form(int(team["id"]), key)
                data = last_five_rows(recent, int(team["id"]))
                if len(data):
                    st.dataframe(data, hide_index=True, use_container_width=True)
                else:
                    st.caption("Keine Spiele verfügbar.")
            except RuntimeError as exc:
                st.info(f"Spiele nicht abrufbar: {exc}")


st.title("⚽ Live-Fußballmonitor")
st.caption("Live-Ergebnisse, Torschüsse, Ecken, xG (falls verfügbar), Tabellenstand, Form und direkte Duelle. Keine Quoten, keine Wetten.")

key = st.sidebar.text_input(
    "API-FOOTBALL-Schlüssel",
    value=os.getenv("APIFOOTBALL_KEY", ""),
    type="password",
    help="Ein persönlicher API-FOOTBALL-Schlüssel ist für Live-Daten erforderlich.",
)
st.sidebar.caption("Live-Ansicht aktualisiert ungefähr jede Minute. Datenumfang hängt vom Anbieter und API-Tarif ab.")

if not key:
    st.info("Zum Start den API-FOOTBALL-Schlüssel links eingeben oder in der Umgebungsvariable APIFOOTBALL_KEY hinterlegen.")
    st.stop()


@st.fragment(run_every="60s")
def live_monitor() -> None:
    st.caption(f"Letzte Bildschirmaktualisierung: {datetime.now().strftime('%H:%M:%S')}")
    try:
        games = fetch("fixtures", (("live", "all"),), key)
    except RuntimeError as exc:
        st.error(str(exc))
        return

    if not games:
        st.info("Derzeit keine laufenden Spiele in den für diesen API-Schlüssel verfügbaren Wettbewerben.")
        return

    league_names = sorted({str(game.get("league", {}).get("name", "Unbekannte Liga")) for game in games})
    col_search, col_league = st.columns(2)
    with col_search:
        search = st.text_input("Teams suchen", key="search_team").casefold().strip()
    with col_league:
        league_filter = st.selectbox("Liga", ["Alle Ligen"] + league_names, key="league_filter")

    filtered = []
    for game in games:
        if league_filter != "Alle Ligen" and game.get("league", {}).get("name") != league_filter:
            continue
        teams = game.get("teams", {})
        label = f"{teams.get('home', {}).get('name', '')} {teams.get('away', {}).get('name', '')}"
        if search and search not in label.casefold():
            continue
        filtered.append(game)

    st.metric("Laufende Spiele", len(games))
    if not filtered:
        st.info("Keine Spiele entsprechen diesem Filter.")
        return

    rows = []
    for game in filtered:
        f = game.get("fixture", {})
        teams = game.get("teams", {})
        rows.append({
            "Liga": game.get("league", {}).get("name"),
            "Minute": f.get("status", {}).get("elapsed"),
            "Heim": teams.get("home", {}).get("name"),
            "Ergebnis": get_score(game),
            "Gast": teams.get("away", {}).get("name"),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    options = {int(game["fixture"]["id"]): game for game in filtered}
    selected = st.selectbox(
        "Spiel für Details auswählen",
        options=list(options),
        format_func=lambda i: format_fixture(options[i]),
        key="selected_game",
    )
    if selected is not None:
        view_fixture(options[selected], key)


live_monitor()
