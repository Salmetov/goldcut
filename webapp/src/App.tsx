import { useEffect, useState } from "react";
import { getMe, getDeliveries, subscribe, type Me, type Delivery } from "./api";
import { openInvoice } from "./telegram";

function mmss(s: number | null): string {
  if (s == null) return "";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([getMe(), getDeliveries()])
      .then(([m, d]) => { setMe(m); setDeliveries(d.items); })
      .catch((e) => setErr(String(e)));
  }, []);

  async function onSubscribe() {
    setBusy(true);
    try {
      const { invoice_url } = await subscribe();
      const status = await openInvoice(invoice_url);
      if (status === "paid") setMe(await getMe());
    } catch (e) { setErr(String(e)); } finally { setBusy(false); }
  }

  if (err) return <div className="wrap"><div className="card err">Ошибка: {err}<br /><span className="dim">Открой через кнопку в боте @goldcut_dev_bot</span></div></div>;
  if (!me) return <div className="wrap"><div className="dim center">Загрузка…</div></div>;

  const q = me.quota;
  const paid = me.plan === "paid";

  return (
    <div className="wrap">
      <h1>GoldCut</h1>

      <div className="card">
        <div className="row"><span>План</span><b>{paid ? "Premium ∞" : "Триал"}</b></div>
        <div className="row">
          <span>Вырезки за неделю</span>
          <b>{paid ? "без лимита" : `${q.used} / ${q.limit}`}</b>
        </div>
        {!paid && (
          <button className="btn primary" disabled={busy} onClick={onSubscribe}>
            {busy ? "…" : "Оформить Premium ⭐"}
          </button>
        )}
      </div>

      <h2>Мои вырезки {deliveries.length ? `(${deliveries.length})` : ""}</h2>
      <div className="card">
        {deliveries.length === 0 && <div className="dim center">Пока пусто — вырежи первый кусок в боте</div>}
        {deliveries.map((d) => (
          <div key={d.id} className="clip">
            <div className="clip-title">{d.title || "Вырезка"}</div>
            <div className="dim small">
              {mmss(d.start_s)}–{mmss(d.end_s)}
              {d.created_at ? " · " + d.created_at.slice(0, 10) : ""}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
