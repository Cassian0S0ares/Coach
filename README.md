# Rift Coach

Analisa a partida mais recente de League of Legends de um jogador. O relatório mostra KDA, farm, visão, comparação com o oponente de rota, lutas em equipe, mortes, objetivos e pontos de atenção. Com uma chave do Gemini, também traz um parecer em português.

## Interface web

No Linux, configure o `.env` e execute `./run.sh`. Ele verifica o Python, instala as dependências que faltarem e inicia o servidor. Se `xdg-open` estiver disponível, abre a página no navegador.

No Windows, configure o `.env` e execute `run.bat`. Ele verifica o Python, instala as dependências que faltarem, abre o navegador e inicia o servidor.

Para iniciar manualmente:

1. Instale as dependências: `python3 -m pip install -r requirements.txt`
2. Copie `.env.example` para `.env` e configure `RIOT_API_KEY` e, opcionalmente, `GEMINI_API_KEY`.
3. Execute `python3 web.py`.
4. Abra `http://127.0.0.1:8000` e informe o Riot ID no formato `Nome#TAG`.

O servidor escuta apenas em `127.0.0.1`. A chave da Riot e a chave do Gemini ficam no servidor; o navegador não recebe essas chaves. A interface analisa uma partida recente por vez.

Na seção **Visão do coach**, a imagem do coach acompanha o botão **Ouvir explicação**. A narração usa `edge-tts`, com as vozes brasileiras Antônio e Francisca, sem chave ou API paga e sem depender de vozes instaladas no navegador. Requer internet: o texto é enviado ao serviço de voz online da Microsoft. O player permite pausar e continuar; o seletor ajusta a velocidade e **Parar** cancela a reprodução. Sem parecer de IA, lê um resumo das métricas. A imagem é estática, com destaque durante a fala; a voz é sintética, não uma clonagem. Após atualizar, reinicie com `./run.sh` ou `run.bat` para instalar a nova dependência e ativar a rota de áudio.

## Terminal

O comando original continua disponível: `python3 main.py "Nome#TAG" --region br1 --count 1`. Use `--no-ai` para obter somente as métricas.

## Meta vivo

O bot aprende o meta sozinho, a partir de partidas ranqueadas de Challenger/Grandmaster/Master do patch atual (Riot API + Data Dragon, sem site de terceiros). Cada partida vira um registro compacto em `cache/meta_store.json` (ignorado pelo git).

O que ele calcula e usa na análise e no parecer do Gemini:

- **Campeões**: winrate suavizado, pickrate, banrate e tier (S a D) por rota, com o número de jogos. Sem amostra mínima, não classifica.
- **Itens**: itens finais mais usados por campeão, ganho de winrate de cada um e quais itens centrais faltaram na sua build.
- **Importância de cada rota**: quanto estar à frente aos 15 min na lane muda a chance de vitória.
- **Importância de cada objetivo**: quanto liderar dragões, barões, arautos e torres muda a chance de vitória.
- **Referências por rota**: CS/min, visão/min e % de dano/ouro dos vencedores. As réguas de farm e visão usam esses valores multiplicados por `META_BENCH_FACTOR` (padrão 0.8).
- **Ritmo por minuto** (`coach/tactics.py`): CS, ouro e XP aos 5, 10, 15 e 20 min dos vencedores, por campeão e rota (cai para a média da rota com pouca amostra). O relatório mostra onde o seu ritmo caiu e aponta quando fica abaixo de `META_PACE_FLAG` (padrão 0.8) do que os vencedores fazem.
- **Timing de objetivos**: mediana do 1º Vastilarvas, dragão, arauto e torre de quem vence, o winrate de quem pega primeiro e a vantagem de ouro típica por minuto, comparados com o seu time.
- **Perfil de dano**: AD x AP x dano verdadeiro dos dois times; critica build defensiva que não combina (ex.: 2+ itens de armadura contra time com 65%+ de dano mágico).
- **Composição e função**: estilo dos dois times (engage, poke, pick, skirmish) a partir das classes do Data Dragon, falta de frontline, winrate por estilo e se o campeão é incomum na rota jogada.

Os dados táticos só existem para partidas baixadas depois dessa versão (a timeline não é guardada), então os benchmarks por minuto crescem aos poucos a cada atualização do meta.

- **Matchups de lane**: winrate do seu campeão contra o oponente da rota e diferença média de ouro aos 15 min (só com 5+ jogos do confronto).
- **Primeiro item completo**: minuto em que o elo alto fecha o primeiro item lendário, comparado com o seu.
- **Runas, ordem de skills e feitiços**: o padrão do campeão (com % de adoção) contra o que você usou; sem Flash é sempre apontado.
- **Curva de poder**: winrate do campeão por duração da partida (até 25 min, 25 a 32, 32+), para saber se ele é early ou late.

Essas estatísticas por campeão pedem pelo menos 5 a 8 jogos do campeão na amostra; abaixo disso o campo fica vazio.

Atualização:

- `python3 web.py` atualiza em segundo plano ao iniciar e a cada `META_REFRESH_HOURS` (padrão 6). Desligue com `META_AUTO_REFRESH=0`.
- Manualmente: `python3 -m coach.meta --region br1 --matches 100`, ou `python3 main.py "Nome#TAG" --refresh-meta 50`.
- Quando o patch muda, o armazenamento guarda o patch novo e o anterior. O anterior só entra na conta enquanto o atual tem menos de 150 partidas.
- Cada partida custa 2 chamadas à Riot. Com chave de desenvolvimento (100 chamadas a cada 2 min), `META_MIN_INTERVAL` (padrão 1.25 s) espaça as chamadas, então o meta cresce aos poucos. Com chave de produção, reduza esse valor e aumente `META_MATCHES_PER_REFRESH` (padrão 60).

Opcionais no `.env`: `META_REGION`, `META_MIN_INTERVAL`, `META_MATCHES_PER_REFRESH`, `META_REFRESH_HOURS`, `META_BENCH_FACTOR`.
