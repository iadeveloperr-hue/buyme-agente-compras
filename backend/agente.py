"""O agente: um loop de tool calling escrito à mão (sem framework).

O código não decide a ordem das chamadas. Ele só (1) entrega ao modelo as ferramentas
que o servidor MCP anuncia, (2) executa as que o modelo pedir e (3) devolve o resultado,
até o modelo responder com o JSON final."""
import json
import os
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import AsyncOpenAI

MCP_URL = os.getenv("MCP_URL", "http://mcp-server:8001/mcp")
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
MAX_PASSOS = 8

PROMPT = """Você é um agente de compras corporativas. A pessoa descreve o que precisa e você
monta uma requisição de compra usando SOMENTE produtos do catálogo.

Como trabalhar:
- O catálogo está em inglês: busque com termos em inglês (ex.: "notebook" -> "laptop").
- Se a primeira busca não trouxer nada, tente sinônimos antes de desistir.
- Use detalhar_produto só com ids que vieram de uma busca, quando precisar confirmar
  estoque ou detalhes antes de escolher. Nunca tente ids aleatórios para explorar o catálogo.
- Respeite quantidade, orçamento (os preços estão em USD) e qualquer outra restrição do pedido.
- Entre opções que atendem, prefira a de melhor custo-benefício e explique em uma frase.

Regras que você não pode quebrar:
- Nunca invente produto, id, preço ou estoque. Só use o que as ferramentas devolveram.
- Se nada atender (não existe, estoque insuficiente, acima do orçamento), NÃO escolha
  algo parecido para fechar o pedido: devolva itens vazio e diga o que faltou.
- Busca com lista vazia significa que o catálogo NÃO TEM esse produto (não é falha):
  diga que o item não existe no catálogo. Só fale em indisponibilidade se uma
  ferramenta devolver CATALOGO_INDISPONIVEL; nesse caso, pare e informe isso.

Quando terminar, responda APENAS com este JSON:
{"itens": [{"produto_id": <int>, "quantidade": <int>}],
 "resposta": "<texto curto em português para a pessoa: o que escolheu e por quê, ou o que faltou>"}"""


@dataclass
class Resultado:
    itens: list[dict]
    resposta: str
    vistos: dict[int, dict] = field(default_factory=dict)
    ferramentas: list[dict] = field(default_factory=list)
    catalogo_indisponivel: bool = False


class FalhaLLM(Exception):
    """Não deu para falar com o modelo (rede, chave, provedor)."""


class RespostaInvalida(Exception):
    """O modelo respondeu, mas não no formato combinado, ou não concluiu."""


def ferramentas_openai(tools) -> list[dict]:
    """Converte o anúncio de ferramentas do MCP para o formato de function calling."""
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description or "",
                          "parameters": t.inputSchema}} for t in tools]


def registrar_vistos(conteudo: str, vistos: dict[int, dict]) -> None:
    """Guarda todo produto que o catálogo devolveu: é a lista do que o modelo PODE escolher."""
    try:
        dado = json.loads(conteudo)
    except json.JSONDecodeError:
        return
    lista = dado.get("result", dado) if isinstance(dado, dict) else dado
    for p in lista if isinstance(lista, list) else [lista]:
        if isinstance(p, dict) and "id" in p and "preco_usd" in p:
            vistos[int(p["id"])] = p


def id_sem_busca(nome: str, args: dict, vistos: dict[int, dict]) -> str | None:
    """Nos testes o modelo chutava ids (1..5, 101..104) para "explorar" o catálogo, mesmo
    com o prompt proibindo. A regra fica no código: só detalha id que veio de uma busca."""
    if nome != "detalhar_produto":
        return None
    try:
        pid = int(args.get("id"))
    except (TypeError, ValueError):
        return "RECUSADO: id inválido."
    if pid not in vistos:
        return (f"RECUSADO: o id {pid} não veio de nenhuma busca. "
                "Use buscar_produtos primeiro e detalhe só ids do resultado.")
    return None


def extrair_json(texto: str) -> dict:
    """Lê o JSON final mesmo que o modelo o embrulhe em texto ou em bloco ```json."""
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise ValueError("sem JSON")
    return json.loads(texto[inicio:fim + 1])


async def executar(pedido: str) -> Resultado:
    """O cliente MCP roda dentro de um TaskGroup, que embrulha qualquer erro num
    ExceptionGroup. Aqui desembrulhamos para o backend ver o erro original."""
    try:
        return await _executar(pedido)
    except BaseExceptionGroup as grupo:
        raise _primeiro(grupo) from grupo


def _primeiro(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    return e


async def _executar(pedido: str) -> Resultado:
    chave = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise FalhaLLM("OPENROUTER_API_KEY não está configurada no .env")
    llm = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=chave, timeout=60)
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = ferramentas_openai((await mcp.list_tools()).tools)
            msgs = [{"role": "system", "content": PROMPT}, {"role": "user", "content": pedido}]
            res = Resultado(itens=[], resposta="")
            pediu_formato = False

            for _ in range(MAX_PASSOS):
                try:
                    r = await llm.chat.completions.create(
                        model=MODELO, messages=msgs, tools=tools, temperature=0)
                except Exception as e:
                    raise FalhaLLM(f"falha ao chamar o modelo na OpenRouter ({type(e).__name__})") from e
                msg = r.choices[0].message

                if not msg.tool_calls:  # o modelo decidiu que terminou
                    try:
                        final = extrair_json(msg.content or "")
                    except ValueError:  # JSONDecodeError é subclasse de ValueError
                        if pediu_formato:
                            raise RespostaInvalida("o modelo não devolveu o JSON final") from None
                        # Uma segunda chance: às vezes ele conclui em texto livre.
                        pediu_formato = True
                        msgs.append({"role": "assistant", "content": msg.content or ""})
                        msgs.append({"role": "user", "content":
                                     "Responda agora APENAS com o JSON final no formato pedido."})
                        continue
                    res.itens = final.get("itens") or []
                    res.resposta = final.get("resposta", "")
                    return res

                msgs.append(msg.model_dump(exclude_none=True))
                for call in msg.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    recusa = id_sem_busca(call.function.name, args, res.vistos)
                    if recusa:  # não chega ao catálogo; o modelo recebe o motivo e segue
                        res.ferramentas.append({"nome": call.function.name, "argumentos": args,
                                                "erro": recusa})
                        msgs.append({"role": "tool", "tool_call_id": call.id, "content": recusa})
                        continue
                    out = await mcp.call_tool(call.function.name, args)
                    texto = "\n".join(c.text for c in out.content if hasattr(c, "text"))
                    if out.isError and "CATALOGO_INDISPONIVEL" in texto:
                        res.catalogo_indisponivel = True
                    if not out.isError:  # uma lista chega como um bloco de texto por produto
                        for c in out.content:
                            if hasattr(c, "text"):
                                registrar_vistos(c.text, res.vistos)
                    res.ferramentas.append({"nome": call.function.name, "argumentos": args,
                                            "erro": texto if out.isError else None})
                    msgs.append({"role": "tool", "tool_call_id": call.id, "content": texto})

            raise RespostaInvalida(f"o agente não concluiu em {MAX_PASSOS} passos")
