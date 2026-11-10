import os
from contextlib import asynccontextmanager

import agente
import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from validacao import EscolhaInvalida, montar_itens, total

DB_URL = (f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}"
          f"@{os.getenv('POSTGRES_HOST', 'db')}:5432/{os.getenv('POSTGRES_DB')}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)
    yield
    await app.state.pool.close()


app = FastAPI(title="Agente de compras", lifespan=lifespan)


class Pedido(BaseModel):
    pedido: str = Field(min_length=3, max_length=1000)


def _erro(status: int, codigo: str, mensagem: str, ferramentas=None):
    raise HTTPException(status, {"codigo": codigo, "mensagem": mensagem,
                                 "ferramentas": ferramentas or []})


def _item_json(i) -> dict:
    return {"produto_id": i.produto_id, "titulo": i.titulo, "quantidade": i.quantidade,
            "preco_unitario_usd": float(i.preco_unitario_usd), "subtotal_usd": float(i.subtotal_usd)}


@app.post("/requisicoes", status_code=201)
async def criar(p: Pedido):
    try:
        r = await agente.executar(p.pedido)
    except agente.FalhaLLM as e:
        _erro(502, "LLM_INDISPONIVEL", f"Não consegui falar com o modelo de IA: {e}. Tente de novo.")
    except agente.RespostaInvalida as e:
        _erro(502, "RESPOSTA_INVALIDA", f"O agente não conseguiu concluir o pedido ({e}). "
              "Nenhuma requisição foi criada; tente reformular.")
    except Exception as e:  # servidor MCP fora do ar, por exemplo
        _erro(503, "FERRAMENTAS_INDISPONIVEIS",
              f"O serviço de catálogo (MCP) não respondeu ({type(e).__name__}). Tente de novo em instantes.")

    if r.catalogo_indisponivel and not r.itens:
        _erro(503, "CATALOGO_INDISPONIVEL",
              "O catálogo de produtos (DummyJSON) está fora do ar ou lento. "
              "Nenhuma requisição foi criada; tente de novo em instantes.", r.ferramentas)

    if not r.itens:  # o agente disse o que faltou; nada é gravado
        return {"requisicao": None, "atendido": False, "resposta": r.resposta,
                "ferramentas": r.ferramentas}

    try:
        itens = montar_itens(r.itens, r.vistos)
    except EscolhaInvalida as e:
        _erro(422, "ESCOLHA_INVALIDA", f"O agente escolheu algo que não confere com o catálogo ({e}). "
              "Nenhuma requisição foi criada.", r.ferramentas)

    valor = total(itens)
    async with app.state.pool.acquire() as con, con.transaction():
        req_id = await con.fetchval(
            "INSERT INTO requisicoes (pedido, resumo, total_usd) VALUES ($1, $2, $3) RETURNING id",
            p.pedido, r.resposta, valor)
        await con.executemany(
            "INSERT INTO requisicao_itens (requisicao_id, produto_id, titulo, quantidade,"
            " preco_unitario_usd, subtotal_usd) VALUES ($1, $2, $3, $4, $5, $6)",
            [(req_id, i.produto_id, i.titulo, i.quantidade, i.preco_unitario_usd, i.subtotal_usd)
             for i in itens])

    return {"requisicao": {"id": req_id, "total_usd": float(valor),
                           "itens": [_item_json(i) for i in itens]},
            "atendido": True, "resposta": r.resposta, "ferramentas": r.ferramentas}


@app.get("/requisicoes")
async def listar():
    async with app.state.pool.acquire() as con:
        reqs = await con.fetch("SELECT * FROM requisicoes ORDER BY id DESC LIMIT 100")
        itens = await con.fetch("SELECT * FROM requisicao_itens WHERE requisicao_id = ANY($1::int[])"
                                " ORDER BY id", [r["id"] for r in reqs])
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
