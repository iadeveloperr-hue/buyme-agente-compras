"""PTAX do Banco Central simulada com respx: nenhum teste depende de rede."""
import asyncio
from datetime import date

import cotacao
import httpx
import main
import pytest
import respx
from fastapi.testclient import TestClient

BANCO_CENTRAL = "olinda.bcb.gov.br"
DUAS_PTAX = {"value": [
    {"cotacaoCompra": 5.5701, "cotacaoVenda": 5.5707, "dataHoraCotacao": "2026-11-06 13:04:00.100"},
    {"cotacaoCompra": 5.5834, "cotacaoVenda": 5.584, "dataHoraCotacao": "2026-11-10 13:06:01.353"},
]}


@pytest.fixture(autouse=True)
def sem_cache():
    cotacao._cache.clear()
    yield
    cotacao._cache.clear()


@respx.mock
def test_usa_a_ultima_ptax_publicada_na_janela():
    respx.get(host=BANCO_CENTRAL).mock(return_value=httpx.Response(200, json=DUAS_PTAX))
    v = asyncio.run(cotacao.ptax(date(2026, 11, 11)))  # 11/11 de manhã: ainda vale a de 10/11
    assert v["venda"] == 5.584 and v["data"] == "2026-11-10"


@respx.mock
def test_cache_evita_consultar_o_banco_central_a_cada_acesso():
    rota = respx.get(host=BANCO_CENTRAL).mock(return_value=httpx.Response(200, json=DUAS_PTAX))
    asyncio.run(cotacao.ptax())
    asyncio.run(cotacao.ptax())
    assert rota.call_count == 1


@respx.mock
def test_sem_ptax_na_janela():
    respx.get(host=BANCO_CENTRAL).mock(return_value=httpx.Response(200, json={"value": []}))
    with pytest.raises(cotacao.CotacaoIndisponivel, match="nenhuma PTAX"):
        asyncio.run(cotacao.ptax())


@respx.mock
def test_banco_central_fora_do_ar():
    respx.get(host=BANCO_CENTRAL).mock(side_effect=httpx.ConnectTimeout("lento"))
    with pytest.raises(cotacao.CotacaoIndisponivel, match="não respondeu"):
        asyncio.run(cotacao.ptax())


def test_rota_devolve_a_cotacao(monkeypatch):
    async def ok():
        return {"venda": 5.584, "compra": 5.5834, "data": "2026-11-10", "fonte": "PTAX"}
    monkeypatch.setattr(cotacao, "ptax", ok)
    r = TestClient(main.app).get("/cotacao")
    assert r.status_code == 200 and r.json()["venda"] == 5.584


def test_rota_devolve_503_com_codigo_quando_indisponivel(monkeypatch):
    async def fora():
        raise cotacao.CotacaoIndisponivel("o Banco Central não respondeu (ConnectTimeout)")
    monkeypatch.setattr(cotacao, "ptax", fora)
    r = TestClient(main.app, raise_server_exceptions=False).get("/cotacao")
    corpo = r.json()["detail"]
    assert r.status_code == 503 and corpo["codigo"] == "COTACAO_INDISPONIVEL" and corpo["mensagem"]
