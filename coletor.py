"""
Coletor de dados da API Dados de Mercado (https://www.dadosdemercado.com.br/api/docs).

- GET /tickers                  -> lista de ativos
- GET /tickers/:ticker/quotes   -> cotações diárias de um ativo

Os dados são gravados num SQLite local. A coleta de cotações é incremental:
na primeira vez baixa ~400 dias de histórico; depois só o que faltar.

Uso por linha de comando (ex.: num cron diário):
    DDM_TOKEN=seu_token python coletor.py
    DDM_TOKEN=seu_token python coletor.py --tipos stock fii --workers 4
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import pandas as pd
import requests

BASE_URL = "https://api.dadosdemercado.com.br/v1"
DB_PATH = os.getenv("DDM_DB_PATH", "dados_mercado.db")
HIST_DIAS = int(os.getenv("DDM_HIST_DIAS", "400"))  # ~ 1 ano de pregões + folga

ProgressCB = Callable[[int, int, str], None]


# --------------------------------------------------------------------------- #
# Banco de dados
# --------------------------------------------------------------------------- #
def conectar(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def criar_tabelas(db_path: str = DB_PATH) -> None:
    with conectar(db_path) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickers (
                ticker       TEXT PRIMARY KEY,
                type         TEXT,
                market_type  TEXT,
                market       TEXT,
                issuer_code  TEXT,
                currency     TEXT,
                isin         TEXT,
                atualizado_em TEXT
            );
            CREATE TABLE IF NOT EXISTS quotes (
                ticker    TEXT NOT NULL,
                date      TEXT NOT NULL,
                open      REAL,
                max       REAL,
                min       REAL,
                close     REAL,
                adj_close REAL,
                volume    INTEGER,
                PRIMARY KEY (ticker, date)
            );
            CREATE INDEX IF NOT EXISTS ix_quotes_date ON quotes(date);
            CREATE TABLE IF NOT EXISTS meta (
                chave TEXT PRIMARY KEY,
                valor TEXT
            );
            """
        )


def get_meta(chave: str, padrao: str | None = None, db_path: str = DB_PATH) -> str | None:
    criar_tabelas(db_path)
    with conectar(db_path) as con:
        row = con.execute("SELECT valor FROM meta WHERE chave=?", (chave,)).fetchone()
    return row[0] if row else padrao


def set_meta(chave: str, valor: str, db_path: str = DB_PATH) -> None:
    criar_tabelas(db_path)
    with conectar(db_path) as con:
        con.execute("INSERT OR REPLACE INTO meta(chave, valor) VALUES (?, ?)", (chave, valor))


# --------------------------------------------------------------------------- #
# Cliente HTTP
# --------------------------------------------------------------------------- #
class DDMClient:
    def __init__(self, token: str, timeout: int = 30, max_retries: int = 6):
        if not token:
            raise ValueError("Token da API Dados de Mercado não informado.")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self._local = threading.local()  # uma Session por thread

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(
                {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            )
            self._local.s = s
        return s

    @staticmethod
    def _normalizar(payload) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in ("data", "results", "items", "quotes", "tickers"):
                if isinstance(payload.get(k), list):
                    return payload[k]
            return [payload]
        return []

    def _get(self, path: str, params: dict | None = None) -> list[dict]:
        ultimo_erro = None
        for tentativa in range(self.max_retries):
            try:
                r = self._session().get(
                    BASE_URL + path, params=params, timeout=self.timeout
                )
            except requests.RequestException as e:
                ultimo_erro = e
                time.sleep(min(2**tentativa, 30))
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                espera = r.headers.get("Retry-After")
                try:
                    espera = float(espera) if espera else 2**tentativa
                except ValueError:
                    espera = 2**tentativa
                time.sleep(min(espera, 60))
                ultimo_erro = requests.HTTPError(f"HTTP {r.status_code}", response=r)
                continue
            if r.status_code == 404:
                return []
            if r.status_code in (401, 403):
                raise PermissionError(
                    f"Acesso negado pela API (HTTP {r.status_code}). Verifique o token/plano."
                )
            r.raise_for_status()
            return self._normalizar(r.json())
        raise RuntimeError(f"Falha ao consultar {path}: {ultimo_erro}")

    def tickers(self, ticker_type: str | None = None) -> list[dict]:
        params = {"ticker_type": ticker_type} if ticker_type else None
        return self._get("/tickers", params)

    def quotes(
        self, ticker: str, period_init: str | None = None, period_end: str | None = None
    ) -> list[dict]:
        params = {}
        if period_init:
            params["period_init"] = period_init
        if period_end:
            params["period_end"] = period_end
        return self._get(f"/tickers/{ticker}/quotes", params or None)


# --------------------------------------------------------------------------- #
# Coleta
# --------------------------------------------------------------------------- #
COLS_TICKER = ["ticker", "type", "market_type", "market", "issuer_code", "currency", "isin"]
COLS_QUOTE = ["date", "open", "max", "min", "close", "adj_close", "volume"]


def atualizar_lista_ativos(client: DDMClient, db_path: str = DB_PATH) -> int:
    criar_tabelas(db_path)
    dados = client.tickers()
    agora = dt.datetime.now().isoformat(timespec="seconds")
    linhas = [
        tuple(d.get(c) for c in COLS_TICKER) + (agora,)
        for d in dados
        if d.get("ticker")
    ]
    with conectar(db_path) as con:
        con.executemany(
            f"INSERT OR REPLACE INTO tickers({','.join(COLS_TICKER)}, atualizado_em) "
            f"VALUES ({','.join('?' * (len(COLS_TICKER) + 1))})",
            linhas,
        )
    set_meta("ultima_lista_ativos", agora, db_path)
    return len(linhas)


def _ultimas_datas(db_path: str) -> dict[str, str]:
    with conectar(db_path) as con:
        rows = con.execute("SELECT ticker, MAX(date) FROM quotes GROUP BY ticker").fetchall()
    return {t: d for t, d in rows}


def _baixar(client: DDMClient, ticker: str, inicio: str, fim: str) -> tuple[str, list[tuple]]:
    dados = client.quotes(ticker, period_init=inicio, period_end=fim)
    linhas = []
    for q in dados:
        data = q.get("date")
        if not data:
            continue
        linhas.append((ticker, str(data)[:10]) + tuple(q.get(c) for c in COLS_QUOTE[1:]))
    return ticker, linhas


def atualizar_cotacoes(
    client: DDMClient,
    tickers: Iterable[str],
    workers: int = 4,
    progress_cb: ProgressCB | None = None,
    db_path: str = DB_PATH,
) -> dict:
    criar_tabelas(db_path)
    hoje = dt.date.today()
    fim = hoje.isoformat()
    inicio_padrao = (hoje - dt.timedelta(days=HIST_DIAS)).isoformat()
    ultimas = _ultimas_datas(db_path)

    tarefas = []
    for t in tickers:
        ult = ultimas.get(t)
        if ult and ult >= fim:
            continue
        inicio = (
            (dt.date.fromisoformat(ult) + dt.timedelta(days=1)).isoformat()
            if ult
            else inicio_padrao
        )
        tarefas.append((t, inicio))

    total, feitos, novas, erros = len(tarefas), 0, 0, []
    if progress_cb:
        progress_cb(0, total, "Iniciando")

    con = conectar(db_path)
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(_baixar, client, t, ini, fim): t for t, ini in tarefas}
            for fut in as_completed(futs):
                t = futs[fut]
                feitos += 1
                try:
                    _, linhas = fut.result()
                    if linhas:
                        con.executemany(
                            "INSERT OR REPLACE INTO quotes(ticker, date, open, max, min, "
                            "close, adj_close, volume) VALUES (?,?,?,?,?,?,?,?)",
                            linhas,
                        )
                        novas += len(linhas)
                    if feitos % 25 == 0:
                        con.commit()
                except PermissionError:
                    raise
                except Exception as e:  # segue com os demais ativos
                    erros.append(f"{t}: {e}")
                if progress_cb:
                    progress_cb(feitos, total, t)
        con.commit()
    finally:
        con.close()

    set_meta("ultima_atualizacao", dt.datetime.now().isoformat(timespec="seconds"), db_path)
    return {"ativos_consultados": total, "linhas_gravadas": novas, "erros": erros}


def tickers_por_tipo(tipos: list[str] | None, db_path: str = DB_PATH) -> list[str]:
    criar_tabelas(db_path)
    with conectar(db_path) as con:
        if tipos:
            q = f"SELECT ticker FROM tickers WHERE type IN ({','.join('?' * len(tipos))})"
            rows = con.execute(q, tipos).fetchall()
        else:
            rows = con.execute("SELECT ticker FROM tickers").fetchall()
    return sorted(r[0] for r in rows)


def atualizar_tudo(
    token: str,
    tipos: list[str] | None = None,
    workers: int = 4,
    progress_cb: ProgressCB | None = None,
    db_path: str = DB_PATH,
) -> dict:
    client = DDMClient(token)
    n = atualizar_lista_ativos(client, db_path)
    alvo = tickers_por_tipo(tipos, db_path)
    res = atualizar_cotacoes(client, alvo, workers, progress_cb, db_path)
    res["ativos_na_lista"] = n
    return res


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Coleta dados da API Dados de Mercado")
    ap.add_argument("--tipos", nargs="*", help="Tipos de ativo (campo 'type'). Vazio = todos")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args()

    token = os.getenv("DDM_TOKEN")
    tipos = a.tipos or [t for t in (get_meta("tipos_coleta", "", a.db) or "").split(",") if t]

    def _p(f, t, nome):
        if t and (f % 50 == 0 or f == t):
            print(f"{f}/{t} ({nome})", flush=True)

    r = atualizar_tudo(token, tipos or None, a.workers, _p, a.db)
    print(
        f"Ativos na lista: {r['ativos_na_lista']} | consultados: {r['ativos_consultados']} | "
        f"linhas gravadas: {r['linhas_gravadas']} | erros: {len(r['erros'])}"
    )
    for e in r["erros"][:20]:
        print("  -", e)
