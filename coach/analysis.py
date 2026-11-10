"""Extrai métricas da partida e aponta onde o jogador está errando.

Além dos números básicos (KDA, farm, visão), reconstrói a partir da timeline:
- teamfights (agrupando abates próximos no tempo e no espaço)
- objetivos (dragão, barão, arauto, torres...) e se você estava perto
- contexto de cada morte (zona do mapa, inimigos envolvidos, aliados por perto)

Limite: a Riot só dá UMA posição por minuto por jogador; entre os frames a
posição é interpolada linearmente, então tudo é aproximado.
"""
import math
import os
from dataclasses import dataclass, field

from coach import meta as meta_mod
from coach import tactics

# Referências aproximadas (elo médio) por posição. Ajuste conforme seu elo.
BENCH = {
    #            cs/min  visão/min
    "TOP":     (6.5, 0.8),
    "JUNGLE":  (5.5, 1.1),
    "MIDDLE":  (7.0, 0.9),
    "BOTTOM":  (7.2, 0.9),
    "UTILITY": (1.2, 2.2),
}

# O meta vem de Challenger/GM/Master; a régua é uma fração do que os vencedores fazem lá.
META_BENCH_FACTOR = float(os.getenv("META_BENCH_FACTOR", "0.8"))
MIN_ITEM_CHECK_MIN = 25   # só cobra itens centrais em partidas longas

MAP_SIDE = 14870          # x + y == MAP_SIDE é a diagonal do rio
ENEMY_ZONE = 2500         # quão além do rio conta como "território"
FIGHT_GAP_MS = 15_000     # intervalo máx. entre abates da mesma luta
FIGHT_RADIUS = 3500       # distância máx. entre abates da mesma luta
MIN_FIGHT_KILLS = 3       # abates mínimos para considerar teamfight
PRESENT_DIST = 4500       # a esta distância você "estava no objetivo/luta"
ALLY_NEAR_DIST = 3000     # aliado "por perto" de uma morte
DEAD_WINDOW_MS = 45_000   # morte recente (aprox. tempo de respawn)
AFTERMATH_MS = 90_000     # janela para ligar luta/morte a objetivo
EPIC = {"DRAGON", "BARON_NASHOR", "RIFTHERALD", "HORDE", "ATAKHAN"}


@dataclass
class DeathInfo:
    time: str
    zone: str                       # território inimigo / rio-centro / território aliado
    enemies_involved: int           # quem te matou + assistências
    allies_nearby: int              # aliados a < 3000 unidades
    in_teamfight: bool
    followed_by_enemy_objective: str | None = None  # ex.: "DRAGON (+35s)"


@dataclass
class Teamfight:
    time: str
    zone: str
    ally_kills: int
    ally_deaths: int
    result: str                     # vitória / derrota / empate
    i_participated: bool
    i_died: bool
    i_died_first: bool
    my_distance_at_start: int | None
    absent: bool                    # longe, vivo e fora da luta
    objective_after: str | None = None  # ex.: "inimigo: DRAGON (+40s)"


@dataclass
class Objective:
    kind: str                       # DRAGON, BARON_NASHOR, TOWER, INHIBITOR...
    time: str
    taken_by: str                   # aliado / inimigo
    my_distance: int | None
    present: bool
    i_was_dead: bool


@dataclass
class MatchReport:
    match_id: str
    champion: str
    role: str
    win: bool
    minutes: float
    kda: str
    kda_ratio: float
    cs_per_min: float
    vision_per_min: float
    control_wards: int
    kill_participation: float | None
    team_damage_share: float | None
    # comparação com oponente de lane (None se não encontrado)
    opponent: str | None = None
    side: str = ""                  # "azul" (time 100) ou "vermelho" (time 200)
    wards_placed: int = 0
    wards_killed: int = 0
    control_wards_placed: int = 0
    shop_times: list[str] = field(default_factory=list)   # quando foi à loja (aprox. quando voltou à base)
    gold_diff_10: int | None = None
    gold_diff_15: int | None = None
    cs_diff_10: int | None = None
    cs_diff_15: int | None = None
    deaths_early: int = 0   # antes dos 10 min
    deaths_mid: int = 0     # 10-20 min
    deaths_late: int = 0    # depois dos 20 min
    death_times: list[str] = field(default_factory=list)
    deaths: list[DeathInfo] = field(default_factory=list)
    teamfights: list[Teamfight] = field(default_factory=list)
    objectives: list[Objective] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)   # contexto do meta vivo (vazio se não houver cache)


# ---------------------------------------------------------------- helpers

def _fmt(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def _xy(p: dict) -> tuple[float, float]:
    return p["x"], p["y"]


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _pos_at(frames: list, pid: int, t_ms: float):
    """Posição interpolada entre os frames de 1 minuto. None se indisponível."""
    last = len(frames) - 1
    f = min(max(t_ms, 0) / 60000, last)
    i = int(f)
    j = min(i + 1, last)
    w = f - i
    a = frames[i]["participantFrames"].get(str(pid), {}).get("position")
    b = frames[j]["participantFrames"].get(str(pid), {}).get("position")
    if not a and not b:
        return None
    if not a or not b:
        return _xy(a or b)
    return (a["x"] + (b["x"] - a["x"]) * w, a["y"] + (b["y"] - a["y"]) * w)


def _zone(pos, team: int) -> str:
    depth = (pos[0] + pos[1] - MAP_SIDE) * (1 if team == 100 else -1)
    if depth > ENEMY_ZONE:
        return "território inimigo"
    if depth < -ENEMY_ZONE:
        return "território aliado"
    return "rio/centro"


def _frame_at(timeline: dict, minute: int) -> dict | None:
    frames = timeline["info"]["frames"]
    return frames[minute]["participantFrames"] if minute < len(frames) else None


def _cs(pf: dict) -> int:
    return pf["minionsKilled"] + pf["jungleMinionsKilled"]


def _cluster_fights(kills: list[dict]) -> list[list[dict]]:
    fights, cur = [], []
    for e in kills:
        if (
            cur
            and e["timestamp"] - cur[-1]["timestamp"] <= FIGHT_GAP_MS
            and _dist(_xy(e["position"]), _xy(cur[-1]["position"])) <= FIGHT_RADIUS
        ):
            cur.append(e)
        else:
            if cur:
                fights.append(cur)
            cur = [e]
    if cur:
        fights.append(cur)
    return [c for c in fights if len(c) >= MIN_FIGHT_KILLS]


# ---------------------------------------------------------------- análise

def analyze(match: dict, timeline: dict, puuid: str, meta: dict | None = None) -> MatchReport:
    info = match["info"]
    me = next(p for p in info["participants"] if p["puuid"] == puuid)
    pid, my_team = me["participantId"], me["teamId"]
    role = me.get("teamPosition") or "UNKNOWN"
    minutes = info["gameDuration"] / 60 if info["gameDuration"] < 100000 else info["gameDuration"] / 60000
    cs = me["totalMinionsKilled"] + me["neutralMinionsKilled"]
    k, d, a = me["kills"], me["deaths"], me["assists"]
    ch = me.get("challenges", {})

    rep = MatchReport(
        match_id=match["metadata"]["matchId"],
        champion=me["championName"],
        role=role,
        win=me["win"],
        minutes=round(minutes, 1),
        kda=f"{k}/{d}/{a}",
        kda_ratio=round((k + a) / max(d, 1), 2),
        cs_per_min=round(cs / minutes, 2),
        vision_per_min=round(me["visionScore"] / minutes, 2),
        control_wards=me.get("visionWardsBoughtInGame", 0),
        side="azul" if my_team == 100 else "vermelho",
        wards_placed=me.get("wardsPlaced", 0),
        wards_killed=me.get("wardsKilled", 0),
        control_wards_placed=me.get("detectorWardsPlaced", 0),
        kill_participation=ch.get("killParticipation"),
        team_damage_share=ch.get("teamDamagePercentage"),
    )

    # --- oponente de lane ---
    opp = next(
        (p for p in info["participants"] if p["teamId"] != my_team and p.get("teamPosition") == role),
        None,
    )
    if opp:
        rep.opponent = opp["championName"]
        for minute, g_attr, c_attr in ((10, "gold_diff_10", "cs_diff_10"), (15, "gold_diff_15", "cs_diff_15")):
            frame = _frame_at(timeline, minute)
            if frame:
                mine, theirs = frame[str(pid)], frame[str(opp["participantId"])]
                setattr(rep, g_attr, mine["totalGold"] - theirs["totalGold"])
                setattr(rep, c_attr, _cs(mine) - _cs(theirs))

    frames = timeline["info"]["frames"]
    team_of = {p["participantId"]: p["teamId"] for p in info["participants"]}
    events = sorted((e for f in frames for e in f["events"]), key=lambda e: e["timestamp"])
    kills = [e for e in events if e["type"] == "CHAMPION_KILL" and e.get("position")]
    # idas à loja: compras separadas por mais de 40s; a primeira (início do jogo) é ignorada
    buys = [e["timestamp"] for e in events if e["type"] == "ITEM_PURCHASED" and e.get("participantId") == pid]
    shops, last = [], None
    for t in buys:
        if last is None or t - last > 40_000:
            shops.append(t)
        last = t
    rep.shop_times = [_fmt(t) for t in shops if t > 60_000][:15]
    my_death_ts = [e["timestamp"] for e in kills if e["victimId"] == pid]

    def dead_recently(ts: float) -> bool:
        return any(0 <= ts - t <= DEAD_WINDOW_MS for t in my_death_ts)

    # --- objetivos ---
    objectives: list[tuple[int, Objective]] = []
    for e in events:
        if e["type"] == "ELITE_MONSTER_KILL":
            kind, taker = e["monsterType"], e.get("killerTeamId")
        elif e["type"] == "BUILDING_KILL":
            kind = e["buildingType"].replace("_BUILDING", "")
            taker = 200 if e["teamId"] == 100 else 100
        else:
            continue
        ts = e["timestamp"]
        mine = _pos_at(frames, pid, ts)
        dist = _dist(mine, _xy(e["position"])) if mine and e.get("position") else None
        objectives.append((ts, Objective(
            kind=kind,
            time=_fmt(ts),
            taken_by="aliado" if taker == my_team else "inimigo",
            my_distance=round(dist) if dist is not None else None,
            present=dist is not None and dist <= PRESENT_DIST,
            i_was_dead=dead_recently(ts),
        )))
    rep.objectives = [o for _, o in objectives]

    def next_objective(ts: float, only_enemy: bool = False):
        for t, o in objectives:
            if ts <= t <= ts + AFTERMATH_MS and (not only_enemy or o.taken_by == "inimigo"):
                return t, o
        return None

    # --- teamfights ---
    fights = _cluster_fights(kills)
    in_fight_ids = {id(e) for f in fights for e in f}
    for fight in fights:
        start, end = fight[0]["timestamp"], fight[-1]["timestamp"]
        cx = sum(e["position"]["x"] for e in fight) / len(fight)
        cy = sum(e["position"]["y"] for e in fight) / len(fight)
        ally_kills = sum(1 for e in fight if team_of.get(e["victimId"]) != my_team)
        ally_deaths = len(fight) - ally_kills
        died = any(e["victimId"] == pid for e in fight)
        participated = died or any(
            e.get("killerId") == pid or pid in e.get("assistingParticipantIds", []) for e in fight
        )
        mine = _pos_at(frames, pid, start)
        dist = _dist(mine, (cx, cy)) if mine else None
        absent = (not participated) and dist is not None and dist > PRESENT_DIST and not dead_recently(start)
        after = next_objective(end)
        rep.teamfights.append(Teamfight(
            time=_fmt(start),
            zone=_zone((cx, cy), my_team),
            ally_kills=ally_kills,
            ally_deaths=ally_deaths,
            result="vitória" if ally_kills > ally_deaths else "derrota" if ally_kills < ally_deaths else "empate",
            i_participated=participated,
            i_died=died,
            i_died_first=fight[0]["victimId"] == pid,
            my_distance_at_start=round(dist) if dist is not None else None,
            absent=absent,
            objective_after=f"{after[1].taken_by}: {after[1].kind} (+{int((after[0] - end) / 1000)}s)" if after else None,
        ))

    # --- mortes ---
    for e in kills:
        if e["victimId"] != pid:
            continue
        ts, pos = e["timestamp"], _xy(e["position"])
        m = ts / 60000
        rep.death_times.append(_fmt(ts))
        if m < 10:
            rep.deaths_early += 1
        elif m < 20:
            rep.deaths_mid += 1
        else:
            rep.deaths_late += 1

        allies = 0
        for other, team in team_of.items():
            if other != pid and team == my_team:
                p = _pos_at(frames, other, ts)
                if p and _dist(p, pos) <= ALLY_NEAR_DIST:
                    allies += 1
        nxt = next_objective(ts, only_enemy=True)
        rep.deaths.append(DeathInfo(
            time=_fmt(ts),
            zone=_zone(pos, my_team),
            enemies_involved=1 + len(e.get("assistingParticipantIds", [])),
            allies_nearby=allies,
            in_teamfight=id(e) in in_fight_ids,
            followed_by_enemy_objective=f"{nxt[1].kind} (+{int((nxt[0] - ts) / 1000)}s)" if nxt else None,
        ))

    my_items = [me[f"item{n}"] for n in range(6) if me.get(f"item{n}")]
    rep.meta = meta_mod.context(meta, role, me["championName"], rep.opponent, my_items,
                                {"me": me, "timeline": timeline})
    rep.meta["game_minutes"] = rep.minutes
    try:
        rep.meta["tactics"] = tactics.analyze(match, timeline, me, (meta or {}).get("tactics"),
                                              (meta or {}).get("ddragon_version"), my_items)
    except Exception:  # dados táticos são um bônus: nunca derrubam o relatório
        rep.meta["tactics"] = {}
    rep.issues = _detect_issues(rep)
    return rep


# ---------------------------------------------------------------- problemas

def _detect_issues(r: MatchReport) -> list[str]:
    issues = []
    cs_bench, vis_bench = BENCH.get(r.role, (6.5, 0.9))
    src = "referência fixa"
    wb = r.meta.get("role_benchmark_winners") if r.meta else None
    if wb and wb.get("cs_min") and wb.get("vision_min"):
        cs_bench, vis_bench = round(wb["cs_min"] * META_BENCH_FACTOR, 2), round(wb["vision_min"] * META_BENCH_FACTOR, 2)
        src = f"meta: vencedores de elo alto x{META_BENCH_FACTOR}"

    if r.cs_per_min < cs_bench * 0.85 and r.role != "UTILITY":
        issues.append(f"Farm baixo: {r.cs_per_min} CS/min ({src}: ~{cs_bench}).")
    if r.vision_per_min < vis_bench * 0.7:
        issues.append(f"Pouca visão: {r.vision_per_min}/min ({src}: ~{vis_bench}).")
    if r.control_wards < max(1, r.minutes // 8) and r.role != "UTILITY":
        issues.append(f"Poucos control wards comprados ({r.control_wards}).")
    if r.deaths_early >= 2:
        issues.append(f"{r.deaths_early} mortes antes dos 10 min — fase de rotas instável.")
    if r.deaths_late >= 3:
        issues.append(f"{r.deaths_late} mortes após os 20 min — posicionamento em teamfights.")
    if r.gold_diff_15 is not None and r.gold_diff_15 < -800:
        issues.append(f"Atrás {abs(r.gold_diff_15)} de ouro do oponente aos 15 min.")
    if r.cs_diff_10 is not None and r.cs_diff_10 < -10:
        issues.append(f"{abs(r.cs_diff_10)} CS atrás do oponente aos 10 min.")
    if r.kill_participation is not None and r.kill_participation < 0.4:
        issues.append(f"Participação em abates baixa ({r.kill_participation:.0%}).")
    if r.kda_ratio < 2 and r.role != "UTILITY":
        issues.append(f"KDA baixo ({r.kda}, ratio {r.kda_ratio}).")

    # --- meta: build e campeão ---
    items = r.meta.get("items") if r.meta else None
    if items and r.minutes >= MIN_ITEM_CHECK_MIN and len(items["core_missing"]) >= 2:
        issues.append(
            "Build fora do meta do campeão: faltaram itens centrais "
            + ", ".join(items["core_missing"][:3]) + f" (construiu: {', '.join(items['built']) or 'nenhum item final'})."
        )
    mc = r.meta.get("my_champion") if r.meta else None
    if mc and mc["tier"] in ("C", "D") and mc["games"] >= 15:
        issues.append(
            f"{mc['name']} está fraco no patch atual (tier {mc['tier']}, WR {mc['winrate']:.0%} em {mc['games']} jogos) "
            "— dependa mais de execução e do matchup."
        )

    m = r.meta or {}
    spells = m.get("summoner_spells")
    if spells and not spells.get("has_flash"):
        issues.append(f"Sem Flash nos feitiços ({spells['mine']}); o padrão do campeão é {spells['meta']}.")
    elif spells and spells.get("my_rate", 1) < 0.1:
        issues.append(f"Feitiços incomuns ({spells['mine']}); o padrão do campeão é {spells['meta']}.")
    ft = m.get("first_item_timing")
    if ft and ft.get("my_min") and ft["my_min"] > ft["meta_median_min"] + 3:
        issues.append(
            f"Primeiro item completo aos {ft['my_min']:.0f} min ({ft['my_first_item']}); "
            f"o elo alto fecha por volta dos {ft['meta_median_min']:.0f} min — farm/recall atrasados."
        )
    rn = m.get("runes")
    if rn and rn.get("my_keystone") and rn.get("my_keystone_rate", 1) < 0.1:
        issues.append(f"Runa principal incomum ({rn['my_keystone']}); o padrão é {rn['meta_keystone']}.")
    sk = m.get("skill_order")
    if sk and sk.get("mine") and sk.get("my_rate", 1) < 0.1:
        issues.append(f"Ordem de skills incomum ({sk['mine']}); a mais usada é {sk['meta']}.")

    issues.extend(tactics.issues((r.meta or {}).get("tactics") or {}, r.role))

    # --- teamfights ---
    fights = r.teamfights
    absent = [f for f in fights if f.absent]
    if len(absent) >= 2:
        issues.append(
            f"Ausente (longe e vivo) de {len(absent)}/{len(fights)} teamfights: "
            + ", ".join(f.time for f in absent) + "."
        )
    first = [f for f in fights if f.i_died_first]
    if len(first) >= 2:
        issues.append(
            f"Foi o primeiro a morrer em {len(first)} teamfights ({', '.join(f.time for f in first)}) "
            "— entrando antes do time ou exposto."
        )
    lost_then_obj = [f for f in fights if f.result == "derrota" and f.objective_after and f.objective_after.startswith("inimigo")]
    if lost_then_obj:
        issues.append(
            f"{len(lost_then_obj)} teamfight(s) perdida(s) seguida(s) de objetivo inimigo: "
            + "; ".join(f"{f.time} → {f.objective_after}" for f in lost_then_obj) + "."
        )

    # --- mortes ---
    deep = [x for x in r.deaths if x.zone == "território inimigo"]
    if len(deep) >= 2 and len(deep) / max(len(r.deaths), 1) >= 0.4:
        issues.append(
            f"{len(deep)}/{len(r.deaths)} mortes em território inimigo ({', '.join(x.time for x in deep)})."
        )
    alone = [x for x in r.deaths if x.allies_nearby == 0 and not x.in_teamfight and x.enemies_involved >= 2]
    if len(alone) >= 2:
        issues.append(
            f"{len(alone)} mortes isoladas, pego por 2+ inimigos sem aliados por perto ({', '.join(x.time for x in alone)})."
        )
    costly = [x for x in r.deaths if x.followed_by_enemy_objective]
    if len(costly) >= 2:
        issues.append(
            f"{len(costly)} mortes seguidas de objetivo inimigo: "
            + "; ".join(f"{x.time} → {x.followed_by_enemy_objective}" for x in costly) + "."
        )

    # --- objetivos épicos (mais relevante para jungle/mid/suporte) ---
    if r.role in ("JUNGLE", "MIDDLE", "UTILITY"):
        epic = [o for o in r.objectives if o.kind in EPIC]
        missed = [o for o in epic if not o.present and not o.i_was_dead]
        if len(epic) >= 3 and len(missed) / len(epic) > 0.6:
            issues.append(
                f"Longe de {len(missed)}/{len(epic)} objetivos épicos "
                "(" + ", ".join(f"{o.kind} {o.time}" for o in missed) + ")."
            )
    return issues
