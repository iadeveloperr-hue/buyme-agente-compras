"""Testes das ferramentas pelo protocolo MCP de verdade (cliente em memÃ³ria).
A DummyJSON Ã© simulada com respx: nÃ£o precisa de rede nem de chave de LLM."""
import json

import httpx
import pytest
import respx
from mcp.shared.memory import create_connected_server_and_client_session
from server import DUMMYJSON_URL, mcp

LAPTOP = {"id": 78, "title": "Apple MacBook Pro 14 Inch Space Grey", "brand": "Apple",
          "category": "laptops", "price": 1999.99, "stock": 24,
          "description": "M1 Pro", "warrantyInformation": "1 year", "rating": 4.6,
          "minimumOrderQuantity": 1}


async def chamar(nome, args):
    async with create_connected_server_and_client_session(mcp._mcp_server) as s:
        r = await s.call_tool(nome, args)
        texto = r.content[0].text if r.content else ""
        return r.isError, r.structuredContent, texto


@pytest.mark.asyncio
@respx.mock
async def test_buscar_produtos_resume_resultado():
    respx.get(f"{DUMMYJSON_URL}/products/search").mock(
        return_value=httpx.Response(200, json={"products": [LAPTOP]}))
    erro, dados, _ = await chamar("buscar_produtos", {"termo": "laptop"})
    assert not erro
    item = dados["result"][0]
    assert item == {"id": 78, "titulo": LAPTOP["title"], "marca": "Apple",
                    "categoria": "laptops", "preco_usd": 1999.99, "estoque": 24}


@pytest.mark.asyncio
@respx.mock
async def test_buscar_produtos_sem_resultado_devolve_lista_vazia():
    respx.get(f"{DUMMYJSON_URL}/products/search").mock(
        return_value=httpx.Response(200, json={"products": []}))
    erro, dados, _ = await chamar("buscar_produtos", {"termo": "trator"})
    assert not erro and dados["result"] == []


@pytest.mark.asyncio
@respx.mock
async def test_detalhar_produto():
    respx.get(f"{DUMMYJSON_URL}/products/78").mock(return_value=httpx.Response(200, json=LAPTOP))
    erro, _, texto = await chamar("detalhar_produto", {"id": 78})
    dados = json.loads(texto)
    assert not erro
    assert dados["garantia"] == "1 year" and dados["preco_usd"] == 1999.99


@pytest.mark.asyncio
@respx.mock
async def test_detalhar_produto_inexistente():
    respx.get(f"{DUMMYJSON_URL}/products/9999").mock(return_value=httpx.Response(404))
    erro, _, texto = await chamar("detalhar_produto", {"id": 9999})
    assert erro and "PRODUTO_NAO_ENCONTRADO" in texto


@pytest.mark.asyncio
@respx.mock
async def test_dummyjson_lenta_vira_erro_claro():
    respx.get(f"{DUMMYJSON_URL}/products/search").mock(side_effect=httpx.ReadTimeout("lenta"))
    erro, _, texto = await chamar("buscar_produtos", {"termo": "laptop"})
    assert erro and "CATALOGO_INDISPONIVEL" in texto


@pytest.mark.asyncio
@respx.mock
async def test_dummyjson_fora_do_ar_vira_erro_claro():
    respx.get(f"{DUMMYJSON_URL}/products/search").mock(return_value=httpx.Response(503))
    erro, _, texto = await chamar("buscar_produtos", {"termo": "laptop"})
    assert erro and "503" in texto

