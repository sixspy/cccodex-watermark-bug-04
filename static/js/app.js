(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let templates = [];
  let batches = [];
  let cards = [];
  let conflicts = [];
  let marks = [];
  let dedup = [];
  let deadLetters = [];
  let queue = [];
  let outcomes = [];
  let reports = [];
  let strategies = [
    { key: "keep_first", label: "留存先手" },
    { key: "rollback_all", label: "整笔回滚·待重打" },
  ];
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  let mergeDraft = {};
  let verifyResult = null;
  let gapVerify = null;

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    return j;
  }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[c]);
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  async function refresh() {
    const meta = await api("/api/meta");
    document.getElementById("lab-name").textContent = meta.lab_name || "血缘水印台";
    if (meta.shadow_strategies && meta.shadow_strategies.length) {
      strategies = meta.shadow_strategies;
    }
    templates = (await api("/api/templates")).items || [];
    batches = (await api("/api/batches")).items || [];
    cards = (await api("/api/batch-cards")).items || [];
    conflicts = (await api("/api/conflicts")).items || [];
    marks = (await api("/api/marks")).items || [];
    dedup = (await api("/api/dedup-progress")).items || [];
    deadLetters = (await api("/api/dead-letters")).items || [];
    queue = (await api("/api/restamp-queue")).items || [];
    outcomes = (await api("/api/merge-outcomes")).items || [];
    reports = (await api("/api/gap-reports")).items || [];
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function marksTable(items) {
    return `<table>
      <thead><tr><th>运行码</th><th>序</th><th>来路</th><th>印记值</th><th>状态</th><th>库存行</th></tr></thead>
      <tbody>
        ${items
          .map(
            (m) => `<tr>
          <td>${esc(m.run_code)}</td>
          <td>${esc(m.mark_seq)}</td>
          <td><span class="chip ${m.origin === "先手" ? "chip-live" : "chip-dead"}">${esc(m.origin)}</span></td>
          <td>${esc(m.mark_value)}</td>
          <td><span class="chip ${m.state === "有效" ? "chip-live" : "chip-warn"}">${esc(m.state)}</span></td>
          <td>${m.source_ok ? "同源" : "异源"}</td>
        </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderBatches() {
    return `
      <h2>批次卡片</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账；卡片同时标出印记枚数与去重停等。</p>
      <div class="cards">
        ${cards
          .map(
            (b) => `<article class="cardish ${b.conflict ? "card-conflict" : ""}">
          <p class="eyebrow">${esc(b.table_name)}</p>
          <h3>${esc(b.run_code)}</h3>
          <p class="muted">业务日 ${esc(b.biz_date)} · 编号 ${esc(b.id)}</p>
          <div class="row">
            <span class="chip">${esc(b.status)}</span>
            <span class="chip ${b.mark_count > 1 ? "chip-warn" : "chip-live"}">印记 ${esc(b.mark_count)} 枚</span>
            ${
              b.conflict
                ? `<span class="chip chip-warn">双影停等</span>`
                : ""
            }
          </div>
          <p>库存值 <b>${esc(b.watermark_value || "未盖印")}</b></p>
          <p class="muted">有效印记 ${esc(b.live_mark || "无")}${
            b.dedup_reason ? " · " + esc(b.dedup_reason) : ""
          }</p>
          ${
            b.status === "待盖印" && !b.conflict
              ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
              : ""
          }
        </article>`
          )
          .join("")}
      </div>
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <p class="muted">一次写库：印记与库存值同源，失败只回一句。</p>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderConflicts() {
    if (!conflicts.length) {
      return `<h2>冲突目录</h2><p class="muted">当前没有双影待处置。</p>`;
    }
    return `
      <h2>冲突目录</h2>
      <p class="muted">一处双影两种落库命运；点合并即服务一次写库，失败只回短句。</p>
      ${conflicts
        .map((c) => {
          const draft = mergeDraft[c.run_code] || {};
          const pending = c.pending !== false && c.stopped;
          const keptChip = c.kept_chip
            ? `<span class="chip chip-live" data-kept-chip="${esc(c.run_code)}">保留 ${esc(
                c.kept_chip
              )}</span>`
            : pending
            ? `<span class="chip chip-warn">待处置</span>`
            : `<span class="chip">无有效印记·待重打</span>`;
          return `<article class="cardish card-conflict" data-conflict="${esc(c.run_code)}">
          <p class="eyebrow">${esc(c.table_name || "-")} · 业务日 ${esc(c.biz_date || "-")}</p>
          <h3>${esc(c.run_code)}</h3>
          <div class="row">
            ${keptChip}
            <span class="chip">印记 ${esc(c.mark_count)} 枚</span>
            <span class="chip ${c.source_ok ? "chip-live" : "chip-warn"}">${
            c.source_ok ? "运行码与库存行同源" : "同源校验失败"
          }</span>
            <span class="chip chip-warn">差额台停等</span>
          </div>
          ${marksTable(c.marks)}
          ${
            pending
              ? `<div class="merge-box">
            <div class="row">
              ${strategies
                .map(
                  (s) => `<label class="inline"><input type="radio" name="strategy-${
                    esc(c.run_code)
                  }" value="${esc(s.key)}" ${
                    draft.strategy === s.key ? "checked" : ""
                  } data-strategy="${esc(c.run_code)}" />${esc(s.label)}</label>`
                )
                .join("")}
            </div>
            ${
              draft.strategy === "rollback_all"
                ? `<label>重打水印值<input data-target="${esc(c.run_code)}" value="${esc(
                    draft.target || "WM-" + c.run_code.toUpperCase() + "-R1"
                  )}" /></label>`
                : ""
            }
            <button type="button" data-merge="${esc(c.run_code)}">合并</button>
            <p class="muted">留存先手：后手整笔回滚入死信；整笔回滚：两枚一并回滚，交排队席重打唯一印记。</p>
          </div>`
              : `<p class="muted">${
                  c.outcome
                    ? "合并结局：" + esc(c.outcome.outcome)
                    : "已处置"
                }</p>`
          }
        </article>`;
        })
        .join("")}`;
  }
  function renderMarks() {
    return `
      <h2>印记列表</h2>
      <p class="muted">印记是盖印的唯一事实来源；每行必须对得上库存行。</p>
      ${marks.length ? marksTable(marks) : `<p class="muted">暂无印记。</p>`}`;
  }

  function renderDedup() {
    const outcomeRows = outcomes.length
      ? `<table>
          <thead><tr><th>运行码</th><th>方案</th><th>结局</th><th>留下印记</th><th>死信</th><th>状态</th></tr></thead>
          <tbody>${outcomes
            .map(
              (o) => `<tr>
              <td>${esc(o.run_code)}</td>
              <td>${esc(o.strategy)}</td>
              <td>${esc(o.outcome)}</td>
              <td>${esc(o.kept_mark_value || "-")}</td>
              <td>${esc(o.dead_count)}</td>
              <td>${esc(o.status)}</td>
            </tr>`
            )
            .join("")}</tbody>
        </table>`
      : `<p class="muted">暂无合并结局。</p>`;
    return `
      <h2>去重进度</h2>
      <p class="muted">死信因逐码亮出；重打完成前保持停等。</p>
      ${
        dedup.length
          ? dedup
              .map(
                (d) => `<article class="cardish ${
                  d.status === "停等" ? "card-conflict" : ""
                }">
          <p class="eyebrow">运行码 ${esc(d.run_code)}</p>
          <div class="row">
            <span class="chip">印记 ${esc(d.total_marks)} 枚</span>
            <span class="chip chip-live">保留 ${esc(d.kept_marks)} 枚</span>
            <span class="chip ${d.dead_letters ? "chip-dead" : ""}">死信 ${esc(
                  d.dead_letters
                )} 枚</span>
            <span class="chip ${d.status === "停等" ? "chip-warn" : "chip-live"}">${esc(
                  d.status
                )}</span>
          </div>
          <p><b>死信因</b>：${esc(d.reason)}</p>
          ${d.outcome ? `<p class="muted">合并结局：${esc(d.outcome.outcome)}</p>` : ""}
        </article>`
              )
              .join("")
          : `<p class="muted">暂无去重任务。</p>`
      }
      <h2>死信箱</h2>
      ${
        deadLetters.length
          ? `<table>
        <thead><tr><th>序号</th><th>运行码</th><th>来路</th><th>印记值</th><th>死信因</th></tr></thead>
        <tbody>${deadLetters
          .map(
            (d) => `<tr>
          <td>${esc(d.id)}</td>
          <td>${esc(d.run_code)}</td>
          <td>${esc(d.origin)}</td>
          <td>${esc(d.mark_value)}</td>
          <td>${esc(d.reason)}</td>
        </tr>`
          )
          .join("")}</tbody>
      </table>`
          : `<p class="muted">死信箱为空。</p>`
      }
      <h2>排队席</h2>
      ${
        queue.length
          ? `<table>
        <thead><tr><th>序号</th><th>运行码</th><th>目标印记</th><th>状态</th><th></th></tr></thead>
        <tbody>${queue
          .map(
            (q) => `<tr>
          <td>${esc(q.id)}</td>
          <td>${esc(q.run_code)}</td>
          <td>${esc(q.target_value)}</td>
          <td>${esc(q.status)}</td>
          <td>${
            q.status === "待重打"
              ? `<button type="button" class="ghost" data-restamp="${esc(q.id)}">重打唯一印记</button>`
              : ""
          }</td>
        </tr>`
          )
          .join("")}</tbody>
      </table>`
          : `<p class="muted">排队席为空。</p>`
      }
      <h2>合并结局</h2>
      ${outcomeRows}
      ${
        verifyResult
          ? `<h2>去重校验</h2>
        <div class="row"><span class="chip ${verifyResult.ok ? "chip-live" : "chip-warn"}">${
              verifyResult.ok ? "通过" : "未通过"
            }</span></div>
        <table>
          <thead><tr><th>项</th><th>结果</th><th>说明</th></tr></thead>
          <tbody>${verifyResult.checks
            .map(
              (c) => `<tr><td>${esc(c.name)}</td><td>${c.ok ? "过" : "不过"}</td><td>${esc(
                c.detail
              )}</td></tr>`
            )
            .join("")}</tbody>
        </table>`
          : ""
      }
      <button type="button" id="btn-verify">去重校验</button>`;
  }

  function renderTrace() {
    const src = (traceResult && traceResult.sources) || [];
    const down = (traceResult && traceResult.downstream) || [];
    const dead = (traceResult && traceResult.dead_letters) || [];
    return `
      <h2>下游追索</h2>
      <label>水印值<input id="trace-input" value="${esc(traceValue)}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button></div>
      ${
        traceResult
          ? `<div>
        <h3>源批次</h3>
        <ul>${src
          .map(
            (s) =>
              `<li>${esc(s.run_code)} · ${esc(s.table_name)} · ${esc(
                s.mark_state || ""
              )}${s.mark_origin ? " · " + esc(s.mark_origin) : ""}</li>`
          )
          .join("")}</ul>
        <h3>下游</h3>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · ${esc(d.status)} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
        ${
          dead.length
            ? `<h3>同值死信</h3>
          <ul>${dead
            .map((d) => `<li>${esc(d.run_code)} · ${esc(d.origin)} · ${esc(d.reason)}</li>`)
            .join("")}</ul>`
            : ""
        }
      </div>`
          : ""
      }`;
  }

  function renderGaps() {
    return `
      <h2>缺口对账</h2>
      <p class="muted">应盖印与已盖印都只从库存行算，双影不参与计数。</p>
      <div class="row">
        <button type="button" id="btn-gap">生成对账</button>
        <button type="button" class="ghost" id="btn-gap-verify">差额校验</button>
      </div>
      ${
        gapVerify
          ? `<div class="row"><span class="chip ${
              gapVerify.ok ? "chip-live" : "chip-warn"
            }">${gapVerify.ok ? "差额校验通过" : "差额校验未通过"}</span>
              <span class="chip">应盖 ${gapVerify.expected_count} · 已盖 ${
              gapVerify.stamped_count
            } · 缺口 ${gapVerify.gap_count}</span></div>
            <table>
              <thead><tr><th>项</th><th>结果</th><th>说明</th></tr></thead>
              <tbody>${gapVerify.checks
                .map(
                  (c) =>
                    `<tr><td>${esc(c.name)}</td><td>${
                      c.ok ? "过" : "不过"
                    }</td><td>${esc(c.detail)}</td></tr>`
                )
                .join("")}</tbody>
            </table>`
          : ""
      }
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>应盖</th><th>已盖</th><th>缺口</th><th>结论</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.expected_count)}</td>
            <td>${esc(r.stamped_count)}</td>
            <td>${esc(r.gap_count)}</td>
            <td><span class="chip ${/停等|失败/.test(r.summary) ? "chip-warn" : ""}">${esc(
                (r.summary.match(/（(.+?)）/g) || ["（）"])[0]
              )}</span></td>
            <td>${esc(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "conflicts") view.innerHTML = renderConflicts();
    else if (tab === "marks") view.innerHTML = renderMarks();
    else if (tab === "dedup") view.innerHTML = renderDedup();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    bind();
  }

  function bind() {
    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = cards.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }

    view.querySelectorAll("[data-strategy]").forEach((el) => {
      el.onchange = () => {
        const code = el.dataset.strategy;
        mergeDraft[code] = { ...(mergeDraft[code] || {}), strategy: el.value };
        if (el.value === "rollback_all" && !mergeDraft[code].target) {
          mergeDraft[code].target = "WM-" + code.toUpperCase() + "-R1";
        }
        render();
      };
    });
    view.querySelectorAll("[data-target]").forEach((el) => {
      el.oninput = () => {
        const code = el.dataset.target;
        mergeDraft[code] = { ...(mergeDraft[code] || {}), target: el.value };
      };
    });
    view.querySelectorAll("[data-merge]").forEach((btn) => {
      btn.onclick = async () => {
        const code = btn.dataset.merge;
        const draft = mergeDraft[code] || {};
        setMsg("");
        if (!draft.strategy) {
          setMsg("请先选定处置方案");
          return;
        }
        try {
          await api("/api/conflicts/" + encodeURIComponent(code) + "/merge", {
            method: "POST",
            body: JSON.stringify({
              strategy: draft.strategy,
              target_value: draft.target || "",
            }),
          });
          mergeDraft[code] = {};
          await refresh();
          const o = outcomes.find((x) => x.run_code === code);
          setMsg(o ? "合并结局：" + o.outcome : "合并完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    view.querySelectorAll("[data-restamp]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          await api("/api/restamp-queue/" + btn.dataset.restamp + "/run", {
            method: "POST",
          });
          await refresh();
          setMsg("唯一印记已重打");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
    const verifyBtn = document.getElementById("btn-verify");
    if (verifyBtn) {
      verifyBtn.onclick = async () => {
        setMsg("");
        try {
          verifyResult = await api("/api/verify/dedup");
          setMsg(verifyResult.ok ? "去重校验通过" : "去重校验未通过");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }

    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({ watermark_value: traceValue }),
          });
          setMsg("追索完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const gapVerifyBtn = document.getElementById("btn-gap-verify");
    if (gapVerifyBtn) {
      gapVerifyBtn.onclick = async () => {
        setMsg("");
        try {
          gapVerify = await api("/api/verify/gap");
          setMsg(gapVerify.ok ? "差额校验通过" : "差额校验未通过");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
