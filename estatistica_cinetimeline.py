
import os
import time
import requests
import pandas as pd

from pathlib import Path
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TMDB_TOKEN = os.getenv('TMDB_TOKEN')

URL_BASE = 'https://api.themoviedb.org/3'

ANO_INICIAL = 1900
ANO_FINAL = 2026

INTERVALO = 0.25
SALVAR_A_CADA = 50

PASTA = Path('Dataset_Filmes/dados')
PASTA.mkdir(exist_ok=True)

ARQUIVO_FINAL = PASTA / 'estatisticas_filmes.csv'
ARQUIVO_BASE = PASTA / 'progresso_pais_ano.csv'

if not TMDB_TOKEN:
    raise ValueError('TMDB_TOKEN não encontrado no arquivo .env')

sessao = requests.Session()

sessao.headers.update({
    'Authorization': f'Bearer {TMDB_TOKEN}',
    'accept': 'application/json'
})

retries = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET'],
    respect_retry_after_header=True
)

sessao.mount(
    'https://',
    HTTPAdapter(max_retries=retries)
)


def consultar(endpoint, parametros=None):
    resposta = sessao.get(
        f'{URL_BASE}/{endpoint}',
        params=parametros or {},
        timeout=(10, 30)
    )

    resposta.raise_for_status()

    time.sleep(INTERVALO)

    return resposta.json()


def obter_paises():
    dados = consultar('configuration/countries')

    return {
        pais['iso_3166_1']: pais['english_name']
        for pais in dados
    }


def obter_generos():
    dados = consultar(
        'genre/movie/list',
        {'language': 'pt-BR'}
    )

    return {
        genero['id']: genero['name']
        for genero in dados['genres']
    }


def carregar_chaves(arquivo, colunas):
    if not arquivo.exists() or arquivo.stat().st_size == 0:
        return set()

    df = pd.read_csv(arquivo)

    return set(
        map(
            tuple,
            df[colunas].itertuples(index=False, name=None)
        )
    )


def salvar(registros, arquivo):
    if not registros:
        return

    df = pd.DataFrame(registros)

    df.to_csv(
        arquivo,
        mode='a',
        index=False,
        header=not arquivo.exists() or arquivo.stat().st_size == 0,
        encoding='utf-8-sig'
    )


def consultar_quantidade(ano, pais, genero=None):
    parametros = {
        'primary_release_year': ano,
        'with_origin_country': pais,
        'page': 1
    }

    if genero is not None:
        parametros['with_genres'] = genero

    dados = consultar('discover/movie', parametros)

    return dados['total_results']


def coletar_base(paises):
    concluidos = carregar_chaves(
        ARQUIVO_BASE,
        ['ano', 'codigo_pais']
    )

    registros = []

    total = len(paises) * (ANO_FINAL - ANO_INICIAL + 1)
    processados = len(concluidos)

    print('\nETAPA 1: CONSULTANDO PAÍSES E ANOS')

    for ano in range(ANO_INICIAL, ANO_FINAL + 1):

        for codigo, nome in paises.items():

            chave = (ano, codigo)

            if chave in concluidos:
                continue

            try:
                quantidade = consultar_quantidade(
                    ano,
                    codigo
                )

                registros.append({
                    'ano': ano,
                    'codigo_pais': codigo,
                    'pais': nome,
                    'quantidade_total': quantidade
                })

                processados += 1

                if len(registros) >= SALVAR_A_CADA:
                    salvar(registros, ARQUIVO_BASE)
                    registros.clear()

                if processados % 500 == 0:
                    print(
                        f'Etapa 1: {processados:,}/{total:,}'
                    )

            except requests.RequestException as erro:
                salvar(registros, ARQUIVO_BASE)

                print(
                    f'Erro em {ano} - {codigo}: {erro}'
                )

                return False

    salvar(registros, ARQUIVO_BASE)

    print('Etapa 1 concluída!')

    return True


def coletar_generos(generos):
    base = pd.read_csv(ARQUIVO_BASE)

    base = base[base['quantidade_total'] > 0]

    concluidos = carregar_chaves(
        ARQUIVO_FINAL,
        ['ano', 'codigo_pais', 'id_genero']
    )

    total = len(base) * len(generos)
    processados = len(concluidos)

    registros = []

    print('\nETAPA 2: CONSULTANDO GÊNEROS')

    for linha in base.itertuples(index=False):

        for id_genero, nome_genero in generos.items():

            chave = (
                linha.ano,
                linha.codigo_pais,
                id_genero
            )

            if chave in concluidos:
                continue

            try:
                quantidade = consultar_quantidade(
                    linha.ano,
                    linha.codigo_pais,
                    id_genero
                )

                registros.append({
                    'ano': linha.ano,
                    'codigo_pais': linha.codigo_pais,
                    'pais': linha.pais,
                    'id_genero': id_genero,
                    'genero': nome_genero,
                    'quantidade_filmes': quantidade
                })

                processados += 1

                if len(registros) >= SALVAR_A_CADA:
                    salvar(registros, ARQUIVO_FINAL)
                    registros.clear()

                if processados % 500 == 0:
                    print(
                        f'Etapa 2: {processados:,}/{total:,}'
                    )

            except requests.RequestException as erro:
                salvar(registros, ARQUIVO_FINAL)

                print(
                    f'Erro em {linha.ano} - '
                    f'{linha.codigo_pais} - '
                    f'{nome_genero}: {erro}'
                )

                return

    salvar(registros, ARQUIVO_FINAL)

    print('\nCOLETA FINALIZADA!')
    print(f'Dataset salvo em: {ARQUIVO_FINAL}')


def main():
    print('Obtendo países e gêneros...')

    try:
        paises = obter_paises()
        generos = obter_generos()

    except requests.RequestException as erro:
        print(f'Erro ao consultar informações iniciais: {erro}')
        return

    print(f'Países encontrados: {len(paises)}')
    print(f'Gêneros encontrados: {len(generos)}')

    if not coletar_base(paises):
        print('Primeira etapa interrompida. Execute novamente para continuar.')
        return

    coletar_generos(generos)


if __name__ == '__main__':
    main()
