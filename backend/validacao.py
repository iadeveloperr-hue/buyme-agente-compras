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
        itens.append(Item(pid, p["titulo"], qtd, Decimal(str(p["preco_usd"]))))
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


def conferir_moeda_do_pedido(pedido: str) -> None:
    """Regra fixa, independente do modelo: um pedido com valor em reais ou euros e nenhuma menção
    a dólar não vira compra. Pegou o caso real do QA: "até R$ 2.000 cada" virou laptops de US$ 1.499."""
    sem_r = pedido.replace("R$", "")  # "R$" contém "$"; não pode contar como dólar
    if _OUTRA_MOEDA.search(pedido) and not _DOLAR.search(sem_r):
        raise EscolhaInvalida("o pedido traz um valor em outra moeda e o catálogo é em dólar; "
                              "sem cotação, informe o orçamento em US$")
