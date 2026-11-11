# BuyME: agente de compras

*Buy* + *ME*: o agente de compras pensado para o dia a dia da Mercado Eletrônico.

A pessoa escreve o que precisa num chat. O agente busca no catálogo da DummyJSON por um servidor
MCP, escolhe os itens e salva uma requisição de compra no Postgres. Se nada atende, diz o que
faltou, sugere o que existe e não cria requisição.

## Como funciona

1. A pessoa escreve o pedido no chat (React), que o envia para `POST /requisicoes` no backend.
2. O backend chama o modelo (Claude Haiku 4.5, pela OpenRouter) com as ferramentas que o
   servidor MCP anuncia.
3. O modelo decide o que fazer: busca no catálogo (`buscar_produtos`), detalha um item
   (`detalhar_produto`) e repete até ter o que precisa. O servidor MCP consulta a DummyJSON.
4. O modelo responde com os itens escolhidos (id e quantidade) e o orçamento que leu no pedido.
5. O backend confere o que envolve dinheiro contra o catálogo: preço, estoque, pedido mínimo e
   orçamento. Se algo não confere, o modelo recebe o motivo e corrige uma vez.
6. Com tudo certo, a requisição e os itens são gravados juntos no Postgres, e o chat mostra o
   número, os itens, o prazo de envio de cada um e o total. Se nada atende, o chat explica o que
   faltou e nada é gravado.

## Como rodar

Precisa de Docker com o Compose.

```bash
cp .env.example .env    # cole a chave da OpenRouter em OPENROUTER_API_KEY
docker compose up
```

Abra **http://localhost:3000** e envie o pedido de exemplo, que já vem preenchido.

- **Porta em uso?** Se a 3000 (chat) ou a 8000 (API) já estiver ocupada, troque `CHAT_PORT` ou
  `API_PORT` no `.env` e rode `docker compose up` de novo.
- **Mudou o código?** Rode `docker compose up --build` para montar as imagens de novo.
- **Quer começar do zero?** `docker compose down -v` apaga o banco, e as tabelas são recriadas na
  próxima subida.

## Principais decisões

1. **O modelo decide quais ferramentas chamar.** O loop de tool calling é escrito à mão, sem
   framework, e não tem fluxo fixo ([backend/agente.py](backend/agente.py)).
2. **Tudo o que envolve dinheiro é conferido no código, nunca pelo LLM.** Preço, estoque, pedido
   mínimo do fornecedor, orçamento e total vêm do catálogo e do código. O modelo devolve só id,
   quantidade e o orçamento que leu no pedido. Se o código recusa a escolha, o modelo recebe o
   motivo e tem uma chance de corrigir.
3. **Nenhum erro silencioso.** Toda falha tem código, mensagem clara no chat e linha no log
   ([tabela](#tratamento-de-erros)).
4. **Modelo `anthropic/claude-haiku-4.5`.** A chave do case bloqueia provedores sem
   *Zero Data Retention*, e o `gpt-4o-mini` foi recusado. Custa cerca de US$ 0,006 por pedido
   ([custo](#custo)). Para trocar, mude `OPENROUTER_MODEL`.
5. **Gravar fica no backend, não numa 3ª ferramenta MCP.** A escrita passa pelas conferências do
   item 2 numa transação: requisição e itens entram juntos, ou nada entra.

**Testado contra prejuízo:** uma [bateria de QA](#bateria-de-qa) com 22 cenários (orçamento,
moeda, estoque, pedido mínimo, manipulação do pedido) e auditoria do banco contra o catálogo.
**Uso de IA:** construído com o Claude Code ([como usei](#como-usei-ia)).

---

## Detalhes

### Peças

| Peça | Porta | O que é |
|---|---|---|
| `frontend` | 3000 | chat em React (o nginx serve o build e repassa `/api` ao backend) |
| `backend` | 8000 | FastAPI com `POST /requisicoes`, `GET /requisicoes` e `GET /cotacao`; o agente mora aqui |
| `mcp-server` | interna | servidor MCP com `buscar_produtos(termo)` e `detalhar_produto(id)` |
| `db` | interna | Postgres 16; as tabelas nascem do [db/init.sql](db/init.sql) na primeira subida |

O backend só sobe depois que o Postgres responde ao *healthcheck*, que é feito por TCP de
propósito. Na primeira subida, o Postgres roda um servidor temporário, só no socket local,
enquanto cria as tabelas, e um healthcheck pelo socket diria "pronto" cedo demais. Como segunda
proteção, o backend ainda tenta conectar por até 10 s, registrando cada tentativa no log. As peças
se encontram pelo nome na rede interna do Compose (`db`, `mcp-server`, `backend`).

### Testes

```bash
cd mcp-server && pip install -r requirements-dev.txt && pytest -q   # 8 testes das ferramentas
cd backend    && pip install -r requirements-dev.txt && pytest -q   # 38 testes: regras, erros e cotação
python scripts/avaliar.py      # com o ambiente no ar: 8 pedidos de teste, mostra os acertos
python scripts/qa_riscos.py    # com o ambiente no ar: bateria de QA focada em prejuízo
```

Os testes das ferramentas usam o protocolo MCP de verdade, com um cliente em memória, e a DummyJSON
simulada (`respx`). Nenhum teste unitário precisa de chave de LLM ou de rede. O CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) roda o lint, os testes e o build.

O `avaliar.py` checa o agente sem depender de impressão. Para cada pedido ele confere se foi
atendido, a quantidade e o teto de preço, sem exigir um produto específico. Cinco dos oito
pedidos **não** devem ser atendidos:
- tratores;
- laptops abaixo de US$ 500;
- um "iPhone 19", em que ele também confere se as alternativas sugeridas existem;
- um orçamento em reais;
- uma compra abaixo do mínimo do fornecedor.

A última execução deu 8/8.

### O agente e o que ele não pode inventar

- O prompt manda buscar em inglês e no singular (o catálogo é em inglês), tentar sinônimos,
  respeitar quantidade, orçamento, estoque e pedido mínimo, e responder sempre num JSON fixo:
  `{"itens": [{"produto_id", "quantidade"}], "orcamento": {"moeda", "max_unitario",
  "max_total"}, "resposta"}`. Perguntas e recusas também vão nesse JSON.
- O backend guarda todo produto que as ferramentas devolveram e confere a escolha contra eles
  ([backend/validacao.py](backend/validacao.py)). Ele recusa:
  - id que não veio do catálogo;
  - quantidade acima do estoque ou abaixo do pedido mínimo;
  - preço acima do limite por unidade e total acima do limite total;
  - um pedido com valor em reais ou euros e sem menção a dólar, por uma regra fixa que não
    depende do modelo.

  A conta é feita em `Decimal`.
- **Correção em vez de desistência:** a mesma conferência roda ainda dentro do loop. Se ela recusa
  (ex.: "Brown Leather Belt Watch: pedido 6, mínimo do fornecedor 7"), o modelo recebe o motivo e
  corrige uma vez. A correção aparece na lista de ferramentas do chat como
  `conferencia_do_sistema`, e o backend confere de novo antes de gravar.
- **Id chutado:** num teste com "mouses" (que a DummyJSON não tem), o modelo chutava ids (1 a 5,
  depois 101 a 104) para "explorar" o catálogo, mesmo com o prompt proibindo. A regra foi para o
  código: `detalhar_produto` com um id que não veio de busca nem chega ao catálogo.
- **Quando nada atende:** se a busca trouxe parecidos, o agente cita alternativas reais com preço
  ("iPhone 19 não existe; há iPhone 13 Pro por US$ 1.099,99…"). A requisição fica vazia e a
  pessoa decide. Sem critério nenhum ("Quero 2 celulares"), ele pede o modelo ou o orçamento em
  vez de escolher às cegas. Com quantidade e limite de preço, ele escolhe sozinho.
- **Por que o Haiku 4.5:** testei quais modelos a chave aceita com ferramentas
  ([scripts/testar_modelos.py](scripts/testar_modelos.py)). O Haiku 4.5 é rápido e passa na
  avaliação e na bateria de QA. A comparação com o Haiku 5.5 está em [Custo](#custo).

### O catálogo: o que usamos e o que sabemos

Analisei os 194 produtos da DummyJSON e os 22 campos de cada um. O agente usa preço, estoque e
pedido mínimo. O resto foi decidido assim:

- **Desconto: sabemos que existe e, de propósito, não aplicamos.** Todo produto tem
  `discountPercentage`, de 0,04% a 19,6% (cerca de 10% em média), e o carrinho da própria
  DummyJSON aplica esse desconto na compra. A requisição usa o preço de lista (`price`), sem
  desconto. Isso tem dois efeitos: o total pode sair acima do que seria cobrado, e um item que
  caberia no orçamento com desconto pode ser recusado. É o lado conservador. Qual preço vale para
  a compra (de lista, com desconto ou negociado com o fornecedor) é uma decisão de negócio do
  cliente. Com ela definida, a mudança é pequena: trazer o desconto na busca e calcular o preço
  em `backend/validacao.py`.
- **Prazo de envio** (`shippingInformation`, de "Ships overnight" a "Ships in 1 month"): aparece em
  cada item da requisição (coluna "Envio" do chat, traduzida) e é gravado no banco, porque
  dependendo do prazo a compra não faz sentido. O agente também vê o prazo na busca. Ainda não é
  uma regra conferida pelo código: num pedido com prazo ("preciso até sexta"), a escolha depende
  do modelo. Virar regra exige definir dias úteis ou corridos, e envio ou entrega.
- **Garantia e devolução** (`warrantyInformation`, `returnPolicy`): 12 produtos não têm garantia e
  44 não aceitam devolução. Não viraram regra porque dependem da política de compras do cliente
  (ex.: "eletrônico só com 1 ano de garantia"). Com a política, viram mais uma conferência no
  código.
- **Produtos impossíveis de comprar:** 17 têm estoque menor que o pedido mínimo (ex.: ovos, com
  estoque 9 e mínimo 32), e 8 deles aparecem no catálogo como "In Stock". O agente não depende
  desse status: as regras de estoque e de mínimo, juntas, recusam esses itens sem nenhuma regra
  especial.

### Cotação do dólar

O topo do chat mostra a PTAX do Banco Central (ex.: "Dólar PTAX 10/11: R$ 5,5840"), só como
referência. O backend busca a cotação (`backend/cotacao.py`, rota `GET /cotacao`), guarda por
1 hora e usa a última publicada nos últimos 7 dias, o que cobre fim de semana e feriado.

Ela fica fora do agente e da requisição, que continuam em dólar. De propósito, não há um "≈ R$" ao
lado do total: esse valor pareceria definitivo, mas não inclui impostos, frete nem o câmbio do dia
do pagamento. Se o Banco Central não responder, o selo mostra "Cotação do dólar indisponível" e o
resto funciona normalmente.

### Bateria de QA

[scripts/qa_riscos.py](scripts/qa_riscos.py) simula um QA de compras: cada cenário é um erro que
custaria dinheiro. Roda de ponta a ponta contra o sistema no ar e termina auditando cada item
gravado contra o catálogo real (preço, estoque e pedido mínimo).

| Achado na primeira rodada | Risco | Como ficou |
|---|---|---|
| "3 laptops de até **R$** 2.000" virou compra de laptops de US$ 1.099 a 1.499 | crítico: ~4x acima do orçamento | o modelo copia limite e moeda; o código confere; valor em reais sem dólar nunca vira compra |
| Requisições abaixo do mínimo do fornecedor (2 Realme C35, mínimo 3; relógios, mínimo 7) | alto: pedido recusado pelo fornecedor | o mínimo vem na busca e é conferido no código |
| "3 laptops de até US$ 1.200" recusado: achou que precisava de 3 modelos diferentes | médio: retrabalho | prompt: N unidades podem ser do mesmo modelo |
| Pergunta ou recusa do modelo saía do JSON e chegava à pessoa como erro 502 | médio: a pessoa via um erro | JSON sempre; o texto que sobrar vira resposta, sem requisição |
| Busca vazia chegava ao modelo como texto vazio, e ele culpava o catálogo | baixo | a lista vazia chega explícita |
| O mesmo pedido enviado 2 vezes ao mesmo tempo cria 2 requisições | alto em produção | **em aberto**, ver [limites](#limites-conhecidos) |

Resultado atual: **22/22 cenários** e a auditoria do banco sem nenhum problema. Também passaram 4/4
payloads inválidos, SQL injection no texto e as tentativas de mudar o preço ou as regras pelo
pedido.

### Custo

Medido pelo próprio relatório de uso da OpenRouter, em 6 pedidos variados (3 sem critério, 1 com
orçamento, o pedido de exemplo e um item inexistente):

| | Haiku 4.5 (em uso) | Haiku 5.5 (testado) |
|---|---|---|
| Preço por 1M de tokens (entrada / saída) | US$ 1,00 / 5,00 | US$ 0,10 / 0,50 |
| Tokens por pedido (entrada / saída) | ~4.800 / ~250 | ~7.400 / ~700 |
| Chamadas ao modelo por pedido | 2,2 | 2,3 |
| **Custo por pedido** | **~US$ 0,006** | ~US$ 0,001 |
| Tempo por pedido | ~4 s | ~6 s |

- Mil pedidos custam cerca de US$ 6. O limite de 8 passos e 100 s segura o pior caso em alguns
  centavos.
- O Haiku 5.5 tem o token 10x mais barato, mas o pedido sai cerca de 5x mais barato, porque ele
  gasta mais tokens. Também é 1,5x mais lento e, no teste, não seguiu uma regra do prompt: em
  "Quero 2 celulares", sem critério nenhum, escolheu um modelo sozinho. Se o volume crescer, é a
  primeira alavanca. Antes, porém, seria preciso ajustar o prompt e passar de novo pela avaliação
  e pela bateria de QA.
- O cache de prompt ainda não se aplica: a parte fixa (instruções e ferramentas) tem cerca de
  2 mil tokens, e o mínimo para cachear no Haiku 4.5 é 4.096.

### Tratamento de erros

Toda falha devolve um código fixo, uma mensagem que diz o que aconteceu e se algo foi gravado, e
uma linha no log (`docker compose logs backend mcp-server`). O formato é sempre o mesmo:

```json
{"detail": {"codigo": "CATALOGO_INDISPONIVEL", "mensagem": "...", "ferramentas": [...]}}
```

| O que falhou | Código | HTTP | O que a pessoa vê no chat |
|---|---|---|---|
| DummyJSON lenta (> 5 s), fora do ar, erro 5xx ou resposta que não é JSON | `CATALOGO_INDISPONIVEL` | 503 | "O catálogo está fora do ar ou lento. Nenhuma requisição foi criada." |
| Servidor MCP fora do ar ou caiu no meio | `FERRAMENTAS_INDISPONIVEIS` | 503 | "O serviço de catálogo (MCP) não respondeu." |
| Chave ausente, inválida/expirada (401), sem crédito (402), modelo bloqueado (403/404), limite (429) | `LLM_INDISPONIVEL` | 502 | o motivo exato, por exemplo "a chave da OpenRouter é inválida ou expirou" |
| Modelo devolveu resposta vazia ou não concluiu em 8 passos | `RESPOSTA_INVALIDA` | 502 | "O agente não conseguiu concluir o pedido. Tente reformular." |
| Agente passou de 100 s no total | `TEMPO_ESGOTADO` | 504 | "O agente não concluiu em 100 s." |
| Escolha que não confere mesmo depois da correção: id fora do catálogo, acima do estoque, abaixo do mínimo, acima do orçamento, orçamento em outra moeda | `ESCOLHA_INVALIDA` | 422 | o motivo, por exemplo "Realme C35: pedido 2, mínimo do fornecedor 3" |
| Postgres fora do ar ao gravar ou listar | `BANCO_INDISPONIVEL` | 503 | "Nada foi salvo." |
| Pedido vazio ou com mais de 1000 caracteres | `PEDIDO_INVALIDO` | 422 | "Escreva o pedido com 3 a 1000 caracteres." |
| Banco Central fora do ar ou sem PTAX nos últimos 7 dias (só a cotação do topo) | `COTACAO_INDISPONIVEL` | 503 | o selo mostra "Cotação do dólar indisponível"; os pedidos seguem normais |
| Qualquer erro não previsto | `ERRO_INTERNO` | 500 | "Erro inesperado no servidor." (o stack vai para o log) |
| Backend caiu (o nginx responde no lugar dele) ou o navegador perdeu a conexão | `HTTP_502`, `SEM_CONEXAO` | — | "O backend está fora do ar ou não respondeu." |

O que o modelo pode corrigir sozinho não interrompe a conversa: produto não encontrado, id que não
veio de busca, argumentos mal formados e escolha recusada pela conferência. Nesses casos ele recebe
o motivo, tenta de novo, e a chamada aparece em vermelho na lista "Ferramentas chamadas". Se o
modelo responder em texto livre (uma pergunta, uma recusa) mesmo depois de uma segunda chance, o
texto vira a resposta, sem requisição, e o log registra `RESPOSTA_FORA_DO_FORMATO`. Cada linha da
tabela tem um teste ([backend/tests/test_erros.py](backend/tests/test_erros.py) e
[mcp-server/tests/test_tools.py](mcp-server/tests/test_tools.py)).

"Nada encontrado" **não** é erro: o agente responde que o catálogo não tem o item, com
`atendido: false`.

Para ver a falha do catálogo acontecendo, coloque `DUMMYJSON_URL=http://10.255.255.1` no `.env`
(um IP que não responde), rode `docker compose up -d` e envie um pedido. Depois apague a linha e
suba de novo.

### Limites conhecidos

- **Duplicidade:** o mesmo pedido enviado duas vezes ao mesmo tempo cria duas requisições. O chat
  trava o botão enquanto espera, mas um cliente da API que repete a chamada duplica a compra. O
  próximo passo é aceitar um cabeçalho `Idempotency-Key` gravado com restrição de unicidade.
- **Sem memória entre pedidos:** cada pedido é independente. Quando falta algo, o agente diz o que
  incluir num novo pedido, em vez de fazer uma pergunta que a pessoa não teria como responder.
- **Orçamento:** o código confere a escolha contra o limite que o modelo leu no pedido. Se o modelo
  ler errado (ex.: "US$ 150 cada" como total), o erro tende ao lado seguro, que é recusar.
- **Preço de lista e prazo de envio:** o desconto do catálogo não é aplicado, e o prazo de envio é
  mostrado, mas não conferido contra um prazo pedido. Ver
  [O catálogo](#o-catálogo-o-que-usamos-e-o-que-sabemos).
- **Sem conversão de moeda**, sem reserva de estoque (o estoque é conferido na hora do pedido, não
  reservado) e sem autenticação.
- **Busca por palavra em inglês:** um item que existe com outro nome pode não ser achado.
- O LLM não é determinístico. Por isso as regras de dinheiro estão no código, e a avaliação e a
  bateria de QA foram rodadas várias vezes.

### Como usei IA

O projeto foi construído com o **Claude Code**, o agente de programação da Anthropic, do jeito que
trabalho no dia a dia:

- **Eu defini** o escopo a partir do enunciado, a ordem das peças (MCP → backend → chat → extras,
  um commit por peça) e o critério de pronto de cada uma, e revisei as decisões acima.
- **O Claude Code escreveu** a maior parte do código, rodou os testes e o ambiente, e investigou os
  erros que apareceram.
- **Eu testei o chat com pedidos fora do roteiro.** Foi assim que surgiram três casos:
  - os "mouses", que revelaram os ids chutados;
  - o "iPhone 19", que virou a regra das alternativas;
  - os "celulares", que mostraram o agente perguntando quando já tinha como escolher.
- **Pedi ao Claude Code para agir como um QA focado em prejuízo.** A bateria `qa_riscos.py` achou
  3 falhas que custariam dinheiro e 2 respostas que viravam erro. Corrigimos todas, e os casos
  críticos entraram no `avaliar.py` para não voltarem.
- **Uma lição de prompt:** um ajuste amplo demais fez o pedido de exemplo regredir (o agente passou
  a perguntar o modelo), e o `avaliar.py` pegou na hora. Desde então, só entra no prompt a linha
  que corrige uma falha medida. A mesma vigilância valeu para dados novos: quando o prazo de envio
  entrou na busca, o modelo passou a "ajustar" a quantidade para o mínimo do fornecedor (2 virava
  3, ou seja, comprava mais que o pedido). A avaliação pegou, e a regra "nunca mude a quantidade
  pedida" entrou no prompt. Depois disso, três rodadas seguidas deram 8/8 e 22/22.
- **O que os testes pegaram no caminho** (está no histórico de commits):
  - a chave com bloqueio ZDR recusando o primeiro modelo;
  - o cliente MCP embrulhando erros num `ExceptionGroup`, o que escondia a causa real;
  - o resultado em lista do MCP chegando um bloco por produto, o que fez a trava anti-invenção
    recusar um produto legítimo. Corrigi a leitura, não a trava;
  - corridas na primeira subida do banco e no reinício do backend.
