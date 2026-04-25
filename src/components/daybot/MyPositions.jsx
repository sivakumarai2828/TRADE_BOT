import { useEffect, useState } from "react";
import { addUserPosition, closeUserPosition, fetchUserPositions } from "../../api.js";

const EMPTY_FORM = {
  symbol: "",
  side: "BUY",
  asset_type: "stock",
  qty: "",
  entry_price: "",
  stop_price: "",
  target_price: "",
  notes: "",
  option_type: "call",
  strike: "",
  expiry: "",
  underlying_stop: "",
};

export default function MyPositions() {
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [closingId, setClosingId] = useState(null);
  const [exitPrices, setExitPrices] = useState({});

  const load = () =>
    fetchUserPositions()
      .then((d) => setPositions(d.positions ?? []))
      .catch(() => {})
      .finally(() => setLoading(false));

  useEffect(() => {
    load();
  }, []);

  const handleSave = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const body = {
        symbol: form.symbol.toUpperCase(),
        side: form.side,
        asset_type: form.asset_type,
        qty: parseFloat(form.qty),
        entry_price: parseFloat(form.entry_price),
        stop_price: form.stop_price ? parseFloat(form.stop_price) : undefined,
        target_price: form.target_price ? parseFloat(form.target_price) : undefined,
        notes: form.notes || undefined,
      };
      if (form.asset_type === "option") {
        Object.assign(body, {
          option_type: form.option_type,
          strike: form.strike ? parseFloat(form.strike) : undefined,
          expiry: form.expiry || undefined,
          underlying_stop: form.underlying_stop ? parseFloat(form.underlying_stop) : undefined,
        });
      }
      await addUserPosition(body);
      setForm(EMPTY_FORM);
      setShowForm(false);
      load();
    } catch (err) {
      alert(`Save failed: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handleClose = async (id) => {
    const ep = parseFloat(exitPrices[id] || "0");
    if (!ep) return alert("Enter exit price first");
    setClosingId(id);
    try {
      await closeUserPosition(id, ep);
      load();
    } catch (err) {
      alert(`Close failed: ${err.message}`);
    } finally {
      setClosingId(null);
    }
  };

  const fieldCls =
    "w-full rounded-md border border-neutral-700 bg-neutral-800 px-2 py-1.5 text-xs text-white placeholder-neutral-600 focus:border-indigo-500 focus:outline-none";

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-white">My Robinhood Positions</h3>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
        >
          {showForm ? "Cancel" : "+ Log Trade"}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSave} className="mb-4 rounded-lg border border-neutral-700 bg-neutral-800/50 p-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Symbol</label>
              <input className={fieldCls} placeholder="AAPL" value={form.symbol}
                onChange={(e) => setForm({ ...form, symbol: e.target.value })} required />
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Side</label>
              <select className={fieldCls} value={form.side}
                onChange={(e) => setForm({ ...form, side: e.target.value })}>
                <option>BUY</option>
                <option>SELL</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Type</label>
              <select className={fieldCls} value={form.asset_type}
                onChange={(e) => setForm({ ...form, asset_type: e.target.value })}>
                <option value="stock">Stock</option>
                <option value="option">Option</option>
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Qty</label>
              <input className={fieldCls} type="number" placeholder="10" value={form.qty}
                onChange={(e) => setForm({ ...form, qty: e.target.value })} required />
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Entry $</label>
              <input className={fieldCls} type="number" step="0.01" placeholder="180.00" value={form.entry_price}
                onChange={(e) => setForm({ ...form, entry_price: e.target.value })} required />
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Stop $</label>
              <input className={fieldCls} type="number" step="0.01" placeholder="175.00" value={form.stop_price}
                onChange={(e) => setForm({ ...form, stop_price: e.target.value })} />
            </div>
            <div>
              <label className="mb-1 block text-xs text-neutral-500">Target $</label>
              <input className={fieldCls} type="number" step="0.01" placeholder="190.00" value={form.target_price}
                onChange={(e) => setForm({ ...form, target_price: e.target.value })} />
            </div>
            {form.asset_type === "option" && (
              <>
                <div>
                  <label className="mb-1 block text-xs text-neutral-500">Call/Put</label>
                  <select className={fieldCls} value={form.option_type}
                    onChange={(e) => setForm({ ...form, option_type: e.target.value })}>
                    <option value="call">Call</option>
                    <option value="put">Put</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-neutral-500">Strike $</label>
                  <input className={fieldCls} type="number" step="0.5" placeholder="185" value={form.strike}
                    onChange={(e) => setForm({ ...form, strike: e.target.value })} />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-neutral-500">Expiry</label>
                  <input className={fieldCls} type="date" value={form.expiry}
                    onChange={(e) => setForm({ ...form, expiry: e.target.value })} />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-neutral-500">Underlying Stop $</label>
                  <input className={fieldCls} type="number" step="0.01" placeholder="178.00" value={form.underlying_stop}
                    onChange={(e) => setForm({ ...form, underlying_stop: e.target.value })} />
                </div>
              </>
            )}
            <div className="col-span-2 sm:col-span-3">
              <label className="mb-1 block text-xs text-neutral-500">Notes (optional)</label>
              <input className={fieldCls} placeholder="e.g. earnings play" value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })} />
            </div>
          </div>
          <button
            type="submit"
            disabled={saving}
            className="mt-3 rounded-md bg-emerald-600 px-4 py-1.5 text-xs font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save Position"}
          </button>
        </form>
      )}

      {loading ? (
        <p className="text-xs text-neutral-500">Loading…</p>
      ) : positions.length === 0 ? (
        <p className="text-xs text-neutral-500">No open positions logged. Use "+ Log Trade" to track a Robinhood trade.</p>
      ) : (
        <div className="flex flex-col gap-2">
          {positions.map((p) => {
            const isOption = p.asset_type === "option";
            const stop = p.stop_price ?? p.underlying_stop;
            return (
              <div key={p.id} className="rounded-lg border border-neutral-700/60 bg-neutral-800/40 p-3">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <span className="font-semibold text-white">{p.symbol}</span>
                    {isOption && (
                      <span className="ml-2 text-xs text-neutral-400">
                        {p.option_type?.toUpperCase()} ${p.strike} exp {p.expiry}
                      </span>
                    )}
                    <span className={`ml-2 text-xs font-medium ${p.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>
                      {p.side}
                    </span>
                  </div>
                  <span className="rounded px-1.5 py-0.5 text-xs bg-neutral-700 text-neutral-300">
                    {isOption ? "Option" : "Stock"}
                  </span>
                </div>

                <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                  <div>
                    <span className="text-neutral-600">Entry</span>
                    <p className="text-neutral-200">${parseFloat(p.entry_price).toFixed(2)}</p>
                  </div>
                  <div>
                    <span className="text-neutral-600">Stop</span>
                    <p className="text-rose-400">{stop != null ? `$${parseFloat(stop).toFixed(2)}` : "—"}</p>
                  </div>
                  <div>
                    <span className="text-neutral-600">Target</span>
                    <p className="text-emerald-400">
                      {p.target_price != null ? `$${parseFloat(p.target_price).toFixed(2)}` : "—"}
                    </p>
                  </div>
                </div>

                {p.notes && <p className="mt-1 text-xs text-neutral-600 italic">{p.notes}</p>}

                <div className="mt-2 flex items-center gap-2">
                  <input
                    type="number"
                    step="0.01"
                    placeholder="Exit price"
                    className="w-28 rounded border border-neutral-700 bg-neutral-900 px-2 py-1 text-xs text-white placeholder-neutral-600 focus:border-indigo-500 focus:outline-none"
                    value={exitPrices[p.id] ?? ""}
                    onChange={(e) => setExitPrices({ ...exitPrices, [p.id]: e.target.value })}
                  />
                  <button
                    onClick={() => handleClose(p.id)}
                    disabled={closingId === p.id}
                    className="rounded bg-rose-600/80 px-2 py-1 text-xs text-white hover:bg-rose-600 disabled:opacity-50"
                  >
                    {closingId === p.id ? "Closing…" : "Close"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-3 text-xs text-neutral-600">
        Bot monitors these positions every 5 min and sends Telegram alerts on stop breach.
      </p>
    </div>
  );
}
