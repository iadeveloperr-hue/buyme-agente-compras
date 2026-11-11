import { useEffect, useState } from "react";

const usd = (v) => v.toLocaleString("en-US", { style: "currency", currency: "USD" });
const EXEMPLO = "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.";
const TIMEOUT_MS = 120_000;

// Os seis prazos que o catálogo usa; qualquer outro aparece como veio.
const PRAZOS = {
  "Ships overnight": "no dia seguinte",
  "Ships in 1-2 business days": "em 1 a 2 dias úteis",
  "Ships in 3-5 business days": "em 3 a 5 dias úteis",
  "Ships in 1 week": "em 1 semana",
  "Ships in 2 weeks": "em 2 semanas",
  "Ships in 1 month": "em 1 mês",
};
const prazo = (p) => (p ? PRAZOS[p] || p : "—");

async function buscarCotacao() {
  try {
    const r = await fetch("/api/cotacao");
    const corpo = await r.json().catch(() => null);
    if (r.ok && typeof corpo?.venda === "number") return corpo;
    return { erro: corpo?.detail?.mensagem || "Cotação do dólar indisponível." };
  } catch {
    return { erro: "Cotação do dólar indisponível." };
  }
}

// Só referência: os preços e a requisição continuam em dólar (ver backend/cotacao.py).
function Cotacao({ c }) {
  if (!c) return null;
  if (c.erro) return <div className="cotacao indisponivel" title={c.erro}>Cotação do dólar indisponível</div>;
  const [, mes, dia] = c.data.split("-");
  const valor = c.venda.toLocaleString("pt-BR", { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  return (
    <div className="cotacao" title="Cotação de referência. Os preços e a requisição são em dólar.">
      Dólar PTAX {dia}/{mes}: <b>R$ {valor}</b> <span>· Banco Central</span>
    </div>
  );
}

async function enviarPedido(pedido) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch("/api/requisicoes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pedido }),
      signal: ctrl.signal,
    });
    const corpo = await r.json().catch(() => null);
    if (!r.ok) {
      // Todo erro do backend vem como {detail: {codigo, mensagem, ferramentas}}.
      const d = corpo?.detail;
      if (d?.mensagem) return { erro: d.mensagem, codigo: d.codigo, ferramentas: d.ferramentas || [] };
      // Sem esse formato, quem respondeu foi o nginx: o backend caiu ou não respondeu.
      return {
        erro: [502, 503, 504].includes(r.status)
          ? "O backend está fora do ar ou não respondeu. Nenhuma requisição foi criada."
          : `O servidor respondeu ${r.status} sem explicação. Nenhuma requisição foi criada.`,
        codigo: `HTTP_${r.status}`, ferramentas: [],
      };
    }
    if (!corpo || typeof corpo.atendido !== "boolean") {
      return { erro: "O backend respondeu algo que não consegui ler.", codigo: "RESPOSTA_ILEGIVEL",
               ferramentas: [] };
    }
    return corpo;
  } catch (e) {
    return {
      erro: e.name === "AbortError"
        ? "O agente demorou demais para responder. Tente de novo."
        : "Não consegui falar com o backend. Ele está rodando?",
      codigo: e.name === "AbortError" ? "TEMPO_ESGOTADO" : "SEM_CONEXAO",
      ferramentas: [],
    };
  } finally {
    clearTimeout(t);
  }
}

function Ferramentas({ lista }) {
  if (!lista?.length) return null;
  return (
    <details className="ferramentas">
      <summary>Ferramentas chamadas pelo agente ({lista.length})</summary>
      <ol>
        {lista.map((f, i) => (
          <li key={i} className={f.erro ? "falhou" : ""}>
            <code>{f.nome}({JSON.stringify(f.argumentos)})</code>
            {f.erro && <span> — {f.erro}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Requisicao({ req }) {
  return (
    <div className="requisicao">
      <div className="numero">Requisição nº {req.id}</div>
      <table>
        <thead>
          <tr><th>Item</th><th>Qtd.</th><th>Preço unit.</th><th>Envio</th><th>Subtotal</th></tr>
        </thead>
        <tbody>
          {req.itens.map((i) => (
            <tr key={i.produto_id}>
              <td>{i.titulo}</td><td>{i.quantidade}</td>
              <td>{usd(i.preco_unitario_usd)}</td><td className="envio">{prazo(i.prazo_envio)}</td>
              <td>{usd(i.subtotal_usd)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><td colSpan={4}>Total</td><td>{usd(req.total_usd)}</td></tr>
        </tfoot>
      </table>
    </div>
  );
}

function Mensagem({ m }) {
  if (m.autor === "pessoa") return <div className="msg pessoa">{m.texto}</div>;
  if (m.carregando) return <div className="msg agente carregando">Consultando o catálogo…</div>;
  const r = m.resposta;
  return (
    <div className={`msg agente ${r.erro ? "erro" : ""}`}>
      {r.erro ? <p>⚠️ {r.erro} {r.codigo && <code className="codigo">{r.codigo}</code>}</p>
              : <p>{r.resposta}</p>}
      {r.requisicao && <Requisicao req={r.requisicao} />}
      {!r.erro && !r.atendido && <p className="aviso">Nenhuma requisição foi criada.</p>}
      <Ferramentas lista={r.ferramentas} />
    </div>
  );
}

export default function App() {
  const [texto, setTexto] = useState(EXEMPLO);
  const [msgs, setMsgs] = useState([]);
  const [cotacao, setCotacao] = useState(null);
  const ocupado = msgs.some((m) => m.carregando);

  useEffect(() => { buscarCotacao().then(setCotacao); }, []);

  async function enviar(e) {
    e.preventDefault();
    const pedido = texto.trim();
    if (!pedido || ocupado) return;
    setTexto("");
    setMsgs((ms) => [...ms, { autor: "pessoa", texto: pedido }, { autor: "agente", carregando: true }]);
    const resposta = await enviarPedido(pedido);
    setMsgs((ms) => [...ms.slice(0, -1), { autor: "agente", resposta }]);
  }

  return (
    <main>
      <header className="topo">
        <div>
          <h1>BuyME <span className="tag">agente de compras</span></h1>
          <p className="sub">Descreva o que precisa. O agente busca no catálogo e monta a requisição.</p>
        </div>
        <Cotacao c={cotacao} />
      </header>
      <section className="conversa">
        {msgs.map((m, i) => <Mensagem key={i} m={m} />)}
      </section>
      <form onSubmit={enviar}>
        <input value={texto} onChange={(e) => setTexto(e.target.value)} placeholder="Ex.: 4 relógios até US$ 150 cada" />
        <button disabled={ocupado || !texto.trim()}>{ocupado ? "Aguarde…" : "Enviar"}</button>
      </form>
    </main>
  );
}
