"""Meta vivo: aprende o que funciona no patch atual a partir de partidas de elo alto.

Fluxo:
1. `refresh()` descobre o patch vigente (Data Dragon), pega jogadores Challenger/GM/Master,
   baixa partidas ranqueadas novas (partida + timeline) e guarda um registro compacto de cada uma
   em `cache/meta_store.json`. Cada execução é incremental e respeita um orçamento de partidas.
2. `build()` agrega o armazenamento em estatísticas:
   - winrate/pickrate/banrate por campeão e rota (suavizado, com tamanho de amostra);
   - itens finais por campeão (frequência e ganho de winrate);
   - referências por rota (farm, visão, dano, ouro) do que os VENCEDORES fazem;
   - peso de cada rota: quanto a vantagem de ouro aos 15 min na lane muda a chance de vitória;
   - peso de cada objetivo: quanto liderar em dragões/barões/torres muda a chance de vitória.
3. `context()` recorta isso para UMA partida (campeão, oponente, build, rota) e alimenta a análise e o Gemini.

Tudo vem da API da Riot e do Data Dragon, então o meta acompanha cada patch sozinho.
"""
import json
import os
import statistics
import time
from pathlib import Path

import requests

from coach import tactics

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
STORE = CACHE_DIR / "meta_store.json"
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
DDRAGON = "https://ddragon.leagueoflegends.com"

KEEP_PATCHES = 2             # patch atual + anterior (usado só se o atual tiver pouca amostra)
MIN_PATCH_MATCHES = 150      # abaixo disso, mistura o patch anterior
MIN_GAMES_TIER = 8           # amostra mínima para classificar um campeão
PRIOR_GAMES = 20             # força da suavização do winrate rumo a 50%
LEAD_GOLD = 400              # vantagem de ouro aos 15 min que conta como "à frente"
MIN_SWING_SAMPLE = 20        # amostra mínima por lado para calcular um peso
STALE_AFTER_H = 12
OBJECTIVES = ("dragon", "baron", "riftHerald", "tower", "inhibitor", "horde")


# ------------------------------------------------------------- Data Dragon

def _get_json(url: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return r.json()


def live_patch() -> tuple[str, str]:
    """(patch 'major.minor', versão completa do Data Dragon)."""
    version = _get_json(f"{DDRAGON}/api/versions.json")[0]
    return ".".join(version.split(".")[:2]), version


def _ddragon(version: str) -> dict:
    """Itens finais e campeões da versão, com cache em disco."""
    path = CACHE_DIR / f"ddragon_{version}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("schema") == 2:
            return cached
    items_raw = _get_json(f"{DDRAGON}/cdn/{version}/data/pt_BR/item.json")["data"]
    items, legend = {}, []
    for iid, it in items_raw.items():
        gold = it.get("gold", {})
        if not gold.get("purchasable") or it.get("consumed") or "Trinket" in it.get("tags", []):
            continue
        if it.get("into"):  # tem evolução: não é item final
            continue
        boots = "Boots" in it.get("tags", [])
        if gold.get("total", 0) >= 2200 or (boots and gold.get("total", 0) >= 900):
            items[iid] = it["name"]
            if not boots and gold.get("total", 0) >= 2200:
                legend.append(iid)
    champs_raw = _get_json(f"{DDRAGON}/cdn/{version}/data/pt_BR/champion.json")["data"]
    champs = {c["key"]: {"id": c["id"], "name": c["name"]} for c in champs_raw.values()}
    runes = {}
    for style in _get_json(f"{DDRAGON}/cdn/{version}/data/pt_BR/runesReforged.json"):
        runes[str(style["id"])] = style["name"]
        for slot in style["slots"]:
            for rn in slot["runes"]:
                runes[str(rn["id"])] = rn["name"]
    spells = {v["key"]: v["name"] for v in
              _get_json(f"{DDRAGON}/cdn/{version}/data/pt_BR/summoner.json")["data"].values()}
    data = {"schema": 2, "items": items, "legend": legend, "champs": champs, "runes": runes, "spells": spells}
    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))
    return data


# ------------------------------------------------------------- armazenamento

def _load_store() -> dict:
    try:
        return json.loads(STORE.read_text())
    except (OSError, ValueError):
        return {"matches": {}, "players": [], "players_at": 0, "cursor": 0, "updated": 0}


def _save_store(store: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, separators=(",", ":")))
    tmp.replace(STORE)


SKILL_LETTERS = "QWER"


def participant_extras(timeline: dict, pid: int, legend: set[str]) -> dict:
    """Do timeline: minuto do primeiro item lendário e ordem de maximização das skills."""
    first_item = None
    counts = {1: 0, 2: 0, 3: 0}
    maxed: list[int] = []
    for f in timeline["info"]["frames"]:
        for e in f["events"]:
            if e.get("participantId") != pid:
                continue
            if e["type"] == "ITEM_PURCHASED" and first_item is None and str(e["itemId"]) in legend:
                first_item = (round(e["timestamp"] / 60000, 1), e["itemId"])
            elif e["type"] == "SKILL_LEVEL_UP" and e.get("levelUpType") == "NORMAL" and e["skillSlot"] in counts:
                slot = e["skillSlot"]
                counts[slot] += 1
                if counts[slot] == 5 and slot not in maxed:
                    maxed.append(slot)
    rest = sorted((s for s in counts if s not in maxed and counts[s]), key=lambda s: -counts[s])
    order = "".join(SKILL_LETTERS[s - 1] for s in maxed + rest)
    return {"first_item_min": first_item[0] if first_item else None,
            "first_item": first_item[1] if first_item else None,
            "skill_order": order or None}


def build_info(p: dict) -> dict:
    """Runas e feitiços de um participante da partida."""
    styles = p.get("perks", {}).get("styles", [])
    keystone = styles[0]["selections"][0]["perk"] if styles and styles[0].get("selections") else None
    spells = sorted([p.get("summoner1Id"), p.get("summoner2Id")])
    flash = "D" if p.get("summoner1Id") == 4 else "F" if p.get("summoner2Id") == 4 else None
    return {"keystone": keystone, "sub_style": styles[1]["style"] if len(styles) > 1 else None,
            "spells": spells, "flash": flash}


def _record(match: dict, timeline: dict, legend: set[str] = frozenset()) -> dict | None:
    """Reduz uma partida + timeline ao mínimo necessário para o meta."""
    info = match["info"]
    dur = info["gameDuration"] if info["gameDuration"] < 100000 else info["gameDuration"] / 1000
    if dur < 15 * 60 or info.get("queueId") != 420:
        return None
    parts = info["participants"]
    if sorted(p.get("teamPosition", "") for p in parts[:5] + parts[5:]) != sorted(ROLES * 2):
        return None  # papéis incompletos: não dá para comparar lanes
    minutes = dur / 60
    team_dmg, team_gold = {}, {}
    for p in parts:
        team_dmg[p["teamId"]] = team_dmg.get(p["teamId"], 0) + p["totalDamageDealtToChampions"]
        team_gold[p["teamId"]] = team_gold.get(p["teamId"], 0) + p["goldEarned"]

    frames = timeline["info"]["frames"]
    f15 = frames[15]["participantFrames"] if len(frames) > 15 else None
    by_role = {(p["teamId"], p["teamPosition"]): p for p in parts}
    out_parts = []
    for p in parts:
        opp = by_role.get((300 - p["teamId"], p["teamPosition"]))
        gd15 = cd15 = None
        if f15 and opp:
            a, b = f15[str(p["participantId"])], f15[str(opp["participantId"])]
            gd15 = a["totalGold"] - b["totalGold"]
            cd15 = (a["minionsKilled"] + a["jungleMinionsKilled"]) - (b["minionsKilled"] + b["jungleMinionsKilled"])
        ex = participant_extras(timeline, p["participantId"], legend)
        bi = build_info(p)
        out_parts.append({
            "k": bi["keystone"], "s2": bi["sub_style"], "ss": bi["spells"], "fl": bi["flash"],
            "so": ex["skill_order"], "fi": ex["first_item_min"], "fid": ex["first_item"],
            "op": opp["championName"] if opp else None,
            "r": p["teamPosition"], "c": p["championName"], "w": int(p["win"]),
            "i": [p[f"item{n}"] for n in range(6) if p.get(f"item{n}")],
            "cs": round((p["totalMinionsKilled"] + p["neutralMinionsKilled"]) / minutes, 2),
            "vs": round(p["visionScore"] / minutes, 2),
            "dm": round(p["totalDamageDealtToChampions"] / max(team_dmg[p["teamId"]], 1), 3),
            "gs": round(p["goldEarned"] / max(team_gold[p["teamId"]], 1), 3),
            "gd": gd15, "cd": cd15,
        })
    teams = []
    for t in info["teams"]:
        teams.append({
            "w": int(t["win"]),
            "bans": [b["championId"] for b in t.get("bans", []) if b["championId"] > 0],
            "obj": {k: t["objectives"].get(k, {}).get("kills", 0) for k in OBJECTIVES},
        })
    patch = ".".join(info["gameVersion"].split(".")[:2])
    return {"v": 2, "d": round(minutes, 1), "patch": patch, "t": info.get("gameCreation", 0),
            "p": out_parts, "teams": teams, "tx": tactics.record_extras(match, timeline)}


# ------------------------------------------------------------- atualização

def refresh(riot, max_new: int = 60, log=print) -> dict:
    """Baixa até `max_new` partidas novas de elo alto e atualiza o armazenamento."""
    patch, version = live_patch()
    store = _load_store()
    store["ddragon_version"] = version
    legend = set(_ddragon(version)["legend"])

    # jogadores de elite (renova 1x por dia)
    if time.time() - store["players_at"] > 86400 or not store["players"]:
        puuids = []
        for tier in ("challenger", "grandmaster", "master"):
            try:
                entries = riot.get_top_league(tier)
            except Exception as exc:
                log(f"[meta] liga {tier} indisponível: {exc}")
                continue
            for e in entries[:80]:
                try:
                    puuids.append(e.get("puuid") or riot.get_puuid_by_summoner(e["summonerId"]))
                except Exception:
                    continue
            if len(puuids) >= 120:
                break
        if puuids:
            store.update(players=puuids, players_at=time.time(), cursor=0)

    players, seen = store["players"], store["matches"]
    new_ids: list[str] = []
    since = int(time.time()) - 10 * 86400
    scanned = 0
    while players and len(new_ids) < max_new and scanned < len(players):
        puuid = players[store["cursor"] % len(players)]
        store["cursor"] = (store["cursor"] + 1) % len(players)
        scanned += 1
        try:
            for mid in riot.get_match_ids_since(puuid, 10, 420, since):
                if mid not in seen and mid not in new_ids:
                    new_ids.append(mid)
        except Exception as exc:
            log(f"[meta] ids de {puuid[:8]}…: {exc}")
    new_ids = new_ids[:max_new]
    log(f"[meta] patch {patch}: {len(new_ids)} partidas novas para baixar")

    added = 0
    for n, mid in enumerate(new_ids, 1):
        try:
            rec = _record(riot.get_match(mid), riot.get_timeline(mid), legend)
        except Exception as exc:
            log(f"[meta] {mid}: {exc}")
            continue
        seen[mid] = rec or {"skip": True}  # guarda o descarte para não baixar de novo
        added += rec is not None
        if n % 10 == 0:
            _save_store(store)

    # descarta patches antigos
    patches = sorted({r["patch"] for r in seen.values() if "patch" in r}, key=_patch_key, reverse=True)
    keep = set(patches[:KEEP_PATCHES])
    store["matches"] = {k: v for k, v in seen.items() if v.get("skip") or v["patch"] in keep}
    store["updated"] = time.time()
    store["live_patch"] = patch
    _save_store(store)
    log(f"[meta] {added} partidas adicionadas; total {sum(1 for v in store['matches'].values() if 'patch' in v)}")
    return status()


def _patch_key(p: str):
    a, _, b = p.partition(".")
    return int(a), int(b or 0)


# ------------------------------------------------------------- agregação

def _smooth(wins: int, games: int) -> float:
    return (wins + PRIOR_GAMES * 0.5) / (games + PRIOR_GAMES)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 2) if xs else None


def build() -> dict | None:
    """Agrega o armazenamento. Retorna None se ainda não há dados."""
    store = _load_store()
    recs = [v for v in store["matches"].values() if "patch" in v]
    if not recs:
        return None
    patches = sorted({r["patch"] for r in recs}, key=_patch_key, reverse=True)
    used = [patches[0]]
    cur = [r for r in recs if r["patch"] == patches[0]]
    if len(cur) < MIN_PATCH_MATCHES and len(patches) > 1:
        used.append(patches[1])
        cur = [r for r in recs if r["patch"] in used]

    try:
        dd = _ddragon(store.get("ddragon_version") or live_patch()[1])
    except Exception:
        dd = {"items": {}, "champs": {}}
    id_to_champ = {k: v["id"] for k, v in dd["champs"].items()}
    names = {v["id"]: v["name"] for v in dd["champs"].values()}
    item_names = dd["items"]

    parts = [(p, r) for r in cur for p in r["p"]]
    dur_of = {id(p): r.get("d", 30) for r in cur for p in r["p"]}
    n_matches = len(cur)

    # --- por campeão/rota ---
    cr: dict[tuple, dict] = {}
    for p, _ in parts:
        d = cr.setdefault((p["r"], p["c"]), {"g": 0, "w": 0})
        d["g"] += 1
        d["w"] += p["w"]
    bans: dict[str, int] = {}
    for r in cur:
        for t in r["teams"]:
            for cid in t["bans"]:
                name = id_to_champ.get(str(cid))
                if name:
                    bans[name] = bans.get(name, 0) + 1

    champions: dict[str, dict] = {}
    for role in ROLES:
        rows = [(c, d) for (r, c), d in cr.items() if r == role]
        scores = [(_smooth(d["w"], d["g"]) - 0.5) * 100 for _, d in rows if d["g"] >= MIN_GAMES_TIER]
        mu = statistics.fmean(scores) if scores else 0
        sd = statistics.pstdev(scores) if len(scores) > 2 else 0
        role_games = sum(d["g"] for _, d in rows)
        out = {}
        for champ, d in rows:
            wr = _smooth(d["w"], d["g"])
            tier = None
            if d["g"] >= MIN_GAMES_TIER and sd > 0:
                z = ((wr - 0.5) * 100 - mu) / sd
                tier = "S" if z >= 1 else "A" if z >= 0.35 else "B" if z > -0.35 else "C" if z > -1 else "D"
            out[champ] = {
                "name": names.get(champ, champ), "games": d["g"], "winrate": round(wr, 3),
                "pickrate": round(d["g"] / max(role_games, 1), 3),
                "banrate": round(bans.get(champ, 0) / (2 * n_matches), 3),
                "tier": tier,
            }
        champions[role] = out

    # --- itens por campeão (todas as rotas juntas) ---
    by_champ: dict[str, list] = {}
    for p, _ in parts:
        by_champ.setdefault(p["c"], []).append(p)
    items: dict[str, list] = {}
    for champ, ps in by_champ.items():
        games = len(ps)
        base = _smooth(sum(p["w"] for p in ps), games)
        count: dict[str, list] = {}
        for p in ps:
            for iid in {str(i) for i in p["i"]}:
                if iid in item_names:
                    c = count.setdefault(iid, [0, 0])
                    c[0] += 1
                    c[1] += p["w"]
        rows = []
        for iid, (g, w) in count.items():
            if g < 3 and g / games < 0.2:
                continue
            rows.append({
                "id": iid, "name": item_names[iid], "games": g, "rate": round(g / games, 2),
                "winrate": round(_smooth(w, g), 3), "delta": round(_smooth(w, g) - base, 3),
            })
        rows.sort(key=lambda x: (-x["rate"], -x["games"]))
        items[champ] = rows[:12]

    # --- referências por rota (o que os vencedores fazem) ---
    role_bench = {}
    for role in ROLES:
        win = [p for p, _ in parts if p["r"] == role and p["w"]]
        if len(win) >= 20:
            role_bench[role] = {
                "cs_min": _mean(p["cs"] for p in win), "vision_min": _mean(p["vs"] for p in win),
                "damage_share": _mean(p["dm"] for p in win), "gold_share": _mean(p["gs"] for p in win),
                "samples": len(win),
            }

    # --- peso de cada rota: vantagem de ouro aos 15 min vs. chance de vitória ---
    swings = {}
    for role in ROLES:
        ahead = [p["w"] for p, _ in parts if p["r"] == role and p["gd"] is not None and p["gd"] >= LEAD_GOLD]
        behind = [p["w"] for p, _ in parts if p["r"] == role and p["gd"] is not None and p["gd"] <= -LEAD_GOLD]
        if len(ahead) >= MIN_SWING_SAMPLE and len(behind) >= MIN_SWING_SAMPLE:
            swings[role] = {
                "wr_ahead": round(sum(ahead) / len(ahead), 3), "wr_behind": round(sum(behind) / len(behind), 3),
                "samples": len(ahead) + len(behind),
            }
            swings[role]["swing"] = round(swings[role]["wr_ahead"] - swings[role]["wr_behind"], 3)
    total = sum(max(s["swing"], 0) for s in swings.values())
    role_impact = {
        role: {**s, "weight": round(max(s["swing"], 0) / total, 2) if total else None}
        for role, s in swings.items()
    }

    # --- peso de cada objetivo: liderar a contagem vs. ficar atrás ---
    obj_impact = {}
    for obj in OBJECTIVES:
        lead, trail = [], []
        for r in cur:
            a, b = r["teams"][0], r["teams"][1]
            for me, you in ((a, b), (b, a)):
                diff = me["obj"][obj] - you["obj"][obj]
                (lead if diff > 0 else trail if diff < 0 else []).append(me["w"])
        if len(lead) >= MIN_SWING_SAMPLE and len(trail) >= MIN_SWING_SAMPLE:
            obj_impact[obj] = {
                "wr_leading": round(sum(lead) / len(lead), 3), "wr_trailing": round(sum(trail) / len(trail), 3),
                "swing": round(sum(lead) / len(lead) - sum(trail) / len(trail), 3),
            }


    # --- matchups de lane: A contra B na mesma rota ---
    matchups: dict[str, dict] = {}
    for p, _ in parts:
        if p.get("op"):
            m = matchups.setdefault(p["r"], {}).setdefault(p["c"], {}).setdefault(p["op"], [0, 0, 0, 0])
            m[0] += 1
            m[1] += p["w"]
            if p["gd"] is not None:
                m[2] += p["gd"]
                m[3] += 1

    # --- runas, skills, feitiços, primeiro item e curva de poder por campeão ---
    def top(counter: dict, n: int = 3) -> list[dict]:
        total = sum(g for g, _ in counter.values())
        rows = [{"key": k, "games": g, "rate": round(g / total, 2), "winrate": round(_smooth(w, g), 3)}
                for k, (g, w) in counter.items()]
        return sorted(rows, key=lambda x: -x["games"])[:n]

    def bump(d: dict, key, win: int) -> None:
        c = d.setdefault(key, [0, 0])
        c[0] += 1
        c[1] += win

    champ_build: dict[str, dict] = {}
    for champ, ps in by_champ.items():
        keys, subs, skills, firsts, flashes = {}, {}, {}, {}, {}
        for p in ps:
            if p.get("k"):
                bump(keys, p["k"], p["w"])
            if p.get("s2"):
                bump(subs, p["s2"], p["w"])
            if p.get("so"):
                bump(skills, p["so"], p["w"])
            if p.get("fid"):
                bump(firsts, p["fid"], p["w"])
            if p.get("fl"):
                bump(flashes, p["fl"], p["w"])
        times = [p["fi"] for p in ps if p.get("fi")]
        spells_by_role: dict[str, dict] = {}
        for p in ps:
            if p.get("ss"):
                bump(spells_by_role.setdefault(p["r"], {}), tuple(p["ss"]), p["w"])
        # curva de poder: winrate por duração da partida
        buckets = {"ate_25": [0, 0], "25_32": [0, 0], "32_mais": [0, 0]}
        for p, r in ((p, r) for p in ps for r in [dur_of[id(p)]]):
            name = "ate_25" if r < 25 else "25_32" if r < 32 else "32_mais"
            buckets[name][0] += 1
            buckets[name][1] += p["w"]
        champ_build[champ] = {
            "keystones": top(keys), "sub_styles": top(subs), "skill_orders": top(skills),
            "first_items": top(firsts), "flash": top(flashes, 2),
            "spells": {r: top(c, 2) for r, c in spells_by_role.items()},
            "first_item_median_min": round(statistics.median(times), 1) if len(times) >= 5 else None,
            "first_item_samples": len(times),
            "power_curve": {k: {"games": g, "winrate": round(_smooth(w, g), 3)} for k, (g, w) in buckets.items()},
        }

    return {
        "matchups": matchups, "champ_build": champ_build,
        "patches": used, "live_patch": store.get("live_patch"), "ddragon_version": store.get("ddragon_version"), "matches": n_matches,
        "updated": store.get("updated"), "champions": champions, "items": items,
        "role_bench": role_bench, "role_impact": role_impact, "objective_impact": obj_impact,
        "tactics": tactics.aggregate(cur, store.get("ddragon_version")),
    }


_cache: dict = {"at": 0.0, "mtime": 0.0, "data": None}


def load() -> dict | None:
    """Meta agregado, com cache em memória enquanto o arquivo não muda."""
    try:
        mtime = STORE.stat().st_mtime
    except OSError:
        return None
    if _cache["data"] is None or mtime != _cache["mtime"]:
        _cache.update(mtime=mtime, data=build())
    return _cache["data"]


def status() -> dict:
    m = load()
    if not m:
        return {"ready": False}
    return {
        "ready": True, "patches": m["patches"], "live_patch": m["live_patch"], "matches": m["matches"],
        "updated": m["updated"], "stale": is_stale(m),
    }


def is_stale(m: dict | None = None) -> bool:
    m = m or load()
    if not m or not m.get("updated"):
        return True
    return time.time() - m["updated"] > STALE_AFTER_H * 3600 or m["patches"][0] != m.get("live_patch")



# ------------------------------------------------------------- contexto de uma partida

def _champ_line(meta: dict, role: str, champ: str) -> dict | None:
    c = meta["champions"].get(role, {}).get(champ)
    if not c:
        return None
    return {k: c[k] for k in ("name", "games", "winrate", "pickrate", "banrate", "tier")}


def context(meta: dict | None, role: str, champion: str, opponent: str | None, my_items: list[int],
            player: dict | None = None) -> dict:
    """Recorte do meta relevante para a partida analisada. {} se não houver meta."""
    if not meta:
        return {}
    try:
        dd = _ddragon(meta.get("ddragon_version") or live_patch()[1])
    except Exception:
        dd = {"items": {}, "legend": [], "runes": {}, "spells": {}}
    names = dd["items"]
    ctx = {
        "patch_sample": f"patch {'+'.join(meta['patches'])}, {meta['matches']} partidas de Challenger/GM/Master",
        "stale": is_stale(meta),
        "my_champion": _champ_line(meta, role, champion),
        "opponent_champion": _champ_line(meta, role, opponent) if opponent else None,
        "role_benchmark_winners": meta["role_bench"].get(role),
        "lane_importance": {
            r: {"weight": v["weight"], "winrate_ahead_15": v["wr_ahead"], "winrate_behind_15": v["wr_behind"]}
            for r, v in meta["role_impact"].items()
        },
        "objective_importance": dict(sorted(
            ((k, v["swing"]) for k, v in meta["objective_impact"].items()), key=lambda kv: -kv[1])),
    }
    top = [i for i in meta["items"].get(champion, []) if i["games"] >= 3]
    if top:
        core = [i for i in top if i["rate"] >= 0.35][:6]
        mine = {str(i) for i in my_items}
        built = [names.get(str(i), str(i)) for i in my_items if str(i) in names]
        ctx["items"] = {
            "built": built,
            "meta_core": [f"{i['name']} ({i['rate']:.0%} das builds, WR {i['winrate']:.0%})" for i in core],
            "core_missing": [i["name"] for i in core if i["id"] not in mine],
            "off_meta_built": [names[str(i)] for i in my_items
                               if str(i) in names and str(i) not in {t["id"] for t in top}],
            "best_winrate_items": [f"{i['name']} (+{i['delta']:.1%} WR)" for i in
                                   sorted((i for i in top if i["games"] >= 5), key=lambda x: -x["delta"])[:3]
                                   if i["delta"] > 0],
        }
    ctx.update(_build_context(meta, dd, role, champion, opponent, player))
    return ctx


def _matchup(meta: dict, role: str, champion: str, opponent: str | None) -> dict | None:
    m = meta["matchups"].get(role, {}).get(champion, {}).get(opponent or "")
    if not m or m[0] < 5:
        return None
    games, wins, gd_sum, gd_n = m
    wr = _smooth(wins, games)
    return {
        "games": games, "winrate": round(wr, 3),
        "avg_gold_diff_15": round(gd_sum / gd_n) if gd_n else None,
        "verdict": "favorável" if wr >= 0.54 else "desfavorável" if wr <= 0.46 else "equilibrado",
    }


def _pick(rows: list[dict], key) -> dict | None:
    return next((r for r in rows if r["key"] == key), None)


def _build_context(meta: dict, dd: dict, role: str, champion: str, opponent: str | None, player: dict | None) -> dict:
    """Matchup, runas, skill order, feitiços, timing do primeiro item e curva de poder."""
    out: dict = {"matchup": _matchup(meta, role, champion, opponent)}
    cb = meta["champ_build"].get(champion)
    if not cb:
        return out
    runes, spells = dd.get("runes", {}), dd.get("spells", {})
    pc = {k: v for k, v in cb["power_curve"].items() if v["games"] >= 6}
    if len(pc) >= 2:
        best = max(pc, key=lambda k: pc[k]["winrate"])
        out["power_curve"] = {"winrate_por_duracao": pc, "melhor_fase": best}
    mine = None
    if player:
        me = player["me"]
        bi = build_info(me)
        ex = participant_extras(player["timeline"], me["participantId"], set(dd.get("legend", [])))
        mine = (bi, ex)

    k_top = cb["keystones"][0] if cb["keystones"] else None
    if k_top and sum(r["games"] for r in cb["keystones"]) >= 8:
        out["runes"] = {
            "meta_keystone": f"{runes.get(str(k_top['key']), k_top['key'])} ({k_top['rate']:.0%}, WR {k_top['winrate']:.0%})",
            "meta_secondary": [f"{runes.get(str(r['key']), r['key'])} ({r['rate']:.0%})" for r in cb["sub_styles"][:2]],
        }
        if mine and mine[0]["keystone"]:
            row = _pick(cb["keystones"], mine[0]["keystone"])
            out["runes"].update(
                my_keystone=runes.get(str(mine[0]["keystone"]), str(mine[0]["keystone"])),
                my_keystone_rate=row["rate"] if row else 0.0,
                my_secondary=runes.get(str(mine[0]["sub_style"]), str(mine[0]["sub_style"])),
            )

    so_top = cb["skill_orders"][0] if cb["skill_orders"] else None
    if so_top and sum(r["games"] for r in cb["skill_orders"]) >= 8:
        out["skill_order"] = {"meta": f"{so_top['key']} ({so_top['rate']:.0%}, WR {so_top['winrate']:.0%})"}
        if mine and mine[1]["skill_order"]:
            row = _pick(cb["skill_orders"], mine[1]["skill_order"])
            out["skill_order"].update(mine=mine[1]["skill_order"], my_rate=row["rate"] if row else 0.0)

    sp = cb["spells"].get(role)
    if sp and sum(r["games"] for r in sp) >= 5:
        label = lambda ids: " + ".join(spells.get(str(i), str(i)) for i in ids)  # noqa: E731
        out["summoner_spells"] = {"meta": f"{label(sp[0]['key'])} ({sp[0]['rate']:.0%})"}
        if mine:
            row = _pick(sp, tuple(mine[0]["spells"]))
            out["summoner_spells"].update(
                mine=label(mine[0]["spells"]), my_rate=row["rate"] if row else 0.0,
                has_flash=4 in mine[0]["spells"], flash_key=mine[0]["flash"],
                meta_flash_key=cb["flash"][0]["key"] if cb["flash"] else None,
            )

    if cb["first_item_median_min"]:
        out["first_item_timing"] = {
            "meta_median_min": cb["first_item_median_min"], "samples": cb["first_item_samples"],
            "meta_first_item": [dd["items"].get(str(r["key"]), str(r["key"])) + f" ({r['rate']:.0%})"
                                for r in cb["first_items"][:2]],
        }
        if mine and mine[1]["first_item_min"]:
            out["first_item_timing"].update(
                my_min=mine[1]["first_item_min"],
                my_first_item=dd["items"].get(str(mine[1]["first_item"]), str(mine[1]["first_item"])),
            )
    return out


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    ap = argparse.ArgumentParser(description="Atualiza o meta com partidas de elo alto")
    ap.add_argument("--region", default="br1")
    ap.add_argument("--matches", type=int, default=60, help="partidas novas a baixar nesta execução")
    args = ap.parse_args()
    load_dotenv(override=True)
    from coach.riot import RiotClient
    refresh(RiotClient(os.environ["RIOT_API_KEY"], args.region,
                       min_interval=float(os.getenv("META_MIN_INTERVAL", "1.25"))), args.matches)
    print(json.dumps(status(), indent=2))
