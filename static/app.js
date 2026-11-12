/* ProcureFind front end: streaming search results, result filters, and the compare basket. */
(function () {
  'use strict';

  // ---- helpers -------------------------------------------------------------------------
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var safeUrl = function (u) { return /^https?:\/\//i.test(u || '') ? u : '#'; };
  var kes = function (n, d) {
    if (n == null) return '-';
    return 'KES ' + Number(n).toLocaleString('en-US', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  };
  var host = function (u) { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var MAX = 5;
  var WARN = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/></svg>';
  var CHECK = '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12l5 5 9-10"/></svg>';

  // ---- compare basket (kept in this browser) ---------------------------------------------
  var Basket = {
    key: 'pf-compare',
    all: function () { try { return JSON.parse(localStorage.getItem(this.key)) || []; } catch (e) { return []; } },
    save: function (items) { try { localStorage.setItem(this.key, JSON.stringify(items)); } catch (e) {} Basket.changed(); },
    has: function (ref) { return this.all().some(function (i) { return i.ref === ref; }); },
    toggle: function (item) {
      var items = this.all(), at = items.findIndex(function (i) { return i.ref === item.ref; });
      if (at >= 0) { items.splice(at, 1); this.save(items); return false; }
      if (items.length >= MAX) { toast('You can compare up to ' + MAX + ' offers. Remove one first.'); return false; }
      items.push(item); this.save(items); return true;
    },
    remove: function (ref) { this.save(this.all().filter(function (i) { return i.ref !== ref; })); },
    clear: function () { this.save([]); },
    changed: function () {
      var n = Basket.all().length;
      document.querySelectorAll('[data-compare-count]').forEach(function (el) { el.textContent = n; el.hidden = n === 0; });
      renderTray();
      document.dispatchEvent(new CustomEvent('basket'));
    }
  };
  window.addEventListener('storage', function (e) { if (e.key === Basket.key) Basket.changed(); });

  function compareForm(items) {
    var f = document.createElement('form');
    f.method = 'post'; f.action = '/compare';
    items.forEach(function (i) {
      var inp = document.createElement('input'); inp.type = 'hidden'; inp.name = 'ref'; inp.value = i.ref; f.appendChild(inp);
    });
    document.body.appendChild(f);
    return f;
  }

  function renderTray() {
    var tray = $('#compareTray');
    if (!tray) return;
    var items = Basket.all();
    tray.hidden = items.length === 0 || location.pathname === '/compare';
    $('#trayText', tray).innerHTML = '<strong>' + items.length + '</strong> of ' + MAX + ' in compare' +
      (items.length < 2 ? ' <span class="muted">· add at least 2</span>' : '');
    $('#trayGo', tray).disabled = items.length < 2;
  }

  function toast(text) {
    var t = $('#toast');
    if (!t) return;
    t.textContent = text; t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { t.hidden = true; }, 3200);
  }

  document.addEventListener('click', function (e) {
    var go = e.target.closest('#trayGo, [data-compare-now]');
    if (go) { e.preventDefault(); var items = Basket.all(); if (items.length >= 2) { var f = compareForm(items); Basket.clear(); f.submit(); } }
    if (e.target.closest('#trayClear, [data-clear-basket]')) { e.preventDefault(); Basket.clear(); }
  });

  // ---- theme toggle -------------------------------------------------------------------------
  var themeBtn = $('#themeToggle');
  if (themeBtn) themeBtn.addEventListener('click', function () {
    var root = document.documentElement;
    var dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('pf-theme', root.dataset.theme); } catch (e) {}
  });

  // ---- Compare tab ------------------------------------------------------------------------------
  var basketList = $('#basketList');
  if (basketList) {
    var drawBasket = function () {
      var items = Basket.all();
      $('#basketEmpty').hidden = items.length > 0;
      $('#basketActions').hidden = items.length === 0;
      $('#basketGo').disabled = items.length < 2;
      basketList.innerHTML = items.map(function (i) {
        return '<div class="basket-row">' +
          '<div class="thumb">' + (i.image ? '<img src="' + esc(safeUrl(i.image)) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '') + '</div>' +
          '<div class="basket-main"><div class="eyebrow-sm">' + esc(i.supplier) + '</div>' +
          '<a href="' + esc(safeUrl(i.url)) + '" target="_blank" rel="noopener" class="row-title">' + esc(i.name) + '</a>' +
          '<div class="small muted">From search "' + esc(i.query) + '" · <a href="/?id=' + esc(i.search_id) + '">open search</a></div></div>' +
          '<div class="basket-price">' + (i.total != null ? '<strong>' + kes(i.total) + '</strong><div class="small muted">incl. VAT</div>' :
            '<span class="muted">' + (i.no_price ? 'No public price' : 'VAT basis unknown') + '</span>') + '</div>' +
          '<button class="icon-btn" type="button" data-remove="' + esc(i.ref) + '" aria-label="Remove from compare">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>';
      }).join('');
    };
    basketList.addEventListener('click', function (e) {
      var b = e.target.closest('[data-remove]');
      if (b) Basket.remove(b.dataset.remove);
    });
    document.addEventListener('basket', drawBasket);
    drawBasket();
  }

  // ---- Dashboard: search and results -------------------------------------------------------------
  var dataEl = $('#pf-data');
  if (!dataEl) { Basket.changed(); return; }
  var DATA = JSON.parse(dataEl.textContent);
  MAX = DATA.max_compare || MAX;

  var hour = new Date().getHours();
  var greet = $('#greeting');
  if (greet) greet.textContent = (hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening') + (DATA.user ? ', ' + DATA.user : '');

  var form = $('#searchForm'), results = $('#results'), home = $('#homeExtra');
  var list = $('#offerList'), chipsEl = $('#resultChips'), statusEl = $('#supplierStatus');
  var state = { view: null, offers: [], ready: false, filter: 'all', text: '', sort: 'best', quantity: 1, suppliers: {} };

  $('#filtersToggle').addEventListener('click', function () {
    var panel = $('#filtersPanel'), open = panel.hidden;
    panel.hidden = !open;
    this.setAttribute('aria-expanded', String(open));
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var params = new URLSearchParams();
    new FormData(form).forEach(function (v, k) { if (String(v).trim() !== '') params.set(k, String(v).trim()); });
    if (!params.get('q')) { $('#q').focus(); return; }
    history.pushState({}, '', '/?' + params.toString());
    runSearch(params);
  });

  window.addEventListener('popstate', function () { location.reload(); });

  function showResults() { results.hidden = false; home.hidden = true; document.body.classList.add('has-results'); }

  function runSearch(params) {
    showResults();
    state = { view: null, offers: [], ready: false, filter: 'all', text: '', sort: params.get('sort') || 'best',
              quantity: parseInt(params.get('quantity') || '1', 10) || 1, suppliers: {} };
    $('#resultFilter').value = '';
    $('#sortSelect').value = state.sort;
    $('#bannerQuery').textContent = params.get('q');
    $('#bannerMeta').textContent = 'Searching Tier 1 supplier websites…';
    $('#bannerStats').innerHTML = '';
    $('#notes').innerHTML = '';
    $('#nearby').innerHTML = '';
    $('#savedNote').hidden = true;
    results.classList.add('is-loading');
    list.innerHTML = skeleton(3);
    chipsEl.innerHTML = '';
    var started = performance.now();

    fetch('/api/search?' + params.toString(), { headers: { Accept: 'application/x-ndjson' } }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      if (!res.body || !res.body.getReader) return res.text().then(function (t) { t.split('\n').forEach(handleLine); });
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
      return (function pump() {
        return reader.read().then(function (r) {
          if (r.done) { if (buf.trim()) handleLine(buf); return; }
          buf += dec.decode(r.value, { stream: true });
          var lines = buf.split('\n'); buf = lines.pop();
          lines.forEach(handleLine);
          return pump();
        });
      })();
    }).catch(function () {
      results.classList.remove('is-loading');
      list.innerHTML = '<div class="card empty"><h2>Search failed</h2><p class="muted">ProcureFind couldn\'t finish this search. Check the connection and try again.</p></div>';
    });

    function handleLine(line) {
      if (!line.trim()) return;
      var ev = JSON.parse(line);
      if (ev.type === 'start') {
        ev.suppliers.forEach(function (s) { state.suppliers[s.key] = { name: s.name, tier: s.tier, status: 'wait', count: 0 }; });
        state.checking = ev.checking || 0;
        state.place = ev.place;
        drawStatus();
      } else if (ev.type === 'supplier') {
        var st = state.suppliers[ev.key];
        if (!st) {                      // a nearby supplier's website was just checked
          state.checking = Math.max(0, (state.checking || 0) - 1);
          if (!ev.offers.length) { drawStatus(); return; }
          st = state.suppliers[ev.key] = { name: ev.name, tier: ev.offers[0].tier, status: 'ok', count: 0 };
        }
        st.status = ev.note ? 'fail' : 'ok'; st.count = ev.offers.length; st.note = ev.note ? ev.note.text : '';
        state.offers = state.offers.concat(ev.offers);
        drawStatus(); drawChips(); drawList();
        $('#bannerMeta').textContent = state.offers.length + ' offers so far…';
      } else if (ev.type === 'done') {
        var secs = ((performance.now() - started) / 1000).toFixed(1);
        finish(ev.view, 'Found in ' + secs + ' s');
        history.replaceState({}, '', '/?id=' + ev.view.id);
        addRecent(ev.view);
      }
    }
  }

  function finish(view, timing) {
    state.view = view; state.offers = view.offers; state.ready = true; state.quantity = view.quantity || 1;
    results.classList.remove('is-loading');
    var answered = 0, total = 0;
    Object.keys(state.suppliers).forEach(function (k) { total++; if (state.suppliers[k].status === 'ok') answered++; });
    if (!total) {  // saved search: rebuild supplier status from the offers and notes
      (DATA.active || []).forEach(function (s) {
        var note = (view.notes || []).find(function (n) { return n.supplier_key === s.key && n.kind === 'error'; });
        state.suppliers[s.key] = { name: s.name, tier: 1, status: note ? 'fail' : 'ok', note: note ? note.text : '',
          count: view.offers.filter(function (o) { return o.supplier_key === s.key; }).length };
      });
      view.offers.forEach(function (o) {
        if (!state.suppliers[o.supplier_key]) state.suppliers[o.supplier_key] = { name: o.supplier, tier: o.tier, status: 'ok', count: 0 };
        if (o.tier !== 1) state.suppliers[o.supplier_key].count++;
      });
      Object.keys(state.suppliers).forEach(function (k) { total++; if (state.suppliers[k].status === 'ok') answered++; });
    }
    $('#bannerMeta').textContent = [timing, answered + ' of ' + total + ' suppliers answered',
      state.quantity > 1 ? 'quantity ' + state.quantity : '',
      view.filtered_out ? view.filtered_out + ' outside your price range hidden' : '',
      view.filters && view.filters.ai_understood ? 'AI read this as "' + view.filters.ai_understood + '"' : ''
    ].filter(Boolean).join(' · ');
    state.checking = 0;
    var verified = (view.nearby || []).filter(function (n) { return n.tier === 2; }).length;
    $('#bannerStats').innerHTML =
      stat('Cheapest incl. VAT', view.cheapest || '-', view.cheapest_supplier || 'No priced offers') +
      stat('Offers', view.offers.length, Object.keys(view.per_supplier || {}).length + ' suppliers') +
      stat('Same product', view.same_product_groups || 0, 'at more than one supplier') +
      stat('Nearby suppliers', view.nearby_total || 0, (view.place || 'Nairobi') + (verified ? ' · ' + verified + ' verified' : ''));
    drawStatus(); drawChips(); drawList(); drawNotes(view.notes || []); drawNearby(view);
  }

  function scoreClass(r) { return r.flags && r.flags.some(function (f) { return /debarred/i.test(f); }) ? 'blocked' : r.score >= 70 ? 'hi' : r.score >= 45 ? 'mid' : 'lo'; }

  function ratingBadge(r) {
    if (!r) return '';
    var tip = 'Rating ' + r.score + '/100 (' + r.label + '), based on ' + (r.evidence || 0) + ' points of checks:\n' +
      (r.flags || []).map(function (f) { return 'Warning: ' + f; }).concat(r.reasons || []).join('\n');
    return '<span class="score score-' + scoreClass(r) + ' score-sm" title="' + esc(tip) + '">' + esc(r.score) + '</span>' +
      '<span class="tier">' + esc(r.label) + '</span>';
  }

  function drawNearby(view) {
    var box = $('#nearby'), list = view.nearby || [];
    if (!box) return;
    if (!list.length) {
      var d = view.directory || {};
      box.innerHTML = d.status === 'running' ? '<div class="card empty-inline"><p class="muted">Finding suppliers near ' + esc(view.place) +
        ' on OpenStreetMap… They\'ll appear in your next searches.</p></div>' : '';
      return;
    }
    var shown = state.nearbyAll ? list : list.slice(0, 9);
    box.innerHTML = '<div class="section-head"><h2>Nearby suppliers · ' + esc(view.place) + '</h2><a href="/suppliers?location=' +
      encodeURIComponent(view.place) + '">All ' + view.nearby_total + ' with ratings</a></div>' +
      '<p class="small muted nearby-intro">Found on OpenStreetMap near ' + esc(view.place) + '. Most don\'t publish prices online: send them an RFQ to add their quote to the comparison.</p>' +
      '<div class="nearby-grid">' + shown.map(function (n) {
        var r = n.rating || {};
        return '<article class="nearby-card' + (n.blocked ? ' is-blocked' : '') + '">' +
          '<div class="nearby-top"><span class="score score-' + scoreClass(r) + '" title="' + esc((r.reasons || []).join('\n')) + '">' + (n.blocked ? '0' : esc(r.score)) + '</span>' +
          '<div class="nearby-title"><strong>' + esc(n.name) + '</strong><small>' + esc(n.kind || 'Supplier') +
          (n.distance_km != null ? ' · ' + esc(n.distance_km) + ' km' : '') + '</small></div>' +
          (n.blocked ? '<span class="tier-tag tb">Blocked</span>' : '<span class="tier-tag t' + n.tier + '">Tier ' + n.tier + '</span>') + '</div>' +
          (r.flags && r.flags.length ? '<p class="small flag">' + WARN + ' ' + esc(r.flags.join(' · ')) + '</p>' : '') +
          '<p class="small muted">' + esc(n.has_offers ? 'Prices included above' : n.site) + '</p>' +
          '<div class="nearby-contacts small">' + (n.phone ? '<span>Tel ' + esc(n.phone) + '</span>' : '') +
          (n.website ? '<a href="' + esc(safeUrl(n.website)) + '" target="_blank" rel="noopener">' + esc(host(n.website)) + '</a>' : '') + '</div>' +
          '<div class="nearby-actions">' + (n.blocked ? '' : '<a class="btn btn-sm btn-ghost" href="/rfq?search_id=' + view.id + '&supplier=' + encodeURIComponent(n.id) + '">Draft RFQ</a>') +
          (n.osm_url ? '<a class="btn btn-sm btn-link" href="' + esc(safeUrl(n.osm_url)) + '" target="_blank" rel="noopener">Map ↗</a>' : '') + '</div></article>';
      }).join('') + '</div>' +
      (list.length > 9 ? '<div class="more-row"><button type="button" class="btn btn-ghost btn-sm" id="nearbyMore">' +
        (state.nearbyAll ? 'Show fewer' : 'Show ' + (list.length - 9) + ' more') + '</button></div>' : '') +
      '<p class="small muted attribution">Locations and contacts © <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>.</p>';
    var more = $('#nearbyMore');
    if (more) more.addEventListener('click', function () { state.nearbyAll = !state.nearbyAll; drawNearby(view); });
  }

  function stat(label, value, sub) {
    return '<div class="stat"><div class="stat-label">' + esc(label) + '</div><div class="stat-value">' + esc(value) +
      '</div><div class="stat-sub">' + esc(sub) + '</div></div>';
  }

  function drawStatus() {
    statusEl.innerHTML = Object.keys(state.suppliers).map(function (k) {
      var s = state.suppliers[k];
      var label = s.status === 'wait' ? 'Searching…' : s.status === 'fail' ? "Didn't respond" : s.count + ' offer' + (s.count === 1 ? '' : 's');
      return '<span class="status-pill is-' + s.status + '" title="' + esc(s.note || '') + '"><span class="status-dot"></span>' +
        (s.tier && s.tier !== 1 ? '<span class="pill-tier">T' + s.tier + '</span>' : '') +
        esc(s.name) + ' <span class="muted">· ' + label + '</span></span>';
    }).join('') + (state.checking ? '<span class="status-pill is-wait"><span class="status-dot"></span>Checking ' + state.checking +
      ' nearby supplier website' + (state.checking === 1 ? '' : 's') + '…</span>' : '');
  }

  function matches(o) {
    var f = state.filter;
    if (f === 'stock' && o.stock !== 'in stock') return false;
    if (f === 'priced' && o.total_incl_vat == null) return false;
    if (f === 'noprice' && !o.no_public_price) return false;
    if (f.indexOf('supplier:') === 0 && o.supplier_key !== f.slice(9)) return false;
    if (f.indexOf('tier:') === 0 && String(o.tier) !== f.slice(5)) return false;
    if (f.indexOf('group:') === 0 && String(o.group) !== f.slice(6)) return false;
    if (state.text) {
      var hay = (o.name + ' ' + (o.model || '') + ' ' + o.supplier).toLowerCase();
      if (!state.text.toLowerCase().split(/\s+/).every(function (w) { return hay.indexOf(w) >= 0; })) return false;
    }
    return true;
  }

  function drawChips() {
    var o = state.offers, n = function (fn) { return o.filter(fn).length; };
    var chips = [['all', 'All', o.length], ['stock', 'In stock', n(function (x) { return x.stock === 'in stock'; })],
      ['priced', 'With price', n(function (x) { return x.total_incl_vat != null; })],
      ['noprice', 'No public price', n(function (x) { return x.no_public_price; })]];
    [2, 3].forEach(function (t) {
      var c = n(function (x) { return x.tier === t; });
      if (c) chips.push(['tier:' + t, t === 2 ? 'Tier 2 · verified' : 'Tier 3 · unverified', c]);
    });
    Object.keys(state.suppliers).forEach(function (k) {
      var c = n(function (x) { return x.supplier_key === k; });
      if (c || state.suppliers[k].tier === 1)
        chips.push(['supplier:' + k, state.suppliers[k].name.replace(/\s*\(.*\)$/, '').replace(/ & Solutions$/, ''), c]);
    });
    if (state.filter.indexOf('group:') === 0) chips.push([state.filter, 'Same product ' + state.filter.slice(6), n(function (x) { return String(x.group) === state.filter.slice(6); })]);
    chipsEl.innerHTML = chips.map(function (c) {
      return '<button type="button" class="chip-btn' + (state.filter === c[0] ? ' active' : '') + '" data-filter="' + esc(c[0]) + '"' +
        (c[2] === 0 && c[0] !== 'all' && state.filter !== c[0] ? ' disabled' : '') + '>' + esc(c[1]) + ' <span class="count">' + c[2] + '</span></button>';
    }).join('');
  }

  chipsEl.addEventListener('click', function (e) {
    var b = e.target.closest('[data-filter]');
    if (!b) return;
    state.filter = b.dataset.filter; drawChips(); drawList();
  });
  $('#resultFilter').addEventListener('input', function () { state.text = this.value.trim(); drawList(); });
  $('#sortSelect').addEventListener('change', function () { state.sort = this.value; drawList(); });

  function sorted(offers) {
    var arr = offers.slice();
    if (state.sort === 'price') {
      arr.sort(function (a, b) {
        var pa = a.total_incl_vat != null ? a.total_incl_vat : (a.price != null ? a.price : Infinity);
        var pb = b.total_incl_vat != null ? b.total_incl_vat : (b.price != null ? b.price : Infinity);
        return pa - pb;
      });
    } else if (!state.ready) {
      arr.sort(function (a, b) { return (b.relevance || 0) - (a.relevance || 0); });
    }
    return arr;
  }

  function drawList() {
    var shown = sorted(state.offers.filter(matches));
    if (!state.offers.length && !state.ready) { list.innerHTML = skeleton(3); return; }
    if (!shown.length) {
      list.innerHTML = state.ready && !state.offers.length ?
        '<div class="card empty"><div class="empty-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg></div><h2>No offers found</h2><p class="muted">None of the Tier 1 supplier websites list this item. Try fewer words, or the model number, or send a request for quotation.</p>' +
        '<a class="btn btn-primary" href="/rfq?search_id=' + state.view.id + '">Draft an RFQ</a></div>' :
        '<div class="card empty small-empty"><p class="muted">No offers match this filter.</p></div>';
      return;
    }
    var best = null;
    state.offers.forEach(function (o) { if (o.total_incl_vat != null && (best == null || o.total_incl_vat < best)) best = o.total_incl_vat; });
    var lastTier = null, html = '';
    shown.forEach(function (o) {
      if (state.sort !== 'price' && o.tier !== lastTier) {
        lastTier = o.tier;
        html += '<div class="tier-head"><span class="tier-tag t' + o.tier + '">Tier ' + o.tier + '</span>' +
          ({ 1: 'Trusted suppliers', 2: 'Verified suppliers', 3: 'Unverified suppliers: check before buying' }[o.tier] || '') + '</div>';
      }
      html += offerCard(o, best);
    });
    list.innerHTML = html + (state.ready ? '' : skeleton(1));
  }

  function offerCard(o, best) {
    var sid = state.view ? state.view.id : null, ref = sid != null ? sid + ':' + o.idx : '';
    var inBasket = ref && Basket.has(ref);
    var lowered = (o.price_text || '').toLowerCase();
    var priceBlock;
    if (o.no_public_price) {
      priceBlock = '<div class="noprice">No public price</div><div class="small muted">Site shows "' + esc(o.price_text) + '"</div>';
    } else if (o.total_incl_vat != null) {
      priceBlock = '<div class="total' + (o.total_incl_vat === best ? ' is-best' : '') + '">' + kes(o.total_incl_vat) + '</div>' +
        '<div class="small muted">incl. ' + DATA.vat_pct + '% VAT' + (o.total_incl_vat === best ? ' · <span class="best-tag">Lowest</span>' : '') + '</div>' +
        '<div class="small muted price-src">Site: "' + esc(o.price_text) + '"' +
        (o.vat_basis === 'excl' && lowered.indexOf('vat') < 0 && lowered.indexOf('tax') < 0 ? ' + VAT' : '') + '</div>' +
        (state.quantity > 1 ? '<div class="small"><strong>' + kes(o.total_incl_vat * state.quantity) + '</strong> for ' + state.quantity + '</div>' : '') +
        (o.per_unit_incl_vat ? '<div class="small muted">' + kes(o.per_unit_incl_vat, 2) + ' per ' + (o.unit_kind === 'metre' ? 'metre' : 'piece') + '</div>' : '');
    } else {
      priceBlock = '<div class="total is-muted">' + kes(o.price) + '</div><div class="flag">VAT basis unknown: total not shown</div>';
    }
    var tags = [];
    if (o.tier === 3) tags.push('<span class="chip chip-red">Unverified supplier</span>');
    if (o.price_change) {
      var up = o.price_change.pct > 0;
      tags.push('<span class="chip ' + (up ? 'chip-red' : 'chip-green') + '" title="Was ' + esc(kes(o.price_change.price)) + ' before VAT on ' +
        esc((o.price_change.fetched_at || '').slice(0, 10)) + '">' + (up ? '▲ ' : '▼ ') + Math.abs(o.price_change.pct) + '% since ' +
        esc((o.price_change.fetched_at || '').slice(5, 10).replace('-', '/')) + '</span>');
    }
    if (o.model) tags.push('<span class="chip">Model ' + esc(o.model) + '</span>');
    if (o.group) tags.push('<button type="button" class="chip chip-blue" data-filter-group="' + esc(o.group) + '" title="Show the same product at other suppliers">Same product · ' + esc(o.group) + '</button>');
    if (o.unit_label && o.unit_label !== 'each') tags.push('<span class="chip chip-amber">Sold per ' + esc(o.unit_label) + '</span>');
    tags.push(o.stock === 'in stock' ? '<span class="chip chip-green">In stock</span>' :
      o.stock ? '<span class="chip chip-red">' + esc(o.stock.charAt(0).toUpperCase() + o.stock.slice(1)) + '</span>' :
      '<span class="chip">Stock not shown</span>');
    var actions = [];
    if (state.ready) {
      actions.push('<button type="button" class="btn btn-sm ' + (inBasket ? 'btn-dark' : 'btn-ghost') + '" data-compare="' + esc(o.idx) + '" aria-pressed="' + inBasket + '">' +
        (inBasket ? CHECK + ' In compare' : '+ Compare') + '</button>');
      if (o.no_public_price || o.total_incl_vat == null)
        actions.push('<a class="btn btn-sm btn-primary" href="/rfq?search_id=' + sid + '&offer=' + o.idx + '">Draft RFQ</a>');
    }
    actions.push('<a class="btn btn-sm btn-link" href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener">View on site ↗</a>');
    return '<article class="offer' + (o.no_public_price ? ' is-noprice' : '') + (inBasket ? ' is-picked' : '') + '">' +
      '<div class="thumb">' + (o.image ? '<img src="' + esc(safeUrl(o.image)) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '') + '</div>' +
      '<div class="offer-main"><div class="eyebrow-sm">' + esc(o.supplier) + ' ' + ratingBadge(o.rating) + '</div>' +
      '<h3><a href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener">' + esc(o.name) + '</a></h3>' +
      '<div class="tags">' + tags.join('') + '</div>' +
      (o.note ? '<p class="small muted">' + esc(o.note) + '</p>' : '') +
      (o.flags || []).map(function (f) { return '<p class="small flag">' + WARN + ' ' + esc(f) + '</p>'; }).join('') +
      '<p class="small muted src">' + esc(host(o.url)) + ' · fetched ' + esc((o.fetched_at || '').replace('T', ' ').slice(0, 16)) + ' UTC</p></div>' +
      '<div class="offer-side"><div class="offer-price">' + priceBlock + '</div><div class="offer-actions">' + actions.join('') + '</div></div></article>';
  }

  list.addEventListener('click', function (e) {
    var g = e.target.closest('[data-filter-group]');
    if (g) { state.filter = 'group:' + g.dataset.filterGroup; drawChips(); drawList(); window.scrollTo({ top: results.offsetTop - 80, behavior: 'smooth' }); return; }
    var b = e.target.closest('[data-compare]');
    if (!b || !state.view) return;
    var o = state.offers.find(function (x) { return String(x.idx) === b.dataset.compare; });
    var added = Basket.toggle({ ref: state.view.id + ':' + o.idx, search_id: state.view.id, idx: o.idx, query: state.view.query,
      supplier: o.supplier, name: o.name, total: o.total_incl_vat, no_price: o.no_public_price, url: o.url, image: o.image });
    if (added) toast('Added to compare: ' + o.name);
    drawList();
  });
  document.addEventListener('basket', function () { if (state.ready) drawList(); });

  function drawNotes(notes) {
    $('#notes').innerHTML = notes.length ? '<div class="card notes-card"><h2>Notes</h2>' + notes.map(function (n) {
      var rfq = (n.kind === 'error' || n.kind === 'rfq') && state.view;
      return '<div class="note-row is-' + esc(n.kind) + '"><span>' + esc(n.text) + '</span>' +
        (rfq ? '<a class="btn btn-sm btn-ghost" href="/rfq?search_id=' + state.view.id + '&supplier=' + encodeURIComponent(n.supplier_key) + '">Draft RFQ</a>' : '') + '</div>';
    }).join('') + '</div>' : '';
  }

  function skeleton(n) {
    var s = '';
    for (var i = 0; i < n; i++) s += '<div class="offer is-skeleton" aria-hidden="true"><div class="thumb sk"></div><div class="offer-main"><div class="sk sk-line w30"></div><div class="sk sk-line w70"></div><div class="sk sk-line w40"></div></div><div class="offer-side"><div class="sk sk-line w60"></div></div></div>';
    return s;
  }

  function addRecent(view) {
    var box = $('#recentChips');
    if (!box) return;
    box.querySelectorAll('[data-recent-q]').forEach(function (a) { if (a.dataset.recentQ.toLowerCase() === view.query.toLowerCase()) a.remove(); });
    var a = document.createElement('a');
    a.className = 'recent-chip'; a.href = '/?id=' + view.id; a.dataset.recentQ = view.query;
    a.innerHTML = esc(view.query) + ' <span class="count">' + view.offers.length + '</span>';
    box.insertBefore(a, box.querySelector('.recent-chip'));
    box.hidden = false;
    var extra = box.querySelectorAll('.recent-chip');
    for (var i = 6; i < extra.length; i++) extra[i].remove();
  }

  // ---- start: saved search, pending search, or the home view ---------------------------------
  if (DATA.saved) {
    showResults();
    $('#bannerQuery').textContent = DATA.saved.query;
    $('#savedNote').hidden = false;
    $('#savedWhen').textContent = (DATA.saved.created_at || '').replace('T', ' ').slice(0, 16) + ' UTC';
    finish(DATA.saved, 'Saved search');
  } else if (DATA.pending) {
    var p = new URLSearchParams();
    Object.keys(DATA.pending).forEach(function (k) { if (DATA.pending[k] !== '' && DATA.pending[k] != null) p.set(k, DATA.pending[k]); });
    runSearch(p);
  }
  Basket.changed();
})();
