import os
import re
import time
import unicodedata
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

TOKEN = os.getenv("TMDB_TOKEN")

if not TOKEN:
    raise ValueError(
        f"Token TMDB_TOKEN não encontrado no arquivo {RAIZ / '.env'}. "
        "Confira o nome da variável dentro do arquivo."
    )

ANO_INICIAL = 2000
ANO_FINAL = 2025
IDIOMA = "pt-BR"
MAX_PAGINAS_POR_BUSCA = 10

PASTA_DADOS = RAIZ / "data" / "processed"
ARQUIVO_SAIDA = PASTA_DADOS / "filmes_2000_2025.csv"

ARQUIVOS_EXISTENTES = [
    ARQUIVO_SAIDA,
    PASTA_DADOS / "filmes_2010_2025.csv",
]

COLUNAS = [
    "id_tmdb",
    "titulo",
    "titulo_original",
    "data_lancamento",
    "ano",
    "generos",
    "genero_ids",
    "nota",
    "numero_votos",
    "popularidade",
    "orcamento_usd",
    "bilheteria_usd",
    "duracao_min",
    "paises_producao",
    "codigos_paises",
    "idioma_original",
    "diretores",
    "elenco_principal",
    "sinopse",
]

sessao = requests.Session()
sessao.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "accept": "application/json",
})


def consultar_api(endpoint, params=None):
    url = f"https://api.themoviedb.org/3/{endpoint}"

    for tentativa in range(6):
        resposta = sessao.get(url, params=params, timeout=30)

        if resposta.status_code == 429 or resposta.status_code >= 500:
            espera = int(
                resposta.headers.get("Retry-After", 2 ** tentativa)
            )
            time.sleep(min(espera, 60))
            continue

        resposta.raise_for_status()
        time.sleep(0.15)
        return resposta.json()

    raise RuntimeError(f"Falha após várias tentativas: {endpoint}")


def normalizar_texto(texto):
    texto = unicodedata.normalize("NFD", str(texto))
    return "".join(
        caractere
        for caractere in texto
        if unicodedata.category(caractere) != "Mn"
    ).strip().casefold()


def extrair_ids(valor):
    if pd.isna(valor):
        return set()

    return {
        int(numero)
        for numero in re.findall(r"\d+", str(valor))
    }


def carregar_generos():
    resposta = consultar_api(
        "genre/movie/list",
        {"language": IDIOMA},
    )

    generos = resposta["genres"]

    mapa_nome_id = {
        normalizar_texto(genero["name"]): genero["id"]
        for genero in generos
    }

    return generos, mapa_nome_id


def ids_dos_generos(linha, mapa_nome_id):
    ids = extrair_ids(linha.get("genero_ids"))

    if ids:
        return ids

    nomes = str(linha.get("generos", "")).split(",")

    for nome in nomes:
        nome_normalizado = normalizar_texto(nome)

        if nome_normalizado in mapa_nome_id:
            ids.add(mapa_nome_id[nome_normalizado])

    return ids


def carregar_base():
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    caminho = next(
        (arquivo for arquivo in ARQUIVOS_EXISTENTES if arquivo.exists()),
        None,
    )

    if caminho is None:
        raise FileNotFoundError(
            "Não encontrei o CSV existente. Confira os arquivos em "
            f"{PASTA_DADOS}"
        )

    df = pd.read_csv(caminho, dtype={"id_tmdb": "string"})

    for coluna in COLUNAS:
        if coluna not in df.columns:
            df[coluna] = pd.NA

    df["id_tmdb"] = df["id_tmdb"].astype("string").str.strip()
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce")

    df = df.dropna(subset=["id_tmdb"])
    df = df[df["id_tmdb"] != ""]
    df = df.drop_duplicates(subset="id_tmdb", keep="first")
    df = df[COLUNAS]

    print(f"Base carregada: {caminho}")
    print(f"Filmes existentes: {len(df)}")

    return df


def generos_presentes_no_ano(df, ano, mapa_nome_id):
    presentes = set()
    filmes_ano = df[df["ano"] == ano]

    for _, linha in filmes_ano.iterrows():
        presentes.update(ids_dos_generos(linha, mapa_nome_id))

    return presentes


def obter_dados_filme(filme_id):
    detalhes = consultar_api(
        f"movie/{filme_id}",
        {"language": IDIOMA},
    )

    creditos = consultar_api(
        f"movie/{filme_id}/credits",
        {"language": IDIOMA},
    )

    data_lancamento = detalhes.get("release_date", "")
    ano = (
        int(data_lancamento[:4])
        if len(data_lancamento) >= 4
        else None
    )

    generos = detalhes.get("genres", [])
    paises = detalhes.get("production_countries", [])

    diretores = [
        pessoa["name"]
        for pessoa in creditos.get("crew", [])
        if pessoa.get("job") == "Director"
    ]

    elenco = [
        pessoa["name"]
        for pessoa in creditos.get("cast", [])[:10]
    ]

    return {
        "id_tmdb": str(detalhes["id"]),
        "titulo": detalhes.get("title"),
        "titulo_original": detalhes.get("original_title"),
        "data_lancamento": data_lancamento,
        "ano": ano,
        "generos": ", ".join(g["name"] for g in generos),
        "genero_ids": ", ".join(str(g["id"]) for g in generos),
        "nota": detalhes.get("vote_average"),
        "numero_votos": detalhes.get("vote_count"),
        "popularidade": detalhes.get("popularity"),
        "orcamento_usd": detalhes.get("budget"),
        "bilheteria_usd": detalhes.get("revenue"),
        "duracao_min": detalhes.get("runtime"),
        "paises_producao": ", ".join(
            pais["name"] for pais in paises
        ),
        "codigos_paises": ", ".join(
            pais["iso_3166_1"] for pais in paises
        ),
        "idioma_original": detalhes.get("original_language"),
        "diretores": ", ".join(diretores),
        "elenco_principal": ", ".join(elenco),
        "sinopse": detalhes.get("overview"),
    }


def buscar_filme_para_genero(ano, genero_id, ids_existentes):
    for pagina in range(1, MAX_PAGINAS_POR_BUSCA + 1):
        resultado = consultar_api(
            "discover/movie",
            {
                "language": IDIOMA,
                "sort_by": "popularity.desc",
                "include_adult": "false",
                "include_video": "false",
                "with_genres": genero_id,
                "primary_release_date.gte": f"{ano}-01-01",
                "primary_release_date.lte": f"{ano}-12-31",
                "page": pagina,
            },
        )

        filmes = resultado.get("results", [])

        if not filmes:
            break

        for filme in filmes:
            filme_id = str(filme["id"])

            if filme_id in ids_existentes:
                continue

            if genero_id not in filme.get("genre_ids", []):
                continue

            dados = obter_dados_filme(filme_id)

            if dados["ano"] != ano:
                continue

            if genero_id not in extrair_ids(dados["genero_ids"]):
                continue

            return dados

        if pagina >= resultado.get("total_pages", 0):
            break

    return None


def salvar_base(df):
    df = df.drop_duplicates(subset="id_tmdb", keep="first")
    df = df[COLUNAS]

    df.to_csv(
        ARQUIVO_SAIDA,
        index=False,
        encoding="utf-8-sig",
    )


def completar_cobertura(df, generos, mapa_nome_id):
    ids_existentes = set(df["id_tmdb"].astype(str))
    adicionados = 0

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        print(f"\nVerificando {ano}...")

        for genero in generos:
            genero_id = genero["id"]
            nome_genero = genero["name"]

            presentes = generos_presentes_no_ano(
                df, ano, mapa_nome_id
            )

            if genero_id in presentes:
                continue

            print(f"  Procurando {nome_genero} em {ano}...")

            try:
                dados = buscar_filme_para_genero(
                    ano,
                    genero_id,
                    ids_existentes,
                )
            except (requests.RequestException, RuntimeError, ValueError) as erro:
                print(f"  Erro na busca: {erro}")
                salvar_base(df)
                continue

            if dados is None:
                print(f"  Nenhum filme novo encontrado para {nome_genero}.")
                continue

            df = pd.concat(
                [df, pd.DataFrame([dados], columns=COLUNAS)],
                ignore_index=True,
            )

            ids_existentes.add(str(dados["id_tmdb"]))
            adicionados += 1

            print(f"  Adicionado: {dados['titulo']} ({ano})")
            salvar_base(df)

    salvar_base(df)

    print(f"\nFilmes adicionados: {adicionados}")
    print(f"Total de filmes: {len(df)}")

    return df


def verificar_cobertura(df, generos, mapa_nome_id):
    falhas = []

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        presentes = generos_presentes_no_ano(
            df, ano, mapa_nome_id
        )

        for genero in generos:
            if genero["id"] not in presentes:
                falhas.append({
                    "ano": ano,
                    "genero": genero["name"],
                })

    print("\nResultado da verificação:")

    if falhas:
        df_falhas = pd.DataFrame(falhas)

        print(f"Combinações ainda ausentes: {len(df_falhas)}")
        print(df_falhas.to_string(index=False))

        df_falhas.to_csv(
            PASTA_DADOS / "generos_ausentes_por_ano.csv",
            index=False,
            encoding="utf-8-sig",
        )
    else:
        print("Todos os gêneros foram encontrados em todos os anos.")

    print("\nQuantidade de filmes por ano:")
    print(
        df.groupby("ano")
        .size()
        .reindex(range(ANO_INICIAL, ANO_FINAL + 1), fill_value=0)
        .to_string()
    )


def main():
    print("Carregando base e gêneros do TMDB...")

    df = carregar_base()
    generos, mapa_nome_id = carregar_generos()

    df = completar_cobertura(df, generos, mapa_nome_id)

    verificar_cobertura(df, generos, mapa_nome_id)

    print(f"\nArquivo final: {ARQUIVO_SAIDA}")


if __name__ == "__main__":
    main()