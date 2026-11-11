"""Regras que NÃO ficam a cargo do LLM: preço, estoque, pedido mínimo, orçamento e conta.

O modelo só escolhe QUAIS produtos e QUANTOS, e copia do pedido o orçamento que a pessoa deu.
Tudo o que envolve dinheiro é conferido aqui, contra o que o catálogo devolveu:
- um id que não veio de uma ferramenta é invenção e é recusado;
- quantidade acima do estoque ou abaixo do pedido mínimo do fornecedor é recusada;
- preço acima do limite por unidade, ou total acima do limite total, é recusado;
- orçamento em outra moeda (R$, €) nunca vira compra: o catálogo é em dólar e não há cotação."""
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


@dataclass
class Item:
    produto_id: int
    titulo: str
    quantidade: int
    preco_unitario_usd: Decimal
    prazo_envio: str | None = None   # informativo: mostrado e gravado, ainda não é regra

    @property
    def subtotal_usd(self) -> Decimal:
        return (self.preco_unitario_usd * self.quantidade).quantize(Decimal("0.01"), ROUND_HALF_UP)


class EscolhaInvalida(Exception):
    pass


def montar_itens(escolhidos: list[dict], vistos: dict[int, dict]) -> list[Item]:
    """escolhidos: [{"produto_id", "quantidade"}] vindo do modelo.
    vistos: produtos que as ferramentas realmente devolveram nesta conversa, por id."""
    itens = []
    for e in escolhidos:
        try:
            pid = int(e["produto_id"])
            qtd = int(e["quantidade"])
        except (KeyError, TypeError, ValueError):
            raise EscolhaInvalida(f"item mal formado na resposta do agente: {e!r}") from None
        p = vistos.get(pid)
        if p is None:
            raise EscolhaInvalida(f"produto {pid} não veio do catálogo nesta busca")
        if qtd <= 0:
            raise EscolhaInvalida(f"quantidade inválida para o produto {pid}")
        estoque = p.get("estoque")
        if estoque is not None and qtd > estoque:
            raise EscolhaInvalida(f"{p['titulo']}: pedido {qtd}, estoque {estoque}")
        minimo = p.get("pedido_minimo") or 1
        if qtd < minimo:
            raise EscolhaInvalida(f"{p['titulo']}: pedido {qtd}, mínimo do fornecedor {minimo}")
        itens.append(Item(pid, p["titulo"], qtd, Decimal(str(p["preco_usd"])), p.get("prazo_envio")))
    return itens


def total(itens: list[Item]) -> Decimal:
    return sum((i.subtotal_usd for i in itens), Decimal("0.00"))


def _valor(v, campo: str) -> Decimal | None:
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        raise EscolhaInvalida(f"orçamento mal formado ({campo}={v!r})") from None
    if d <= 0:
        raise EscolhaInvalida(f"orçamento inválido ({campo}={v!r})")
    return d


def conferir_orcamento(itens: list[Item], orcamento: dict | None) -> None:
    """orcamento: o que o modelo copiou do pedido, ex. {"moeda": "USD", "max_unitario": 2000,
    "max_total": null}. Sem orçamento no pedido, não há o que conferir."""
    if not orcamento:
        return
    if not isinstance(orcamento, dict):
        raise EscolhaInvalida(f"orçamento mal formado: {orcamento!r}")
    max_unit = _valor(orcamento.get("max_unitario"), "max_unitario")
    max_total = _valor(orcamento.get("max_total"), "max_total")
    moeda = str(orcamento.get("moeda") or "USD").upper()
    if moeda != "USD" and (max_unit is not None or max_total is not None):
        raise EscolhaInvalida(f"o orçamento está em {moeda} e o catálogo é em dólar; "
                              "sem cotação, informe o orçamento em US$")
    if max_unit is not None:
        for i in itens:
            if i.preco_unitario_usd > max_unit:
                raise EscolhaInvalida(f"{i.titulo} custa US$ {i.preco_unitario_usd}, acima do limite "
                                      f"de US$ {max_unit} por unidade")
    if max_total is not None and total(itens) > max_total:
        raise EscolhaInvalida(f"total de US$ {total(itens)} acima do orçamento total de US$ {max_total}")


# "R$" ou "2.000 reais" / "€" ou "500 euros". Exige o símbolo ou um número antes, para "laptops
# reais" não contar como moeda.
_OUTRA_MOEDA = re.compile(r"R\$|\d[\d.,]*\s*(?:mil\s+)?reais\b|€|\d[\d.,]*\s*(?:mil\s+)?euros?\b",
                          re.IGNORECASE)
_DOLAR = re.compile(r"US\$|U\$|\$|\bUSD\b|d[óo]lar(?:es)?\b", re.IGNORECASE)


def _moedas(texto: str) -> tuple[bool, bool]:
    """(cita outra moeda, cita dólar). "R$" contém "$" e não pode contar como dólar."""
    return bool(_OUTRA_MOEDA.search(texto)), bool(_DOLAR.search(texto.replace("R$", "")))


def conferir_moeda_do_pedido(pedido: str) -> None:
    """Regra fixa, independente do modelo: um pedido com valor em reais ou euros e nenhuma menção
    a dólar não vira compra. Pegou o caso real do QA: "até R$ 2.000 cada" virou laptops de US$ 1.499."""
    outra, dolar = _moedas(pedido)
    if outra and not dolar:
        raise EscolhaInvalida("o pedido traz um valor em outra moeda e o catálogo é em dólar; "
                              "sem cotação, informe o orçamento em US$")


_VALOR_EM_DOLAR = re.compile(r"(?:US\$|U\$|\$)\s*\d|\d[\d.,]*\s*(?:mil\s+)?(?:d[óo]lar(?:es)?|USD)\b"
                             r"|\bUSD\s*\d", re.IGNORECASE)


def citou_valor_em_dolar(mensagens_da_pessoa: list[str]) -> bool:
    """A pessoa escreveu algum valor em dólar ("até US$ 150", "2 mil dólares"). Lido do texto, e
    não do JSON do modelo: com limites diferentes por item, o modelo deixa o orçamento vazio."""
    return any(_VALOR_EM_DOLAR.search(m.replace("R$", "")) for m in mensagens_da_pessoa)


def conferir_moeda_da_conversa(mensagens_da_pessoa: list[str]) -> None:
    """A mesma regra numa conversa: vale a última mensagem da pessoa que cita alguma moeda.
    "até R$ 2.000" seguido de "pode seguir" continua bloqueado; seguido de "então até US$ 400
    cada" passa. Mensagem sem moeda nenhuma ("sim, pode aumentar para 4") não muda nada."""
    for texto in reversed(mensagens_da_pessoa):
        outra, dolar = _moedas(texto)
        if outra or dolar:
            conferir_moeda_do_pedido(texto)
            return


def opcoes_que_atendem(vistos: dict[int, dict], categoria, quantidade: int, orcamento: dict | None,
                       com_limite_total: bool = True) -> list[dict]:
    """Os produtos de uma categoria que as buscas devolveram e que atendem a quantidade (estoque
    e pedido mínimo) e o limite de preço, do mais barato ao mais caro."""
    orc = orcamento if isinstance(orcamento, dict) else {}
    max_unit = _valor(orc.get("max_unitario"), "max_unitario")
    max_total = _valor(orc.get("max_total"), "max_total") if com_limite_total else None
    saida = []
    for p in vistos.values():
        if p.get("categoria") != categoria:
            continue
        if (p.get("estoque") or 0) < quantidade or (p.get("pedido_minimo") or 1) > quantidade:
            continue
        preco = Decimal(str(p["preco_usd"]))
        if (max_unit is not None and preco > max_unit) or \
                (max_total is not None and preco * quantidade > max_total):
            continue
        saida.append(p)
    return sorted(saida, key=lambda p: Decimal(str(p["preco_usd"])))


def alternativas(itens: list[Item], vistos: dict[int, dict], orcamento: dict | None) -> list[dict]:
    """As outras opções que também atendem ao pedido, para a pessoa ver a faixa inteira e poder
    trocar: mesma categoria do item escolhido, quantidade possível (estoque e pedido mínimo) e
    dentro do limite. Saem do que as buscas devolveram e são montadas aqui, não pelo modelo:
    a lista não tem como citar produto inventado."""
    escolhidos = {i.produto_id for i in itens}
    opcoes: dict[int, dict] = {}
    for i in itens:
        categoria = (vistos.get(i.produto_id) or {}).get("categoria")
        # O limite total só dá para aplicar por item quando o pedido tem um item só.
        for p in opcoes_que_atendem(vistos, categoria, i.quantidade, orcamento, len(itens) == 1):
            pid = int(p["id"])
            if pid in escolhidos or pid in opcoes:
                continue
            preco = Decimal(str(p["preco_usd"]))
            # Quanto o pedido mudaria trocando o item escolhido por esta opção: é o que a
            # pessoa precisa para decidir se o prazo ou o modelo melhor valem a diferença.
            diferenca = ((preco - i.preco_unitario_usd) * i.quantidade).quantize(Decimal("0.01"))
            opcoes[pid] = {"produto_id": pid, "titulo": p.get("titulo"),
                           "preco_unitario_usd": float(preco), "prazo_envio": p.get("prazo_envio"),
                           "pedido_minimo": p.get("pedido_minimo") or 1,
                           "diferenca_no_pedido_usd": float(diferenca)}
    return sorted(opcoes.values(), key=lambda o: o["preco_unitario_usd"])
