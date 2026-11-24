import csv
import io
import json
import math
import os
import random
import shutil
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TOKEN = os.getenv("TMDB_TOKEN")
PASTA = Path(__file__).resolve().parent / "dados"
ORIGINAL = PASTA / "filmes.csv"
PAISES_CSV = PASTA / "progresso_pais_ano.csv"
FINAL = PASTA / "filmes_ampliados.csv"
BANCO = PASTA / "progresso_ampliacao_v2.sqlite"
RELATORIO = PASTA / "resumo_ampliacao.csv"

URL = "https://api.themoviedb.org/3"
URL_POSTER = "https://image.tmdb.org/t/p/w500"
ANO_INICIAL = 1900
ANO_FINAL = 2026
LIMITE_FILMES = 30000
LIMITE_BYTES = 50_000_000
LIMIAR_ANUAL = 5000
TRABALHADORES = 8
REQUISICOES_POR_SEGUNDO = 25
MAX_PAISES_POR_ANO = 12
MULTIPLICADOR_COMPLETOS = 5
MULTIPLICADOR_INCOMPLETOS = 3
MAX_DETALHES_ANO_POR_ETAPA = 6000
MAX_PAGINAS_ANO_POR_ETAPA = 220
SEMENTE = 42

COLUNAS = [
    "id_tmdb", "titulo", "titulo_original", "data_lancamento", "ano",
    "generos", "genero_ids", "nota", "popularidade", "orcamento_usd",
    "bilheteria_usd", "duracao_min", "paises_producao", "codigos_paises",
    "idioma_original", "diretores", "elenco_principal", "sinopse", "poster_url"
]

ESSENCIAIS = [
    "id_tmdb", "titulo", "titulo_original", "data_lancamento", "ano",
    "generos", "genero_ids", "nota", "popularidade", "duracao_min",
    "paises_producao", "codigos_paises", "idioma_original", "poster_url"
]

_thread = threading.local()
_bloqueio = threading.Lock()
_proxima_requisicao = 0.0


def sessao_http():
    if not hasattr(_thread, "sessao"):
        sessao = requests.Session()
        sessao.headers.update({
            "Authorization": f"Bearer {TOKEN}",
            "accept": "application/json"
        })
        tentativas = Retry(
            total=6, connect=6, read=6, backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"], respect_retry_after_header=True
        )
        sessao.mount("https://", HTTPAdapter(max_retries=tentativas))
        _thread.sessao = sessao
    return _thread.sessao


def consultar(endpoint, params=None):
    global _proxima_requisicao
    with _bloqueio:
        agora = time.monotonic()
        instante = max(agora, _proxima_requisicao)
        _proxima_requisicao = instante + 1 / REQUISICOES_POR_SEGUNDO
    espera = instante - time.monotonic()
    if espera > 0:
        time.sleep(espera)
    resposta = sessao_http().get(
        f"{URL}/{endpoint}", params=params or {}, timeout=(10, 35)
    )
    resposta.raise_for_status()
    return resposta.json()


def descobrir(ano, pais=None, ordenacao="popularity.desc", pagina=1):
    parametros = {
        "primary_release_year": ano,
        "include_adult": "false",
        "include_video": "false",
        "sort_by": ordenacao,
        "page": pagina,
        "language": "pt-BR"
    }
    if pais:
        parametros["with_origin_country"] = pais
    return consultar("discover/movie", parametros)


def banco_dados():
    db = sqlite3.connect(BANCO)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS detalhes (
            id INTEGER PRIMARY KEY, ano INTEGER, qualidade INTEGER, dados TEXT
        );
        CREATE TABLE IF NOT EXISTS fontes (
            etapa INTEGER, ano INTEGER, pais TEXT, ordenacao TEXT,
            total_paginas INTEGER, primeira TEXT,
            PRIMARY KEY (etapa, ano, pais, ordenacao)
        );
        CREATE TABLE IF NOT EXISTS paginas_feitas (
            etapa INTEGER, ano INTEGER, pais TEXT, ordenacao TEXT, pagina INTEGER,
            PRIMARY KEY (etapa, ano, pais, ordenacao, pagina)
        );
        CREATE TABLE IF NOT EXISTS tentativas (
            etapa INTEGER, ano INTEGER, quantidade INTEGER,
            PRIMARY KEY (etapa, ano)
        );
        CREATE TABLE IF NOT EXISTS anos_finalizados (
            etapa INTEGER, ano INTEGER, PRIMARY KEY (etapa, ano)
        );
        CREATE TABLE IF NOT EXISTS metas (
            ano INTEGER PRIMARY KEY, total_tmdb INTEGER, meta INTEGER, inicial INTEGER
        );
    """)
    db.commit()
    return db


def preparar_filmes():
    df_original = pd.read_csv(ORIGINAL, usecols=["id_tmdb", "ano"])
    df_original = df_original.drop_duplicates("id_tmdb")
    if not FINAL.exists():
        if BANCO.exists() and BANCO.stat().st_size > 0:
            raise RuntimeError(
                "Arquivo final ausente, mas existe banco de progresso. "
                "Restaure filmes_ampliados.csv antes de continuar."
            )
        shutil.copy2(ORIGINAL, FINAL)
    with open(FINAL, encoding="utf-8-sig", newline="") as arquivo:
        cabecalho = next(csv.reader(arquivo))
    if cabecalho != COLUNAS:
        raise ValueError("As 19 colunas de filmes_ampliados.csv não correspondem à base.")
    df_atual = pd.read_csv(FINAL, usecols=["id_tmdb", "ano"])
    df_atual = df_atual.drop_duplicates("id_tmdb")
    ids = set(df_atual["id_tmdb"].dropna().astype(int))
    atuais = Counter(df_atual["ano"].dropna().astype(int))
    iniciais = Counter(df_original["ano"].dropna().astype(int))
    return ids, atuais, iniciais


def ler_paises():
    df = pd.read_csv(
        PAISES_CSV,
        dtype={"codigo_pais": str},
        keep_default_na=False
    )
    colunas = {"ano", "codigo_pais", "pais", "quantidade_total"}
    if not colunas.issubset(df.columns):
        raise ValueError("progresso_pais_ano.csv precisa das quatro colunas esperadas.")
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce")
    df["quantidade_total"] = pd.to_numeric(
        df["quantidade_total"], errors="coerce"
    ).fillna(0)
    df = df[
        df["ano"].between(ANO_INICIAL, ANO_FINAL)
        & df["codigo_pais"].str.fullmatch(r"[A-Z]{2}")
        & (df["quantidade_total"] > 0)
    ]
    df = df.groupby(["ano", "codigo_pais"], as_index=False)[
        "quantidade_total"
    ].max()
    resultado = {}
    for ano, grupo in df.groupby("ano"):
        resultado[int(ano)] = [
            (str(linha.codigo_pais), int(linha.quantidade_total))
            for linha in grupo.itertuples(index=False)
        ]
    return resultado


def fonte_cache(db, etapa, ano, pais, ordenacao):
    chave_pais = pais or "*"
    linha = db.execute(
        "SELECT total_paginas, primeira FROM fontes "
        "WHERE etapa=? AND ano=? AND pais=? AND ordenacao=?",
        (etapa, ano, chave_pais, ordenacao)
    ).fetchone()
    if linha:
        return int(linha[0]), json.loads(linha[1])
    dados = descobrir(ano, pais, ordenacao, 1)
    total = min(int(dados.get("total_pages", 0)), 500)
    db.execute(
        "INSERT OR REPLACE INTO fontes VALUES (?,?,?,?,?,?)",
        (etapa, ano, chave_pais, ordenacao, total,
         json.dumps(dados, ensure_ascii=False))
    )
    db.commit()
    return total, dados


def metas_anuais(db, iniciais, atuais):
    salvas = db.execute("SELECT ano, meta FROM metas ORDER BY ano").fetchall()
    if len(salvas) == ANO_FINAL - ANO_INICIAL + 1 and all(meta >= 0 for _, meta in salvas):
        return {int(ano): int(meta) for ano, meta in salvas}

    totais = {}
    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        dado = db.execute(
            "SELECT total_tmdb FROM metas WHERE ano=?", (ano,)
        ).fetchone()
        if dado:
            totais[ano] = int(dado[0])
        else:
            dados = descobrir(ano, ordenacao="popularity.desc")
            totais[ano] = int(dados.get("total_results", 0))
            db.execute(
                "INSERT OR REPLACE INTO metas VALUES (?,?,?,?)",
                (ano, totais[ano], -1, iniciais[ano])
            )
            db.execute(
                "INSERT OR IGNORE INTO fontes VALUES (?,?,?,?,?,?)",
                (2, ano, "*", "popularity.desc",
                 min(int(dados.get("total_pages", 0)), 500),
                 json.dumps(dados, ensure_ascii=False))
            )
            db.commit()
            print(f"\rContagens anuais únicas: {ano}/{ANO_FINAL}", end="", flush=True)
    print()

    desejadas = {
        ano: max(
            iniciais[ano],
            math.ceil(total * (0.10 if total <= LIMIAR_ANUAL else 0.05))
        )
        for ano, total in totais.items()
    }
    vagas = max(0, LIMITE_FILMES - sum(atuais.values()))
    pedidos = {
        ano: max(0, desejadas[ano] - atuais[ano])
        for ano in desejadas
    }
    if sum(pedidos.values()) > vagas and sum(pedidos.values()) > 0:
        soma = sum(pedidos.values())
        fracionarios = {ano: vagas * n / soma for ano, n in pedidos.items()}
        alocados = {ano: math.floor(n) for ano, n in fracionarios.items()}
        faltam = vagas - sum(alocados.values())
        ordem = sorted(
            fracionarios,
            key=lambda ano: (fracionarios[ano] - alocados[ano], -ano),
            reverse=True
        )
        for ano in ordem[:faltam]:
            alocados[ano] += 1
        metas = {ano: atuais[ano] + alocados[ano] for ano in pedidos}
    else:
        metas = {ano: atuais[ano] + pedidos[ano] for ano in pedidos}

    db.executemany(
        "UPDATE metas SET meta=? WHERE ano=?",
        [(meta, ano) for ano, meta in metas.items()]
    )
    db.commit()
    return metas


def selecionar_paises(ano, meta, paises_ano):
    candidatos = sorted(paises_ano, key=lambda x: -x[1])
    if not candidatos:
        return []
    quantidade = min(MAX_PAISES_POR_ANO, max(3, math.ceil(meta / 70)))
    primeiros = [c for c, _ in candidatos[:min(2, quantidade)]]
    restantes = [(c, n) for c, n in candidatos if c not in primeiros]
    rng = random.Random(SEMENTE + ano)
    while len(primeiros) < quantidade and restantes:
        escolha = rng.choices(
            range(len(restantes)),
            weights=[n for _, n in restantes],
            k=1
        )[0]
        primeiros.append(restantes.pop(escolha)[0])
    return primeiros


def fontes_ano(ano, meta, paises_ano, etapa):
    paises = selecionar_paises(ano, meta, paises_ano)
    if etapa == 1:
        return [(None, "revenue.desc")] + [
            (pais, "revenue.desc") for pais in paises
        ] + [(None, "vote_count.desc")]
    return [(None, "popularity.desc")] + [
        (pais, "popularity.desc") for pais in paises
    ] + [(None, "vote_count.desc")]


def nomes(itens):
    return ", ".join(dict.fromkeys(
        item["name"] for item in itens if item.get("name")
    )) or None


def converter(dados):
    data = dados.get("release_date") or ""
    try:
        ano = int(data[:4])
        if len(data) != 10 or not ANO_INICIAL <= ano <= ANO_FINAL:
            return None
    except (ValueError, TypeError):
        return None
    generos = dados.get("genres") or []
    paises = dados.get("production_countries") or []
    creditos = dados.get("credits") or {}
    diretores = [
        x for x in creditos.get("crew", [])
        if x.get("job") == "Director"
    ]
    elenco = sorted(
        creditos.get("cast", []),
        key=lambda x: x.get("order", 99999)
    )[:10]
    orcamento = int(dados.get("budget") or 0)
    receita = int(dados.get("revenue") or 0)
    duracao = int(dados.get("runtime") or 0)
    poster = dados.get("poster_path")
    return {
        "id_tmdb": int(dados["id"]),
        "titulo": dados.get("title") or None,
        "titulo_original": dados.get("original_title") or None,
        "data_lancamento": data,
        "ano": ano,
        "generos": nomes(generos),
        "genero_ids": ", ".join(str(g["id"]) for g in generos) or None,
        "nota": dados.get("vote_average") if int(dados.get("vote_count") or 0) > 0 else None,
        "popularidade": dados.get("popularity"),
        "orcamento_usd": orcamento if orcamento > 0 else None,
        "bilheteria_usd": receita if receita > 0 else None,
        "duracao_min": duracao if duracao > 0 else None,
        "paises_producao": nomes(paises),
        "codigos_paises": ", ".join(
            p["iso_3166_1"] for p in paises if p.get("iso_3166_1")
        ) or None,
        "idioma_original": dados.get("original_language") or None,
        "diretores": nomes(diretores),
        "elenco_principal": nomes(elenco),
        "sinopse": dados.get("overview") or None,
        "poster_url": URL_POSTER + poster if poster else None
    }


def preenchido(valor):
    return valor is not None and (not isinstance(valor, str) or bool(valor.strip()))


def classificar(registro):
    if registro is None:
        return -1
    if not all(preenchido(registro[c]) for c in ESSENCIAIS):
        return -1
    return 2 if all(preenchido(registro[c]) for c in COLUNAS) else 1


def detalhes(id_filme):
    try:
        dados = consultar(
            f"movie/{id_filme}",
            {"language": "pt-BR", "append_to_response": "credits"}
        )
        registro = converter(dados)
        return id_filme, classificar(registro), registro
    except requests.HTTPError as erro:
        if erro.response is not None and erro.response.status_code == 404:
            return id_filme, -1, None
        raise


def adicionar_filme(registro, ids, atuais):
    codigo = int(registro["id_tmdb"])
    if codigo in ids:
        return True
    if len(ids) >= LIMITE_FILMES:
        return False
    buffer = io.StringIO(newline="")
    csv.DictWriter(buffer, fieldnames=COLUNAS, lineterminator="\n").writerow(registro)
    conteudo = buffer.getvalue().encode("utf-8")
    with open(FINAL, "rb+") as arquivo:
        arquivo.seek(0, 2)
        tamanho = arquivo.tell()
        separador = b""
        if tamanho:
            arquivo.seek(-1, 2)
            if arquivo.read(1) not in (b"\r", b"\n"):
                separador = b"\n"
        if tamanho + len(separador) + len(conteudo) > LIMITE_BYTES:
            return False
        arquivo.seek(0, 2)
        arquivo.write(separador + conteudo)
    ids.add(codigo)
    atuais[int(registro["ano"])] += 1
    return True


def aproveitar_cache(db, ano, etapa, meta, ids, atuais):
    linhas = db.execute(
        "SELECT id, qualidade, dados FROM detalhes "
        "WHERE ano=? AND qualidade>=? ORDER BY qualidade DESC, id",
        (ano, 2 if etapa == 1 else 1)
    ).fetchall()
    for codigo, qualidade, texto in linhas:
        if atuais[ano] >= meta:
            break
        if codigo in ids:
            continue
        if not adicionar_filme(json.loads(texto), ids, atuais):
            return False
    return True


def processar_ano(db, executor, etapa, ano, meta, paises_ano, ids, atuais):
    if atuais[ano] >= meta:
        return True
    if not aproveitar_cache(db, ano, etapa, meta, ids, atuais):
        return False
    if atuais[ano] >= meta:
        return True

    inicial = db.execute(
        "SELECT inicial FROM metas WHERE ano=?", (ano,)
    ).fetchone()[0]
    faltam = max(1, meta - inicial)
    multiplicador = (
        MULTIPLICADOR_COMPLETOS if etapa == 1 else MULTIPLICADOR_INCOMPLETOS
    )
    limite_detalhes = min(
        MAX_DETALHES_ANO_POR_ETAPA,
        max(80, faltam * multiplicador)
    )
    limite_paginas = min(
        MAX_PAGINAS_ANO_POR_ETAPA,
        max(8, math.ceil(limite_detalhes / 15))
    )
    ja_tentados = db.execute(
        "SELECT quantidade FROM tentativas WHERE etapa=? AND ano=?",
        (etapa, ano)
    ).fetchone()
    usados = int(ja_tentados[0]) if ja_tentados else 0
    paginas_feitas = db.execute(
        "SELECT COUNT(*) FROM paginas_feitas WHERE etapa=? AND ano=?",
        (etapa, ano)
    ).fetchone()[0]
    fontes = fontes_ano(ano, meta, paises_ano, etapa)
    vistos = set()

    for pagina in range(1, 501):
        for pais, ordenacao in fontes:
            if atuais[ano] >= meta:
                return True
            if usados >= limite_detalhes or paginas_feitas >= limite_paginas:
                return True
            if len(ids) >= LIMITE_FILMES or FINAL.stat().st_size >= LIMITE_BYTES:
                return False

            codigo_pais = pais or "*"
            if db.execute(
                "SELECT 1 FROM paginas_feitas "
                "WHERE etapa=? AND ano=? AND pais=? AND ordenacao=? AND pagina=?",
                (etapa, ano, codigo_pais, ordenacao, pagina)
            ).fetchone():
                continue

            total_paginas, primeira = fonte_cache(
                db, etapa, ano, pais, ordenacao
            )
            if pagina > total_paginas:
                continue
            dados = primeira if pagina == 1 else descobrir(
                ano, pais, ordenacao, pagina
            )
            novas = []
            for filme in dados.get("results", []):
                codigo = int(filme["id"])
                if codigo in ids or codigo in vistos:
                    continue
                vistos.add(codigo)
                if not filme.get("genre_ids") or not filme.get("poster_path"):
                    continue
                if int(filme.get("vote_count") or 0) < 1:
                    continue
                linha = db.execute(
                    "SELECT qualidade, ano, dados FROM detalhes WHERE id=?",
                    (codigo,)
                ).fetchone()
                if linha:
                    qualidade, ano_registro, texto = linha
                    if (
                        texto and ano_registro == ano
                        and qualidade >= (2 if etapa == 1 else 1)
                        and atuais[ano] < meta
                    ):
                        if not adicionar_filme(json.loads(texto), ids, atuais):
                            return False
                    continue
                if usados + len(novas) < limite_detalhes:
                    novas.append(codigo)

            futuros = [executor.submit(detalhes, codigo) for codigo in novas]
            for futuro in as_completed(futuros):
                codigo, qualidade, registro = futuro.result()
                db.execute(
                    "INSERT OR REPLACE INTO detalhes VALUES (?,?,?,?)",
                    (codigo, registro["ano"] if registro else None, qualidade,
                     json.dumps(registro, ensure_ascii=False) if registro else None)
                )
                usados += 1
                if (
                    registro and registro["ano"] == ano
                    and qualidade >= (2 if etapa == 1 else 1)
                    and atuais[ano] < meta
                ):
                    if not adicionar_filme(registro, ids, atuais):
                        db.commit()
                        return False

            db.execute(
                "INSERT OR REPLACE INTO tentativas VALUES (?,?,?)",
                (etapa, ano, usados)
            )
            db.execute(
                "INSERT OR IGNORE INTO paginas_feitas VALUES (?,?,?,?,?)",
                (etapa, ano, codigo_pais, ordenacao, pagina)
            )
            db.commit()
            paginas_feitas += 1
            print(
                f"\rEtapa {etapa} | {ano}: {atuais[ano]}/{meta} | "
                f"detalhes consultados: {usados}/{limite_detalhes} | "
                f"filmes: {len(ids):,} | {FINAL.stat().st_size/1_000_000:.2f} MB",
                end="", flush=True
            )
    return True


def main():
    PASTA.mkdir(parents=True, exist_ok=True)
    if not TOKEN:
        raise ValueError("TMDB_TOKEN ausente no .env")
    if not ORIGINAL.exists():
        raise FileNotFoundError(ORIGINAL)
    if not PAISES_CSV.exists():
        raise FileNotFoundError(PAISES_CSV)
    banco_existia = BANCO.exists()
    if not FINAL.exists() and banco_existia:
        raise RuntimeError("O banco existe, mas filmes_ampliados.csv não. Restaure o CSV.")
    ids, atuais, iniciais = preparar_filmes()
    db = banco_dados()
    try:
        print(f"Filmes existentes: {len(ids):,} | {FINAL.stat().st_size/1_000_000:.2f} MB")
        if len(ids) >= LIMITE_FILMES or FINAL.stat().st_size >= LIMITE_BYTES:
            print("Limite de filmes ou de tamanho já alcançado.")
            return
        paises = ler_paises()
        metas = metas_anuais(db, iniciais, atuais)
        planejados = sum(max(0, metas[ano] - atuais[ano]) for ano in metas)
        print(f"Faltam para as metas: {planejados:,} filmes")
        with ThreadPoolExecutor(max_workers=TRABALHADORES) as executor:
            for etapa in (1, 2):
                print(f"\nETAPA {etapa}: {'FILMES COMPLETOS' if etapa == 1 else 'COMPLEMENTO'}")
                for ano in range(ANO_INICIAL, ANO_FINAL + 1):
                    if atuais[ano] >= metas[ano]:
                        continue
                    if db.execute(
                        "SELECT 1 FROM anos_finalizados WHERE etapa=? AND ano=?",
                        (etapa, ano)
                    ).fetchone():
                        continue
                    ok = processar_ano(
                        db, executor, etapa, ano, metas[ano],
                        paises.get(ano, []), ids, atuais
                    )
                    if not ok:
                        print("\nLimite do CSV ou de filmes alcançado.")
                        return
                    db.execute(
                        "INSERT OR IGNORE INTO anos_finalizados VALUES (?,?)",
                        (etapa, ano)
                    )
                    db.commit()
                    print(f"\nAno {ano}: {atuais[ano]}/{metas[ano]}")
                print(f"Etapa {etapa} concluída.")
    except KeyboardInterrupt:
        print("\nInterrompido. Execute novamente para retomar.")
    finally:
        db.commit()
        linhas_metas = db.execute(
            "SELECT ano, total_tmdb, meta, inicial FROM metas ORDER BY ano"
        ).fetchall()
        if linhas_metas and all(meta >= 0 for _, _, meta, _ in linhas_metas):
            relatorio = pd.DataFrame([
                {
                    "ano": ano,
                    "total_tmdb": total,
                    "meta": meta,
                    "filmes_existentes_antes": inicial,
                    "filmes_atuais": atuais[ano],
                    "faltantes_para_meta": max(0, meta - atuais[ano])
                }
                for ano, total, meta, inicial in linhas_metas
            ])
            relatorio.to_csv(RELATORIO, index=False, encoding="utf-8-sig")
            print(f"Metas anuais atingidas: {(relatorio['faltantes_para_meta'] == 0).sum()}/{len(relatorio)}")
            print(f"Ainda faltantes para as metas: {relatorio['faltantes_para_meta'].sum():,}")
            print(f"Relatório: {RELATORIO}")
        db.close()
        print(f"\nTotal final: {len(ids):,} filmes")
        print(f"Tamanho final: {FINAL.stat().st_size / 1_000_000:.2f} MB")
        print(f"Arquivo: {FINAL}")


if __name__ == "__main__":
    main()
