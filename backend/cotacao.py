"""Cotação do dólar (PTAX do Banco Central), só para mostrar como referência no topo do chat.

Não entra no agente nem na requisição, que continuam em dólar. Um valor em reais ao lado do
total pareceria definitivo, mas não inclui impostos, frete nem o câmbio do dia do pagamento."""
import time
from datetime import date, timedelta

import httpx

URL = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
       "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)")
CACHE_S = 3600          # a PTAX muda uma vez por dia; não precisa consultar a cada acesso
JANELA_DIAS = 7         # cobre fim de semana e feriado: vale a última PTAX publicada
TIMEOUT_S = 5

_cache: dict = {}


class CotacaoIndisponivel(Exception):
    pass


async def ptax(hoje: date | None = None) -> dict:
    """Última PTAX publicada: {"venda", "compra", "data", "fonte"}."""
    agora = time.monotonic()
    if _cache and agora - _cache["em"] < CACHE_S:
        return _cache["valor"]

    hoje = hoje or date.today()
    params = {"@dataInicial": f"'{hoje - timedelta(days=JANELA_DIAS):%m-%d-%Y}'",
              "@dataFinalCotacao": f"'{hoje:%m-%d-%Y}'", "$format": "json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(URL, params=params)
        resp.raise_for_status()
        linhas = resp.json().get("value") or []
    except (httpx.HTTPError, ValueError) as e:
        raise CotacaoIndisponivel(f"o Banco Central não respondeu ({type(e).__name__})") from e
    if not linhas:
        raise CotacaoIndisponivel(f"nenhuma PTAX publicada nos últimos {JANELA_DIAS} dias")

    ultima = max(linhas, key=lambda linha: linha["dataHoraCotacao"])
    valor = {"venda": ultima["cotacaoVenda"], "compra": ultima["cotacaoCompra"],
             "data": ultima["dataHoraCotacao"][:10], "fonte": "PTAX, Banco Central do Brasil"}
    _cache.update(em=agora, valor=valor)
    return valor
