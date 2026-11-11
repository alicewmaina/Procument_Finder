// Small localStorage wrapper. Data remains in this browser only.
window.Store = {
  get(key, fallback = []) { try { return JSON.parse(localStorage.getItem("pf_" + key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { localStorage.setItem("pf_" + key, JSON.stringify(value)); },
  addUnique(key, value) { const items = this.get(key); if (!items.includes(value)) items.push(value); this.set(key, items); return items; },
  remove(key, value) { const items = this.get(key).filter(x => x !== value); this.set(key, items); return items; }
};