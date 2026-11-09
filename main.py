"""Uso: python main.py "Nome#TAG" [--region br1] [--count 1] [--queue 420] [--no-ai]"""
import argparse
import os
import sys

from dotenv import load_dotenv

from coach.analysis import analyze
from coach.riot import RiotClient, RiotError


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Coach de LoL")
    ap.add_argument("riot_id", help='Seu Riot ID, ex: "Fulano#BR1"')
    ap.add_argument("--region", default="br1")
    ap.add_argument("--count", type=int, default=1, help="quantas partidas recentes analisar")
    ap.add_argument("--queue", type=int, default=None, help="420=solo/duo, 440=flex")
    ap.add_argument("--no-ai", action="store_true", help="só métricas, sem chamar o Claude")
    args = ap.parse_args()

    if "#" not in args.riot_id:
        sys.exit('Use o formato "Nome#TAG".')
    name, tag = args.riot_id.rsplit("#", 1)

    key = os.getenv("RIOT_API_KEY")
    if not key:
        sys.exit("Defina RIOT_API_KEY no .env (https://developer.riotgames.com).")

    try:
        riot = RiotClient(key, args.region)
        puuid = riot.get_puuid(name, tag)
        ids = riot.get_match_ids(puuid, args.count, args.queue)
        if not ids:
            sys.exit("Nenhuma partida encontrada.")

        for mid in ids:
            report = analyze(riot.get_match(mid), riot.get_timeline(mid), puuid)
            print(f"\n{'=' * 60}\n{report.match_id} — {report.champion} ({report.role}) "
                  f"{'VITÓRIA' if report.win else 'DERROTA'} | KDA {report.kda} | {report.minutes} min")
            print(f"CS/min {report.cs_per_min} | Visão/min {report.vision_per_min} | "
                  f"Gold@15 {report.gold_diff_15} | CS@10 {report.cs_diff_10} | mortes {report.death_times}")
            if report.teamfights:
                print("Teamfights:")
                for f in report.teamfights:
                    me = "morreu" if f.i_died else "participou" if f.i_participated else "AUSENTE" if f.absent else "fora"
                    print(f"  {f.time} {f.result} ({f.ally_kills}x{f.ally_deaths}) — você {me}"
                          + (f" → {f.objective_after}" if f.objective_after else ""))
            for issue in report.issues:
                print(f"  ⚠ {issue}")

            if not args.no_ai:
                from coach.feedback import coach_feedback
                print("\n" + coach_feedback(report))
    except RiotError as e:
        sys.exit(f"Erro: {e}")


if __name__ == "__main__":
    main()
