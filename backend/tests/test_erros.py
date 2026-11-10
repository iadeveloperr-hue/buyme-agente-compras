"""Cada falha prevista sai com o seu código e uma mensagem, nunca muda.
Sem LLM, sem MCP e sem banco: o agente e o pool são trocados por dublês."""
import asyncio
from contextlib import asynccontextmanager

import agente
import httpx
import main
import pytest
from fastapi.testclient import TestClient

PEDIDO = {"pedido": "3 laptops até US$ 2.000 cada"}
LAPTOP = {"id": 78, "titulo": "MacBook Pro 14", "preco_usd": 1999.99, "estoque": 24}


@pytest.fixture
def cliente():
    # Sem "with": o lifespan (que abriria o pool do Postgres) não roda.
    return TestClient(main.app, raise_server_exceptions=False)


def agente_que(monkeypatch, comportamento):
    async def falso(_pedido):
        if isinstance(comportamento, BaseException):
            raise comportamento
        return comportamento
    monkeypatch.setattr(agente, "executar", falso)


def detalhe(r):
    corpo = r.json()["detail"]
    assert corpo["mensagem"], "toda falha precisa de mensagem para a pessoa"
    return corpo["codigo"]


@pytest.mark.parametrize("excecao, status, codigo", [
    (agente.FalhaLLM("a chave da OpenRouter é inválida ou expirou"), 502, "LLM_INDISPONIVEL"),
    (agente.RespostaInvalida("o modelo não devolveu o JSON final"), 502, "RESPOSTA_INVALIDA"),
    (httpx.ConnectError("recusada"), 503, "FERRAMENTAS_INDISPONIVEIS"),
    (RuntimeError("bug que ninguém previu"), 500, "ERRO_INTERNO"),
])
def test_falhas_do_agente_tem_codigo_e_mensagem(cliente, monkeypatch, excecao, status, codigo):
    agente_que(monkeypatch, excecao)
    r = cliente.post("/requisicoes", json=PEDIDO)
    assert r.status_code == status and detalhe(r) == codigo


def test_agente_lento_estoura_o_prazo(cliente, monkeypatch):
    async def lento(_pedido):
        await asyncio.sleep(5)
    monkeypatch.setattr(agente, "executar", lento)
    monkeypatch.setattr(main, "AGENTE_TIMEOUT_S", 0.05)
    r = cliente.post("/requisicoes", json=PEDIDO)
    assert r.status_code == 504 and detalhe(r) == "TEMPO_ESGOTADO"


def test_catalogo_fora_do_ar(cliente, monkeypatch):
    agente_que(monkeypatch, agente.Resultado(itens=[], resposta="", catalogo_indisponivel=True))
    r = cliente.post("/requisicoes", json=PEDIDO)
    assert r.status_code == 503 and detalhe(r) == "CATALOGO_INDISPONIVEL"


def test_nada_atende_responde_mesmo_sem_texto_do_modelo(cliente, monkeypatch):
    agente_que(monkeypatch, agente.Resultado(itens=[], resposta=""))
    r = cliente.post("/requisicoes", json=PEDIDO)
    corpo = r.json()
    assert r.status_code == 201 and corpo["atendido"] is False and corpo["resposta"]


def test_item_mal_formado_do_modelo(cliente, monkeypatch):
    agente_que(monkeypatch, agente.Resultado(itens=[{"quantidade": 3}], resposta="ok",
                                             vistos={78: LAPTOP}))
    r = cliente.post("/requisicoes", json=PEDIDO)
    assert r.status_code == 422 and detalhe(r) == "ESCOLHA_INVALIDA"


def test_banco_fora_do_ar_ao_gravar(cliente, monkeypatch):
    agente_que(monkeypatch, agente.Resultado(itens=[{"produto_id": 78, "quantidade": 3}],
                                             resposta="ok", vistos={78: LAPTOP}))

    class PoolQuebrado:
        @asynccontextmanager
        async def acquire(self):
            raise ConnectionRefusedError("postgres fora")
            yield

    main.app.state.pool = PoolQuebrado()
    r = cliente.post("/requisicoes", json=PEDIDO)
    assert r.status_code == 503 and detalhe(r) == "BANCO_INDISPONIVEL"
    r = cliente.get("/requisicoes")
    assert r.status_code == 503 and detalhe(r) == "BANCO_INDISPONIVEL"


@pytest.mark.parametrize("corpo", [{"pedido": ""}, {}, {"pedido": "x" * 1001}])
def test_pedido_invalido(cliente, corpo):
    r = cliente.post("/requisicoes", json=corpo)
    assert r.status_code == 422 and detalhe(r) == "PEDIDO_INVALIDO"


def test_motivo_da_falha_da_openrouter_por_codigo_http():
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    from openai import APIStatusError
    for status, trecho in [(401, "expirou"), (402, "sem crédito"), (429, "limite")]:
        e = APIStatusError("x", response=httpx.Response(status, request=req), body=None)
        assert trecho in agente.motivo_falha_llm(e)
