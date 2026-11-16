
import os
import time
import requests
import pandas as pd

from pathlib import Path
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TMDB_TOKEN = os.getenv("TMDB_TOKEN")

URL_BASE = "https://api.themoviedb.org/3"

INTERVALO = 0.01
MAX_PAGINAS = 500

PASTA = Path(__file__).resolve().parent / "dados"
PASTA.mkdir(parents=True, exist_ok=True)

ARQUIVO_BASE = PASTA / "estatisticas_filmes.csv"
ARQUIVO_FINAL = PASTA / "estatisticas_completas.csv"
ARQUIVO_CACHE = PASTA / "cache_detalhes_filmes.csv"

if not TMDB_TOKEN:
    raise ValueError("TMDB_TOKEN não encontrado no arquivo .env")

if not ARQUIVO_BASE.exists():
    raise FileNotFoundError(
        f"Dataset de referência não encontrado: {ARQUIVO_BASE}"
    )

sessao = requests.Session()

sessao.headers.update({
    "Authorization": f"Bearer {TMDB_TOKEN}",
    "accept": "application/json"
})

retries = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=True
)

sessao.mount(
    "https://",
    HTTPAdapter(max_retries=retries)
)


def consultar(endpoint, parametros=None):
    resposta = sessao.get(
        f"{URL_BASE}/{endpoint}",
        params=parametros or {},
        timeout=(10, 30)
    )

    resposta.raise_for_status()

    time.sleep(INTERVALO)

    return resposta.json()


def carregar_cache():
    if not ARQUIVO_CACHE.exists() or ARQUIVO_CACHE.stat().st_size == 0:
        return {}

    df = pd.read_csv(ARQUIVO_CACHE)

    cache = {}

    for linha in df.itertuples(index=False):
        cache[int(linha.id_filme)] = {
            "orcamento": int(linha.orcamento or 0),
            "bilheteria": int(linha.bilheteria or 0)
        }

    return cache


def carregar_concluidos():
    if not ARQUIVO_FINAL.exists() or ARQUIVO_FINAL.stat().st_size == 0:
        return set()

    df = pd.read_csv(ARQUIVO_FINAL)

    return {
        (
            int(linha.ano),
            str(linha.codigo_pais),
            int(linha.id_genero)
        )
        for linha in df.itertuples(index=False)
    }


def salvar_registro(registro, arquivo):
    df = pd.DataFrame([registro])

    df.to_csv(
        arquivo,
        mode="a",
        index=False,
        header=not arquivo.exists() or arquivo.stat().st_size == 0,
        encoding="utf-8-sig"
    )


def obter_detalhes_financeiros(id_filme, cache):
    if id_filme in cache:
        return cache[id_filme]

    dados = consultar(f"movie/{id_filme}")

    detalhes = {
        "orcamento": int(dados.get("budget") or 0),
        "bilheteria": int(dados.get("revenue") or 0)
    }

    salvar_registro(
        {
            "id_filme": id_filme,
            "orcamento": detalhes["orcamento"],
            "bilheteria": detalhes["bilheteria"]
        },
        ARQUIVO_CACHE
    )

    cache[id_filme] = detalhes

    return detalhes


def obter_filmes(ano, codigo_pais, id_genero):
    parametros = {
        "primary_release_year": ano,
        "with_origin_country": codigo_pais,
        "with_genres": id_genero,
        "page": 1
    }

    primeira_pagina = consultar(
        "discover/movie",
        parametros
    )

    total_resultados = primeira_pagina.get("total_results", 0)
    total_paginas = primeira_pagina.get("total_pages", 0)

    paginas = min(total_paginas, MAX_PAGINAS)

    filmes = {}

    for filme in primeira_pagina.get("results", []):
        filmes[int(filme["id"])] = filme

    for pagina in range(2, paginas + 1):
        parametros["page"] = pagina

        dados = consultar(
            "discover/movie",
            parametros
        )

        for filme in dados.get("results", []):
            filmes[int(filme["id"])] = filme

        if pagina % 50 == 0:
            print(f"    Página {pagina}/{paginas}")

    incompleta = (
        total_resultados > 10000
        or len(filmes) < total_resultados
    )

    return list(filmes.values()), total_resultados, incompleta


def calcular_estatisticas(filmes, cache):
    soma_notas = 0.0
    quantidade_com_nota = 0

    soma_popularidade = 0.0

    soma_orcamento = 0
    soma_bilheteria = 0

    quantidade_com_orcamento = 0
    quantidade_com_bilheteria = 0

    for filme in filmes:
        nota = float(filme.get("vote_average") or 0)
        votos = int(filme.get("vote_count") or 0)

        popularidade = float(filme.get("popularity") or 0)

        if votos > 0:
            soma_notas += nota
            quantidade_com_nota += 1

        soma_popularidade += popularidade

        detalhes = obter_detalhes_financeiros(
            int(filme["id"]),
            cache
        )

        orcamento = detalhes["orcamento"]
        bilheteria = detalhes["bilheteria"]

        soma_orcamento += orcamento
        soma_bilheteria += bilheteria

        if orcamento > 0:
            quantidade_com_orcamento += 1

        if bilheteria > 0:
            quantidade_com_bilheteria += 1

    nota_media = (
        soma_notas / quantidade_com_nota
        if quantidade_com_nota > 0
        else 0
    )

    return {
        "nota_media": round(nota_media, 4),
        "quantidade_com_nota": quantidade_com_nota,
        "popularidade_total": round(soma_popularidade, 4),
        "orcamento_total": soma_orcamento,
        "bilheteria_total": soma_bilheteria,
        "quantidade_com_orcamento": quantidade_com_orcamento,
        "quantidade_com_bilheteria": quantidade_com_bilheteria
    }


def main():
    base = pd.read_csv(
        ARQUIVO_BASE,
        dtype={"codigo_pais": str}
    )

    colunas = {
        "ano",
        "codigo_pais",
        "pais",
        "id_genero",
        "genero",
        "quantidade_filmes"
    }

    if not colunas.issubset(base.columns):
        raise ValueError(
            "O dataset de referência não possui as colunas esperadas."
        )

    base = base.drop_duplicates(
        subset=["ano", "codigo_pais", "id_genero"]
    )

    cache = carregar_cache()
    concluidos = carregar_concluidos()

    total = len(base)

    print("COLETA DE ESTATÍSTICAS DO CINEMA")
    print("--------------------------------")
    print(f"Combinações encontradas: {total}")
    print(f"Combinações concluídas: {len(concluidos)}")
    print(f"Filmes no cache financeiro: {len(cache)}")
    print("--------------------------------\n")

    for indice, linha in enumerate(
        base.itertuples(index=False),
        start=1
    ):
        chave = (
            int(linha.ano),
            str(linha.codigo_pais),
            int(linha.id_genero)
        )

        if chave in concluidos:
            continue

        print(
            f"[{indice}/{total}] "
            f"{linha.ano} | {linha.pais} | {linha.genero}"
        )

        try:
            quantidade_base = int(linha.quantidade_filmes or 0)

            if quantidade_base == 0:
                filmes = []
                quantidade_api = 0
                incompleta = False

            else:
                filmes, quantidade_api, incompleta = obter_filmes(
                    int(linha.ano),
                    str(linha.codigo_pais),
                    int(linha.id_genero)
                )

            estatisticas = calcular_estatisticas(
                filmes,
                cache
            )

            registro = {
                "ano": int(linha.ano),
                "codigo_pais": str(linha.codigo_pais),
                "pais": linha.pais,
                "id_genero": int(linha.id_genero),
                "genero": linha.genero,
                "quantidade_filmes": quantidade_api,
                "quantidade_filmes_coletados": len(filmes),
                "coleta_incompleta": incompleta,
                **estatisticas
            }

            salvar_registro(
                registro,
                ARQUIVO_FINAL
            )

            concluidos.add(chave)

            print(
                f"    Quantidade: {quantidade_api} | "
                f"Coletados: {len(filmes)} | "
                f"Nota média: {estatisticas['nota_media']} | "
                f"Popularidade total: {estatisticas['popularidade_total']}"
            )

            if incompleta:
                print(
                    "    ATENÇÃO: resultados possivelmente incompletos."
                )

        except requests.RequestException as erro:
            print(f"\nErro na consulta: {erro}")
            print("Execute novamente para continuar de onde parou.")
            return

        except Exception as erro:
            print(f"\nErro inesperado: {erro}")
            print("Execute novamente para continuar de onde parou.")
            return

    print("\nCOLETA FINALIZADA!")
    print(f"Dataset final: {ARQUIVO_FINAL}")
    print(f"Cache financeiro: {ARQUIVO_CACHE}")


if __name__ == "__main__":
    main()
