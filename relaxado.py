
import math
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

TOKEN = (os.getenv("TMDB_TOKEN") or "").strip()

if not TOKEN:
    raise ValueError(
        f"Token não encontrado em {RAIZ / '.env'}. "
        "Configure TMDB_TOKEN no arquivo."
    )

ANO_INICIAL = int(input("Digite o ano inicial: "))
ANO_FINAL = int(input("Digite o ano final: "))
FILMES_POR_ANO = 50 if ANO_INICIAL > 1970 else 20
MAX_PAGINAS_POR_BUSCA = 20
INTERVALO_REQUISICOES = 0.05
SALVAR_A_CADA = 25
IDIOMA = "pt-BR"
VALOR_AUSENTE = False

ESTRATEGIAS_BUSCA = [
    ("popularity.desc", 0.50),
    ("vote_count.desc", 0.25),
    ("primary_release_date.asc", 0.25),
]

PASTA_DADOS = RAIZ / "dados"
ARQUIVO_SAIDA = PASTA_DADOS / "filmes.csv"

COLUNAS = [
    "id_tmdb",
    "titulo",
    "titulo_original",
    "data_lancamento",
    "ano",
    "generos",
    'genero_ids',
    "nota",
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
    "poster_url"]

COLUNAS_OBRIGATORIAS = {
    "id_tmdb",
    "titulo",
    "titulo_original",
    "data_lancamento",
    "ano",
    "generos",
    "nota",
    "popularidade",
    "duracao_min",
    "paises_producao",
    "codigos_paises",
    "idioma_original",
    "elenco_principal",
    "poster_url"}

sessao = requests.Session()
sessao.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "accept": "application/json",
})


def consultar_api(endpoint, params=None):
    url = f"https://api.themoviedb.org/3/{endpoint}"

    for tentativa in range(7):
        try:
            resposta = sessao.get(url, params=params, timeout=40)

            if resposta.status_code == 429 or resposta.status_code >= 500:
                espera = resposta.headers.get("Retry-After")
                try:
                    espera = float(espera) if espera else 2 ** tentativa
                except (TypeError, ValueError):
                    espera = 2 ** tentativa

                time.sleep(min(espera, 60))
                continue

            resposta.raise_for_status()
            time.sleep(INTERVALO_REQUISICOES)
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
    return {int(numero) for numero in re.findall(r"\d+", str(valor))}


def valor_disponivel(valor):
    if valor is None or pd.isna(valor):
        return False
    return bool(str(valor).strip())


def preencher_campos(dados):
    resultado = {}

    for coluna in COLUNAS:
        valor = dados.get(coluna)

        if not valor_disponivel(valor):
            resultado[coluna] = VALOR_AUSENTE
        else:
            resultado[coluna] = valor

    return resultado


def registro_valido(dados):
    if not dados:
        return False

    for coluna in COLUNAS_OBRIGATORIAS:
        if not valor_disponivel(dados.get(coluna)):
            return False

    try:
        ano = int(dados["ano"])
    except (TypeError, ValueError):
        return False

    return 1800 <= ano <= ANO_FINAL + 2


def linhas_validas(df):
    validas = pd.Series(True, index=df.index)

    for coluna in COLUNAS_OBRIGATORIAS:
        validas &= df[coluna].notna()
        validas &= df[coluna].astype("string").str.strip().ne("")
        validas &= df[coluna].astype("string").ne(VALOR_AUSENTE)

    return validas


def carregar_generos():
    resposta = consultar_api(
        "genre/movie/list",
        {"language": IDIOMA},
    )

    generos = resposta.get("genres", [])
    mapa_nome_id = {
        normalizar_texto(genero["name"]): genero["id"]
        for genero in generos
    }

    print(f"Gêneros encontrados no TMDB: {len(generos)}")
    return generos, mapa_nome_id


def carregar_base():
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)

    if not ARQUIVO_SAIDA.exists():
        print("Nenhuma base anterior encontrada. Iniciando coleta.")
        return pd.DataFrame(columns=COLUNAS)

    df = pd.read_csv(
        ARQUIVO_SAIDA,
        dtype={"id_tmdb": "string"},
        keep_default_na=False,
    )

    for coluna in COLUNAS:
        if coluna not in df.columns:
            df[coluna] = VALOR_AUSENTE

    df = df[COLUNAS].copy()
    df["id_tmdb"] = df["id_tmdb"].astype("string").str.strip()
    df["ano"] = pd.to_numeric(df["ano"], errors="coerce")

    validas = linhas_validas(df)
    removidos = int((~validas).sum())
    df = df[validas].copy()
    df = df.drop_duplicates(subset="id_tmdb", keep="first")
    df = df.reset_index(drop=True)

    for coluna in COLUNAS:
        df[coluna] = df[coluna].replace("", VALOR_AUSENTE)
        df[coluna] = df[coluna].fillna(VALOR_AUSENTE)

    if removidos:
        print(f"Registros sem informações essenciais removidos: {removidos}")

    print(f"Base anterior carregada: {ARQUIVO_SAIDA}")
    print(f"Filmes existentes: {len(df)}")
    return df


def salvar_base(df):
    if df.empty:
        df = pd.DataFrame(columns=COLUNAS)

    df = df[COLUNAS].copy()
    df = df.drop_duplicates(subset="id_tmdb", keep="first")
    df = df[linhas_validas(df)].copy()

    for coluna in COLUNAS:
        df[coluna] = df[coluna].replace("", VALOR_AUSENTE)
        df[coluna] = df[coluna].fillna(VALOR_AUSENTE)

    df["ano_ordem"] = pd.to_numeric(df["ano"], errors="coerce")
    df["popularidade_ordem"] = pd.to_numeric(
        df["popularidade"].replace(VALOR_AUSENTE, pd.NA),
        errors="coerce",
    )

    df = df.sort_values(
        ["ano_ordem", "popularidade_ordem"],
        ascending=[True, False],
        na_position="last",
    )
    df = df.drop(columns=["ano_ordem", "popularidade_ordem"])

    if df[COLUNAS].isna().any().any():
        raise ValueError("A base ainda contém valores nulos.")

    df.to_csv(
        ARQUIVO_SAIDA,
        index=False,
        encoding="utf-8-sig",
    )


def obter_dados_filme(filme_id, resumo=None):
    detalhes = consultar_api(
        f"movie/{filme_id}",
        {
            "language": IDIOMA,
            "append_to_response": "credits",
        },
    )

    creditos = detalhes.get("credits", {})
    resumo = resumo or {}

    data_lancamento = (
        detalhes.get("release_date")
        or resumo.get("release_date")
        or ""
    )

    ano = None
    if len(data_lancamento) >= 4:
        try:
            ano = int(data_lancamento[:4])
        except ValueError:
            pass

    generos = detalhes.get("genres") or []
    genero_ids = [genero["id"] for genero in generos]

    if not genero_ids:
        genero_ids = resumo.get("genre_ids") or []

    nomes_generos = [genero["name"] for genero in generos]

    if not nomes_generos and genero_ids:
        nomes_generos = [str(genero_id) for genero_id in genero_ids]

    paises = detalhes.get("production_countries") or []

    diretores = [
        pessoa.get("name")
        for pessoa in creditos.get("crew", [])
        if pessoa.get("job") == "Director" and pessoa.get("name")
    ]

    elenco = [
        pessoa.get("name")
        for pessoa in creditos.get("cast", [])[:10]
        if pessoa.get("name")
    ]

    poster_path = detalhes.get("poster_path") or resumo.get("poster_path")

    poster_url = (
        f"https://image.tmdb.org/t/p/w500{poster_path}"
        if poster_path else None
    )

    orcamento = detalhes.get("budget")
    bilheteria = detalhes.get("revenue")
    duracao = detalhes.get("runtime")

    if not orcamento or orcamento < 0:
        orcamento = None
    if not bilheteria or bilheteria < 0:
        bilheteria = None
    if not duracao or duracao <= 0:
        duracao = None

    dados = {
        "id_tmdb": str(detalhes.get("id") or resumo.get("id") or filme_id),
        "titulo": detalhes.get("title") or resumo.get("title"),
        "titulo_original": detalhes.get("original_title") or detalhes.get("title") or resumo.get("title"),
        "data_lancamento": data_lancamento,
        "ano": ano,
        "generos": ", ".join(nomes_generos) if nomes_generos else None,
        "genero_ids": ", ".join(str(genero_id) for genero_id in genero_ids) if genero_ids else None,
        "nota": detalhes.get("vote_average", resumo.get("vote_average")),
        "numero_votos": detalhes.get("vote_count", resumo.get("vote_count")),
        "popularidade": detalhes.get("popularity", resumo.get("popularity")),
        "orcamento_usd": orcamento,
        "bilheteria_usd": bilheteria,
        "duracao_min": duracao,
        "paises_producao": next((pais.get("name") for pais in paises if pais.get("name")), None),
        "codigos_paises": next((pais.get("iso_3166_1") for pais in paises if pais.get("iso_3166_1")), None),
        "idioma_original": detalhes.get("original_language") or resumo.get("original_language"),
        "diretores": ", ".join(diretores) if diretores else None,
        "elenco_principal": ", ".join(elenco) if elenco else None,
        "sinopse": detalhes.get("overview") or None,
        "poster_url": poster_url,
    }

    dados = preencher_campos(dados)
    return dados if registro_valido(dados) else None


def registrar_filme(df, dados, ids_existentes):
    if dados is None or not registro_valido(dados):
        return df, False

    filme_id = str(dados["id_tmdb"])

    if filme_id in ids_existentes:
        return df, False

    df = pd.concat(
        [df, pd.DataFrame([dados], columns=COLUNAS)],
        ignore_index=True,
    )

    ids_existentes.add(filme_id)
    return df, True


def buscar_filmes_do_ano(ano, estrategia, quantidade, ids_existentes):
    encontrados = []
    adicionados_ids = set()

    for pagina in range(1, MAX_PAGINAS_POR_BUSCA + 1):
        resultado = consultar_api(
            "discover/movie",
            {
                "language": IDIOMA,
                "sort_by": estrategia,
                "include_adult": "false",
                "include_video": "false",
                "primary_release_date.gte": f"{ano}-01-01",
                "primary_release_date.lte": f"{ano}-12-31",
                "page": pagina,
            },
        )

        filmes = resultado.get("results", [])

        if not filmes:
            break

        for filme in filmes:
            filme_id = str(filme.get("id"))

            if not filme_id or filme_id in ids_existentes or filme_id in adicionados_ids:
                continue

            try:
                dados = obter_dados_filme(filme_id, filme)
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as erro:
                print(f"\nErro no filme {filme_id}: {erro}")
                continue

            if dados is None or str(dados["ano"]) != str(ano):
                continue

            encontrados.append(dados)
            adicionados_ids.add(filme_id)

            if len(encontrados) >= quantidade:
                return encontrados

        if pagina >= resultado.get("total_pages", 0):
            break

    return encontrados


def coletar_filmes_populares(df):
    ids_existentes = set(df["id_tmdb"].astype(str))
    adicionados_total = 0

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        print(f"\nColeta de {ano}")

        quantidade_atual = int(
            (pd.to_numeric(df["ano"], errors="coerce") == ano).sum()
        )

        faltam = max(0, FILMES_POR_ANO - quantidade_atual)

        if faltam == 0:
            print(f"  Meta de {FILMES_POR_ANO} filmes já atingida.")
            continue

        novos_no_ano = 0
        restantes = faltam

        for estrategia, proporcao in ESTRATEGIAS_BUSCA:
            if restantes <= 0:
                break

            quota = max(1, math.ceil(faltam * proporcao))
            quota = min(quota, restantes)

            print(f"  Buscando por {estrategia}: até {quota} filmes")

            candidatos = buscar_filmes_do_ano(
                ano,
                estrategia,
                quota,
                ids_existentes,
            )

            for dados in candidatos:
                df, foi_adicionado = registrar_filme(
                    df,
                    dados,
                    ids_existentes,
                )

                if foi_adicionado:
                    novos_no_ano += 1
                    adicionados_total += 1

                    if adicionados_total % SALVAR_A_CADA == 0:
                        salvar_base(df)

            restantes = max(
                0,
                FILMES_POR_ANO - quantidade_atual - novos_no_ano,
            )

            print(f"  Filmes adicionados em {ano}: {novos_no_ano}")

        salvar_base(df)

        print(
            f"  Total registrado em {ano}: "
            f"{quantidade_atual + novos_no_ano}"
        )

        if quantidade_atual + novos_no_ano < FILMES_POR_ANO:
            print(
                f"  O TMDB não forneceu filmes suficientes para atingir "
                f"{FILMES_POR_ANO} neste ano."
            )

    salvar_base(df)

    print(f"\nColeta inicial concluída. Novos filmes: {adicionados_total}")
    return df


def generos_presentes_no_ano(df, ano, mapa_nome_id):
    presentes = set()

    filmes_ano = df[
        pd.to_numeric(df["ano"], errors="coerce") == ano
    ]

    for valor in filmes_ano["genero_ids"]:
        presentes.update(extrair_ids(valor))

    for valor in filmes_ano["generos"]:
        if valor == VALOR_AUSENTE:
            continue

        for nome in str(valor).split(","):
            nome_normalizado = normalizar_texto(nome)

            if nome_normalizado in mapa_nome_id:
                presentes.add(mapa_nome_id[nome_normalizado])

    return presentes


def buscar_filme_para_genero(ano, genero_id, ids_existentes):
    for pagina in range(1, MAX_PAGINAS_POR_BUSCA + 1):
        resultado = consultar_api(
            "discover/movie",
            {
                "language": IDIOMA,
                "sort_by": "vote_count.desc",
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
            filme_id = str(filme.get("id"))

            if not filme_id or filme_id in ids_existentes:
                continue

            if genero_id not in filme.get("genre_ids", []):
                continue

            dados = obter_dados_filme(filme_id, filme)

            if dados is None or str(dados["ano"]) != str(ano):
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
        print(f"\nCompletando gêneros de {ano}")

        for genero in generos:
            genero_id = genero["id"]
            nome_genero = genero["name"]

            if genero_id in generos_presentes_no_ano(df, ano, mapa_nome_id):
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
                        "motivo": "Nenhum filme elegível encontrado no TMDB",
                    })
                    continue

                df, foi_adicionado = registrar_filme(
                    df,
                    dados,
                    ids_existentes,
                )

                if foi_adicionado:
                    adicionados += 1
                    print(f"  Adicionado: {nome_genero}")

            except (requests.RequestException, RuntimeError, ValueError, KeyError) as erro:
                falhas_busca.append({
                    "ano": ano,
                    "genero": nome_genero,
                    "motivo": str(erro),
                })

                print(f"  Erro em {nome_genero}: {erro}")

        salvar_base(df)

    print(f"\nEtapa de gêneros concluída. Filmes adicionados: {adicionados}")
    return df, falhas_busca


def verificar_cobertura(df, generos, mapa_nome_id):
    ausentes = []

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):
        presentes = generos_presentes_no_ano(df, ano, mapa_nome_id)

        for genero in generos:
            if genero["id"] not in presentes:
                ausentes.append({
                    "ano": ano,
                    "genero": genero["name"],
                })

    caminho_ausentes = PASTA_DADOS / "generos_ausentes_por_ano.csv"

    if ausentes:
        pd.DataFrame(ausentes).to_csv(
            caminho_ausentes,
            index=False,
            encoding="utf-8-sig",
        )

        print(f"\nCombinações de ano e gênero ausentes: {len(ausentes)}")

    elif caminho_ausentes.exists():
        caminho_ausentes.unlink()
        print("\nTodos os gêneros foram encontrados em todos os anos.")

    contagem = (
        pd.to_numeric(df["ano"], errors="coerce")
        .value_counts()
        .reindex(range(ANO_INICIAL, ANO_FINAL + 1), fill_value=0)
        .sort_index()
    )

    print("\nQuantidade de filmes por ano:")
    print(contagem.to_string())

    print(f"\nTotal de filmes únicos: {df['id_tmdb'].nunique()}")
    print(f"Arquivo final: {ARQUIVO_SAIDA}")


def main():
    if ANO_INICIAL > ANO_FINAL:
        raise ValueError("O ano inicial não pode ser maior que o ano final.")

    print("Iniciando o CineTimeline")
    print(f"Período: {ANO_INICIAL}–{ANO_FINAL}")
    print(f"Meta inicial: {FILMES_POR_ANO} filmes por ano")

    df = carregar_base()
    generos, mapa_nome_id = carregar_generos()

    df = coletar_filmes_populares(df)

    df, falhas_busca = completar_generos_por_ano(
        df,
        generos,
        mapa_nome_id,
    )

    salvar_base(df)

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
            "dados/erros_coleta_generos.csv."
        )


if __name__ == "__main__":
    main()
