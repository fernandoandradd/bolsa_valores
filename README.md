# Painel de Ativos B3 — API Dados de Mercado

App Streamlit que lista todos os ativos da API Dados de Mercado e mostra os mais baratos (em relação ao próprio histórico) e os que mais desvalorizaram.

## Arquivos
- `app.py` — painel Streamlit
- `coletor.py` — coleta da API e gravação em SQLite (`dados_mercado.db`); também roda sozinho por linha de comando
- `requirements.txt`

## Rodar localmente
```bash
pip install -r requirements.txt
mkdir .streamlit
echo 'DDM_TOKEN = "seu_token"' > .streamlit/secrets.toml
streamlit run app.py
```
Na barra lateral: escolha os tipos de ativo e clique em **Atualizar agora**. A primeira carga baixa ~400 dias de histórico por ativo (uma chamada por ativo); as seguintes são incrementais.

## Deixar no ar (VPS Linux)
```bash
pip install -r requirements.txt
export DDM_TOKEN=seu_token
nohup streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true &
```
Para iniciar com o servidor, use um serviço systemd:
```ini
# /etc/systemd/system/painel-b3.service
[Unit]
Description=Painel B3
After=network.target

[Service]
WorkingDirectory=/opt/painel-b3
Environment=DDM_TOKEN=seu_token
ExecStart=/usr/bin/python3 -m streamlit run app.py --server.port 8501 --server.headless true
Restart=always

[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now painel-b3`

### Atualização diária
- **Automática no app** (padrão): em dias úteis, após 19h (Brasília), o próprio app dispara a coleta. Configure em *Atualização automática* na barra lateral.
- **Alternativa via cron** (independe do app estar aberto):
  `30 19 * * 1-5 cd /opt/painel-b3 && DDM_TOKEN=seu_token python3 coletor.py >> coleta.log 2>&1`

### Streamlit Community Cloud
Funciona, mas o disco é efêmero: o SQLite é perdido quando o app reinicia/hiberna e a coleta recomeça do zero. Para uso contínuo, prefira uma VPS.

## Critérios do painel
- **Mais baratos**: % abaixo da máxima de 52 semanas, proximidade da mínima de 52 semanas, ou menor preço nominal.
- **Maiores quedas**: variação em 1D, 5D, 1M, 3M, 6M, 12M ou no ano, sobre o preço ajustado.
- **Filtro de liquidez**: volume financeiro médio de 21 pregões — evita ativos sem negociação distorcendo os rankings.

“Barato” aqui é técnico (preço vs. histórico), não fundamentalista.
