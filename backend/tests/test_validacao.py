from decimal import Decimal

import pytest
from agente import id_sem_busca, registrar_vistos
from validacao import EscolhaInvalida, montar_itens, total

VISTOS = {78: {"id": 78, "titulo": "MacBook Pro 14", "preco_usd": 1999.99, "estoque": 24},
          81: {"id": 81, "titulo": "Lenovo Yoga 920", "preco_usd": 1099.99, "estoque": 2}}


def test_preco_e_total_vem_do_catalogo_e_do_codigo():
    itens = montar_itens([{"produto_id": 81, "quantidade": 2}], VISTOS)
    assert itens[0].preco_unitario_usd == Decimal("1099.99")
    assert total(itens) == Decimal("2199.98")


def test_id_que_o_catalogo_nao_devolveu_e_invencao():
    with pytest.raises(EscolhaInvalida, match="não veio do catálogo"):
        montar_itens([{"produto_id": 999, "quantidade": 1}], VISTOS)


def test_quantidade_acima_do_estoque():
    with pytest.raises(EscolhaInvalida, match="estoque 2"):
        montar_itens([{"produto_id": 81, "quantidade": 3}], VISTOS)


def test_registrar_vistos_le_lista_e_produto_unico():
    vistos: dict = {}
    registrar_vistos('{"result": [{"id": 1, "preco_usd": 10}]}', vistos)
    registrar_vistos('{"id": 2, "preco_usd": 20, "descricao": "x"}', vistos)
    registrar_vistos('texto que não é JSON', vistos)
    assert set(vistos) == {1, 2}


def test_detalhar_id_que_nao_veio_de_busca_e_recusado():
    assert "RECUSADO" in id_sem_busca("detalhar_produto", {"id": 101}, VISTOS)
    assert id_sem_busca("detalhar_produto", {"id": 78}, VISTOS) is None
    assert id_sem_busca("buscar_produtos", {"termo": "mouse"}, VISTOS) is None


# --- Achados da bateria de QA: cada regra de dinheiro conferida no código, não no modelo.
from validacao import conferir_moeda_do_pedido, conferir_orcamento  # noqa: E402

REALME = {128: {"id": 128, "titulo": "Realme C35", "preco_usd": 149.99, "estoque": 48, "pedido_minimo": 3}}


def test_abaixo_do_pedido_minimo_do_fornecedor():
    with pytest.raises(EscolhaInvalida, match="mínimo do fornecedor 3"):
        montar_itens([{"produto_id": 128, "quantidade": 2}], REALME)
    assert montar_itens([{"produto_id": 128, "quantidade": 3}], REALME)[0].quantidade == 3


def test_preco_acima_do_limite_por_unidade():
    itens = montar_itens([{"produto_id": 78, "quantidade": 1}], VISTOS)  # US$ 1.999,99
    with pytest.raises(EscolhaInvalida, match="por unidade"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": 1200, "max_total": None})
    conferir_orcamento(itens, {"moeda": "USD", "max_unitario": 2000, "max_total": None})


def test_total_acima_do_orcamento_total():
    itens = montar_itens([{"produto_id": 81, "quantidade": 2}], VISTOS)  # US$ 2.199,98
    with pytest.raises(EscolhaInvalida, match="orçamento total"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": None, "max_total": 2000})


def test_orcamento_em_reais_nunca_vira_compra():
    itens = montar_itens([{"produto_id": 81, "quantidade": 1}], VISTOS)
    with pytest.raises(EscolhaInvalida, match="BRL"):
        conferir_orcamento(itens, {"moeda": "BRL", "max_unitario": 2000, "max_total": None})


def test_orcamento_ausente_ou_mal_formado():
    itens = montar_itens([{"produto_id": 81, "quantidade": 1}], VISTOS)
    conferir_orcamento(itens, None)  # pedido sem limite: nada a conferir
    with pytest.raises(EscolhaInvalida, match="mal formado"):
        conferir_orcamento(itens, {"moeda": "USD", "max_unitario": "dois mil"})


@pytest.mark.parametrize("pedido", ["Preciso de 3 laptops de até R$ 2.000 cada.",
                                    "3 notebooks, uns 5 mil reais no total",
                                    "Quero 2 monitores de até 300 euros"])
def test_valor_em_outra_moeda_no_texto_e_bloqueado(pedido):
    with pytest.raises(EscolhaInvalida, match="outra moeda"):
        conferir_moeda_do_pedido(pedido)


@pytest.mark.parametrize("pedido", ["Preciso de 3 laptops, até US$ 2.000 cada.",
                                    "Preciso de 3 laptops reais, não de demonstração",
                                    "3 laptops até R$ 10.000 (uns 2 mil dólares)",
                                    "Preciso de 3 laptops"])
def test_dolar_ou_sem_valor_passa(pedido):
    conferir_moeda_do_pedido(pedido)


def test_conferencia_dentro_do_loop_devolve_o_motivo_para_o_modelo_corrigir():
    from agente import Resultado, conferir_escolha
    abaixo = Resultado(itens=[{"produto_id": 128, "quantidade": 2}], resposta="", vistos=dict(REALME))
    assert "mínimo do fornecedor 3" in conferir_escolha("Quero 2 Realme C35", abaixo)
    ok = Resultado(itens=[{"produto_id": 128, "quantidade": 3}], resposta="", vistos=dict(REALME))
    assert conferir_escolha("Quero 3 Realme C35", ok) is None
    em_reais = Resultado(itens=[{"produto_id": 128, "quantidade": 3}], resposta="", vistos=dict(REALME))
    assert "outra moeda" in conferir_escolha("3 Realme C35 até R$ 900 cada", em_reais)


def test_prazo_de_envio_acompanha_o_item():
    vistos = {78: {**VISTOS[78], "prazo_envio": "Ships in 2 weeks"}}
    assert montar_itens([{"produto_id": 78, "quantidade": 1}], vistos)[0].prazo_envio == "Ships in 2 weeks"
    assert montar_itens([{"produto_id": 78, "quantidade": 1}], VISTOS)[0].prazo_envio is None


def test_moeda_vale_a_ultima_que_a_pessoa_citou_na_conversa():
    from validacao import conferir_moeda_da_conversa
    with pytest.raises(EscolhaInvalida, match="outra moeda"):
        conferir_moeda_da_conversa(["2 celulares até R$ 2.000 cada.", "Pode seguir assim mesmo."])
    with pytest.raises(EscolhaInvalida, match="outra moeda"):
        conferir_moeda_da_conversa(["3 laptops até US$ 2.000 cada.", "Agora 2 celulares até R$ 1.500 cada."])
    conferir_moeda_da_conversa(["2 celulares até R$ 2.000 cada.", "Então até US$ 400 cada."])
    conferir_moeda_da_conversa(["Quero 3 Apple AirPods.", "Sim, pode aumentar para 4."])


def test_alternativas_mesma_categoria_quantidade_possivel_e_dentro_do_limite():
    from validacao import alternativas
    lap = {"categoria": "laptops", "pedido_minimo": 1}
    vistos = {
        81: {**lap, "id": 81, "titulo": "Lenovo", "preco_usd": 1099.99, "estoque": 40},
        80: {**lap, "id": 80, "titulo": "Huawei", "preco_usd": 1399.99, "estoque": 75,
             "prazo_envio": "Ships overnight"},
        78: {**lap, "id": 78, "titulo": "MacBook", "preco_usd": 1999.99, "estoque": 2},     # estoque < 3
        79: {**lap, "id": 79, "titulo": "Asus", "preco_usd": 1799.99, "estoque": 45,
             "pedido_minimo": 5},                                                          # mínimo > 3
        82: {**lap, "id": 82, "titulo": "Caro", "preco_usd": 2500.00, "estoque": 9},       # acima do teto
        100: {"id": 100, "titulo": "AirPods", "preco_usd": 129.99, "estoque": 67,
              "pedido_minimo": 1, "categoria": "mobile-accessories"},                     # outra categoria
    }
    itens = montar_itens([{"produto_id": 81, "quantidade": 3}], vistos)
    alts = alternativas(itens, vistos, {"moeda": "USD", "max_unitario": 2000, "max_total": None})
    assert [(a["produto_id"], a["prazo_envio"], a["diferenca_no_pedido_usd"]) for a in alts] == \
        [(80, "Ships overnight", 900.0)]
    # Sem orçamento, só estoque, mínimo e categoria filtram, em ordem de preço.
    assert [a["produto_id"] for a in alternativas(itens, vistos, None)] == [80, 82]
    # Orçamento total num pedido de um item: 3 x 1.399,99 = 4.199,97 passa de 4.000.
    assert alternativas(itens, vistos, {"moeda": "USD", "max_unitario": None, "max_total": 4000}) == []


def test_conversa_anterior_vai_como_contexto_antes_da_mensagem_nova():
    from agente import mensagens_da_pessoa, montar_mensagem
    hist = [{"autor": "pessoa", "texto": "Quero 3 Apple AirPods."},
            {"autor": "agente", "texto": "O mínimo é 4. Quer pedir 4?"}]
    msg = montar_mensagem("Sim, pode aumentar para 4.", hist)
    assert (msg.index("Pessoa: Quero 3 Apple AirPods.") < msg.index("Agente: O mínimo é 4")
            < msg.index("Mensagem nova da pessoa: Sim, pode aumentar para 4."))
    assert montar_mensagem("Quero 2 celulares.", []) == "Quero 2 celulares."
    assert mensagens_da_pessoa("Sim, 4.", hist) == ["Quero 3 Apple AirPods.", "Sim, 4."]


def test_criterio_mais_barato_e_conferido_contra_as_opcoes_que_cabem():
    from agente import Resultado, conferir_criterio
    lap = {"categoria": "laptops", "pedido_minimo": 1, "estoque": 40}
    vistos = {81: {**lap, "id": 81, "titulo": "Lenovo Yoga 920", "preco_usd": 1099.99},
              80: {**lap, "id": 80, "titulo": "Huawei Matebook X Pro", "preco_usd": 1399.99}}
    orc = {"moeda": "USD", "max_unitario": 2000}
    huawei = [{"produto_id": 80, "quantidade": 3}]
    motivo = conferir_criterio(Resultado(itens=huawei, resposta="", vistos=vistos, orcamento=orc,
                                         criterio="mais_barato"))
    assert "Lenovo Yoga 920" in motivo
    # O padrão é o mais barato, mesmo com o critério escrito de outro jeito ou ausente.
    for outro in ("cheapest", "custo-benefício", None):
        assert "Lenovo Yoga 920" in conferir_criterio(Resultado(itens=huawei, resposta="", vistos=vistos,
                                                                orcamento=orc, criterio=outro))
    # Com a necessidade dita pela pessoa, o mais caro pode ser a escolha certa.
    assert conferir_criterio(Resultado(itens=huawei, resposta="", vistos=vistos, orcamento=orc,
                                       criterio="necessidade: envio no dia seguinte")) is None
    assert conferir_criterio(Resultado(itens=[{"produto_id": 81, "quantidade": 3}], resposta="",
                                       vistos=vistos, orcamento=orc, criterio="mais_barato")) is None


def test_pergunta_mesmo_com_limite_de_preco_pede_revisao():
    from agente import Resultado, perguntou_com_limite
    vistos = {80: {"id": 80, "titulo": "Huawei", "preco_usd": 1399.99}}
    orc = {"moeda": "USD", "max_unitario": 1000}
    assert perguntou_com_limite(Resultado(itens=[], resposta="Qual você prefere?", vistos=vistos,
                                          orcamento=orc))
    # Sem limite, a pergunta é legítima (falta critério); em reais, a regra de moeda já cuida.
    assert not perguntou_com_limite(Resultado(itens=[], resposta="Qual você prefere?", vistos=vistos))
    assert not perguntou_com_limite(Resultado(itens=[], resposta="Qual você prefere?", vistos=vistos,
                                              orcamento={"moeda": "BRL", "max_unitario": 2000}))
    # Recusa explicada, sem pergunta, passa direto.
    assert not perguntou_com_limite(Resultado(itens=[], resposta="Nenhum laptop custa até US$ 500.",
                                              vistos=vistos, orcamento=orc))


def test_recusa_por_minimo_vem_com_as_opcoes_que_atendem():
    from agente import Resultado, opcoes_para_corrigir
    cel = {"categoria": "smartphones", "estoque": 50}
    vistos = {128: {**cel, "id": 128, "titulo": "Realme C35", "preco_usd": 149.99, "pedido_minimo": 3},
              132: {**cel, "id": 132, "titulo": "Vivo S1", "preco_usd": 249.99, "pedido_minimo": 1},
              123: {**cel, "id": 123, "titulo": "iPhone 13 Pro", "preco_usd": 1099.99, "pedido_minimo": 1}}
    res = Resultado(itens=[{"produto_id": 128, "quantidade": 2}], resposta="", vistos=vistos,
                    orcamento={"moeda": "USD", "max_unitario": 350})
    fatos = opcoes_para_corrigir(res)
    assert "Vivo S1" in fatos and "Realme C35" not in fatos and "iPhone 13 Pro" not in fatos


def test_montar_e_perguntar_pede_revisao_contra_requisicao_parcial():
    from agente import REVISAR_MONTOU_E_PERGUNTOU, REVISAR_PERGUNTA, Resultado, revisao_da_resposta
    vistos = {81: {"id": 81, "titulo": "Lenovo Yoga 920", "preco_usd": 1099.99}}
    orc = {"moeda": "USD", "max_unitario": 2000}
    parcial = Resultado(itens=[{"produto_id": 81, "quantidade": 3}], vistos=vistos, orcamento=orc,
                        resposta="Escolhi 3 Lenovo. Quanto aos relógios, quer aumentar para 7?")
    assert revisao_da_resposta(parcial) == REVISAR_MONTOU_E_PERGUNTOU
    pergunta = Resultado(itens=[], vistos=vistos, orcamento=orc, resposta="Qual você prefere?")
    assert revisao_da_resposta(pergunta) == REVISAR_PERGUNTA
    pronto = Resultado(itens=[{"produto_id": 81, "quantidade": 3}], vistos=vistos, orcamento=orc,
                       resposta="Escolhi 3 Lenovo Yoga 920, o mais barato que atende.")
    assert revisao_da_resposta(pronto) is None


def test_limite_em_dolar_lido_do_texto_da_pessoa():
    from agente import Resultado, perguntou_com_limite
    from validacao import citou_valor_em_dolar
    assert citou_valor_em_dolar(["3 laptops de até US$ 2.000 cada e 5 relógios de até US$ 150 cada."])
    assert citou_valor_em_dolar(["uns 2 mil dólares no total"])
    assert not citou_valor_em_dolar(["Preciso de 2 celulares até R$ 2.000 cada."])  # "R$ 2" não é dólar
    assert not citou_valor_em_dolar(["Quero 3 Apple AirPods."])
    vistos = {81: {"id": 81, "titulo": "Lenovo", "preco_usd": 1099.99}}
    pergunta = Resultado(itens=[], vistos=vistos, resposta="Qual você prefere para os relógios?")
    # Sem orçamento no JSON (limites diferentes por item), o texto da pessoa basta.
    assert perguntou_com_limite(pergunta, ["3 laptops até US$ 2.000 cada e 5 relógios até US$ 150 cada."])
    assert not perguntou_com_limite(pergunta, ["2 celulares até R$ 2.000 cada."])
