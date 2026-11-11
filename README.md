# BuyME: agente de compras

*Buy* + *ME*: o agente de compras pensado para o dia a dia da Mercado Eletrônico.

A pessoa escreve o que precisa num chat. O agente busca no catálogo da DummyJSON por um servidor
MCP, escolhe os itens e salva uma requisição de compra no Postgres. Se nada atende, diz o que
faltou, sugere o que existe e não cria requisição.

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
2. **Preço, estoque e total vêm do catálogo e do código, nunca do LLM.** O modelo devolve só id e
   quantidade, e um id que não veio de uma busca é recusado como invenção.
3. **Nenhum erro silencioso.** Toda falha tem código, mensagem clara no chat e linha no log
   ([tabela](#tratamento-de-erros)).
4. **Modelo `anthropic/claude-haiku-4.5`.** A chave do case bloqueia provedores sem
   *Zero Data Retention*, e o `gpt-4o-mini` foi recusado. Para trocar, mude `OPENROUTER_MODEL`.
5. **Gravar fica no backend, não numa 3ª ferramenta MCP.** A escrita passa pela validação do item 2
   numa transação: requisição e itens entram juntos, ou nada entra.

**Uso de IA:** o projeto foi construído com o Claude Code. Os detalhes estão em
[Como usei IA](#como-usei-ia).

---

## Detalhes

### Peças

| Peça | Porta | O que é |
|---|---|---|
| `frontend` | 3000 | chat em React (o nginx serve o build e repassa `/api` ao backend) |
| `backend` | 8000 | FastAPI com `POST /requisicoes` e `GET /requisicoes`; o agente mora aqui |
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
cd backend    && pip install -r requirements-dev.txt && pytest -q   # 18 testes: regras e erros
python scripts/avaliar.py    # com o ambiente no ar: 6 pedidos de teste, mostra os acertos
```

Os testes das ferramentas usam o protocolo MCP de verdade, com um cliente em memória, e a DummyJSON
simulada (`respx`). Nenhum teste precisa de chave de LLM ou de rede. O CI
([.github/workflows/ci.yml](.github/workflows/ci.yml)) roda o lint, os testes e o build.

O `avaliar.py` checa o agente sem depender de impressão. Para cada pedido ele confere se foi
atendido, a quantidade e o teto de preço, sem exigir um produto específico. Três dos seis pedidos
**não** devem ser atendidos: tratores, laptops abaixo de US$ 500 e um "iPhone 19". No caso do
iPhone, ele também confere se as alternativas sugeridas existem no catálogo. A última execução deu 6/6.

### O agente e o que ele não pode inventar

- O prompt manda buscar em inglês (o catálogo é em inglês), tentar sinônimos antes de desistir,
  respeitar quantidade e orçamento e responder num JSON fixo: `{"itens": [{"produto_id",
  "quantidade"}], "resposta"}`.
- O backend guarda todo produto que as ferramentas devolveram e monta os itens a partir deles
  ([backend/validacao.py](backend/validacao.py)). Um id que não veio do catálogo é recusado, uma
  quantidade acima do estoque também, e a conta é feita em `Decimal`.
- **Id chutado:** num teste com "mouses" (que a DummyJSON não tem), o modelo chutava ids (1 a 5,
  depois 101 a 104) para "explorar" o catálogo, mesmo com o prompt proibindo. A regra foi para o
  código: `detalhar_produto` com um id que não veio de busca nem chega ao catálogo.
- **Quando nada atende:** se a busca trouxe parecidos, o agente cita até 3 alternativas reais com
  preço ("iPhone 19 não existe; há iPhone 13 Pro por US$ 1.099,99…"). A requisição fica vazia e a
  pessoa decide.
- **Por que o Haiku:** testei quais modelos a chave aceita com ferramentas
  ([scripts/testar_modelos.py](scripts/testar_modelos.py)). Passaram o Claude Haiku/Sonnet 4.5, o
  GPT-4.1-mini e o GPT-5-mini. O Haiku é rápido, barato e bom em chamar ferramentas.

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
| Modelo não devolveu o JSON final (mesmo depois de uma segunda chance) ou não concluiu em 8 passos | `RESPOSTA_INVALIDA` | 502 | "O agente não conseguiu concluir o pedido. Tente reformular." |
| Agente passou de 100 s no total | `TEMPO_ESGOTADO` | 504 | "O agente não concluiu em 100 s." |
| Id que não veio do catálogo, quantidade acima do estoque ou item mal formado | `ESCOLHA_INVALIDA` | 422 | o motivo, por exemplo "pedido 3, estoque 2" |
| Postgres fora do ar ao gravar ou listar | `BANCO_INDISPONIVEL` | 503 | "Nada foi salvo." |
| Pedido vazio ou com mais de 1000 caracteres | `PEDIDO_INVALIDO` | 422 | "Escreva o pedido com 3 a 1000 caracteres." |
| Qualquer erro não previsto | `ERRO_INTERNO` | 500 | "Erro inesperado no servidor." (o stack vai para o log) |
| Backend caiu (o nginx responde no lugar dele) ou o navegador perdeu a conexão | `HTTP_502`, `SEM_CONEXAO` | — | "O backend está fora do ar ou não respondeu." |

O que o modelo pode corrigir sozinho não interrompe a conversa: produto não encontrado, id que não
veio de busca e argumentos mal formados. Nesses casos ele recebe o motivo como resultado da
ferramenta, tenta de novo, e a chamada aparece em vermelho na lista "Ferramentas chamadas". Cada
linha da tabela tem um teste ([backend/tests/test_erros.py](backend/tests/test_erros.py) e
[mcp-server/tests/test_tools.py](mcp-server/tests/test_tools.py)).

"Nada encontrado" **não** é erro: o agente responde que o catálogo não tem o item, com
`atendido: false`.

Para ver a falha do catálogo acontecendo, coloque `DUMMYJSON_URL=http://10.255.255.1` no `.env`
(um IP que não responde), rode `docker compose up -d` e envie um pedido. Depois apague a linha e
suba de novo.

### Limites conhecidos

A DummyJSON tem cerca de 190 produtos e a busca é por palavra em inglês. O prompt orienta a
traduzir ("notebook" → "laptop"), mas um item que existe com outro nome pode não ser achado. Os
preços estão em USD, sem conversão de moeda, e não há autenticação.

### Como usei IA

O projeto foi construído com o **Claude Code**, o agente de programação da Anthropic, do jeito que
trabalho no dia a dia:

- **Eu defini** o escopo a partir do enunciado, a ordem das peças (MCP → backend → chat → extras,
  um commit por peça) e o critério de pronto de cada uma, e revisei as decisões acima.
- **O Claude Code escreveu** a maior parte do código, rodou os testes e o ambiente, e investigou os
  erros que apareceram.
- **Eu testei o chat com pedidos fora do roteiro.** Foi assim que surgiram o caso dos "mouses" (que
  revelou os ids chutados) e o do "iPhone 19" (que virou a regra das alternativas).
- **O que os testes pegaram no caminho** (está no histórico de commits):
  - a chave com bloqueio ZDR recusando o primeiro modelo;
  - o cliente MCP embrulhando erros num `ExceptionGroup`, o que escondia a causa real;
  - o resultado em lista do MCP chegando um bloco por produto, o que fez a trava anti-invenção
    recusar um produto legítimo. Corrigi a leitura, não a trava.
