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

## Terminal

O comando original continua disponível: `python3 main.py "Nome#TAG" --region br1 --count 1`. Use `--no-ai` para obter somente as métricas.
