"""Táticas de elo alto: ritmo por minuto, timing de objetivos, perfil de dano e composição.

Complementa `meta.py` sem depender dele:
- `record_extras()` extrai de uma partida de elo alto o que falta no registro do meta;
- `aggregate()` transforma esses registros em estatísticas (entra em `meta.build()["tactics"]`);
- `analyze()` olha a SUA partida, compara com essas estatísticas e devolve o contexto para o parecer;
- `issues()` converte esse contexto em pontos de atenção.

Partidas gravadas antes desta versão não têm os dados novos (a timeline não é guardada); elas só
contam para a escolha de função. Os benchmarks crescem conforme o meta baixa partidas novas.
"""
import json
import os
import statistics
import time
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
DDRAGON = "https://ddragon.leagueoflegends.com"

MINUTES = (5, 10, 15, 20)
MIN_PACE_CHAMP = 5           # vencedores mínimos para usar a régua do campeão na rota
MIN_PACE_ROLE = 20           # idem para a régua da rota
MIN_TIMING_SAMPLE = 15       # partidas mínimas para citar o timing de um objetivo
MIN_COMP_SAMPLE = 15
MIN_ROLE_GAMES = 15          # jogos do campeão para opinar sobre a função
OFF_ROLE_SHARE = 0.08
PACE_FLAG = float(os.getenv("META_PACE_FLAG", "0.8"))   # abaixo disso (x vencedores) vira ponto de atenção
LATE_OBJECTIVE_S = 120       # atraso sobre a mediana dos vencedores que vira ponto de atenção
DOMINANT_DAMAGE = 0.65       # fração de AD ou AP que torna o time "unidimensional"

FIRST_KINDS = ("horde", "dragon", "herald", "tower")
KIND_LABEL = {"horde": "Vastilarvas", "dragon": "dragão", "herald": "arauto", "tower": "torre"}
_MONSTER_KIND = {"DRAGON": "dragon", "RIFTHERALD": "herald", "HORDE": "horde"}


def _fmt(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# ------------------------------------------------------------- Data Dragon (classes e itens defensivos)

_live: dict = {"at": 0.0, "version": None}


def _version(hint: str | None) -> str | None:
    if hint:
        return hint
    if _live["version"] and time.time() - _live["at"] < 6 * 3600:
        return _live["version"]
    try:
        r = requests.get(f"{DDRAGON}/api/versions.json", timeout=15)
        r.raise_for_status()
        _live.update(at=time.time(), version=r.json()[0])
    except Exception:
        pass
    return _live["version"]


def _dd(version: str | None) -> dict:
    """Classes dos campeões e itens defensivos puros. {} se indisponível."""
    version = _version(version)
    if not version:
        return {}
    path = CACHE_DIR / f"tactics_dd_{version}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    try:
        base = f"{DDRAGON}/cdn/{version}/data/pt_BR"
        champs = requests.get(f"{base}/champion.json", timeout=20).json()["data"]
        items = requests.get(f"{base}/item.json", timeout=20).json()["data"]
    except Exception:
        return {}
    defense = {}
    for iid, it in items.items():
        tags = set(it.get("tags", []))
        if not it.get("gold", {}).get("purchasable") or not ({"Armor", "SpellBlock"} & tags):
            continue
        if {"Damage", "SpellDamage"} & tags:   # Zhonya, Sterak etc. são itens ofensivos com defesa de brinde
            continue
        if it.get("gold", {}).get("total", 0) < 900 or it.get("into") or "Trinket" in tags:
            continue
        defense[iid] = "both" if {"Armor", "SpellBlock"} <= tags else "armor" if "Armor" in tags else "mr"
    data = {"tags": {c["id"]: c.get("tags", []) for c in champs.values()},
            "names": {c["id"]: c["name"] for c in champs.values()}, "defense": defense}
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


# ------------------------------------------------------------- extração de uma partida

def snapshots(frames: list, pid: int) -> dict:
    """CS, ouro e XP do jogador aos marcos: {"10": [cs, ouro, xp]}."""
    out = {}
    for m in MINUTES:
        pf = frames[m]["participantFrames"].get(str(pid)) if len(frames) > m else None
        if pf:
            out[str(m)] = [pf["minionsKilled"] + pf["jungleMinionsKilled"], pf["totalGold"], pf.get("xp", 0)]
    return out


def first_takes(frames: list) -> dict:
    """Primeiro objetivo de cada tipo por time, em segundos: {100: {"dragon": 412}}."""
    first: dict = {}
    for f in frames:
        for e in f["events"]:
            if e["type"] == "ELITE_MONSTER_KILL":
                kind, team = _MONSTER_KIND.get(e["monsterType"]), e.get("killerTeamId")
            elif e["type"] == "BUILDING_KILL" and e.get("buildingType") == "TOWER_BUILDING":
                kind, team = "tower", 300 - e["teamId"]
            else:
                continue
            if kind and team in (100, 200):
                d = first.setdefault(team, {})
                d[kind] = min(d.get(kind, e["timestamp"] // 1000), e["timestamp"] // 1000)
    return first


def gold_lead(frames: list, parts: list) -> dict:
    """Vantagem de ouro de cada time nos marcos: {100: {"15": +1800}}."""
    out = {100: {}, 200: {}}
    for m in MINUTES:
        if len(frames) <= m:
            continue
        pf = frames[m]["participantFrames"]
        tot = {t: sum(pf[str(p["participantId"])]["totalGold"] for p in parts
                      if p["teamId"] == t and str(p["participantId"]) in pf) for t in (100, 200)}
        out[100][str(m)], out[200][str(m)] = tot[100] - tot[200], tot[200] - tot[100]
    return out


def damage_profile(members: list) -> dict:
    """Fração de AD, AP e dano verdadeiro do time (dano a campeões)."""
    phys = sum(p.get("physicalDamageDealtToChampions", 0) for p in members)
    magic = sum(p.get("magicDamageDealtToChampions", 0) for p in members)
    true = sum(p.get("trueDamageDealtToChampions", 0) for p in members)
    total = max(phys + magic + true, 1)
    return {"ad": round(phys / total, 3), "ap": round(magic / total, 3), "true": round(true / total, 3)}


def record_extras(match: dict, timeline: dict) -> dict:
    """Dados extras de uma partida de elo alto (mesma ordem de participantes de `info.participants`)."""
    parts = match["info"]["participants"]
    frames = timeline["info"]["frames"]
    first, lead = first_takes(frames), gold_lead(frames, parts)
    teams = []
    for t in match["info"]["teams"]:
        tid = t["teamId"]
        members = [p for p in parts if p["teamId"] == tid]
        teams.append({"id": tid, "w": int(t["win"]), "first": first.get(tid, {}),
                      "champs": [p["championName"] for p in members],
                      "dmg": damage_profile(members), "gl": lead.get(tid, {})})
    return {"tl": [snapshots(frames, p["participantId"]) for p in parts], "teams": teams}


# ------------------------------------------------------------- composição

def classify_comp(champs: list[str], tags_by_id: dict) -> dict:
    """Estilo da composição por heurística das classes do Data Dragon."""
    n: dict[str, int] = {}
    for c in champs:
        for t in tags_by_id.get(c, []):
            n[t] = n.get(t, 0) + 1
    tank, fighter, assassin = n.get("Tank", 0), n.get("Fighter", 0), n.get("Assassin", 0)
    ranged = n.get("Mage", 0) + n.get("Marksman", 0)
    if assassin >= 2:
        style = "pick/skirmish de assassinos"
    elif ranged >= 3 and tank == 0:
        style = "poke/alcance"
    elif tank >= 2 or (tank >= 1 and fighter >= 2):
        style = "engage/frontline"
    elif fighter >= 2:
        style = "skirmish de lutadores"
    else:
        style = "misto"
    return {"style": style, "frontline": tank >= 1 or fighter >= 2, "classes": n}


def _dominant(dmg: dict) -> str | None:
    phys_magic = dmg["ad"] + dmg["ap"]
    if phys_magic <= 0:
        return None
    if dmg["ad"] / phys_magic >= DOMINANT_DAMAGE:
        return "AD"
    if dmg["ap"] / phys_magic >= DOMINANT_DAMAGE:
        return "AP"
    return None


# ------------------------------------------------------------- agregação (entra em meta.build())

def _mean(xs):
    xs = list(xs)
    return round(statistics.fmean(xs), 1) if xs else None


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(int(q * len(xs)), len(xs) - 1)]


def _pace(ps: list[dict], min_n: int) -> dict:
    out = {}
    for m in map(str, MINUTES):
        rows = [p["tl"][m] for p in ps if m in p["tl"]]
        if len(rows) >= min_n:
            out[m] = {"cs": _mean(r[0] for r in rows), "gold": _mean(r[1] for r in rows),
                      "xp": _mean(r[2] for r in rows), "n": len(rows)}
    return out


def _rate(rows: list[int]) -> dict | None:
    return {"wr": round(sum(rows) / len(rows), 3), "n": len(rows)} if len(rows) >= MIN_COMP_SAMPLE else None


def aggregate(recs: list[dict], ddragon_version: str | None = None) -> dict:
    """Estatísticas táticas a partir dos registros do meta (`recs`)."""
    tx_recs = [r for r in recs if r.get("tx")]
    out: dict = {"samples": len(tx_recs)}

    # função em que cada campeão aparece (usa todas as partidas)
    games: dict[str, dict[str, int]] = {}
    for r in recs:
        for p in r["p"]:
            d = games.setdefault(p["c"], {})
            d[p["r"]] = d.get(p["r"], 0) + 1
    out["role_games"] = {c: d for c, d in games.items() if sum(d.values()) >= MIN_ROLE_GAMES}

    if not tx_recs:
        return out

    # ritmo por minuto dos vencedores: por campeão/rota, com fallback por rota
    winners = []
    for r in tx_recs:
        for p, tl in zip(r["p"], r["tx"]["tl"]):
            if p["w"]:
                winners.append({"r": p["r"], "c": p["c"], "tl": tl})
    by_role: dict[str, list] = {}
    by_champ: dict[tuple, list] = {}
    for w in winners:
        by_role.setdefault(w["r"], []).append(w)
        by_champ.setdefault((w["r"], w["c"]), []).append(w)
    pace_champ: dict[str, dict] = {}
    for (role, champ), ps in by_champ.items():
        pc = _pace(ps, MIN_PACE_CHAMP)
        if pc:
            pace_champ.setdefault(role, {})[champ] = pc
    out["pace"] = {"role": {role: pr for role, ps in by_role.items() if (pr := _pace(ps, MIN_PACE_ROLE))},
                   "champ": pace_champ}

    # timing dos primeiros objetivos
    timing = {}
    for kind in FIRST_KINDS:
        win_t, first_wins = [], []
        for r in tx_recs:
            ts = [(t["first"].get(kind), t["w"]) for t in r["tx"]["teams"]]
            if len(ts) != 2:
                continue
            win_t += [t for t, w in ts if t is not None and w]
            taken = [(t, w) for t, w in ts if t is not None]
            if taken:
                first_wins.append(min(taken)[1])
        if len(win_t) >= MIN_TIMING_SAMPLE:
            timing[kind] = {"winners_median_s": int(statistics.median(win_t)),
                            "winners_p25_s": int(_quantile(win_t, 0.25)),
                            "wr_first": round(sum(first_wins) / len(first_wins), 3), "n": len(win_t)}
    out["timing"] = timing

    # vantagem de ouro típica de quem vence
    lead = {}
    for m in map(str, MINUTES):
        xs = [t["gl"][m] for r in tx_recs for t in r["tx"]["teams"] if t["w"] and m in t["gl"]]
        if len(xs) >= MIN_TIMING_SAMPLE:
            lead[m] = int(statistics.median(xs))
    out["gold_lead_winners"] = lead

    # composição: o que muda a chance de vitória
    tags = _dd(ddragon_version).get("tags", {})
    if tags:
        styles: dict[str, list] = {}
        front: dict[bool, list] = {True: [], False: []}
        onedim: dict[bool, list] = {True: [], False: []}
        for r in tx_recs:
            for t in r["tx"]["teams"]:
                c = classify_comp(t["champs"], tags)
                styles.setdefault(c["style"], []).append(t["w"])
                front[c["frontline"]].append(t["w"])
                onedim[_dominant(t["dmg"]) is not None].append(t["w"])
        out["comp"] = {
            "style": {s: v for s, rows in styles.items() if (v := _rate(rows))},
            "frontline": {"com": _rate(front[True]), "sem": _rate(front[False])},
            "damage_one_dimensional": {"sim": _rate(onedim[True]), "nao": _rate(onedim[False])},
        }
    return out


# ------------------------------------------------------------- análise da SUA partida

def _pick_pace(tac: dict, role: str, champion: str, m: str) -> tuple[dict, str] | None:
    pace = tac.get("pace") or {}
    c = pace.get("champ", {}).get(role, {}).get(champion, {}).get(m)
    if c:
        return c, "campeão"
    r = pace.get("role", {}).get(role, {}).get(m)
    return (r, "rota") if r else None


def analyze(match: dict, timeline: dict, me: dict, tac: dict | None, version: str | None = None,
            my_items: list[int] | None = None) -> dict:
    """Contexto tático da partida. Funciona sem `tac`; nesse caso só traz os dados da própria partida."""
    tac = tac or {}
    info = match["info"]
    frames = timeline["info"]["frames"]
    parts = info["participants"]
    my_team, role, champion = me["teamId"], me.get("teamPosition") or "UNKNOWN", me["championName"]
    minutes = info["gameDuration"] / 60 if info["gameDuration"] < 100000 else info["gameDuration"] / 60000
    dd = _dd(version)
    ctx: dict = {}

    # --- 6. ritmo por minuto contra os vencedores ---
    pace = {}
    for m, (cs, gold, xp) in snapshots(frames, me["participantId"]).items():
        pick = _pick_pace(tac, role, champion, m)
        row = {"mine": {"cs": cs, "gold": gold, "xp": xp}}
        if pick:
            bench, src = pick
            row["winners"] = {k: bench[k] for k in ("cs", "gold", "xp")} | {"samples": bench["n"], "source": src}
            row["delta_pct"] = {k: round((v / bench[k] - 1) * 100) for k, v in
                                (("cs", cs), ("gold", gold), ("xp", xp)) if bench[k]}
        pace[m] = row
    if pace:
        ctx["pace_vs_winners"] = pace

    # --- 7. timing de objetivos e vantagem de ouro ---
    first = first_takes(frames)
    enemy_team = 300 - my_team
    timing = {}
    for kind in FIRST_KINDS:
        mine, theirs = first.get(my_team, {}).get(kind), first.get(enemy_team, {}).get(kind)
        if mine is None and theirs is None:
            continue
        row = {"my_team": _fmt(mine) if mine is not None else None, "my_team_s": mine,
               "enemy_team": _fmt(theirs) if theirs is not None else None,
               "first": None if mine is None and theirs is None else
                        "seu time" if theirs is None or (mine is not None and mine < theirs) else "inimigo"}
        t = (tac.get("timing") or {}).get(kind)
        if t:
            row.update(winners_median=_fmt(t["winners_median_s"]), winners_median_s=t["winners_median_s"],
                       winrate_of_team_that_is_first=t["wr_first"])
        timing[kind] = row
    if timing:
        ctx["objective_timing"] = timing
    lead = gold_lead(frames, parts)[my_team]
    if lead:
        ctx["team_gold_lead"] = {"mine": lead, "winners_typical": tac.get("gold_lead_winners") or None}

    # --- 8. perfil de dano e itens defensivos ---
    ally = [p for p in parts if p["teamId"] == my_team]
    enemy = [p for p in parts if p["teamId"] != my_team]
    ally_dmg, enemy_dmg = damage_profile(ally), damage_profile(enemy)
    dominant = _dominant(enemy_dmg)
    ctx["damage_profile"] = {"ally": ally_dmg, "enemy": enemy_dmg,
                             "enemy_one_dimensional": dominant, "ally_one_dimensional": _dominant(ally_dmg)}
    items = my_items if my_items is not None else [me[f"item{n}"] for n in range(6) if me.get(f"item{n}")]
    defense = dd.get("defense")
    if defense is not None and dd:
        kinds = [defense[str(i)] for i in items if str(i) in defense]
        armor, mr = sum(k in ("armor", "both") for k in kinds), sum(k in ("mr", "both") for k in kinds)
        d = {"armor_items": armor, "mr_items": mr, "mismatch": None}
        if dominant == "AP" and armor >= 2 and mr == 0:
            d["mismatch"] = "armadura contra time majoritariamente AP"
        elif dominant == "AD" and mr >= 2 and armor == 0:
            d["mismatch"] = "resistência mágica contra time majoritariamente AD"
        ctx["defense"] = d

    # --- 9. composição e função ---
    tags = dd.get("tags") or {}
    if tags:
        mine_c = classify_comp([p["championName"] for p in ally], tags)
        theirs_c = classify_comp([p["championName"] for p in enemy], tags)
        needs = []
        if not mine_c["frontline"]:
            needs.append("seu time não tinha frontline (nenhum tanque e menos de 2 lutadores)")
        if _dominant(ally_dmg):
            needs.append(f"o dano do seu time era quase todo {_dominant(ally_dmg)} — fácil de itemizar contra")
        if theirs_c["style"] == "engage/frontline" and mine_c["style"] == "poke/alcance":
            needs.append("composição de poke contra engage inimigo: evitar lutas longas à distância, precisa de peel/disengage")
        if mine_c["style"] == "engage/frontline" and theirs_c["style"] == "poke/alcance":
            needs.append("sua composição é de engage contra poke: forçar a luta cedo, não deixar o inimigo desgastar")
        comp = {"ally": {**mine_c, "champions": [p["championName"] for p in ally]},
                "enemy": {**theirs_c, "champions": [p["championName"] for p in enemy]},
                "my_champion_classes": tags.get(champion, []), "needs": needs}
        stats = tac.get("comp")
        if stats:
            comp["winrate_by_style"] = {s: v["wr"] for s, v in stats["style"].items()}
            comp["winrate_frontline"] = {k: v["wr"] for k, v in stats["frontline"].items() if v}
        ctx["composition"] = comp
    rg = (tac.get("role_games") or {}).get(champion)
    if rg:
        total = sum(rg.values())
        share = rg.get(role, 0) / total
        ctx["role_fit"] = {"games_in_role_share": round(share, 3), "main_role": max(rg, key=rg.get),
                           "off_role": share < OFF_ROLE_SHARE}
    ctx["game_minutes"] = round(minutes, 1)
    return ctx


# ------------------------------------------------------------- pontos de atenção

def issues(ctx: dict, role: str) -> list[str]:
    out = []
    if not ctx:
        return out

    behind = []
    for m in ("10", "15"):
        row = (ctx.get("pace_vs_winners") or {}).get(m)
        if not row or "winners" not in row:
            continue
        for key, label in (("cs", "CS"), ("gold", "ouro")):
            if key == "cs" and role == "UTILITY":
                continue
            mine, bench = row["mine"][key], row["winners"][key]
            if bench and mine < bench * PACE_FLAG:
                behind.append(f"{label} aos {m} min: {mine} vs ~{bench:.0f} dos vencedores "
                              f"({row['delta_pct'][key]:+d}%, ref. {row['winners']['source']})")
    if behind:
        out.append("Ritmo abaixo dos vencedores de elo alto — " + "; ".join(behind) + ".")

    late = []
    for kind, row in (ctx.get("objective_timing") or {}).items():
        mine, med = row.get("my_team_s"), row.get("winners_median_s")
        if mine is not None and med is not None and mine > med + LATE_OBJECTIVE_S:
            late.append(f"{KIND_LABEL[kind]} aos {row['my_team']} (vencedores ~{row['winners_median']})")
    if late:
        out.append("Primeiros objetivos do seu time atrasados: " + ", ".join(late[:3]) + ".")

    d = ctx.get("defense") or {}
    dp = ctx.get("damage_profile") or {}
    if d.get("mismatch") and ctx.get("game_minutes", 0) >= 20:
        side = "ap" if "AP" in d["mismatch"] else "ad"
        out.append(f"Build defensiva errada: {d['mismatch']} ({dp['enemy'][side]:.0%} do dano inimigo; "
                   f"seus itens: {d['armor_items']} de armadura, {d['mr_items']} de resistência mágica).")

    rf = ctx.get("role_fit") or {}
    if rf.get("off_role"):
        out.append(f"Campeão pouco usado nesta rota no elo alto ({rf['games_in_role_share']:.0%} das partidas; "
                   f"normalmente jogado de {rf['main_role']}) — válido só como pick surpresa.")
    return out
