"""Servidor MCP do catálogo: expõe a DummyJSON como ferramentas para o agente."""
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

DUMMYJSON_URL = os.getenv("DUMMYJSON_URL", "https://dummyjson.com")
TIMEOUT_S = float(os.getenv("DUMMYJSON_TIMEOUT_S", "5"))

mcp = FastMCP("catalogo", host="0.0.0.0", port=int(os.getenv("PORT", "8001")))


def _resumo(p: dict) -> dict:
    """Só o que o agente precisa para escolher: menos tokens, menos chance de confundir."""
    return {
        "id": p["id"],
        "titulo": p.get("title"),
        "marca": p.get("brand"),
        "categoria": p.get("category"),
        "preco_usd": p.get("price"),
        "estoque": p.get("stock"),
    }


async def _get(path: str, params: dict | None = None) -> dict:
    # Falha da DummyJSON vira ToolError com texto claro: o agente recebe o erro
    # como resultado da ferramenta e o backend consegue repassar ao chat.
    try:
        async with httpx.AsyncClient(base_url=DUMMYJSON_URL, timeout=TIMEOUT_S) as client:
            resp = await client.get(path, params=params)
    except httpx.TimeoutException as e:
        raise ToolError(f"CATALOGO_INDISPONIVEL: a DummyJSON não respondeu em {TIMEOUT_S:.0f}s") from e
    except httpx.HTTPError as e:
        raise ToolError(f"CATALOGO_INDISPONIVEL: erro de conexão com a DummyJSON ({type(e).__name__})") from e
    if resp.status_code == 404:
        raise ToolError("PRODUTO_NAO_ENCONTRADO: não existe produto com esse id")
    if resp.status_code >= 400:
        raise ToolError(f"CATALOGO_INDISPONIVEL: a DummyJSON respondeu {resp.status_code}")
    return resp.json()


@mcp.tool()
async def buscar_produtos(termo: str) -> list[dict]:
    """Busca produtos no catálogo por palavra-chave EM INGLÊS (ex.: 'laptop', 'phone').
    Retorna id, título, marca, categoria, preço em USD e estoque. Lista vazia = nada encontrado."""
    data = await _get("/products/search", {"q": termo, "limit": 20})
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
        "pedido_minimo": p.get("minimumOrderQuantity"),
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
