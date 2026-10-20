/* SXS · Smart Exit Selector */
    const $ = (id) => document.getElementById(id);
    const hist = { size: [], ok: [], lat: [], score: [] };

    (function () {
      const saved = localStorage.getItem("pp-theme");
      const preferLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
      const theme = saved || (preferLight ? "light" : "dark");
      document.documentElement.setAttribute("data-theme", theme);
      $("theme-icon") && ($("theme-icon").innerHTML = '<use href="#i-' + (theme === "light" ? "sun" : "moon") + '"/>');
    })();
    $("theme-toggle").onclick = () => {
      const t = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", t);
      localStorage.setItem("pp-theme", t);
      $("theme-icon") && ($("theme-icon").innerHTML = '<use href="#i-' + (t === "light" ? "sun" : "moon") + '"/>');
      refreshGeoMap();
    };

    const titles = {
      dashboard: ["经营总览", "实时资源健康 · 隧道效率 · 质量漏斗"],
      proxies: ["代理资产", "完整 IP 资产与质量"],
      tunnel: ["隧道效率", "连接、切换与稳定性"],
      verify: ["质量校验", "探活漏斗与累计通过率"],
      help: ["指标释义", "看懂每一块数据"],
    };
    function switchView(id) {
      document.querySelectorAll(".nav-item").forEach((b) => {
        b.classList.toggle("active", b.dataset.view === id);
      });
      document.querySelectorAll(".m-tab").forEach((b) => {
        b.classList.toggle("active", b.dataset.view === id);
      });
      document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
      const sec = document.getElementById("view-" + id);
      if (sec) sec.classList.add("active");
      $("page-title").textContent = titles[id][0];
      $("page-sub").textContent = titles[id][1];
      window.scrollTo({ top: 0, behavior: "smooth" });
      if (id === "proxies") setTimeout(refreshGeoMap, 50);
    }
    document.querySelectorAll(".nav-item, .m-tab").forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });
    // 支持 #proxies 这类深链
    (function () {
      const h = (location.hash || "").replace(/^#/, "");
      if (h && titles[h]) switchView(h);
    })();

    async function api(path) {
      const res = await fetch(path);
      return res.json();
    }

    function push(arr, v, n = 48) {
      arr.push(v);
      if (arr.length > n) arr.shift();
    }

    function sparkline(id, data, mode) {
      const c = $(id);
      if (!c) return;
      // 高清：按 dpr 放大画布，CSS 尺寸不变
      const cssW = c.clientWidth || 180;
      const cssH = c.clientHeight || 42;
      const dpr = window.devicePixelRatio || 1;
      const w = Math.floor(cssW * dpr);
      const h = Math.floor(cssH * dpr);
      // 仅尺寸变化时改 canvas.width（赋值会清空画布，导致闪烁）
      if (c.width !== w || c.height !== h) {
        c.width = w;
        c.height = h;
      }
      const ctx = c.getContext("2d");
      ctx.clearRect(0, 0, w, h);

      if (!data || data.length < 2) return;
      const sig = data.length + ":" + data[data.length-1] + ":" + data[0];
      if (c.__sig === sig) return;
      c.__sig = sig;

      // 缓冲缩放，避免新点加入时整条线跳动/拉直
      let min = Math.min(...data);
      let max = Math.max(...data);
      const pad = (max - min) * 0.12 || Math.abs(max) * 0.08 || 1;
      min -= pad;
      max += pad;
      const span = max - min || 1;

      const cs = getComputedStyle(document.documentElement);
      const c1 = mode === "c" ? cs.getPropertyValue("--accent2").trim() : cs.getPropertyValue("--accent").trim();
      const c2 = mode === "c" ? cs.getPropertyValue("--accent").trim() : cs.getPropertyValue("--violet").trim();

      const n = data.length;
      const pts = data.map((v, i) => {
        const x = (i / (n - 1)) * (w - 8 * dpr) + 4 * dpr;
        const y = h - 6 * dpr - ((v - min) / span) * (h - 12 * dpr);
        return [x, y];
      });

      // 面积
      const fill = ctx.createLinearGradient(0, 0, 0, h);
      fill.addColorStop(0, c1 + "66");
      fill.addColorStop(1, c1 + "00");
      ctx.beginPath();
      pts.forEach((pt, i) => (i ? ctx.lineTo(pt[0], pt[1]) : ctx.moveTo(pt[0], pt[1])));
      ctx.lineTo(pts[n - 1][0], h);
      ctx.lineTo(pts[0][0], h);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();

      // 平滑曲线（二次贝塞尔）
      const grad = ctx.createLinearGradient(0, 0, w, 0);
      grad.addColorStop(0, c1);
      grad.addColorStop(1, c2);
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 0; i < n - 1; i++) {
        const x0 = pts[i][0], y0 = pts[i][1];
        const x1 = pts[i + 1][0], y1 = pts[i + 1][1];
        const cx = (x0 + x1) / 2;
        ctx.bezierCurveTo(cx, y0, cx, y1, x1, y1);
      }
      ctx.strokeStyle = grad;
      ctx.lineWidth = 2.2 * dpr;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();

      // 端点
      const last = pts[n - 1];
      ctx.beginPath();
      ctx.arc(last[0], last[1], 3.2 * dpr, 0, Math.PI * 2);
      ctx.fillStyle = c1;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(last[0], last[1], 8 * dpr, 0, Math.PI * 2);
      ctx.fillStyle = c1 + "33";
      ctx.fill();
    }

    function esc(s) {
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function barHtml(items) {
      return items.map((it) => `
        <div class="bar-item">
          <div class="row"><span>${esc(it.name)}</span><span>${esc(it.text)}</span></div>
          <div class="track"><i class="${it.cls}" style="width:${it.pct}%"></i></div>
        </div>`).join("");
    }

    // 省份名归一：湖北省 -> 湖北，内蒙古自治区 -> 内蒙古
    function normProvince(p) {
      if (!p) return "";
      return String(p)
        .replace(/(省|市|壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区)$/u, "")
        .trim();
    }

    // 列表/卡片用的归属地短文案
    function geoText(g) {
      g = g || {};
      const prov = normProvince(g.province) || (g.province || "");
      const parts = [];
      if (prov) parts.push(prov);
      if (g.city) parts.push(g.city);
      if (g.isp) parts.push(g.isp);
      return parts.join(" · ");
    }

    // ECharts 中国地图（本地 vendor）
    // 注意：刷新数据时不能 setOption(..., true)，否则会重置缩放/中心点
    let _geoChart = null;
    let _geoLast = { byProv: {}, max: 1 };

    function _geoIsDark() {
      return document.documentElement.getAttribute("data-theme") !== "light";
    }

    function _geoThemeOption() {
      const dark = _geoIsDark();
      return {
        visualMap: {
          inRange: {
            color: dark
              ? ["#1f2d3d", "#2b6cb0", "#63b3ed", "#bee3f8"]
              : ["#e8f1fb", "#7db3e8", "#2b6cb0", "#1a4f8b"],
          },
          textStyle: { color: dark ? "#9aa0a6" : "#666" },
        },
        series: [
          {
            label: {
              show: true,
              fontSize: 10,
              color: dark ? "#9aa0a6" : "#555",
            },
            itemStyle: {
              areaColor: dark ? "#222b36" : "#f4f7fb",
              borderColor: dark ? "#3a4552" : "#d0d7de",
              borderWidth: 0.8,
            },
            emphasis: {
              label: { color: dark ? "#fff" : "#111" },
              itemStyle: { areaColor: dark ? "#3d6fb8" : "#c5dcf5" },
            },
          },
        ],
      };
    }

    function ensureGeoMap() {
      const el = $("geo-map");
      if (!el || typeof echarts === "undefined") return null;
      if (!_geoChart) {
        _geoChart = echarts.init(el, null, { renderer: "canvas" });
        const theme = _geoThemeOption();
        _geoChart.setOption({
          backgroundColor: "transparent",
          tooltip: {
            trigger: "item",
            formatter: (p) => `${p.name}<br/>资产 ${p.value || 0}`,
          },
          visualMap: {
            min: 0,
            max: 1,
            left: 8,
            bottom: 8,
            text: ["多", "少"],
            calculable: true,
            ...(theme.visualMap || {}),
          },
          series: [
            {
              type: "map",
              map: "china",
              roam: true,
              scaleLimit: { min: 0.8, max: 6 },
              ...(theme.series[0] || {}),
              data: [],
            },
          ],
        });
        window.addEventListener("resize", () => {
          try {
            _geoChart && _geoChart.resize();
          } catch (_) {}
        });
      }
      return _geoChart;
    }

    function refreshGeoMap() {
      if (!ensureGeoMap()) return;
      try {
        _geoChart.resize();
        // 只换配色，合并模式保留 zoom/center
        _geoChart.setOption(_geoThemeOption());
        renderGeoMap(_geoLast.byProv, _geoLast.max);
      } catch (_) {}
    }

    function renderGeoMap(byProv, max) {
      if (!ensureGeoMap()) return;
      const data = Object.entries(byProv || {}).map(([name, value]) => ({
        name,
        value,
      }));
      try {
        // 合并更新：只改数据与色阶上限，不传 notMerge，保留用户缩放/平移
        _geoChart.setOption({
          visualMap: { max: Math.max(1, max || 1) },
          series: [{ data }],
        });
      } catch (_) {}
    }

    function renderGeo(proxies) {
      const byProv = {};
      let known = 0;
      (proxies || []).forEach((p) => {
        const g = p.geo || {};
        const prov = normProvince(g.province);
        if (!prov) return;
        known++;
        byProv[prov] = (byProv[prov] || 0) + 1;
      });

      // 只展示 Top N，避免卡片被撑长
      const TOP_N = 8;
      const ranked = Object.entries(byProv)
        .sort((a, b) => b[1] - a[1])
        .slice(0, TOP_N);
      const max = ranked.length ? ranked[0][1] : 1;

      if ($("geo-provinces")) {
        $("geo-provinces").innerHTML = ranked.length
          ? barHtml(ranked.map(([name, n]) => ({
              name,
              text: n,
              pct: (n / max) * 100,
              cls: "grad-acc",
            })))
          : '<div class="empty">暂无归属地数据（后台补全中…）</div>';
      }

      _geoLast = { byProv, max };
      renderGeoMap(byProv, max);

      if ($("geo-hint")) {
        $("geo-hint").textContent = known
          ? `已识别 ${known} 个出口 · 覆盖 ${Object.keys(byProv).length} 个省级地区`
          : "归属地由后台静默补全";
      }
    }

    function renderBars(proxies) {
      const total = proxies.length || 1;
      const ok = proxies.filter((p) => p.last_ok === true).length;
      const bad = proxies.filter((p) => p.last_ok === false).length;
      const unk = proxies.filter((p) => p.last_ok == null).length;
      $("bars-status").innerHTML = barHtml([
        { name: "已验证可用", text: ok, pct: (ok / total) * 100, cls: "grad-ok" },
        { name: "验证失败", text: bad, pct: (bad / total) * 100, cls: "grad-err" },
        { name: "未验证", text: unk, pct: (unk / total) * 100, cls: "grad-mut" },
      ]);

      const b = [0, 0, 0, 0];
      proxies.forEach((p) => {
        const v = p.ewma_latency_ms ?? p.latency_ms;
        if (v == null) return;
        if (v < 500) b[0]++;
        else if (v < 1000) b[1]++;
        else if (v < 2000) b[2]++;
        else b[3]++;
      });
      const s = b.reduce((x, y) => x + y, 0) || 1;
      $("bars-lat").innerHTML = barHtml([
        { name: "极速 < 500ms", text: b[0], pct: (b[0] / s) * 100, cls: "grad-ok" },
        { name: "流畅 500ms–1s", text: b[1], pct: (b[1] / s) * 100, cls: "grad-cyan" },
        { name: "一般 1s–2s", text: b[2], pct: (b[2] / s) * 100, cls: "grad-warn" },
        { name: "较慢 > 2s", text: b[3], pct: (b[3] / s) * 100, cls: "grad-err" },
      ]);
    }

    function renderTop(proxies) {
      const top = [...proxies].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, 6);
      $("top-cards").innerHTML = top.map((p) => `
        <div class="ip-card">
          <div class="addr">${p.host}:${p.port}${p.username ? " · " + p.username : ""}</div>
          <div class="score">${p.score ?? "—"}</div>
          <div class="tags">
            ${p.hot ? '<span class="pill warn">热池</span>' : ""}
            ${p.last_ok === true ? '<span class="pill ok">通</span>' : p.last_ok === false ? '<span class="pill err">断</span>' : '<span class="pill muted">未测</span>'}
            <span class="pill muted">${p.remaining ?? "-"}s</span>
          </div>
          <div class="ip-meta">
            <span>延迟 ${((p.ewma_latency_ms ?? p.latency_ms) != null) ? Math.round(p.ewma_latency_ms ?? p.latency_ms) + "ms" : "—"}</span>
            <span>成 ${p.ok_count ?? 0} · 败 ${p.fail_count ?? 0}</span>
          </div>
        </div>`).join("") || '<div class="empty">暂无</div>';
    }

    // ---- 资产清单：筛选 + 排序 ----
    let _lastProxies = [];
    const _tableFilter = { q: "", status: "all", hot: "all" };
    const _tableSort = { key: "score", dir: "desc" };

    function filterSortProxies(list) {
      const q = _tableFilter.q.trim().toLowerCase();
      let out = (list || []).slice();
      if (q) {
        out = out.filter((p) => {
          const geo = geoText(p.geo) || "";
          return (
            String(p.host || "").toLowerCase().includes(q) ||
            String(p.port || "").includes(q) ||
            String(p.username || "").toLowerCase().includes(q) ||
            geo.toLowerCase().includes(q)
          );
        });
      }
      if (_tableFilter.status === "ok") out = out.filter((p) => p.last_ok === true);
      else if (_tableFilter.status === "fail") out = out.filter((p) => p.last_ok === false);
      else if (_tableFilter.status === "untested") out = out.filter((p) => p.last_ok == null);
      if (_tableFilter.hot === "hot") out = out.filter((p) => !!p.hot);
      else if (_tableFilter.hot === "cold") out = out.filter((p) => !p.hot);

      const dir = _tableSort.dir === "asc" ? 1 : -1;
      const key = _tableSort.key;
      const val = (p) => {
        switch (key) {
          case "addr": return p.host + ":" + String(p.port).padStart(5, "0");
          case "geo": return geoText(p.geo) || "";
          case "user": return p.username || "";
          case "remaining": return p.remaining ?? -1;
          case "latency": return p.ewma_latency_ms ?? p.latency_ms ?? 1e9;
          case "ewma": return p.ewma_latency_ms ?? 1e9;
          case "score": return p.score ?? 0;
          case "hot": return p.hot ? 1 : 0;
          case "status": return p.last_ok === true ? 2 : p.last_ok === false ? 0 : 1;
          case "ok": return p.ok_count ?? 0;
          case "fail": return p.fail_count ?? 0;
          default: return p.score ?? 0;
        }
      };
      out.sort((a, b) => {
        const va = val(a), vb = val(b);
        if (typeof va === "string" || typeof vb === "string") {
          return String(va).localeCompare(String(vb), "zh") * dir;
        }
        return (va - vb) * dir;
      });
      return out;
    }

    (function bindTableToolbar() {
      const q = $("px-q");
      const st = $("px-status");
      const hot = $("px-hot-sel");
      const reset = $("px-reset");
      if (q) {
        let timer = 0;
        q.addEventListener("input", () => {
          clearTimeout(timer);
          timer = setTimeout(() => {
            _tableFilter.q = q.value || "";
            renderTable(_lastProxies);
          }, 200);
        });
      }
      if (st) st.addEventListener("change", () => {
        _tableFilter.status = st.value || "all";
        renderTable(_lastProxies);
      });
      if (hot) hot.addEventListener("change", () => {
        _tableFilter.hot = hot.value || "all";
        renderTable(_lastProxies);
      });
      if (reset) reset.addEventListener("click", () => {
        _tableFilter.q = "";
        _tableFilter.status = "all";
        _tableFilter.hot = "all";
        _tableSort.key = "score";
        _tableSort.dir = "desc";
        if (q) q.value = "";
        if (st) st.value = "all";
        if (hot) hot.value = "all";
        renderTable(_lastProxies);
      });
    })();

    function renderTable(proxies) {
      _lastProxies = proxies || [];
      const view = filterSortProxies(_lastProxies);
      const rows = view.map((p) => {
        const score = p.score ?? 0;
        const scoreCls = score >= 250 ? "score-hi" : score >= 180 ? "score-md" : "score-lo";
        const rem = p.remaining ?? 0;
        const remPct = Math.max(0, Math.min(100, (rem / 60) * 100));
        const lat = p.ewma_latency_ms ?? p.latency_ms;
        let latCls = "lat-unk", latTxt = "未测";
        if (lat != null) {
          if (lat < 500) { latCls = "lat-fast"; latTxt = Math.round(lat) + "ms"; }
          else if (lat < 1500) { latCls = "lat-mid"; latTxt = Math.round(lat) + "ms"; }
          else { latCls = "lat-slow"; latTxt = Math.round(lat) + "ms"; }
        }
        const st = p.last_ok === true
          ? '<span class="pill ok">通</span>'
          : p.last_ok === false
            ? '<span class="pill err">断</span>'
            : '<span class="pill muted">未测</span>';
        return `<tr>
          <td><code>${esc(p.host)}:${p.port}</code></td>
          <td class="geo-col">${geoText(p.geo) ? esc(geoText(p.geo)) : "—"}</td>
          <td>${p.username ? '<span class="pill muted">' + esc(p.username) + '</span>' : '—'}</td>
          <td>
            <span class="rem-bar"><i style="width:${remPct}%"></i></span>
            <span style="font-variant-numeric:tabular-nums">${rem}s</span>
          </td>
          <td><span class="lat-dot ${latCls}"></span>${latTxt}</td>
          <td>${p.ewma_latency_ms == null ? '<span style="opacity:.45">—</span>' : Math.round(p.ewma_latency_ms) + "ms"}</td>
          <td><span class="score-chip ${scoreCls}">${score}</span></td>
          <td>${p.hot ? '<span class="pill warn"><svg width="12" height="12" style="vertical-align:-2px"><use href="#i-flame"/></svg> 热池</span>' : '<span class="pill muted">—</span>'}</td>
          <td>${st}</td>
          <td><span class="num-bad ${ (p.ok_count||0) > 0 ? 'on' : '' }">${p.ok_count ?? 0}</span></td>
          <td><span class="num-bad ${ (p.fail_count||0) > 0 ? 'err' : '' }">${p.fail_count ?? 0}</span></td>
        </tr>`;
      }).join("");
      const mobileCards = view.slice(0, 50).map((p) => {
        const score = p.score ?? 0;
        const rem = p.remaining ?? 0;
        const lat = p.ewma_latency_ms ?? p.latency_ms;
        return `
        <div class="m-card">
          <div class="top">
            <div class="addr">${esc(p.host)}:${p.port}</div>
            <div class="score">${score}</div>
          </div>
          <div class="tags" style="margin-top:8px">
            ${p.hot ? '<span class="pill warn">热池</span>' : ''}
            ${p.last_ok === true ? '<span class="pill ok">通</span>' : p.last_ok === false ? '<span class="pill err">断</span>' : '<span class="pill muted">未测</span>'}
            ${p.username ? '<span class="pill muted">' + esc(p.username) + '</span>' : ''}
            ${geoText(p.geo) ? '<span class="pill muted">' + esc(geoText(p.geo)) + '</span>' : ''}
          </div>
          <div class="meta">
            <span>剩余 ${rem}s</span>
            <span>延迟 ${lat == null ? "—" : Math.round(lat) + "ms"}</span>
            <span>成 ${p.ok_count ?? 0}</span>
            <span>败 ${p.fail_count ?? 0}</span>
          </div>
        </div>`;
      }).join("");

      const th = (key, label) => {
        const active = _tableSort.key === key;
        const arrow = active ? (_tableSort.dir === "asc" ? " ↑" : " ↓") : "";
        return `<th class="th-sort${active ? " active" : ""}" data-sort="${key}">${label}${arrow}</th>`;
      };

      $("table-wrap").innerHTML = view.length ? `
        <table><thead><tr>
          ${th("addr", "地址")}${th("geo", "归属地")}${th("user", "账密")}
          ${th("remaining", "剩余")}${th("latency", "延迟")}${th("ewma", "平均")}
          ${th("score", "分数")}${th("hot", "热池")}${th("status", "状态")}
          ${th("ok", "成功")}${th("fail", "失败")}
        </tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="empty">没有符合筛选条件的 IP</div>';

      let mwrap = document.getElementById("m-proxy-list");
      if (!mwrap) {
        mwrap = document.createElement("div");
        mwrap.id = "m-proxy-list";
        mwrap.className = "m-only";
        $("table-wrap").parentNode.appendChild(mwrap);
      }
      mwrap.innerHTML = view.length
        ? mobileCards
        : '<div class="empty">没有符合筛选条件的 IP</div>';

      const total = _lastProxies.length;
      if ($("proxy-total")) {
        $("proxy-total").textContent = view.length === total
          ? `共 ${total} 个`
          : `显示 ${view.length} / 共 ${total} 个`;
      }
      if ($("px-filter-meta")) {
        const bits = [];
        if (_tableFilter.q) bits.push(`关键词「${_tableFilter.q}」`);
        if (_tableFilter.status !== "all") bits.push(_tableFilter.status);
        if (_tableFilter.hot !== "all") bits.push(_tableFilter.hot === "hot" ? "仅热池" : "非热池");
        $("px-filter-meta").textContent = bits.length ? bits.join(" · ") + ` · 排序 ${_tableSort.key}${_tableSort.dir === "asc" ? "↑" : "↓"}` : "";
      }

      $("table-wrap").querySelectorAll("th.th-sort").forEach((el) => {
        el.addEventListener("click", () => {
          const key = el.getAttribute("data-sort");
          if (_tableSort.key === key) {
            _tableSort.dir = _tableSort.dir === "asc" ? "desc" : "asc";
          } else {
            _tableSort.key = key;
            _tableSort.dir = ["addr", "geo", "user"].includes(key) ? "asc" : "desc";
          }
          renderTable(_lastProxies);
        });
      });
    }

    async function loadStatus() {
      const data = await api("/api/status");
      const proxies = data.proxies || [];
      const size = data.size || 0;
      const target = data.target || 1;
      const pct = Math.min(100, Math.round((size / target) * 100));
      const okN = proxies.filter((p) => p.last_ok === true).length;
      const hotN = proxies.filter((p) => p.hot).length;
      const withLat = proxies.filter((p) => (p.ewma_latency_ms ?? p.latency_ms) != null);
      const avgLat = withLat.length
        ? Math.round(withLat.reduce((s, p) => s + (p.ewma_latency_ms ?? p.latency_ms), 0) / withLat.length)
        : null;
      const avgScore = proxies.length
        ? Math.round(proxies.reduce((s, p) => s + (p.score || 0), 0) / proxies.length)
        : 0;

      push(hist.size, size);
      push(hist.ok, okN);
      push(hist.lat, avgLat || 0);
      push(hist.score, avgScore);

      $("ring").style.setProperty("--p", pct);
      $("ring-text").textContent = pct + "%";
      $("hero-size").textContent = size;
      $("hero-target").textContent = target;
      window.__poolTarget = target;
      window.__apiMaxNum = data.api_max_num ?? null;
      window.__fetchLimit = data.max_fetch_per_second ?? null;
      $("hero-pct").textContent = pct + "%";
      $("hero-ok").textContent = okN;
      $("hero-lat").textContent = avgLat == null ? "—" : avgLat + "ms";
      $("hero-score").textContent = avgScore;


      renderBars(proxies);
      renderTop(proxies);
      renderTable(proxies);
      $("proxy-total").textContent = "共 " + size + " 个";
      $("ops-hot").textContent = hotN;

      const lv = data.last_verify || {};
      const rate = lv.total ? Math.round((lv.ok / lv.total) * 100) : 0;
      const cum = (lv.cum_ok || 0) + (lv.cum_fail || 0);
      const cumRate = cum ? Math.round((lv.cum_ok / cum) * 100) : 0;
      $("bars-funnel").innerHTML = barHtml([
        { name: "本轮待测", text: lv.total ?? 0, pct: 100, cls: "grad-acc" },
        { name: "通过", text: lv.ok ?? 0, pct: rate, cls: "grad-ok" },
        { name: "失败踢出", text: lv.dropped ?? 0, pct: lv.total ? (lv.dropped / lv.total) * 100 : 0, cls: "grad-err" },
      ]);
      $("funnel-hint").textContent =
        `本轮通过率 ${rate}% · 累计 ${lv.cum_ok ?? 0}/${cum} (${cumRate}%) · 已测 ${lv.verified ?? "—"}`;
      $("v-round").textContent = `${lv.ok ?? 0} / ${lv.total ?? 0}`;
      $("v-round-hint").textContent = `本轮通过率 ${rate}% · 已测 ${lv.verified ?? "—"}`;
      $("v-cum").textContent = `${lv.cum_ok ?? 0} / ${lv.cum_fail ?? 0}`;
      $("v-cum-hint").textContent = cum ? `累计通过率 ${cumRate}%` : "—";
      $("v-top") && ($("v-top").innerHTML = (lv.top || []).map((x) => `<div><code>${x}</code></div>`).join("") || "—");
      $("v-verified") && ($("v-verified").textContent = lv.verified ?? "—");

      // 代理资产页
      if ($("px-total")) {
        $("px-total").textContent = size;
        $("px-ok").textContent = okN;
        $("px-hot").textContent = hotN;
        $("px-high").textContent = proxies.filter((p) => (p.score || 0) >= 200).length;
        const bands = [
          { name: "250+", p: proxies.filter((p) => (p.score || 0) >= 250).length },
          { name: "200–250", p: proxies.filter((p) => (p.score || 0) >= 200 && (p.score || 0) < 250).length },
          { name: "150–200", p: proxies.filter((p) => (p.score || 0) >= 150 && (p.score || 0) < 200).length },
          { name: "100–150", p: proxies.filter((p) => (p.score || 0) >= 100 && (p.score || 0) < 150).length },
          { name: "<100", p: proxies.filter((p) => (p.score || 0) < 100).length },
        ];
        $("px-score-bands").innerHTML = barHtml(bands.map((b) => ({
          name: b.name, text: b.p, pct: (b.p / (proxies.length || 1)) * 100, cls: "grad-acc",
        })));
        const okTop = [...proxies].sort((a, b) => (b.ok_count || 0) - (a.ok_count || 0)).slice(0, 4);
        $("px-ok-top").innerHTML = okTop.map((p) => `
          <div class="ip-card">
            <div class="addr">${p.host}:${p.port}${p.username ? " · " + p.username : ""}</div>
            <div class="score">${p.ok_count ?? 0}</div>
            <div class="tags"><span class="pill ok">成功</span><span class="pill muted">${p.score ?? "—"} 分</span></div>
            <div class="ip-meta"><span>延迟 ${((p.ewma_latency_ms ?? p.latency_ms) != null) ? Math.round(p.ewma_latency_ms ?? p.latency_ms) + "ms" : "—"}</span><span>败 ${p.fail_count ?? 0}</span></div>
          </div>`).join("") || '<div class="empty">暂无</div>';
        renderGeo(proxies);
      }

      // 校验漏斗（共享）
      if ($("v-funnel")) {
        $("v-funnel").innerHTML = barHtml([
          { name: "本轮待测", text: lv.total ?? 0, pct: 100, cls: "grad-acc" },
          { name: "通过", text: lv.ok ?? 0, pct: rate, cls: "grad-ok" },
          { name: "失败踢出", text: lv.dropped ?? 0, pct: lv.total ? (lv.dropped / lv.total) * 100 : 0, cls: "grad-err" },
        ]);
      }
      if ($("v-top-cards")) {
        $("v-top-cards").innerHTML = (lv.top || []).slice(0, 4).map((x) => `
          <div class="ip-card"><div class="addr">${x}</div><div class="tags"><span class="pill ok">Top</span></div></div>
        `).join("") || '<div class="empty">—</div>';
      }

      // 后端时序 + 带宽
      try {
        const m = await api("/api/metrics?limit=48");
        if (m && m.ok !== false && m.series) {
          const toPts = (name) => (m.series[name] || []).map((p) => p[1]);
          const sSize = toPts("pool_size");
          const sOk = toPts("ok_count");
          const sLat = toPts("avg_latency");
          const sScore = toPts("avg_score");
          sparkline("spark-size", sSize.length > 1 ? sSize : hist.size, "a");
          sparkline("spark-ok", sOk.length > 1 ? sOk : hist.ok, "c");
          sparkline("spark-lat", sLat.length > 1 ? sLat : hist.lat, "a");
          sparkline("spark-score", sScore.length > 1 ? sScore : hist.score, "c");
          const bw = m.bandwidth || {};
          $("ops-bw").textContent = fmtBw(bw.last_bps || 0);
          $("ops-bw-peak").textContent = fmtBw(bw.peak_bps || 0);
          if ($("t-bw")) $("t-bw").textContent = fmtBw(bw.last_bps || 0);
          if ($("t-bw-peak")) $("t-bw-peak").textContent = fmtBw(bw.peak_bps || 0);
          if ($("t-bw-total")) $("t-bw-total").textContent = fmtBytes(bw.total_bytes || 0);
          if ($("ops-total-bytes")) $("ops-total-bytes").textContent = fmtBytes(bw.total_bytes || 0);
          if ((m.series.bandwidth || []).length > 1) sparkline("spark-bw", (m.series.bandwidth || []).map((x) => x[1]), "c");
        }
      } catch (_) {}

      window.__fmtBw = fmtBw;
    }

    function fmtDur(sec) {
      sec = Number(sec) || 0;
      if (sec < 60) return sec.toFixed(0) + "s";
      if (sec < 3600) return (sec / 60).toFixed(sec < 600 ? 1 : 0) + "m";
      if (sec < 86400) {
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        return h + "h" + (m ? " " + m + "m" : "");
      }
      const d = Math.floor(sec / 86400);
      const h = Math.floor((sec % 86400) / 3600);
      return d + "d" + (h ? " " + h + "h" : "");
    }

    // 字节 / 速率自动换单位：B → KB → MB → GB → TB → PB（1024 进制，两位小数）
    function fmtUnit(v, units) {
      let x = Number(v);
      if (!isFinite(x) || x < 0) x = 0;
      let i = 0;
      while (x >= 1024 && i < units.length - 1) {
        x /= 1024;
        i++;
      }
      return x.toFixed(2) + " " + units[i];
    }

    function fmtBytes(n) {
      return fmtUnit(n, ["B", "KB", "MB", "GB", "TB", "PB"]);
    }

    function fmtBw(bps) {
      return fmtUnit(bps, ["B/s", "KB/s", "MB/s", "GB/s", "TB/s", "PB/s"]);
    }

    let __loadingTunnel = false;
    async function loadTunnel() {
        if (__loadingTunnel) return;
        __loadingTunnel = true;
        try {
            const t = await api("/api/tunnel");
            if (!t || t.ok === false) {
                if ($("stat-failover")) $("stat-failover").textContent = "离线";
                return;
            }
            const conn = t.connections || 0;
            const succ = t.success || 0;
            const rate = conn ? Math.round((succ / conn) * 100) : 0;
            if ($("t-addr")) $("t-addr").textContent = (t.host || "") + ":" + (t.port || "");
            if ($("t-success")) $("t-success").textContent = succ + " / " + conn;
            if ($("t-rate")) $("t-rate").textContent = conn ? rate + "%" : "—";
            if ($("t-sw")) $("t-sw").textContent = (t.failovers ?? 0) + " / " + (t.kicked ?? 0);
            if ($("ops-sw")) $("ops-sw").textContent = (t.failovers ?? 0) + " / " + (t.kicked ?? 0);
            const adaptive = t.adaptive_timeout;
            const hedgeOn = t.hedge_enabled;
            const cfgMain = (adaptive ? "自适应" : String(t.connect_timeout ?? "—") + "s") + " / " + (t.max_failover ?? "—");
            if ($("t-cfg")) $("t-cfg").textContent = cfgMain;
            if ($("t-cfg-hint")) {
              $("t-cfg-hint").textContent =
                (adaptive ? "建连超时按延迟+剩余 TTL 动态" : "固定建连超时") +
                " · failover≤" + (t.max_failover ?? "—") +
                (hedgeOn ? " · 对冲已开" : " · 对冲关");
            }
            if ($("t-last")) $("t-last").textContent = t.last_proxy || "—";
            if ($("t-err")) $("t-err").textContent = t.last_error || "—";
            if ($("t-uptime")) $("t-uptime").textContent = fmtDur(t.uptime || 0);
            if ($("stat-failover")) $("stat-failover").textContent = (t.failovers ?? 0) + " / " + (t.kicked ?? 0);
            if ($("t-active")) $("t-active").textContent = t.active ?? 0;
            if ($("ops-active")) $("ops-active").textContent = t.active ?? 0;
            if ($("ops-uptime")) $("ops-uptime").textContent = fmtDur(t.uptime || 0);
            if ($("ops-rate")) $("ops-rate").textContent = t.success ?? 0;
            if ($("ops-conn")) $("ops-conn").textContent = t.connections ?? 0;
            if ($("ops-tunnel")) $("ops-tunnel").textContent = rate + "%";
            if ($("stat-tunnel-ok")) $("stat-tunnel-ok").textContent = (t.success ?? 0) + " / " + (t.connections ?? 0);
            const hg = t.hedge || {};
            if ($("t-hedge")) $("t-hedge").textContent = (hg.saved ?? 0) + " / " + (hg.wasted ?? 0);
            if ($("t-adaptive")) $("t-adaptive").textContent = adaptive ? "开" : "关";
            if ($("t-target") && window.__poolTarget != null) {
              $("t-target").textContent = window.__poolTarget;
            }
            if ($("t-num") && window.__apiMaxNum != null) {
              $("t-num").textContent = window.__apiMaxNum;
            }
            if ($("t-limit") && window.__fetchLimit != null) {
              $("t-limit").textContent = window.__fetchLimit + "/s";
            }
            try {
                const m = await api("/api/metrics?limit=2");
                const bw = (m && m.bandwidth) || {};
                if ($("ops-bw")) $("ops-bw").textContent = fmtBw(bw.last_bps || 0);
                if ($("ops-bw-peak")) $("ops-bw-peak").textContent = fmtBw(bw.peak_bps || 0);
                if ($("t-bw")) $("t-bw").textContent = fmtBw(bw.last_bps || 0);
                if ($("t-bw-peak")) $("t-bw-peak").textContent = fmtBw(bw.peak_bps || 0);
                if ($("t-bw-total")) $("t-bw-total").textContent = fmtBytes(bw.total_bytes || 0);
                if ($("ops-total-bytes")) $("ops-total-bytes").textContent = fmtBytes(bw.total_bytes || 0);
            } catch (_) {}
        } catch (e) {
            console.debug("loadTunnel", e);
        } finally {
            __loadingTunnel = false;
        }
    }


    async function tick() {
      try {
        await loadStatus();
        await loadTunnel();
      } catch (e) {
        console.error(e);
      }
    }
    tick();
    // 当前连接 / 带宽：1s 刷新
    setInterval(async () => {
      try {
        await loadTunnel();
      } catch (_) {}
    }, 1000);
    // 全量状态：5s
    setInterval(tick, 5000);
