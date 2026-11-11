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
from validacao import (
    EscolhaInvalida,
    alternativas,
    citou_valor_em_dolar,
    conferir_moeda_da_conversa,
    conferir_orcamento,
    montar_itens,
    opcoes_que_atendem,
)

log = logging.getLogger("agente")

MCP_URL = os.getenv("MCP_URL", "http://mcp-server:8001/mcp")
MODELO = os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
MAX_PASSOS = 8
MAX_HISTORICO = 8  # mensagens anteriores (as mais recentes) que vão como contexto

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
- Para escolher entre opções que atendem: se a pessoa disse a necessidade (básico, potente, uma
  marca, o uso, o prazo), siga-a; se não disse, escolha a mais barata que atende. O limite de
  preço é um teto, não uma meta. Diga o critério em uma frase e copie-o em "criterio":
  "mais_barato" ou "necessidade: <o que a pessoa disse>".
- Com quantidade e limite de preço, no pedido ou na conversa, monte a requisição na hora, mesmo
  com muitas opções: nunca pergunte qual modelo a pessoa prefere. O sistema mostra abaixo da
  requisição a lista conferida das outras opções que também cabem, com preço e prazo, e a pessoa
  pode pedir troca: não liste essas opções no texto. Ao montar, a resposta tem no máximo duas
  frases: o que escolheu e o critério.
- Se existe uma opção que atende ao pedido exatamente como foi feito, monte a requisição com
  ela: não peça confirmação nem sugira mudar a quantidade.
- Os preços do catálogo estão em dólar: escreva valores sempre como US$. prazo_envio é o prazo
  de despacho do fornecedor, não de entrega: escreva "envio", nunca "entrega".

Conversa:
- A mensagem pode trazer a conversa anterior como contexto. Use-a para entender a mensagem
  nova: ela pode responder a uma pergunta sua ("5 unidades, até US$ 1.500 cada") ou ajustar
  algo ("pode aumentar para 4"). Antes de montar, busque de novo: ids e preços citados antes
  podem ter mudado, e só valem produtos que as ferramentas devolverem agora.
- Requisição já gravada (aparece como "Requisição nº N gravada") não muda. Se a pessoa pedir
  troca ou ajuste, monte uma nova e avise que a nº N continua gravada.
- Se faltar algo essencial para escolher (o que comprar, quantas unidades, ou, quando o pedido
  é só uma categoria como "celulares", sem limite de preço nem necessidade), busque primeiro e
  pergunte mostrando o que existe, assim: "Encontrei N <produtos>, de US$ X a US$ Y. Para
  <quantidade> unidades, os mais baratos são <A> (US$ ...) e <B> (US$ ...). Qual o limite por
  unidade, ou alguma necessidade (básico, potente, marca)?". Só cite opções que atendem a
  quantidade (estoque e pedido mínimo). A requisição fica vazia até a resposta.
- Se a pessoa pedir as requisições já feitas, diga que elas aparecem em "Requisições salvas",
  no topo do chat.

Orçamento:
- Copie em "orcamento" o limite que a pessoa deu: "até US$ X cada" em max_unitario,
  "orçamento total de US$ X" em max_total. Sem limite no pedido, "orcamento" é null.
- Orçamento em outra moeda (R$, reais, €): não converta, porque não há cotação. Devolva itens
  vazio, copie a moeda (BRL, EUR) em orcamento.moeda e peça o valor em US$.

Regras que você não pode quebrar:
- Nunca cite produto, categoria, id, preço ou estoque que as ferramentas não devolveram, nem
  como exemplo (nada de "ex.: laptops, impressoras"). Se a pessoa perguntar o que existe no
  catálogo, responda só: "Eu busco no catálogo pelo que você precisar. O que você quer comprar,
  quantas unidades e até quanto por unidade?"
- Se nada atender (não existe, estoque insuficiente, abaixo do pedido mínimo, acima do
  orçamento), NÃO escolha algo parecido para fechar o pedido: devolva itens vazio e diga o que
  faltou.
- Nesse caso, se as buscas trouxeram produtos próximos do que a pessoa pediu (outro modelo,
  outra faixa de preço), cite na resposta até 3 deles, com nome e preço, para ela decidir.
  Só cite produtos que as ferramentas devolveram; a requisição continua vazia.
- Nunca mude por conta própria a quantidade que a pessoa pediu, nem para cumprir o pedido
  mínimo. Se o mínimo do fornecedor for maior que o pedido, não monte a requisição: diga qual é
  o mínimo e pergunte se ela quer pedir essa quantidade.
- Pedido com mais de um item: monte todos ou nenhum. Se algum item não puder ser atendido (não
  existe, estoque, pedido mínimo ou limite), devolva itens vazio, diga o que atende e o que
  falta, e pergunte se a pessoa quer seguir só com o que atende. Antes de dizer que um item não
  atende, confira estoque e pedido_minimo de todos os produtos que as buscas devolveram. Não
  peça para incluir um item que o catálogo não tem.
- Busca com lista vazia significa que o catálogo NÃO TEM esse produto (não é falha):
  diga que o item não existe no catálogo. Só fale em indisponibilidade se uma
  ferramenta devolver CATALOGO_INDISPONIVEL; nesse caso, pare e informe isso.

Toda resposta final, inclusive quando faltar informação ou você recusar o pedido, é APENAS
este JSON:
{"itens": [{"produto_id": <int>, "quantidade": <int>}],
 "orcamento": {"moeda": "USD", "max_unitario": <número ou null>, "max_total": <número ou null>},
 "criterio": "<mais_barato, ou necessidade: o que a pessoa disse; null se não montou requisição>",
 "resposta": "<texto curto em português para a pessoa: o que escolheu e por quê, ou o que faltou>"}"""


@dataclass
class Resultado:
    itens: list[dict]
    resposta: str
    orcamento: dict | None = None   # o limite que o modelo copiou do pedido; o código confere
    criterio: str | None = None     # "mais_barato" ou a necessidade que a pessoa disse
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


def mensagens_da_pessoa(pedido: str, historico: list[dict]) -> list[str]:
    """Tudo o que a PESSOA escreveu na conversa, em ordem. É o que a regra de moeda confere e o
    que fica gravado como pedido ("Quero 3 AirPods" + "pode aumentar para 4")."""
    return [t["texto"] for t in historico if t.get("autor") == "pessoa"] + [pedido]


def montar_mensagem(pedido: str, historico: list[dict]) -> str:
    """A conversa anterior vai como contexto, num bloco só, antes da mensagem nova. Assim o modelo
    entende uma resposta curta ("5 unidades, até US$ 1.500 cada") e o loop não muda de formato."""
    if not historico:
        return pedido
    linhas = [f"{'Pessoa' if t.get('autor') == 'pessoa' else 'Agente'}: {t['texto']}"
              for t in historico]
    return ("Conversa até aqui (só contexto; ids, preços e estoque podem ter mudado, busque de novo):\n"
            + "\n".join(linhas) + f"\n\nMensagem nova da pessoa: {pedido}")


def conferir_escolha(pedido: str | list[str], res: "Resultado") -> str | None:
    """As mesmas conferências que o backend faz antes de gravar (validacao.py), rodadas ainda
    dentro do loop, para o modelo poder corrigir. Devolve o motivo da recusa, ou None.
    pedido: o texto do pedido ou, numa conversa, todas as mensagens da pessoa."""
    try:
        conferir_moeda_da_conversa([pedido] if isinstance(pedido, str) else pedido)
        conferir_orcamento(montar_itens(res.itens, res.vistos), res.orcamento)
    except EscolhaInvalida as e:
        return str(e)
    return None


REVISAR_PERGUNTA = (
    "Revise: a pessoa já deu um limite de preço. Se ela também disse o que comprar e quantas "
    "unidades, e algum produto que as ferramentas devolveram atende, monte a requisição agora pelo "
    "critério (a mais barata que atende, ou a necessidade que ela disse), sem perguntar qual modelo "
    "ela prefere. Se falta o produto ou a quantidade, ou se um item do pedido não existe no "
    "catálogo, mantenha itens vazio e a sua pergunta. Escreva para a pessoa sem mencionar esta "
    "revisão. Responda só com o JSON.")


REVISAR_MONTOU_E_PERGUNTOU = (
    "Revise: você montou a requisição e também fez uma pergunta. Se algum item do pedido ficou de "
    "fora, não monte requisição parcial: confira de novo estoque e pedido_minimo de todos os "
    "produtos que as buscas devolveram; se mesmo assim algum item não for atendido, devolva itens "
    "vazio, diga o que atende e o que falta, e pergunte se a pessoa quer seguir só com o que "
    "atende. Se todos os itens estão na requisição, não pergunte nada. Escreva para a pessoa sem "
    "mencionar esta revisão. Responda só com o JSON.")


def revisao_da_resposta(res: "Resultado", da_pessoa: list[str] | tuple = ()) -> str | None:
    """Duas respostas que o prompt proíbe e o código consegue notar, por uma pergunta no texto:
    - montou a requisição E perguntou algo: no QA, "3 laptops e 5 relógios" virou requisição só
      dos laptops, com a pergunta sobre os relógios (o modelo leu errado o mínimo);
    - não montou e perguntou, mesmo com limite de preço (ver perguntou_com_limite).
    Devolve a instrução de revisão, ou None. O modelo decide; o código só pede para revisar."""
    if res.catalogo_indisponivel or "?" not in res.resposta:
        return None
    if res.itens:
        return REVISAR_MONTOU_E_PERGUNTOU
    return REVISAR_PERGUNTA if perguntou_com_limite(res, da_pessoa) else None


def perguntou_com_limite(res: "Resultado", da_pessoa: list[str] | tuple = ()) -> bool:
    """O enunciado diz "busca e escolhe": com quantidade e limite de preço, o agente monta. Nos
    testes ele às vezes listava as opções e perguntava "qual você prefere?". O código não sabe se
    a quantidade foi dita, por isso só pede uma revisão, e o modelo decide."""
    orc = res.orcamento if isinstance(res.orcamento, dict) else {}
    tem_limite = ((str(orc.get("moeda") or "USD").upper() == "USD"
                   and bool(orc.get("max_unitario") or orc.get("max_total")))
                  or citou_valor_em_dolar(list(da_pessoa)))
    try:  # se a última moeda que a pessoa citou não é dólar, o certo é pedir o valor em US$
        conferir_moeda_da_conversa(list(da_pessoa))
    except EscolhaInvalida:
        return False
    # "?" em qualquer ponto: a pergunta às vezes vem no meio, seguida da lista de opções.
    return tem_limite and bool(res.vistos) and not res.catalogo_indisponivel and "?" in res.resposta


def opcoes_para_corrigir(res: "Resultado") -> str:
    """Junto com a recusa por mínimo, estoque ou limite, o código entrega as opções que atendem.
    No teste, recusado o Realme C35 (mínimo 3) para 2 unidades, o modelo concluiu que nenhum
    celular servia, com o Vivo S1 (mínimo 1) na mesma busca. A escolha continua sendo dele."""
    partes = []
    for e in res.itens:
        try:
            pid, qtd = int(e["produto_id"]), int(e["quantidade"])
        except (KeyError, TypeError, ValueError):
            continue
        p = res.vistos.get(pid)
        if p is None or qtd <= 0:
            continue
        try:
            ops = opcoes_que_atendem(res.vistos, p.get("categoria"), qtd, res.orcamento,
                                     len(res.itens) == 1)
        except EscolhaInvalida:
            continue
        if ops:
            nomes = "; ".join(f"{o['titulo']} (id {o['id']}, US$ {o['preco_usd']}, "
                              f"mínimo {o.get('pedido_minimo') or 1})" for o in ops[:6])
            partes.append(f"Para {qtd} unidade(s) dessa categoria, atendem: {nomes}.")
        else:
            partes.append(f"Para {qtd} unidade(s) dessa categoria, nenhum produto das buscas "
                          "atende estoque, mínimo e limite.")
    return " ".join(partes)


def conferir_criterio(res: "Resultado") -> str | None:
    """Sem necessidade dita, o critério é o mais barato que atende. No teste, o modelo escolheu
    o Huawei (US$ 1.399,99) e o chamou de "o mais econômico" com o Lenovo (US$ 1.099,99) na mesma
    busca. Aqui o código compara com as opções que também cabem; o modelo corrige uma vez.
    Não bloqueia a gravação: é regra de escolha, não de dinheiro (essas ficam em validacao.py).
    O padrão é o mais barato; só escapa da conferência o critério "necessidade: ...", que diz o
    que a pessoa pediu (com o pedido em inglês, o modelo escreveu o critério de outro jeito)."""
    if str(res.criterio or "").strip().lower().startswith("necessidade") or not res.itens:
        return None
    try:
        itens = montar_itens(res.itens, res.vistos)
        mais_baratas = [a for a in alternativas(itens, res.vistos, res.orcamento)
                        if a["diferenca_no_pedido_usd"] < 0]
    except EscolhaInvalida:  # conferir_escolha já cuida desse caso
        return None
    if not mais_baratas:
        return None
    a = mais_baratas[0]
    return (f"o critério é o mais barato, mas {a['titulo']} (US$ {a['preco_unitario_usd']} cada) também "
            "atende e custa menos; escolha a mais barata, ou diga qual necessidade da pessoa justifica outra")


def extrair_json(texto: str) -> dict:
    """Lê o JSON final mesmo que o modelo o embrulhe em texto ou em bloco ```json."""
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio == -1 or fim == -1:
        raise ValueError("sem JSON")
    return json.loads(texto[inicio:fim + 1])


async def executar(pedido: str, historico: list[dict] | None = None) -> Resultado:
    """O cliente MCP roda dentro de um TaskGroup, que embrulha qualquer erro num
    ExceptionGroup. Aqui desembrulhamos para o backend ver o erro original.
    historico: a conversa anterior, [{"autor": "pessoa"|"agente", "texto"}], só como contexto."""
    try:
        return await _executar(pedido, (historico or [])[-MAX_HISTORICO:])
    except BaseExceptionGroup as grupo:
        raise _primeiro(grupo) from grupo


def _primeiro(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    return e


async def _executar(pedido: str, historico: list[dict]) -> Resultado:
    chave = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise FalhaLLM("OPENROUTER_API_KEY não está configurada no .env")
    llm = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=chave, timeout=60)
    async with streamablehttp_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()
            tools = ferramentas_openai((await mcp.list_tools()).tools)
            msgs = [{"role": "system", "content": PROMPT},
                    {"role": "user", "content": montar_mensagem(pedido, historico)}]
            da_pessoa = mensagens_da_pessoa(pedido, historico)
            res = Resultado(itens=[], resposta="")
            pediu_formato = False
            correcoes = 0
            revisou_pergunta = False  # separado das correções: não gasta a que confere dinheiro

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
                    res.criterio = final.get("criterio")
                    res.resposta = str(final.get("resposta") or "").strip()
                    revisao = None if revisou_pergunta else revisao_da_resposta(res, da_pessoa)
                    if revisao:
                        revisou_pergunta = True
                        motivo = "montou e perguntou" if itens else "perguntou em vez de escolher"
                        log.info("pedindo revisão da resposta: %s", motivo)
                        res.ferramentas.append({"nome": "conferencia_do_sistema", "argumentos": res.itens,
                                                "erro": f"REVISAR: {motivo}"})
                        msgs.append({"role": "assistant", "content": msg.content or ""})
                        msgs.append({"role": "user", "content": revisao})
                        continue
                    pode_corrigir = bool(itens) and correcoes < MAX_CORRECOES
                    regra = conferir_escolha(da_pessoa, res) if pode_corrigir else None
                    criterio = conferir_criterio(res) if pode_corrigir and not regra else None
                    if regra is None and criterio is None:
                        return res
                    # O código recusou a escolha (ex.: abaixo do pedido mínimo) ou achou opção mais
                    # barata com o critério "mais_barato". Em vez de desistir, o modelo recebe o
                    # motivo e tem uma chance de corrigir. O backend confere de novo as regras de
                    # dinheiro antes de gravar: a palavra final continua sendo do código.
                    correcoes += 1
                    log.warning("conferência pediu correção: %s", regra or criterio)
                    aviso = f"RECUSADO: {regra}" if regra else f"REVISAR: {criterio}"
                    res.ferramentas.append({"nome": "conferencia_do_sistema", "argumentos": res.itens,
                                            "erro": aviso})
                    msgs.append({"role": "assistant", "content": msg.content or ""})
                    # Recusa de moeda não ganha lista: ali o certo é pedir o valor em US$.
                    fatos = opcoes_para_corrigir(res) if regra and "dólar" not in regra else ""
                    instrucao = (f"O sistema recusou essa requisição: {regra}. {fatos} Corrija a "
                                 "escolha usando só produtos que as ferramentas devolveram, sem mudar "
                                 "a quantidade pedida, ou devolva itens vazio explicando o motivo."
                                 if regra else
                                 f"Revise a escolha: {criterio}. Mantenha a quantidade pedida.")
                    msgs.append({"role": "user", "content": instrucao + " Escreva para a pessoa sem "
                                 "mencionar esta correção. Responda só com o JSON."})
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
