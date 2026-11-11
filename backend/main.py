import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal

import agente
import asyncpg
import cotacao
import httpx
from erros import erro
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field
from validacao import (
    EscolhaInvalida,
    alternativas,
    citou_valor_em_dolar,
    conferir_moeda_da_conversa,
    conferir_orcamento,
    montar_itens,
    total,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agente")

DB_URL = (f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}"
          f"@{os.getenv('POSTGRES_HOST', 'db')}:5432/{os.getenv('POSTGRES_DB')}")

# Prazo total do agente. Fica abaixo do timeout do nginx e do navegador (120 s), para que
# quem desiste primeiro seja o backend, com uma mensagem clara, e não a conexão.
AGENTE_TIMEOUT_S = float(os.getenv("AGENTE_TIMEOUT_S", "100"))

# Falhas de rede ao falar com o servidor MCP (fora do ar, recusou conexão, caiu no meio).
ERROS_MCP = (httpx.HTTPError, McpError, OSError)
# Falhas do Postgres: fora do ar, conexão perdida, erro de SQL.
ERROS_BANCO = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


TENTATIVAS_BANCO = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    # O healthcheck do compose já espera o Postgres; isto é a segunda proteção, para o caso de
    # o banco ainda estar terminando de subir. Cada tentativa vai para o log; se as 10 falharem,
    # o backend não sobe e o erro fica no log do container: falha alta, não silenciosa.
    for tentativa in range(1, TENTATIVAS_BANCO + 1):
        try:
            app.state.pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
            break
        except ERROS_BANCO as e:
            if tentativa == TENTATIVAS_BANCO:
                raise
            log.warning("Postgres ainda não aceita conexão (tentativa %s/%s): %s",
                        tentativa, TENTATIVAS_BANCO, e)
            await asyncio.sleep(1)
    yield
    await app.state.pool.close()


app = FastAPI(title="BuyME · agente de compras", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def pedido_invalido(_: Request, exc: RequestValidationError):
    log.warning("PEDIDO_INVALIDO: %s", exc.errors())
    no_historico = any(len(e.get("loc", ())) > 1 and e["loc"][1] == "historico" for e in exc.errors())
    mensagem = ("O histórico da conversa veio num formato inválido. Comece uma nova conversa."
                if no_historico else "Escreva o pedido com 3 a 1000 caracteres.")
    return JSONResponse(status_code=422, content={"detail": {
        "codigo": "PEDIDO_INVALIDO", "mensagem": mensagem, "ferramentas": []}})


@app.exception_handler(Exception)
async def erro_inesperado(_: Request, exc: Exception):
    # Rede de segurança: um erro que não previmos ainda sai com código, mensagem e stack no log.
    log.exception("ERRO_INTERNO: %s", exc)
    return JSONResponse(status_code=500, content={"detail": {
        "codigo": "ERRO_INTERNO",
        "mensagem": f"Erro inesperado no servidor ({type(exc).__name__}). Nenhuma requisição foi criada.",
        "ferramentas": []}})


class Turno(BaseModel):
    autor: Literal["pessoa", "agente"]
    texto: str = Field(min_length=1, max_length=4000)


class Pedido(BaseModel):
    pedido: str = Field(min_length=3, max_length=1000)
    # A conversa anterior, enviada pelo chat. É só contexto para o modelo: preço, estoque,
    # pedido mínimo e moeda continuam conferidos aqui, contra o catálogo.
    historico: list[Turno] = Field(default_factory=list, max_length=20)


def _item_json(i) -> dict:
    return {"produto_id": i.produto_id, "titulo": i.titulo, "quantidade": i.quantidade,
            "preco_unitario_usd": float(i.preco_unitario_usd), "subtotal_usd": float(i.subtotal_usd),
            "prazo_envio": i.prazo_envio}


@app.post("/requisicoes", status_code=201)
async def criar(p: Pedido):
    historico = [t.model_dump() for t in p.historico][-agente.MAX_HISTORICO:]
    da_pessoa = agente.mensagens_da_pessoa(p.pedido, historico)
    try:
        async with asyncio.timeout(AGENTE_TIMEOUT_S):
            r = await agente.executar(p.pedido, historico)
    except TimeoutError as e:
        raise erro(504, "TEMPO_ESGOTADO", f"O agente não concluiu em {AGENTE_TIMEOUT_S:.0f} s. "
                   "Nenhuma requisição foi criada; tente de novo.", causa=e) from e
    except agente.FalhaLLM as e:
        raise erro(502, "LLM_INDISPONIVEL", f"Não consegui usar o modelo de IA: {e}. "
                   "Nenhuma requisição foi criada.", causa=e) from e
    except agente.RespostaInvalida as e:
        raise erro(502, "RESPOSTA_INVALIDA", f"O agente não conseguiu concluir o pedido ({e}). "
                   "Nenhuma requisição foi criada; tente reformular.", causa=e) from e
    except ERROS_MCP as e:
        raise erro(503, "FERRAMENTAS_INDISPONIVEIS", f"O serviço de catálogo (MCP) não respondeu "
                   f"({type(e).__name__}). Nenhuma requisição foi criada; tente de novo.", causa=e) from e
    # Qualquer outro erro sobe para o erro_inesperado (500 ERRO_INTERNO), com stack no log.

    if r.catalogo_indisponivel and not r.itens:
        raise erro(503, "CATALOGO_INDISPONIVEL",
                   "O catálogo de produtos (DummyJSON) está fora do ar ou lento. "
                   "Nenhuma requisição foi criada; tente de novo em instantes.", r.ferramentas)

    if not r.itens:  # o agente disse o que faltou; nada é gravado
        log.info("pedido não atendido: %s", r.resposta)
        return {"requisicao": None, "atendido": False, "ferramentas": r.ferramentas,
                "resposta": r.resposta or "Não encontrei no catálogo nada que atenda a este pedido."}

    try:  # tudo o que envolve dinheiro é conferido aqui, não pelo modelo (validacao.py)
        conferir_moeda_da_conversa(da_pessoa)
        itens = montar_itens(r.itens, r.vistos)
        conferir_orcamento(itens, r.orcamento)
    except EscolhaInvalida as e:
        raise erro(422, "ESCOLHA_INVALIDA", f"O agente escolheu algo que não confere com o catálogo "
                   f"({e}). Nenhuma requisição foi criada.", r.ferramentas) from e

    valor = total(itens)
    try:
        async with app.state.pool.acquire() as con, con.transaction():
            req_id = await con.fetchval(
                "INSERT INTO requisicoes (pedido, resumo, total_usd) VALUES ($1, $2, $3) RETURNING id",
                "\n".join(da_pessoa), r.resposta, valor)  # numa conversa, tudo o que a pessoa pediu
            await con.executemany(
                "INSERT INTO requisicao_itens (requisicao_id, produto_id, titulo, quantidade,"
                " preco_unitario_usd, subtotal_usd, prazo_envio) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                [(req_id, i.produto_id, i.titulo, i.quantidade, i.preco_unitario_usd, i.subtotal_usd,
                  i.prazo_envio) for i in itens])
    except ERROS_BANCO as e:  # a transação garante: ou grava requisição e itens, ou nada
        raise erro(503, "BANCO_INDISPONIVEL", "O agente montou a requisição, mas não consegui gravá-la "
                   "no banco. Nada foi salvo; tente de novo.", r.ferramentas, causa=e) from e

    log.info("requisição %s criada: %s itens, US$ %s", req_id, len(itens), valor)
    # As opções que também cabem são montadas pelo código a partir do catálogo. Se a pessoa deu
    # valor em dólar mas o limite não chegou ao JSON (limites diferentes por item), a lista sairia
    # sem teto (um Rolex de US$ 16 mil "cabendo" em relógios até US$ 150): nesse caso, não sai.
    orc = r.orcamento if isinstance(r.orcamento, dict) else {}
    limite_conhecido = bool(orc.get("max_unitario") or orc.get("max_total"))
    opcoes = (alternativas(itens, r.vistos, r.orcamento)
              if limite_conhecido or not citou_valor_em_dolar(da_pessoa) else [])
    return {"requisicao": {"id": req_id, "total_usd": float(valor),
                           "itens": [_item_json(i) for i in itens]},
            "atendido": True, "resposta": r.resposta, "ferramentas": r.ferramentas,
            "alternativas": opcoes}


@app.get("/requisicoes")
async def listar():
    try:
        async with app.state.pool.acquire() as con:
            reqs = await con.fetch("SELECT * FROM requisicoes ORDER BY id DESC LIMIT 100")
            itens = await con.fetch("SELECT * FROM requisicao_itens WHERE requisicao_id = ANY($1::int[])"
                                    " ORDER BY id", [r["id"] for r in reqs])
    except ERROS_BANCO as e:
        raise erro(503, "BANCO_INDISPONIVEL", "Não consegui ler as requisições do banco. "
                   "Tente de novo.", causa=e) from e
    por_req: dict[int, list] = {}
    for i in itens:
        por_req.setdefault(i["requisicao_id"], []).append(
            {"produto_id": i["produto_id"], "titulo": i["titulo"], "quantidade": i["quantidade"],
             "preco_unitario_usd": float(i["preco_unitario_usd"]),
             "subtotal_usd": float(i["subtotal_usd"]), "prazo_envio": i["prazo_envio"]})
    return [{"id": r["id"], "pedido": r["pedido"], "resumo": r["resumo"],
             "total_usd": float(r["total_usd"]), "criada_em": r["criada_em"].isoformat(),
             "itens": por_req.get(r["id"], [])} for r in reqs]


@app.get("/cotacao")
async def cotacao_dolar():
    """PTAX do dia, só como referência para o topo do chat (ver cotacao.py)."""
    try:
        return await cotacao.ptax()
    except cotacao.CotacaoIndisponivel as e:
        raise erro(503, "COTACAO_INDISPONIVEL", f"Cotação do dólar indisponível agora ({e}). "
                   "Os pedidos funcionam normalmente.", causa=e) from e


@app.get("/saude")
async def saude():
    return {"ok": True}
