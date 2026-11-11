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
    async def falso(*_args):
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
    async def lento(*_args):
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


@pytest.mark.parametrize("historico", [[{"autor": "pessoa", "texto": "x"}] * 21,
                                       [{"autor": "robo", "texto": "oi"}],
                                       [{"autor": "pessoa", "texto": ""}]])
def test_historico_invalido(cliente, historico):
    r = cliente.post("/requisicoes", json={"pedido": "3 laptops", "historico": historico})
    assert r.status_code == 422 and detalhe(r) == "PEDIDO_INVALIDO"
    assert "histórico" in r.json()["detail"]["mensagem"]


class PoolQueGrava:
    """Dublê do Postgres que guarda o que seria gravado."""
    def __init__(self):
        self.requisicao, self.itens = None, None

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, _sql, *args):
        self.requisicao = args
        return 99

    async def executemany(self, _sql, linhas):
        self.itens = linhas


LAPTOPS = {78: {**LAPTOP, "categoria": "laptops", "pedido_minimo": 1},
           81: {"id": 81, "titulo": "Lenovo Yoga 920", "preco_usd": 1099.99, "estoque": 40,
                "categoria": "laptops", "pedido_minimo": 1},
           100: {"id": 100, "titulo": "Apple Airpods", "preco_usd": 129.99, "estoque": 67,
                 "categoria": "mobile-accessories", "pedido_minimo": 4}}


def test_conversa_vai_ao_agente_e_o_pedido_gravado_e_o_que_a_pessoa_escreveu(cliente, monkeypatch):
    recebido = {}

    async def falso(pedido, historico):
        recebido.update(pedido=pedido, historico=historico)
        return agente.Resultado(itens=[{"produto_id": 81, "quantidade": 3}], resposta="ok",
                                vistos=LAPTOPS, orcamento={"moeda": "USD", "max_unitario": 2000})
    monkeypatch.setattr(agente, "executar", falso)
    main.app.state.pool = pool = PoolQueGrava()
    historico = [{"autor": "pessoa", "texto": "Preciso de laptops para a equipe."},
                 {"autor": "agente", "texto": "Quantos, e até quanto cada?"}]
    r = cliente.post("/requisicoes", json={"pedido": "3, até US$ 2.000 cada.", "historico": historico})
    assert r.status_code == 201
    assert recebido == {"pedido": "3, até US$ 2.000 cada.", "historico": historico}
    assert pool.requisicao[0] == "Preciso de laptops para a equipe.\n3, até US$ 2.000 cada."
    # As opções que também cabem: mesma categoria e dentro do limite (o MacBook); AirPods não.
    alts = r.json()["alternativas"]
    assert [(a["produto_id"], a["diferenca_no_pedido_usd"]) for a in alts] == [(78, 2700.0)]


def test_reais_na_conversa_nao_viram_compra_com_um_pode_seguir(cliente, monkeypatch):
    agente_que(monkeypatch, agente.Resultado(itens=[{"produto_id": 81, "quantidade": 2}],
                                             resposta="ok", vistos=LAPTOPS))
    main.app.state.pool = PoolQueGrava()
    historico = [{"autor": "pessoa", "texto": "Preciso de 2 laptops até R$ 7.000 cada."},
                 {"autor": "agente", "texto": "O catálogo é em dólar; informe o valor em US$."}]
    r = cliente.post("/requisicoes", json={"pedido": "Pode seguir assim mesmo.", "historico": historico})
    assert r.status_code == 422 and detalhe(r) == "ESCOLHA_INVALIDA"


def test_motivo_da_falha_da_openrouter_por_codigo_http():
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    from openai import APIStatusError
    for status, trecho in [(401, "expirou"), (402, "sem crédito"), (429, "limite")]:
        e = APIStatusError("x", response=httpx.Response(status, request=req), body=None)
        assert trecho in agente.motivo_falha_llm(e)


def test_sem_limite_no_json_a_lista_de_opcoes_nao_sai_sem_teto(cliente, monkeypatch):
    relogios = {7: {"id": 7, "titulo": "Women's Wrist Watch", "preco_usd": 129.99, "estoque": 12,
                    "categoria": "womens-watches", "pedido_minimo": 5},
                9: {"id": 9, "titulo": "Rolex Cellini Moonphase", "preco_usd": 15999.99, "estoque": 9,
                    "categoria": "womens-watches", "pedido_minimo": 1}}
    agente_que(monkeypatch, agente.Resultado(itens=[{"produto_id": 7, "quantidade": 5}], resposta="ok",
                                             vistos=relogios, orcamento=None))
    main.app.state.pool = PoolQueGrava()
    r = cliente.post("/requisicoes", json={"pedido": "5 relógios de até US$ 150 cada e 3 laptops"})
    assert r.status_code == 201 and r.json()["alternativas"] == []
