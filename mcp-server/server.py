"""Servidor MCP do catálogo: expõe a DummyJSON como ferramentas para o agente."""
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

DUMMYJSON_URL = os.getenv("DUMMYJSON_URL", "https://dummyjson.com")
TIMEOUT_S = float(os.getenv("DUMMYJSON_TIMEOUT_S", "5"))

mcp = FastMCP("catalogo", host="0.0.0.0", port=int(os.getenv("PORT", "8001")))
log = logging.getLogger("catalogo")


def _falha(msg: str, causa: BaseException | None = None) -> ToolError:
    """Toda falha vira ToolError com prefixo fixo (o backend lê o prefixo) e fica no log."""
    log.warning(msg, exc_info=causa)
    return ToolError(msg)


def _resumo(p: dict) -> dict:
    """Só o que o agente precisa para escolher: menos tokens, menos chance de confundir."""
    return {
        "id": p["id"],
        "titulo": p.get("title"),
        "marca": p.get("brand"),
        "categoria": p.get("category"),
        "preco_usd": p.get("price"),
        "estoque": p.get("stock"),
        # Compra mínima do fornecedor. Fica já na busca: só no detalhe, o modelo raramente via
        # (achado do QA: requisições de 2 Realme C35, cujo mínimo é 3).
        "pedido_minimo": p.get("minimumOrderQuantity"),
        # Prazo de envio do fornecedor ("Ships in 2 weeks"): volta para a pessoa em cada item,
        # porque dependendo do prazo a compra não faz sentido.
        "prazo_envio": p.get("shippingInformation"),
    }


async def _get(path: str, params: dict | None = None) -> dict:
    # Falha da DummyJSON vira ToolError com texto claro: o agente recebe o erro
    # como resultado da ferramenta e o backend consegue repassar ao chat.
    try:
        async with httpx.AsyncClient(base_url=DUMMYJSON_URL, timeout=TIMEOUT_S) as client:
            resp = await client.get(path, params=params)
    except httpx.TimeoutException as e:
        raise _falha(f"CATALOGO_INDISPONIVEL: a DummyJSON não respondeu em {TIMEOUT_S:.0f}s") from e
    except httpx.HTTPError as e:
        raise _falha(f"CATALOGO_INDISPONIVEL: erro de conexão com a DummyJSON ({type(e).__name__})", e) from e
    if resp.status_code == 404:
        raise _falha(f"PRODUTO_NAO_ENCONTRADO: não existe produto em {path}")
    if resp.status_code >= 400:
        raise _falha(f"CATALOGO_INDISPONIVEL: a DummyJSON respondeu {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:  # ex.: página de erro em HTML de um proxy no caminho
        raise _falha("CATALOGO_INDISPONIVEL: a DummyJSON respondeu algo que não é JSON") from e


@mcp.tool()
async def buscar_produtos(termo: str) -> list[dict]:
    """Busca produtos no catálogo por palavra-chave EM INGLÊS (ex.: 'laptop', 'phone').
    Retorna id, título, marca, categoria, preço em USD, estoque, pedido_minimo (a menor
    quantidade que o fornecedor aceita; qualquer quantidade maior, até o estoque, também serve)
    e prazo_envio (em quanto tempo o fornecedor despacha). Lista vazia = o catálogo não tem o item."""
    # limit=0 traz todos os resultados. Cortar em 20 escondia 3 dos 23 itens de "phone", e o
    # agente podia dizer "não tem" quando tinha. O catálogo inteiro tem ~190 produtos.
    data = await _get("/products/search", {"q": termo, "limit": 0})
    return [_resumo(p) for p in data.get("products", [])]


@mcp.tool()
async def detalhar_produto(id: int) -> dict:
    """Detalha um produto pelo id: descrição, preço, estoque, garantia e avaliação."""
    p = await _get(f"/products/{id}")
    return {
        **_resumo(p),
        "descricao": p.get("description"),
        "garantia": p.get("warrantyInformation"),
        "avaliacao": p.get("rating"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mcp.run(transport="streamable-http")
