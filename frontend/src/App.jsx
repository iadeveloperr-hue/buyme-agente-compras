import { useState } from "react";

const usd = (v) => v.toLocaleString("en-US", { style: "currency", currency: "USD" });
const EXEMPLO = "Preciso de 3 laptops para o time de vendas, até US$ 2.000 cada.";
const TIMEOUT_MS = 120_000;

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
      // O backend devolve {detail: {codigo, mensagem, ferramentas}} nos erros conhecidos.
      const d = corpo?.detail;
      return { erro: d?.mensagem || `O servidor respondeu ${r.status}.`, ferramentas: d?.ferramentas || [] };
    }
    return corpo;
  } catch (e) {
    return {
      erro: e.name === "AbortError"
        ? "O agente demorou demais para responder. Tente de novo."
        : "Não consegui falar com o backend. Ele está rodando?",
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
          <tr><th>Item</th><th>Qtd.</th><th>Preço unit.</th><th>Subtotal</th></tr>
        </thead>
        <tbody>
          {req.itens.map((i) => (
            <tr key={i.produto_id}>
              <td>{i.titulo}</td><td>{i.quantidade}</td>
              <td>{usd(i.preco_unitario_usd)}</td><td>{usd(i.subtotal_usd)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr><td colSpan={3}>Total</td><td>{usd(req.total_usd)}</td></tr>
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
      {r.erro ? <p>⚠️ {r.erro}</p> : <p>{r.resposta}</p>}
      {r.requisicao && <Requisicao req={r.requisicao} />}
      {!r.erro && !r.atendido && <p className="aviso">Nenhuma requisição foi criada.</p>}
      <Ferramentas lista={r.ferramentas} />
    </div>
  );
}

export default function App() {
  const [texto, setTexto] = useState(EXEMPLO);
  const [msgs, setMsgs] = useState([]);
  const ocupado = msgs.some((m) => m.carregando);

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
      <h1>Agente de compras</h1>
      <p className="sub">Descreva o que precisa. O agente busca no catálogo e monta a requisição.</p>
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
