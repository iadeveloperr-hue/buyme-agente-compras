import asyncio
import logging
import os
from contextlib import asynccontextmanager

import agente
import asyncpg
import httpx
from erros import erro
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.shared.exceptions import McpError
from pydantic import BaseModel, Field
from validacao import EscolhaInvalida, montar_itens, total

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Se o banco não sobe, o backend não sobe: falha alta no log do container, não silenciosa.
    app.state.pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    yield
    await app.state.pool.close()


app = FastAPI(title="Agente de compras", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def pedido_invalido(_: Request, exc: RequestValidationError):
    log.warning("PEDIDO_INVALIDO: %s", exc.errors())
    return JSONResponse(status_code=422, content={"detail": {
        "codigo": "PEDIDO_INVALIDO",
        "mensagem": "Escreva o pedido com 3 a 1000 caracteres.", "ferramentas": []}})


@app.exception_handler(Exception)
async def erro_inesperado(_: Request, exc: Exception):
    # Rede de segurança: um erro que não previmos ainda sai com código, mensagem e stack no log.
    log.exception("ERRO_INTERNO: %s", exc)
    return JSONResponse(status_code=500, content={"detail": {
        "codigo": "ERRO_INTERNO",
        "mensagem": f"Erro inesperado no servidor ({type(exc).__name__}). Nenhuma requisição foi criada.",
        "ferramentas": []}})


class Pedido(BaseModel):
    pedido: str = Field(min_length=3, max_length=1000)


def _item_json(i) -> dict:
    return {"produto_id": i.produto_id, "titulo": i.titulo, "quantidade": i.quantidade,
            "preco_unitario_usd": float(i.preco_unitario_usd), "subtotal_usd": float(i.subtotal_usd)}


@app.post("/requisicoes", status_code=201)
async def criar(p: Pedido):
    try:
        async with asyncio.timeout(AGENTE_TIMEOUT_S):
            r = await agente.executar(p.pedido)
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

    try:
        itens = montar_itens(r.itens, r.vistos)
    except EscolhaInvalida as e:
        raise erro(422, "ESCOLHA_INVALIDA", f"O agente escolheu algo que não confere com o catálogo "
                   f"({e}). Nenhuma requisição foi criada.", r.ferramentas) from e

    valor = total(itens)
    try:
        async with app.state.pool.acquire() as con, con.transaction():
            req_id = await con.fetchval(
                "INSERT INTO requisicoes (pedido, resumo, total_usd) VALUES ($1, $2, $3) RETURNING id",
                p.pedido, r.resposta, valor)
            await con.executemany(
                "INSERT INTO requisicao_itens (requisicao_id, produto_id, titulo, quantidade,"
                " preco_unitario_usd, subtotal_usd) VALUES ($1, $2, $3, $4, $5, $6)",
                [(req_id, i.produto_id, i.titulo, i.quantidade, i.preco_unitario_usd, i.subtotal_usd)
                 for i in itens])
    except ERROS_BANCO as e:  # a transação garante: ou grava requisição e itens, ou nada
        raise erro(503, "BANCO_INDISPONIVEL", "O agente montou a requisição, mas não consegui gravá-la "
                   "no banco. Nada foi salvo; tente de novo.", r.ferramentas, causa=e) from e

    log.info("requisição %s criada: %s itens, US$ %s", req_id, len(itens), valor)
    return {"requisicao": {"id": req_id, "total_usd": float(valor),
                           "itens": [_item_json(i) for i in itens]},
            "atendido": True, "resposta": r.resposta, "ferramentas": r.ferramentas}


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
             "subtotal_usd": float(i["subtotal_usd"])})
    return [{"id": r["id"], "pedido": r["pedido"], "resumo": r["resumo"],
             "total_usd": float(r["total_usd"]), "criada_em": r["criada_em"].isoformat(),
             "itens": por_req.get(r["id"], [])} for r in reqs]


@app.get("/saude")
async def saude():
    return {"ok": True}
