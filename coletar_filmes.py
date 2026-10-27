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

TOKEN = (os.getenv("TMDB_TOKEN")).strip()

if not TOKEN:
    raise ValueError(
        f"Token não encontrado em {RAIZ / '.env'}. "
        "Configure TMDB_TOKEN no arquivo."
    )

ANO_INICIAL = int(input('Digite o ano inicial: '))
ANO_FINAL = int(input('Digite o ano final: '))
FILMES_INICIAIS_POR_ANO = 50
MAX_PAGINAS_POR_GENERO = 10
IDIOMA = "pt-BR"

PASTA_DADOS = RAIZ / "dados"
ARQUIVO_SAIDA = PASTA_DADOS / "filmes.csv"

ARQUIVOS_EXISTENTES = [
    ARQUIVO_SAIDA,
    PASTA_DADOS / "filmes.csv",
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

    for tentativa in range(7):
        try:
            resposta = sessao.get(
                url,
                params=params,
                timeout=40,
            )

            if resposta.status_code == 429 or resposta.status_code >= 500:
                espera = resposta.headers.get("Retry-After")

                try:
                    espera = float(espera) if espera else 2 ** tentativa
                except ValueError:
                    espera = 2 ** tentativa

                time.sleep(min(espera, 60))
                continue

            resposta.raise_for_status()
            time.sleep(0.15)
            return resposta.json()

        except requests.RequestException:
            if tentativa == 6:
                raise

            time.sleep(min(2 ** tentativa, 60))

    raise RuntimeError(f"Não foi possível consultar {endpoint}.")


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

    print(f"Gêneros encontrados no TMDB: {len(generos)}")

    for genero in generos:
        print(f"  {genero['id']}: {genero['name']}")

    return generos, mapa_nome_id


def carregar_base():
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    caminho = next(
        (
            arquivo
            for arquivo in ARQUIVOS_EXISTENTES
            if arquivo.exists()
        ),
        None,
    )

    if caminho is None:
        df = pd.DataFrame(columns=COLUNAS)
        print("Nenhuma base anterior encontrada. Iniciando coleta.")
        return df

    df = pd.read_csv(
        caminho,
        dtype={"id_tmdb": "string"},
    )

    for coluna in COLUNAS:
        if coluna not in df.columns:
            df[coluna] = pd.NA

    df["id_tmdb"] = df["id_tmdb"].astype("string").str.strip()
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce")

    df = df.dropna(subset=["id_tmdb"])
    df = df[df["id_tmdb"] != ""]
    df = df.drop_duplicates(subset="id_tmdb", keep="first")
    df = df[COLUNAS].reset_index(drop=True)

    print(f"Base anterior carregada: {caminho}")
    print(f"Filmes existentes: {len(df)}")

    return df


def salvar_base(df):
    df = df.drop_duplicates(
        subset="id_tmdb",
        keep="first",
    ).copy()

    df["ano"] = pd.to_numeric(df["ano"], errors="coerce")
    df["popularidade"] = pd.to_numeric(
        df["popularidade"],
        errors="coerce",
    )

    df = df[COLUNAS].sort_values(
        ["ano", "popularidade"],
        ascending=[True, False],
        na_position="last",
    )

    df.to_csv(
        ARQUIVO_SAIDA,
        index=False,
        encoding="utf-8-sig",
    )


def obter_dados_filme(filme_id):
    detalhes = consultar_api(
        f"movie/{filme_id}",
        {"language": IDIOMA},
    )

    creditos = consultar_api(
        f"movie/{filme_id}/credits",
        {"language": IDIOMA},
    )

    data_lancamento = detalhes.get("release_date") or ""

    if len(data_lancamento) >= 4:
        try:
            ano = int(data_lancamento[:4])
        except ValueError:
            ano = None
    else:
        ano = None

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
        "generos": ", ".join(
            genero["name"] for genero in generos
        ),
        "genero_ids": ", ".join(
            str(genero["id"]) for genero in generos
        ),
        "nota": detalhes.get("vote_average"),
        "numero_votos": detalhes.get("vote_count"),
        "popularidade": detalhes.get("popularity"),
        "orcamento_usd": detalhes.get("budget"),
        "bilheteria_usd": detalhes.get("revenue"),
        "duracao_min": detalhes.get("runtime"),
        "paises_producao": next((pais["name"] for pais in paises),""),
        "codigos_paises": next((pais["iso_3166_1"] for pais in paises), ''),
        "idioma_original": detalhes.get("original_language"),
        "diretores": ", ".join(diretores),
        "elenco_principal": ", ".join(elenco),
        "sinopse": detalhes.get("overview"),
    }


def registrar_filme(df, dados, ids_existentes):
    if dados is None:
        return df, False

    filme_id = str(dados["id_tmdb"])

    if filme_id in ids_existentes:
        return df, False

    df = pd.concat(
        [
            df,
            pd.DataFrame([dados], columns=COLUNAS),
        ],
        ignore_index=True,
    )

    ids_existentes.add(filme_id)
    salvar_base(df)

    return df, True



def coletar_filmes_populares(df):
    ids_existentes = set(df["id_tmdb"].astype(str))
    adicionados = 0

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        print(f"\nColeta inicial: {ano}")

        filmes_do_ano = (
            pd.to_numeric(df["ano"], errors="coerce") == ano
        ).sum()

        if filmes_do_ano >= FILMES_INICIAIS_POR_ANO:
            print(
                f"  O ano já possui {filmes_do_ano} filmes. "
                "Meta inicial atingida."
            )
            continue

        novos_no_ano = 0
        pagina = 1
        total_paginas = 1

        while (
            filmes_do_ano < FILMES_INICIAIS_POR_ANO
            and pagina <= total_paginas
        ):
            try:
                resultado = consultar_api(
                    "discover/movie",
                    {
                        "language": IDIOMA,
                        "sort_by": "popularity.desc",
                        "include_adult": "false",
                        "include_video": "false",
                        "primary_release_date.gte": f"{ano}-01-01",
                        "primary_release_date.lte": f"{ano}-12-31",
                        "page": pagina,
                    },
                )

                total_paginas = resultado.get("total_pages", 0)
                filmes = resultado.get("results", [])

            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as erro:
                salvar_base(df)
                print(
                    f"  Erro ao consultar a página {pagina} "
                    f"de {ano}: {erro}"
                )
                break

            if not filmes:
                break

            for filme in filmes:
                if filmes_do_ano >= FILMES_INICIAIS_POR_ANO:
                    break

                filme_id = str(filme["id"])

                if filme_id in ids_existentes:
                    continue

                try:
                    dados = obter_dados_filme(filme_id)

                    if dados["ano"] != ano:
                        continue

                    df, foi_adicionado = registrar_filme(
                        df,
                        dados,
                        ids_existentes,
                    )

                    if foi_adicionado:
                        adicionados += 1
                        novos_no_ano += 1
                        filmes_do_ano += 1

                        print(f"\r  {filmes_do_ano}/{FILMES_INICIAIS_POR_ANO}", end="", flush=True)

                except (
                    requests.RequestException,
                    RuntimeError,
                    ValueError,
                    KeyError,
                ) as erro:
                    salvar_base(df)
                    print(f"  Erro no filme {filme_id}: {erro}")

            salvar_base(df)
            pagina += 1

        print(
            f"  Novos filmes adicionados em {ano}: {novos_no_ano}"
        )
        print(
            f"  Total de filmes registrados em {ano}: "
            f"{filmes_do_ano}"
        )

        if filmes_do_ano < FILMES_INICIAIS_POR_ANO:
            print(
                f"  Aviso: não foi possível atingir a meta de "
                f"{FILMES_INICIAIS_POR_ANO} filmes para {ano}."
            )

    print(
        f"\nColeta inicial concluída. "
        f"Novos filmes adicionados: {adicionados}"
    )

    return df


def generos_presentes_no_ano(df, ano, mapa_nome_id):
    presentes = set()
    filmes_ano = df[df["ano"] == ano]

    for _, linha in filmes_ano.iterrows():
        ids = extrair_ids(linha.get("genero_ids"))

        if not ids:
            nomes = str(linha.get("generos", "")).split(",")

            for nome in nomes:
                nome_normalizado = normalizar_texto(nome)

                if nome_normalizado in mapa_nome_id:
                    ids.add(mapa_nome_id[nome_normalizado])

        presentes.update(ids)

    return presentes


def buscar_filme_para_genero(
    ano,
    genero_id,
    ids_existentes,
):
    for pagina in range(1, MAX_PAGINAS_POR_GENERO + 1):
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


def completar_generos_por_ano(df, generos, mapa_nome_id):
    ids_existentes = set(df["id_tmdb"].astype(str))
    adicionados = 0
    falhas_busca = []

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        print(f"Completando gêneros de {ano}")

        for genero in generos:
            genero_id = genero["id"]
            nome_genero = genero["name"]

            presentes = generos_presentes_no_ano(
                df,
                ano,
                mapa_nome_id,
            )

            if genero_id in presentes:
                continue

            try:
                dados = buscar_filme_para_genero(
                    ano,
                    genero_id,
                    ids_existentes,
                )

                if dados is None:
                    falhas_busca.append({
                        "ano": ano,
                        "genero": nome_genero,
                        "motivo": "Nenhum filme novo encontrado",
                    })

                    print("  Nenhum filme novo encontrado.")
                    continue

                df, foi_adicionado = registrar_filme(
                    df,
                    dados,
                    ids_existentes,
                )

                if foi_adicionado:
                    adicionados += 1
                    print(f"  Adicionado: {nome_genero}")

            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
                KeyError,
            ) as erro:
                falhas_busca.append({
                    "ano": ano,
                    "genero": nome_genero,
                    "motivo": str(erro),
                })

                print(f"  Erro na busca: {erro}")

            salvar_base(df)

    salvar_base(df)

    print(
        f"\nEtapa complementar concluída. "
        f"Filmes adicionados: {adicionados}"
    )

    return df, falhas_busca


def verificar_cobertura(df, generos, mapa_nome_id):
    ausentes = []

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        presentes = generos_presentes_no_ano(
            df,
            ano,
            mapa_nome_id,
        )

        for genero in generos:
            if genero["id"] not in presentes:
                ausentes.append({
                    "ano": ano,
                    "genero": genero["name"],
                })

    if ausentes:
        df_ausentes = pd.DataFrame(ausentes)

        df_ausentes.to_csv(
            PASTA_DADOS / "generos_ausentes_por_ano.csv",
            index=False,
            encoding="utf-8-sig",
        )

        print(
            f"\nCombinações de ano e gênero ainda ausentes: "
            f"{len(df_ausentes)}"
        )

        print(df_ausentes.to_string(index=False))

    else:
        caminho = PASTA_DADOS / "generos_ausentes_por_ano.csv"

        if caminho.exists():
            caminho.unlink()

        print("\nTodos os gêneros foram encontrados em todos os anos.")

    contagem = (
        df.groupby("ano")
        .size()
        .reindex(
            range(ANO_INICIAL, ANO_FINAL + 1),
            fill_value=0,
        )
    )

    print("\nQuantidade de filmes por ano:")
    print(contagem.to_string())

    print(f"\nTotal de filmes únicos: {df['id_tmdb'].nunique()}")
    print(f"Arquivo final: {ARQUIVO_SAIDA}")


def main():
    print("Iniciando o CineTimeline")
    print(f"Ano: {ANO_INICIAL}–{ANO_FINAL}")

    df = carregar_base()
    generos, mapa_nome_id = carregar_generos()

    df = coletar_filmes_populares(df)

    df, falhas_busca = completar_generos_por_ano(
        df,
        generos,
        mapa_nome_id,
    )

    verificar_cobertura(
        df,
        generos,
        mapa_nome_id,
    )

    if falhas_busca:
        pd.DataFrame(falhas_busca).to_csv(
            PASTA_DADOS / "erros_coleta_generos.csv",
            index=False,
            encoding="utf-8-sig",
        )

        print(
            "\nAlgumas buscas falharam. Consulte "
            "data/processed/erros_coleta_generos.csv."
        )


if __name__ == "__main__":
    main()