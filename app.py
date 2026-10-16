"""
Painel de ativos da B3 — API Dados de Mercado
Executar:  streamlit run app.py
Token: .streamlit/secrets.toml (DDM_TOKEN = "...") ou variável de ambiente DDM_TOKEN.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import coletor

TZ = ZoneInfo("America/Sao_Paulo")
DB = coletor.DB_PATH

# janelas em pregões
PERIODOS = {"1D": 1, "5D": 5, "1M": 21, "3M": 63, "6M": 126, "12M": 252}

st.set_page_config(page_title="Painel de Ativos B3", page_icon="📉", layout="wide")


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #
def obter_token() -> str | None:
    try:
        tk = st.secrets.get("DDM_TOKEN")
    except Exception:
        tk = None
    return tk or os.getenv("DDM_TOKEN")


# --------------------------------------------------------------------------- #
# Atualizador em segundo plano (um por processo, sobrevive ao fechamento da aba)
# --------------------------------------------------------------------------- #
class Atualizador:
    def __init__(self):
        self.lock = threading.Lock()
        self.rodando = False
        self.feitos = 0
        self.total = 0
        self.atual = ""
        self.resultado: dict | None = None
        self.erro: str | None = None
        self.inicio: dt.datetime | None = None
        threading.Thread(target=self._agendador, daemon=True).start()

    def _prog(self, f, t, nome):
        self.feitos, self.total, self.atual = f, t, nome

    def iniciar(self, token: str, tipos: list[str] | None, workers: int) -> bool:
        with self.lock:
            if self.rodando:
                return False
            self.rodando, self.erro, self.resultado = True, None, None
            self.feitos = self.total = 0
            self.inicio = dt.datetime.now(TZ)
        threading.Thread(target=self._run, args=(token, tipos, workers), daemon=True).start()
        return True

    def _run(self, token, tipos, workers):
        try:
            self.resultado = coletor.atualizar_tudo(token, tipos, workers, self._prog, DB)
        except Exception as e:
            self.erro = str(e)
        finally:
            self.rodando = False

    def _agendador(self):
        """Atualização automática diária em dias úteis, após o horário configurado."""
        while True:
            try:
                if coletor.get_meta("auto_ativo", "1", DB) == "1":
                    agora = dt.datetime.now(TZ)
                    hora = int(coletor.get_meta("auto_hora", "19", DB))
                    hoje = agora.date().isoformat()
                    if (
                        agora.weekday() < 5
                        and agora.hour >= hora
                        and coletor.get_meta("ultima_auto", "", DB) != hoje
                        and not self.rodando
                    ):
                        token = obter_token()
                        if token:
                            tipos = [
                                t
                                for t in (coletor.get_meta("tipos_coleta", "", DB) or "").split(",")
                                if t
                            ]
                            workers = int(coletor.get_meta("workers", "4", DB))
                            coletor.set_meta("ultima_auto", hoje, DB)
                            self.iniciar(token, tipos or None, workers)
            except Exception:
                pass
            time.sleep(600)


@st.cache_resource
def atualizador() -> Atualizador:
    coletor.criar_tabelas(DB)
    return Atualizador()


# --------------------------------------------------------------------------- #
# Carga e cálculo
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def carregar_tickers(_versao: str) -> pd.DataFrame:
    with coletor.conectar(DB) as con:
        return pd.read_sql("SELECT * FROM tickers", con)


@st.cache_data(show_spinner="Calculando indicadores…")
def carregar_metricas(_versao: str) -> pd.DataFrame:
    corte = (dt.date.today() - dt.timedelta(days=coletor.HIST_DIAS + 10)).isoformat()
    with coletor.conectar(DB) as con:
        q = pd.read_sql(
            "SELECT ticker, date, close, adj_close, max, min, volume FROM quotes WHERE date >= ?",
            con,
            params=(corte,),
        )
    if q.empty:
        return pd.DataFrame()

    q["date"] = pd.to_datetime(q["date"])
    q = q.dropna(subset=["close"]).sort_values(["ticker", "date"])
    q["adj_close"] = q["adj_close"].fillna(q["close"])
    q["vol_fin"] = q["close"] * q["volume"].fillna(0)
    g = q.groupby("ticker", sort=False)

    for nome, n in PERIODOS.items():
        q[f"var_{nome}"] = (q["adj_close"] / g["adj_close"].shift(n) - 1) * 100

    q["max_52s"] = g["adj_close"].transform(lambda s: s.rolling(252, min_periods=1).max())
    q["min_52s"] = g["adj_close"].transform(lambda s: s.rolling(252, min_periods=1).min())
    q["vol_fin_21d"] = g["vol_fin"].transform(lambda s: s.rolling(21, min_periods=1).mean())
    q["pregoes"] = g.cumcount() + 1

    ult = g.tail(1).set_index("ticker")

    # Variação no ano (YTD): último fechamento do ano anterior
    ini_ano = pd.Timestamp(dt.date.today().year, 1, 1)
    base = q[q["date"] < ini_ano].groupby("ticker")["adj_close"].last()
    ult["var_Ano"] = (ult["adj_close"] / base.reindex(ult.index) - 1) * 100

    ult["dist_max_52s"] = (ult["adj_close"] / ult["max_52s"] - 1) * 100
    ult["dist_min_52s"] = (ult["adj_close"] / ult["min_52s"] - 1) * 100

    ult = ult.rename(columns={"date": "ultima_data", "close": "preco"})
    cols = (
        ["ultima_data", "preco"]
        + [f"var_{p}" for p in list(PERIODOS) + ["Ano"]]
        + ["dist_max_52s", "dist_min_52s", "vol_fin_21d", "pregoes"]
    )
    return ult[cols].reset_index()


def col_cfg() -> dict:
    pct = lambda r: st.column_config.NumberColumn(r, format="%.2f%%")
    cfg = {
        "ticker": st.column_config.TextColumn("Ativo"),
        "type": st.column_config.TextColumn("Tipo"),
        "issuer_code": st.column_config.TextColumn("Emissor"),
        "preco": st.column_config.NumberColumn("Preço (R$)", format="%.2f"),
        "ultima_data": st.column_config.DateColumn("Último pregão", format="DD/MM/YYYY"),
        "dist_max_52s": pct("Abaixo da máx. 52s"),
        "dist_min_52s": pct("Acima da mín. 52s"),
        "vol_fin_21d": st.column_config.NumberColumn("Vol. fin. médio 21d (R$)", format="%,.0f"),
        "pregoes": st.column_config.NumberColumn("Pregões na base"),
    }
    for p in list(PERIODOS) + ["Ano"]:
        cfg[f"var_{p}"] = pct(f"Var. {p}")
    return cfg


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
upd = atualizador()
token = obter_token()
st.session_state.setdefault("_versao_vista", coletor.get_meta("ultima_atualizacao", None, DB))

with st.sidebar:
    st.header("⚙️ Dados")
    if not token:
        token = st.text_input("Token da API Dados de Mercado", type="password")
        st.caption("Para não digitar sempre, defina DDM_TOKEN em .streamlit/secrets.toml.")

    tickers_df = carregar_tickers(coletor.get_meta("ultima_lista_ativos", "0", DB) or "0")
    tipos_disp = sorted(tickers_df["type"].dropna().unique()) if not tickers_df.empty else []
    salvos = [t for t in (coletor.get_meta("tipos_coleta", "", DB) or "").split(",") if t]

    if tipos_disp:
        tipos_sel = st.multiselect(
            "Tipos de ativo a coletar cotações",
            tipos_disp,
            default=[t for t in salvos if t in tipos_disp],
            help="Vazio = todos. Cada ativo é uma chamada à API; restrinja para coletas mais rápidas.",
        )
        n_alvo = (tickers_df["type"].isin(tipos_sel)).sum() if tipos_sel else len(tickers_df)
        st.caption(f"{n_alvo:,} ativos serão consultados.".replace(",", "."))
    else:
        tipos_sel = []
        st.caption("Clique em **Atualizar agora** para baixar a lista de ativos.")

    workers = st.slider("Requisições simultâneas", 1, 10, int(coletor.get_meta("workers", "4", DB)))

    if st.button("🔄 Atualizar agora", type="primary", width="stretch", disabled=not token):
        coletor.set_meta("tipos_coleta", ",".join(tipos_sel), DB)
        coletor.set_meta("workers", str(workers), DB)
        if not upd.iniciar(token, tipos_sel or None, workers):
            st.info("Já existe uma atualização em andamento.")

    with st.expander("Atualização automática"):
        auto = st.toggle("Ativa (dias úteis)", coletor.get_meta("auto_ativo", "1", DB) == "1")
        hora = st.number_input("A partir de (hora, Brasília)", 0, 23, int(coletor.get_meta("auto_hora", "19", DB)))
        if st.button("Salvar", width="stretch"):
            coletor.set_meta("auto_ativo", "1" if auto else "0", DB)
            coletor.set_meta("auto_hora", str(hora), DB)
            coletor.set_meta("tipos_coleta", ",".join(tipos_sel), DB)
            coletor.set_meta("workers", str(workers), DB)
            st.success("Configuração salva.")
        st.caption("Requer o token em secrets/variável de ambiente e o app em execução.")

    @st.fragment(run_every=2)
    def status():
        if upd.rodando:
            t = max(upd.total, 1)
            st.progress(upd.feitos / t, text=f"Coletando {upd.feitos}/{upd.total} — {upd.atual}")
        else:
            if upd.erro:
                st.error(upd.erro)
            elif upd.resultado:
                r = upd.resultado
                st.success(
                    f"Concluído: {r['ativos_consultados']} ativos, {r['linhas_gravadas']} cotações novas."
                )
                if r["erros"]:
                    with st.expander(f"{len(r['erros'])} erros"):
                        st.write(r["erros"][:200])
            ult = coletor.get_meta("ultima_atualizacao", None, DB)
            if ult:
                st.caption(f"Última atualização: {dt.datetime.fromisoformat(ult):%d/%m/%Y %H:%M}")
            if st.session_state.get("_versao_vista") != ult:
                st.session_state["_versao_vista"] = ult
                st.rerun()  # recarrega o painel quando chegam dados novos

    status()

# ---------------------------------------------------------------- conteúdo
st.title("📉 Painel de Ativos B3")

versao = coletor.get_meta("ultima_atualizacao", "0", DB) or "0"
met = carregar_metricas(versao)

if tickers_df.empty:
    st.info("Nenhum dado ainda. Informe o token e clique em **Atualizar agora** na barra lateral.")
    st.stop()

base = tickers_df.merge(met, on="ticker", how="left") if not met.empty else tickers_df.copy()

# filtros globais
c1, c2, c3 = st.columns([2, 2, 1])
with c1:
    f_tipos = st.multiselect("Filtrar tipos", tipos_disp, default=[t for t in salvos if t in tipos_disp])
with c2:
    liq_opts = {"Sem filtro": 0, "R$ 100 mil": 1e5, "R$ 500 mil": 5e5, "R$ 1 mi": 1e6, "R$ 5 mi": 5e6, "R$ 20 mi": 2e7}
    liq = liq_opts[
        st.select_slider("Liquidez mínima (vol. financeiro médio diário)", list(liq_opts), value="R$ 500 mil")
    ]
with c3:
    top_n = st.number_input("Top N", 5, 200, 20, step=5)

f = base.copy()
if f_tipos:
    f = f[f["type"].isin(f_tipos)]

if not met.empty:
    data_ref = met["ultima_data"].max()
    negociados = f[
        f["preco"].notna()
        & (f["ultima_data"] >= data_ref - pd.Timedelta(days=7))
        & (f["vol_fin_21d"].fillna(0) >= liq)
    ]
else:
    negociados = f.iloc[0:0]

aba_lista, aba_baratos, aba_quedas, aba_detalhe = st.tabs(
    ["📋 Todos os ativos", "💰 Mais baratos", "📉 Maiores quedas", "🔎 Detalhe do ativo"]
)

# ---------------------------------------------------------------- lista
with aba_lista:
    busca = st.text_input("Buscar ticker / emissor / ISIN").strip().upper()
    lst = f
    if busca:
        m = pd.Series(False, index=lst.index)
        for c in ("ticker", "issuer_code", "isin"):
            m |= lst[c].fillna("").str.upper().str.contains(busca, regex=False)
        lst = lst[m]
    cols = [c for c in ["ticker", "type", "market_type", "issuer_code", "isin", "currency",
                        "preco", "ultima_data", "var_1D", "var_1M", "var_12M", "vol_fin_21d"] if c in lst]
    st.caption(f"{len(lst):,} ativos".replace(",", "."))
    st.dataframe(lst[cols].sort_values("ticker"), column_config=col_cfg(), hide_index=True, width="stretch", height=600)
    st.download_button("⬇️ Baixar CSV", lst[cols].to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       "ativos.csv", "text/csv")

# ---------------------------------------------------------------- baratos
with aba_baratos:
    if negociados.empty:
        st.info("Sem cotações suficientes. Rode uma atualização ou reduza o filtro de liquidez.")
    else:
        criterio = st.radio(
            "Critério",
            ["Mais abaixo da máxima de 52 semanas", "Mais perto da mínima de 52 semanas", "Menor preço nominal"],
            horizontal=True,
        )
        min_preg = st.checkbox("Exigir ao menos 200 pregões de histórico", value=True,
                               help="Evita distorções em ativos recém-listados.")
        d = negociados[negociados["pregoes"] >= 200] if min_preg and criterio != "Menor preço nominal" else negociados

        if criterio.startswith("Mais abaixo"):
            d = d.sort_values("dist_max_52s")
            chave = "dist_max_52s"
        elif criterio.startswith("Mais perto"):
            d = d.sort_values("dist_min_52s")
            chave = "dist_min_52s"
        else:
            d = d.sort_values("preco")
            chave = "preco"
        d = d.head(int(top_n))

        st.dataframe(
            d[["ticker", "type", "issuer_code", "preco", chave if chave != "preco" else "dist_max_52s",
               "var_1M", "var_12M", "vol_fin_21d"]],
            column_config=col_cfg(), hide_index=True, width="stretch",
        )
        if chave != "preco":
            st.bar_chart(d.set_index("ticker")[chave], horizontal=True, height=max(300, 22 * len(d)))
        st.caption("“Barato” aqui é preço em relação ao próprio histórico — não considera fundamentos (P/L, P/VP etc.).")

# ---------------------------------------------------------------- quedas
with aba_quedas:
    if negociados.empty:
        st.info("Sem cotações suficientes. Rode uma atualização ou reduza o filtro de liquidez.")
    else:
        per = st.segmented_control("Período", list(PERIODOS) + ["Ano"], default="1M") or "1M"
        col = f"var_{per}"
        d = negociados.dropna(subset=[col]).sort_values(col).head(int(top_n))
        k1, k2, k3 = st.columns(3)
        k1.metric("Maior queda", f"{d[col].iloc[0]:.2f}%", d["ticker"].iloc[0]) if len(d) else None
        k2.metric("Mediana do universo", f"{negociados[col].median():.2f}%")
        k3.metric("Ativos em queda", f"{(negociados[col] < 0).mean() * 100:.0f}%")
        st.dataframe(
            d[["ticker", "type", "issuer_code", "preco", col, "dist_max_52s", "vol_fin_21d"]],
            column_config=col_cfg(), hide_index=True, width="stretch",
        )
        st.bar_chart(d.set_index("ticker")[col], horizontal=True, height=max(300, 22 * len(d)), color="#d9534f")
        st.caption("Variações calculadas sobre o preço ajustado (proventos e eventos societários).")

# ---------------------------------------------------------------- detalhe
with aba_detalhe:
    opcoes = sorted(negociados["ticker"]) if not negociados.empty else sorted(f["ticker"])
    tk = st.selectbox("Ativo", opcoes, index=None, placeholder="Escolha um ticker")
    if tk:
        with coletor.conectar(DB) as con:
            h = pd.read_sql("SELECT date, close, adj_close, volume FROM quotes WHERE ticker=? ORDER BY date",
                            con, params=(tk,))
        if h.empty:
            st.info("Sem cotações para este ativo. Inclua o tipo dele na coleta e atualize.")
        else:
            h["date"] = pd.to_datetime(h["date"])
            linha = base[base["ticker"] == tk].iloc[0]
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Preço", f"R$ {linha['preco']:.2f}", f"{linha['var_1D']:.2f}%" if pd.notna(linha.get("var_1D")) else None)
            m2.metric("Var. 12M", f"{linha['var_12M']:.2f}%" if pd.notna(linha.get("var_12M")) else "—")
            m3.metric("Abaixo da máx. 52s", f"{linha['dist_max_52s']:.2f}%" if pd.notna(linha.get("dist_max_52s")) else "—")
            m4.metric("Vol. fin. médio 21d", f"R$ {linha['vol_fin_21d']:,.0f}".replace(",", ".") if pd.notna(linha.get("vol_fin_21d")) else "—")
            st.line_chart(h.set_index("date")[["close", "adj_close"]].rename(
                columns={"close": "Fechamento", "adj_close": "Fechamento ajustado"}))
