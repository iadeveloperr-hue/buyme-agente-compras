"""O agente: um loop de tool calling escrito à mão (sem framework).

O código não decide a ordem das chamadas. Ele só (1) entrega ao modelo as ferramentas
que o servidor MCP anuncia, (2) executa as que o modelo pedir e (3) devolve o resultado,
até o modelo responder com o JSON final."""
import json
import logging
import os
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import APIStatusError, AsyncOpenAI
from validacao import EscolhaInvalida, conferir_moeda_do_pedido, conferir_orcamento, montar_itens

log = logging.getLogger("agente")

MCP_URL = os.getenv("MCP_URL", "http://mcp-server:8001/mcp")
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
MAX_PASSOS = 8

PROMPT = """Você é um agente de compras corporativas. A pessoa descreve o que precisa e você
monta uma requisição de compra usando SOMENTE produtos do catálogo.

Como trabalhar:
- O catálogo está em inglês: busque com termos em inglês e no singular (ex.: "notebook" ->
  "laptop", "celular" -> "phone", "relógios" -> "watch"); o singular também acha o plural.
- Se a primeira busca não trouxer nada, tente sinônimos antes de desistir.
- Use detalhar_produto só com ids que vieram de uma busca, quando precisar confirmar
  estoque ou detalhes antes de escolher. Nunca tente ids aleatórios para explorar o catálogo.
- Respeite quantidade, orçamento e qualquer outra restrição do pedido, além do estoque e do
  pedido_minimo de cada produto. pedido_minimo é um piso, não um valor exato: com mínimo 5,
  servem 5, 6, 12 ou qualquer quantidade maior, até o estoque.
- Quantidade é em unidades: N unidades podem ser todas do mesmo modelo, e de preferência são
  (padronização). Só divida entre modelos se nenhum tiver estoque sozinho.
- Entre opções que atendem, escolha você a de melhor custo-benefício e explique em uma frase.
  Se existe uma opção que atende ao pedido exatamente como foi feito, monte a requisição com
  ela: não peça confirmação nem sugira mudar a quantidade.
- Os preços do catálogo estão em dólar: escreva valores sempre como US$.

Orçamento:
- Copie em "orcamento" o limite que a pessoa deu: "até US$ X cada" em max_unitario,
  "orçamento total de US$ X" em max_total. Sem limite no pedido, "orcamento" é null.
- Orçamento em outra moeda (R$, reais, €): não converta, porque não há cotação. Devolva itens
  vazio, copie a moeda (BRL, EUR) em orcamento.moeda e peça o valor em US$.

Regras que você não pode quebrar:
- Nunca invente produto, id, preço ou estoque, nem como exemplo ou sugestão no texto. Só use o
  que as ferramentas devolveram.
- Se nada atender (não existe, estoque insuficiente, abaixo do pedido mínimo, acima do
  orçamento), NÃO escolha algo parecido para fechar o pedido: devolva itens vazio e diga o que
  faltou.
- Nesse caso, se as buscas trouxeram produtos próximos do que a pessoa pediu (outro modelo,
  outra faixa de preço), cite na resposta até 3 deles, com nome e preço, para ela decidir.
  Só cite produtos que as ferramentas devolveram; a requisição continua vazia.
- Se só parte do pedido existir, não monte requisição parcial: diga o que existe e o que falta.
- Busca com lista vazia significa que o catálogo NÃO TEM esse produto (não é falha):
  diga que o item não existe no catálogo. Só fale em indisponibilidade se uma
  ferramenta devolver CATALOGO_INDISPONIVEL; nesse caso, pare e informe isso.
- Cada pedido é independente: a pessoa não responde perguntas nesta conversa. Se faltar algo
  essencial (o que comprar, quantas unidades), devolva itens vazio e diga o que incluir num
  novo pedido.

Toda resposta final, inclusive quando faltar informação ou você recusar o pedido, é APENAS
este JSON:
{"itens": [{"produto_id": <int>, "quantidade": <int>}],
 "orcamento": {"moeda": "USD", "max_unitario": <número ou null>, "max_total": <número ou null>},
 "resposta": "<texto curto em português para a pessoa: o que escolheu e por quê, ou o que faltou>"}"""


@dataclass
class Resultado:
    itens: list[dict]
    resposta: str
    orcamento: dict | None = None   # o limite que o modelo copiou do pedido; o código confere
    vistos: dict[int, dict] = field(default_factory=dict)
    ferramentas: list[dict] = field(default_factory=list)
    catalogo_indisponivel: bool = False


class FalhaLLM(Exception):
    """Não deu para falar com o modelo (rede, chave, provedor)."""


# O que cada resposta de erro da OpenRouter quer dizer para quem está operando o sistema.
MOTIVO_HTTP_LLM = {
    401: "a chave da OpenRouter é inválida ou expirou",
    402: "a chave da OpenRouter está sem crédito",
    403: "a chave da OpenRouter não tem permissão para este modelo",
    404: "o modelo escolhido não está disponível para esta chave (ver OPENROUTER_MODEL)",
    429: "limite de uso da OpenRouter atingido; espere um pouco",
}


def motivo_falha_llm(e: Exception) -> str:
    if isinstance(e, APIStatusError):
        return MOTIVO_HTTP_LLM.get(e.status_code, f"a OpenRouter respondeu {e.status_code}")
    return f"sem resposta da OpenRouter ({type(e).__name__})"


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
        # Não quebra a conversa, mas fica registrado: produto que não foi lido aqui
        # depois aparece como ESCOLHA_INVALIDA, e o log mostra a origem.
        log.warning("resultado de ferramenta que não é JSON: %.200s", conteudo)
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


MAX_CORRECOES = 1


def conferir_escolha(pedido: str, res: "Resultado") -> str | None:
    """As mesmas conferências que o backend faz antes de gravar (validacao.py), rodadas ainda
    dentro do loop, para o modelo poder corrigir. Devolve o motivo da recusa, ou None."""
    try:
        conferir_moeda_do_pedido(pedido)
        conferir_orcamento(montar_itens(res.itens, res.vistos), res.orcamento)
    except EscolhaInvalida as e:
        return str(e)
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
            correcoes = 0

            for _ in range(MAX_PASSOS):
                try:
                    r = await llm.chat.completions.create(
                        model=MODELO, messages=msgs, tools=tools, temperature=0)
                except Exception as e:
                    raise FalhaLLM(motivo_falha_llm(e)) from e
                if not r.choices:
                    raise FalhaLLM("a OpenRouter devolveu uma resposta vazia")
                msg = r.choices[0].message

                if not msg.tool_calls:  # o modelo decidiu que terminou
                    try:
                        final = extrair_json(msg.content or "")
                    except ValueError:  # JSONDecodeError é subclasse de ValueError
                        texto_livre = (msg.content or "").strip()
                        if pediu_formato:
                            if not texto_livre:
                                raise RespostaInvalida("o modelo não devolveu o JSON final") from None
                            # O QA mostrou que isso acontece quando o modelo quer perguntar algo
                            # ou recusar: o texto é útil e seguro. Vira a resposta, sem requisição.
                            log.warning("RESPOSTA_FORA_DO_FORMATO, tratada como não atendido: %.200s",
                                        texto_livre)
                            res.resposta = texto_livre
                            return res
                        # Uma segunda chance: às vezes ele conclui em texto livre.
                        pediu_formato = True
                        msgs.append({"role": "assistant", "content": msg.content or ""})
                        msgs.append({"role": "user", "content":
                                     "Responda agora APENAS com o JSON final no formato pedido "
                                     "(perguntas e recusas também vão no campo resposta)."})
                        continue
                    itens = final.get("itens") or []
                    if not isinstance(itens, list):
                        raise RespostaInvalida("o campo itens do JSON final não é uma lista")
                    res.itens = itens
                    res.orcamento = final.get("orcamento")
                    res.resposta = str(final.get("resposta") or "").strip()
                    recusa = conferir_escolha(pedido, res) if itens and correcoes < MAX_CORRECOES else None
                    if recusa is None:
                        return res
                    # O código recusou a escolha (ex.: abaixo do pedido mínimo). Em vez de desistir,
                    # o modelo recebe o motivo e tem uma chance de corrigir. O backend confere de
                    # novo antes de gravar: a palavra final continua sendo do código.
                    correcoes += 1
                    log.warning("conferência recusou a escolha, pedindo correção: %s", recusa)
                    res.ferramentas.append({"nome": "conferencia_do_sistema", "argumentos": res.itens,
                                            "erro": f"RECUSADO: {recusa}"})
                    msgs.append({"role": "assistant", "content": msg.content or ""})
                    msgs.append({"role": "user", "content":
                                 f"O sistema recusou essa requisição: {recusa}. Corrija a escolha usando "
                                 "só produtos que as ferramentas devolveram, ou devolva itens vazio "
                                 "explicando o motivo. Responda só com o JSON."})
                    continue

                msgs.append(msg.model_dump(exclude_none=True))
                for call in msg.tool_calls:
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args, recusa = {}, "RECUSADO: argumentos da ferramenta não são um JSON válido."
                    else:
                        recusa = id_sem_busca(call.function.name, args, res.vistos)
                    if recusa:  # não chega ao catálogo; o modelo recebe o motivo e segue
                        log.warning("%s(%s) %s", call.function.name, args, recusa)
                        res.ferramentas.append({"nome": call.function.name, "argumentos": args,
                                                "erro": recusa})
                        msgs.append({"role": "tool", "tool_call_id": call.id, "content": recusa})
                        continue
                    out = await mcp.call_tool(call.function.name, args)
                    texto = "\n".join(c.text for c in out.content if hasattr(c, "text"))
                    if not texto and out.structuredContent is not None:
                        # Busca vazia chega sem nenhum bloco de texto. Sem isto, o modelo recebia
                        # "" e chegou a concluir que o catálogo estava fora do ar.
                        texto = json.dumps(out.structuredContent, ensure_ascii=False)
                    if out.isError:  # o modelo recebe o erro como resultado; o log também
                        log.warning("ferramenta %s(%s) falhou: %s", call.function.name, args, texto)
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
