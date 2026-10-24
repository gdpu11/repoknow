const state = {
  user: null,
  repos: [],
  connections: [],
  repo: null,
  file: null,
  filter: "all",
  register: false,
  registrationEnabled: false,
  view: "home",
  routeId: 0,
  fileFilter: "",
  fileScope: "",
};
const app = document.querySelector("#app");
const dialog = document.querySelector("#import-dialog");
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const number = (value) => new Intl.NumberFormat("zh-CN").format(value || 0);
const statusText = (value) =>
  ({
    queued: "等待同步",
    running: "正在索引",
    ready: "已就绪",
    failed: "同步失败",
    unchanged: "无新变更",
    interrupted: "已中断",
  })[value] || value;
const date = (value) =>
  value
    ? new Date(value * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "尚未同步";
const shapes = {
  book: '<path d="M4 3h6a3 3 0 0 1 3 3v15a4 4 0 0 0-4-2H4z"/><path d="M20 3h-4a3 3 0 0 0-3 3v15a4 4 0 0 1 4-2h3z"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  git: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="6" r="2"/><path d="M6 7v10m12-9c0 6-12 2-12 7"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h4m-9 5h6m-6 4h6"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-14-2 16"/>',
  branch:
    '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m12-10c0 7-12 5-12 10"/>',
  graph:
    '<rect x="9" y="2" width="6" height="5" rx="1"/><rect x="2" y="17" width="6" height="5" rx="1"/><rect x="16" y="17" width="6" height="5" rx="1"/><path d="M12 7v5H5v5m7-5h7v5"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M18 5a8 8 0 0 0-13 5m1 9a8 8 0 0 0 13-5"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  external: '<path d="M14 3h7v7m0-7L10 14M9 3H3v18h18v-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  shield:
    '<path d="M12 2 3 6v6c0 5 9 10 9 10s9-5 9-10V6z"/><path d="m8 12 3 3 5-6"/>',
  logout: '<path d="M9 4H3v16h6m5-5 4-3-4-3m-6 3h13"/>',
  menu: '<path d="M3 5h18M3 12h18M3 19h18"/>',
  close: '<path d="m5 5 14 14M19 5 5 19"/>',
  folder: '<path d="M3 7V4h6l3 3h9v13H3z"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  terminal:
    '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m6 8 4 4-4 4m7 0h5"/>',
};
function icon(name) {
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name] || shapes.file}</svg>`;
}
function pill(status) {
  return `<span class="pill ${esc(status)}"><i class="status-dot ${esc(status)}"></i>${esc(statusText(status))}</span>`;
}
function link(repoId, tab = "overview", path = "", line = 0, commit = "") {
  const version = commit || (path && state.repo?.id === repoId ? state.repo.commit_id : "");
  return `#/repo/${encodeURIComponent(repoId)}?${new URLSearchParams({ tab, ...(path ? { path } : {}), ...(line ? { line } : {}), ...(version ? { commit: version } : {}) })}`;
}
function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}
function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = "toast" + (error ? " error" : "");
  item.textContent = message;
  document.querySelector("#toasts").append(item);
  setTimeout(() => item.remove(), 4500);
}
async function api(path, body, method) {
  const response = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-Requested-With": "RepoKnow",
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response
    .json()
    .catch(() => ({ detail: "服务暂时不可用" }));
  if (!response.ok) {
    if (response.status === 401 && state.user) {
      state.user = null;
      showLogin();
    }
    throw new Error(
      typeof result.detail === "string" ? result.detail : "请检查输入格式",
    );
  }
  return result;
}
function sidebar() {
  return `<aside class="sidebar" id="sidebar"><a href="#/" class="brand"><span class="brand-mark">R</span>RepoKnow<small>LOCAL</small></a><a class="nav-link ${state.view === "home" ? "active" : ""}" href="#/">${icon("grid")}知识空间</a><a class="nav-link ${state.view === "search" ? "active" : ""}" href="#/search">${icon("search")}全局检索</a><div class="nav-label">REPOSITORIES · 仓库</div><button class="nav-add" data-action="import">${icon("plus")}导入仓库 / 组 / 组织</button><button class="nav-add" data-action="connections">${icon("lock")}平台凭证</button>${state.user?.is_admin ? `<button class="nav-add" data-action="accounts">${icon("shield")}团队成员</button>` : ""}<nav class="repo-nav">${state.repos.map((r) => `<a href="${link(r.id)}" class="${state.repo?.id === r.id ? "active" : ""}">${icon(r.platform === "github" ? "git" : "branch")}<span>${esc(r.name)}</span><i class="status-dot ${esc(r.sync_state)}"></i></a>`).join("") || '<p class="small-note">导入一个仓库，开始积累知识。</p>'}</nav><div class="sidebar-footer"><span class="avatar">${esc(state.user?.username?.slice(0, 1).toUpperCase())}</span><span>${esc(state.user?.username)}</span><button data-action="logout" aria-label="退出登录" title="退出登录">${icon("logout")}</button></div></aside>`;
}
function shell(content, breadcrumb = "知识空间") {
  app.innerHTML = `<div class="shell">${sidebar()}<main class="main"><header class="topbar"><div class="breadcrumb"><button class="mobile-menu" data-action="menu" aria-label="展开菜单">${icon("menu")}</button><a href="#/">工作台</a><span>/</span><strong>${esc(breadcrumb)}</strong></div><button class="top-search" data-action="focus-search">${icon("search")}搜索代码、文档、符号<kbd>⌘ K</kbd></button></header><div class="workspace" id="content">${content}</div></main></div>`;
}
function loading(title = "正在读取索引") {
  shell(
    `<div class="page-head"><div><div class="eyebrow">REPOSITORY KNOWLEDGE</div><h1 class="page-title">${esc(title)}</h1></div></div><div class="skeleton"></div>`,
    title,
  );
}
function empty(title, description, buttons = "") {
  return `<div class="empty">${icon("book")}<h3>${esc(title)}</h3><p>${esc(description)}</p>${buttons ? `<div class="toolbar">${buttons}</div>` : ""}</div>`;
}
function stats(items) {
  return `<div class="stat-grid">${items.map(([label, value, unit, ico]) => `<div class="stat"><div class="stat-label">${esc(label)}${icon(ico)}</div><div class="stat-number">${number(value)}<span class="stat-unit">${esc(unit)}</span></div></div>`).join("")}</div>`;
}
function home() {
  state.view = "home";
  state.repo = null;
  state.file = null;
  const all = state.repos;
  const repos = all.filter(
    (r) => state.filter === "all" || r.platform === state.filter,
  );
  const total = (key) => all.reduce((n, r) => n + (r.statistics[key] || 0), 0);
  shell(
    `<div class="page-head"><div><div class="eyebrow">YOUR ENGINEERING KNOWLEDGE</div><h1 class="page-title">让每个仓库，都有一本说明书。</h1><p class="intro">文档、代码与依赖关系，在同一个知识空间里找到。每条线索都能追溯到源文件。</p></div><button class="btn primary" data-action="import">${icon("plus")}添加仓库</button></div>${stats(
      [
        ["已接入仓库", all.length, "个", "git"],
        ["仓库文档", total("documents"), "篇", "book"],
        ["代码符号", total("symbols"), "个", "code"],
        ["调用线索", total("relations"), "条", "graph"],
      ],
    )}<div class="section-head"><h2>我的知识库<small>${all.length} 个仓库 · 私有与共享空间</small></h2><div class="filters">${[
      ["all", "全部"],
      ["github", "GitHub"],
      ["gitlab", "GitLab"],
    ]
      .map(
        ([key, label]) =>
          `<button class="${state.filter === key ? "active" : ""}" data-action="filter" data-value="${key}">${label}</button>`,
      )
      .join(
        "",
      )}</div></div>${repos.length ? `<div class="repo-grid">${repos.map((r) => `<a class="repo-card" href="${link(r.id)}"><div class="repo-card-top"><span class="platform-icon ${r.platform}">${icon("git")}</span>${pill(r.sync_state)}</div><h3>${esc(r.name)}</h3><div class="repo-url">${esc(r.url.replace(/^https?:\/\//, ""))}</div><div class="repo-card-meta"><span>${icon("branch")}${esc(r.branch || "默认分支")}</span><span>${icon("file")}${number(r.statistics.files)} 文件</span><span>${icon(r.visibility === "private" ? "lock" : "book")}${r.visibility === "private" ? "私有" : "共享"}</span></div></a>`).join("")}</div>` : empty("从一个仓库开始", "粘贴 GitHub 或 GitLab 地址，文档和源码会一起建立索引。", `<button class="btn primary" data-action="import">${icon("plus")}导入仓库</button>`)}<div class="hint-panel">${icon("shield")}<div><strong>知识有出处，理解有依据。</strong><p>索引来自固定 Git 快照。搜索结果保留 commit、文件路径与行号，可直接打开来源核对。私有仓库默认仅自己可见。</p></div></div>`,
  );
}
const tabs = [
  ["overview", "概览", "grid"],
  ["docs", "文档", "book"],
  ["code", "代码", "code"],
  ["symbols", "符号", "graph"],
  ["activity", "同步记录", "clock"],
];
function repositoryHeader(tab) {
  const r = state.repo;
  const commit = state.file?.commit_id || r.commit_id;
  return `<div class="page-head repo-page-head"><div><div class="eyebrow">${esc(r.platform.toUpperCase())} REPOSITORY</div><div class="repo-heading"><span class="platform-icon ${r.platform}">${icon("git")}</span><h1 class="page-title">${esc(r.name)}</h1><span class="pill private">${icon(r.visibility === "private" ? "lock" : "book")}${r.visibility === "private" ? "私有知识库" : "团队共享"}</span></div><div class="repo-subline"><span>${icon("branch")} ${esc(r.branch || "默认分支")}</span>${commit ? `<code>${esc(commit.slice(0, 10))}</code>` : ""}<span>索引于 ${date(r.indexed_at)}</span></div></div><div class="toolbar"><a class="btn" target="_blank" rel="noopener noreferrer" href="${esc(r.url.replace(/\.git$/, ""))}">${icon("external")}原仓库</a>${r.owner_id === state.user.id ? `<button class="btn" data-action="credentials">更新凭证</button><button class="btn primary" data-action="sync" ${["queued", "running"].includes(r.sync_state) ? "disabled" : ""}>${icon("refresh")}${["queued", "running"].includes(r.sync_state) ? "同步中" : "同步更新"}</button>` : ""}</div></div>${r.error ? `<div class="banner error">${icon("clock")}<div>${esc(r.error)}${r.current_snapshot ? "<br>上一版索引仍可正常使用。" : ""}</div></div>` : ""}${["queued", "running"].includes(r.sync_state) ? `<div class="banner">${icon("refresh")}正在同步仓库并建立索引，完成后页面会自动更新。</div>` : ""}<nav class="tabs">${tabs.map(([key, label, ico]) => `<a href="${link(r.id, key)}" class="tab ${key === tab ? "active" : ""}">${icon(ico)}${label}${key === "docs" ? `<span class="tab-count">${r.statistics.documents || 0}</span>` : key === "symbols" ? `<span class="tab-count">${r.statistics.symbols || 0}</span>` : ""}</a>`).join("")}</nav>`;
}
function renderRepo(tab, content) {
  shell(repositoryHeader(tab) + content, state.repo.name);
}
function overviewPage() {
  const r = state.repo,
    s = r.statistics,
    o = r.overview;
  const docs = r.files.filter((f) => f.kind === "document");
  const readme = docs.find((f) => /^(readme)(\.|$)/i.test(f.path)) || docs[0];
  const skipLabels = {
    sensitive_file: "敏感文件",
    excluded_directory: "排除目录",
    large_file: "大文件",
    generated_file: "生成产物",
    unsupported_or_binary: "其他格式",
    non_utf8: "非 UTF-8",
    binary: "二进制",
    secret_pattern: "疑似密钥",
    symlink_or_submodule: "符号链接 / 子模块",
  };
  renderRepo(
    "overview",
    `${stats([
      ["索引文件", s.files, "个", "file"],
      ["项目文档", s.documents, "篇", "book"],
      ["函数与类型", s.symbols, "个", "code"],
      ["调用线索", s.relations, "条", "graph"],
    ])}${
      r.current_snapshot
        ? `<div class="overview-grid"><div><section class="panel overview-section"><div class="panel-head">${icon("book")}项目文档<small>直接来自仓库</small></div><div class="panel-body">${
            readme
              ? `<p class="small-note">从项目说明开始，再沿目录深入模块和实现。</p><div class="link-list">${docs
                  .slice(0, 8)
                  .map(
                    (f) =>
                      `<a href="${link(r.id, "docs", f.path)}">${icon("file")}${esc(f.path)}<small>${f.line_count} 行 ${icon("arrow")}</small></a>`,
                  )
                  .join("")}</div>`
              : empty("暂未发现项目文档", "可在代码页查看已索引的源文件。")
          }</div></section><section class="panel overview-section"><div class="panel-head">${icon("terminal")}入口候选<small>基于文件名识别</small></div><div class="panel-body"><div class="link-list">${o.entrypoints.map((path) => `<a href="${link(r.id, "code", path)}">${icon("code")}${esc(path)}<small>查看源码 ${icon("arrow")}</small></a>`).join("") || '<p class="small-note">未匹配常见入口文件名，可使用符号或全文检索定位。</p>'}</div></div></section><section class="panel overview-section"><div class="panel-head">项目依赖<small>来自依赖清单</small></div><div class="panel-body">${
            o.dependencies.length
              ? `<table class="dep-table">${o.dependencies
                  .slice(0, 20)
                  .map(
                    (d) =>
                      `<tr><td><a href="${link(r.id, "code", d.path)}">${esc(d.name)}</a></td><td>${esc(d.version)}</td></tr>`,
                  )
                  .join("")}</table>`
              : '<p class="small-note">当前未提取到 package.json、requirements.txt 或 go.mod 依赖。</p>'
          }</div></section></div><div><section class="panel overview-section"><div class="panel-head">文件语言<small>${Object.keys(s.languages || {}).length} 种</small></div><div class="panel-body">${Object.entries(
            s.languages || {},
          )
            .sort((a, b) => b[1] - a[1])
            .map(
              ([name, count]) =>
                `<div class="lang-row"><strong><span class="lang-dot"></span>${esc(name)}</strong><small>${count} 文件</small></div>`,
            )
            .join(
              "",
            )}</div></section><section class="panel overview-section"><div class="panel-head">模块目录</div><div class="panel-body"><div class="link-list">${o.modules.map((m) => `<a href="${link(r.id, "code") + "&dir=" + encodeURIComponent(m.name === "根目录" ? "." : m.name)}">${icon("folder")}${esc(m.name)}<small>${m.files} 文件</small></a>`).join("")}</div></div></section><section class="panel overview-section"><div class="panel-head">索引信息</div><div class="panel-body"><div class="meta-row"><span>索引版本</span><strong class="mono">${esc((r.commit_id || "").slice(0, 10))}</strong></div><div class="meta-row"><span>检索片段</span><strong>${number(s.chunks)}</strong></div><div class="meta-row"><span>已跳过</span><strong>${number(s.skipped)} 文件</strong></div>${Object.entries(
            s.skipped_reasons || {},
          )
            .map(
              ([key, n]) =>
                `<div class="meta-row"><span>${esc(skipLabels[key] || key)}</span><strong>${n}</strong></div>`,
            )
            .join(
              "",
            )}</div><div class="index-note">符号解析：${esc((s.parsed_languages || []).join("、") || "当前语言仅做全文索引")}。调用线索来自静态名称匹配，不代表完整运行时调用链。</div></section></div></div>`
        : empty(
            "仓库正在准备中",
            "首次同步成功后，这里会出现文档、模块和依赖概览。",
          )
    }`,
  );
}
async function filesPage(tab, params, routeId) {
  const r = state.repo;
  const directory = params.get("dir") || "";
  const fileLink = (target, view = tab, line = 0) =>
    link(r.id, view, target, line) + (directory ? "&dir=" + encodeURIComponent(directory) : "");
  const scope = `${r.id}:${tab}:${directory}`;
  if (state.fileScope !== scope) {
    state.fileFilter = "";
    state.fileScope = scope;
  }
  const files = r.files.filter((f) =>
    (tab === "code" || f.kind === "document") &&
    (!directory || (directory === "." ? !f.path.includes("/") : f.path.startsWith(directory + "/")))
  );
  let path = params.get("path") || "";
  if (!path)
    path =
      (files.find((f) => /^(readme)(\.|$)/i.test(f.path)) || files[0])?.path ||
      "";
  if (!path) {
    renderRepo(
      tab,
      empty("暂无可浏览的文件", "同步完成后可在这里查看已索引的文档和源码。"),
    );
    return;
  }
  const tree = files
    .map(
      (f) =>
        `<a class="file-item ${path === f.path ? "active" : ""}" data-path="${esc(f.path.toLowerCase())}" href="${fileLink(f.path)}" title="${esc(f.path)}">${icon(f.kind === "document" ? "file" : "code")}<span>${esc(f.path)}</span></a>`,
    )
    .join("");
  renderRepo(
    tab,
    `<div class="file-layout"><aside class="file-tree"><div class="tree-title">${tab === "docs" ? "DOCUMENTS" : "SOURCE FILES"} · <span id="file-match-count">${files.length}</span></div><div class="file-filter"><input class="input" id="file-filter" aria-label="筛选文件路径" placeholder="筛选文件路径…" value="${esc(state.fileFilter)}"></div>${tree}<p class="small-note file-no-match" id="file-no-match" hidden>没有匹配的文件</p></aside><section class="file-pane" id="file-pane"><div class="loading-content"><div class="skeleton"></div></div></section></div>`,
  );
  filterFileTree();
  const file = await api(
    `/api/repositories/${r.id}/file?${new URLSearchParams({ path, commit: r.commit_id || "" })}`,
  );
  if (routeId !== state.routeId) return;
  state.file = file;
  const showMarkdown = tab === "docs" && file.html !== undefined;
  const selectedLine = Number(params.get("line")) || 0;
  document.querySelector("#file-pane").innerHTML =
    `<div class="file-bar"><span>${esc(path)} <span class="muted">· ${file.line_count} 行 · ${esc(file.commit_id.slice(0, 10))}</span></span><div class="toolbar">${showMarkdown ? `<a class="btn ghost small" href="${link(r.id, "code", path)}">${icon("code")}源码</a>` : ""}<a class="btn ghost small" target="_blank" rel="noopener noreferrer" href="${esc(file.source_url)}" title="打开固定版本来源">${icon("external")}</a></div></div>${
      showMarkdown
        ? `<article class="markdown">${file.html}</article>`
        : `<div class="source-view">${file.content
            .split("\n")
            .slice(0, file.line_count)
            .map(
              (line, i) =>
                `<div class="source-line ${i + 1 === selectedLine ? "highlight" : ""}" id="L${i + 1}"><a class="line-number" href="${fileLink(path, "code", i + 1)}">${i + 1}</a><code>${esc(line)}</code></div>`,
            )
            .join("")}</div>`
    }`;
  if (showMarkdown) {
    rewriteMarkdownLinks(path, r);
    const anchor = params.get("anchor");
    if (anchor) document.getElementById(`doc-${anchor}`)?.scrollIntoView({ block: "start" });
  }
  if (selectedLine)
    document
      .querySelector(`#L${selectedLine}`)
      ?.scrollIntoView({ block: "center" });
}
function filterFileTree() {
  const items = document.querySelectorAll(".file-item");
  let visible = 0;
  for (const item of items) {
    item.hidden = !item.dataset.path.includes(state.fileFilter.toLowerCase());
    if (!item.hidden) visible++;
  }
  const count = document.querySelector("#file-match-count");
  if (count) count.textContent = state.fileFilter ? `${visible} / ${items.length}` : items.length;
  const empty = document.querySelector("#file-no-match");
  if (empty) empty.hidden = visible > 0;
}
function rewriteMarkdownLinks(path, repo) {
  const anchors = new Map();
  document.querySelectorAll(".markdown h1, .markdown h2, .markdown h3, .markdown h4, .markdown h5, .markdown h6").forEach((heading) => {
    const slug = heading.textContent.toLowerCase().replace(/[^\p{L}\p{N}\s_-]/gu, "").replace(/\s/g, "-");
    const count = anchors.get(slug) || 0;
    anchors.set(slug, count + 1);
    heading.id = `doc-${slug}${count ? "-" + count : ""}`;
  });
  document.querySelectorAll(".markdown a").forEach((a) => {
    const href = a.getAttribute("href");
    if (!href) return;
    if (href.startsWith("#")) {
      a.dataset.documentAnchor = "";
      try { a.dataset.documentAnchor = decodeURIComponent(href.slice(1)); } catch { /* Keep malformed anchors inert. */ }
      return;
    }
    if (!/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith("//")) {
      const resolved = new URL(href, "https://repo.invalid/" + path);
      let target;
      try { target = decodeURIComponent(resolved.pathname.slice(1)); } catch { target = ""; }
      const file = repo.files.find((f) => f.path === target);
      a.href = file
        ? link(repo.id, file.kind === "document" ? "docs" : "code", target) + (resolved.hash ? "&anchor=" + resolved.hash.slice(1) : "")
        : repo.url.replace(/\.git$/, "") +
          (repo.platform === "github" ? "/blob/" : "/-/blob/") +
          repo.commit_id +
          "/" +
          resolved.pathname.slice(1);
    } else {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  });
}
async function symbolsPage(query = "", routeId = state.routeId) {
  const data = await api(
    `/api/repositories/${state.repo.id}/symbols?${new URLSearchParams({ q: query })}`,
  );
  if (routeId !== state.routeId) return;
  if (data.commit_id !== state.repo.commit_id)
    throw new Error("索引版本已更新，请重新打开符号列表。");
  renderRepo(
    "symbols",
    `<form id="symbol-search" class="symbol-filter"><input class="input" name="q" placeholder="搜索函数、类或方法，按 Enter 确认" value="${esc(query)}" aria-label="搜索符号"></form>${data.symbols.length ? `<section class="panel"><table class="table"><thead><tr><th>符号 / 定义</th><th>类型</th><th>来源</th><th>关系</th></tr></thead><tbody>${data.symbols.map((s) => `<tr><td><a class="symbol-link" href="${link(state.repo.id, "code", s.path, s.start_line)}"><strong>${esc(s.qualified_name)}</strong></a></td><td><span class="pill">${esc(s.kind)}</span></td><td><a class="path" href="${link(state.repo.id, "code", s.path, s.start_line)}">${esc(s.path)}:${s.start_line}</a></td><td><button class="btn ghost small" data-action="trace" data-id="${s.id}">${icon("graph")}查看</button></td></tr>`).join("")}</tbody></table>${data.truncated ? '<div class="index-note">结果超过 300 项，请输入更具体的符号名称。</div>' : ""}</section>` : empty("没有匹配的符号", "当前支持 Python、Go、JavaScript、TypeScript、Java 的语法树解析。其他文本仍可全文检索。")}`,
  );
}
async function tracePage(symbolId) {
  const id = ++state.routeId;
  const data = await api(
    `/api/repositories/${state.repo.id}/trace/${encodeURIComponent(symbolId)}`,
  );
  if (id !== state.routeId) return;
  if (data.commit_id !== state.repo.commit_id)
    throw new Error("索引版本已更新，请重新打开符号列表。");
  const incoming = data.relations.filter((e) => e.target_id === symbolId);
  const outgoing = data.relations.filter((e) => e.source_id === symbolId);
  const node = (edge, inbound) =>
    `<button class="graph-node" data-action="trace" data-id="${esc(inbound ? edge.source_id : edge.target_id)}">${esc(inbound ? edge.source_name : edge.target_name)}<small>${inbound ? "调用当前符号" : "被当前符号调用"} · ${edge.confidence >= 0.8 ? "同文件名称匹配" : "名称匹配候选"}</small></button>`;
  renderRepo(
    "symbols",
    `<section class="panel"><div class="panel-head">${icon("graph")}直接调用线索<button class="btn ghost small" data-action="symbols-back">返回符号列表</button></div><div class="graph-grid"><div class="graph-column"><h3>调用方 →</h3>${incoming.map((e) => node(e, true)).join("") || '<p class="small-note">未匹配到直接调用方</p>'}</div><div class="graph-column"><h3>当前符号</h3><a class="graph-node root" href="${link(state.repo.id, "code", data.root.path, data.root.start_line)}">${esc(data.root.qualified_name)}<small>${esc(data.root.path)}:${data.root.start_line}</small></a></div><div class="graph-column"><h3>→ 被调用方</h3>${outgoing.map((e) => node(e, false)).join("") || '<p class="small-note">未匹配到直接调用目标</p>'}</div></div><div class="graph-evidence">${data.relations.map((e) => `<a href="${link(state.repo.id, "code", e.path, e.line)}">${esc(e.source_name)} → ${esc(e.target_name)} · ${esc(e.path)}:${e.line}</a>`).join("")}</div><div class="index-note">${esc(data.note)}${data.truncated ? " 当前仅展示前 100 条关系。" : ""}</div></section>`,
  );
}
async function activityPage(routeId) {
  const jobs = await api(`/api/repositories/${state.repo.id}/jobs`);
  if (routeId !== state.routeId) return;
  renderRepo(
    "activity",
    `<section class="panel"><div class="panel-head">同步记录<small>最近 30 次</small></div><div class="timeline">${jobs.map((j) => `<div class="job"><div>${pill(j.state)}</div><div class="job-info">${j.commit_id ? `<code>${esc(j.commit_id.slice(0, 12))}</code>` : "正在获取远端版本"}${j.error ? `<div class="job-error">${esc(j.error)}</div>` : ""}${j.state === "unchanged" ? "<div>远端没有新提交，复用已有索引。</div>" : ""}</div><time class="muted">${date(j.created_at)}</time></div>`).join("")}</div></section>`,
  );
}
async function searchPage(params, routeId) {
  state.view = "search";
  state.file = null;
  const q = params.get("q") || "",
    repoId = params.get("repo") || "",
    kind = params.get("kind") || "";
  state.repo = repoId ? state.repos.find((r) => r.id === repoId) || null : null;
  shell(
    `<div class="page-head"><div><div class="eyebrow">SEARCH WITH EVIDENCE</div><h1 class="page-title">找到答案的出处。</h1><p class="intro">检索文档、源码和代码符号。每条结果都保留版本和来源。</p></div></div><form id="knowledge-search" class="search-box"><input class="input" id="search-input" name="q" value="${esc(q)}" placeholder="函数名、业务关键词，或一个问题…" aria-label="搜索知识库"><select class="input" name="repo" aria-label="仓库范围" style="max-width:180px"><option value="">所有可访问仓库</option>${state.repos.map((r) => `<option value="${r.id}" ${r.id === repoId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select><input type="hidden" name="kind" value="${esc(kind)}"><button class="btn primary" type="submit">${icon("search")}搜索</button></form><div class="search-meta"><span id="search-count">${q ? "正在检索…" : "输入一个关键词开始检索"}</span><div class="filters">${[
      ["", "全部"],
      ["document", "文档"],
      ["code", "源码"],
      ["symbol", "符号"],
    ]
      .map(
        ([key, label]) =>
          `<button class="${kind === key ? "active" : ""}" data-action="search-kind" data-value="${key}">${label}</button>`,
      )
      .join(
        "",
      )}</div></div><div id="search-results">${q ? '<div class="skeleton"></div>' : empty("知识与源码，一次检索", "试试 main、配置、启动、认证，或一个具体的函数名称。")}</div>`,
    "全局检索",
  );
  if (!q) return;
  const data = await api(
    "/api/search?" + new URLSearchParams({ q, repository_id: repoId, kind }),
  );
  if (routeId !== state.routeId) return;
  document.querySelector("#search-count").textContent =
    `展示 ${data.results.length} / ${data.total} 条证据${data.truncated ? "（候选已截断）" : ""} · 关键词 ${data.terms.join(" / ")}`;
  document.querySelector("#search-results").innerHTML = data.results.length
    ? data.results
        .map((item) => {
          const lines = item.text.split("\n");
          const match = lines.findIndex((line) =>
            data.terms.some((t) => line.toLowerCase().includes(t)),
          );
          const start = Math.max(0, match - 2);
          const snippet = lines
            .slice(start, start + 10)
            .join("\n")
            .slice(0, 1300);
          return `<article class="search-result"><div class="result-header">${icon(item.kind === "document" ? "book" : "code")}<strong><a href="${link(item.repository_id, item.kind === "document" ? "docs" : "code", item.path, item.start_line + start, item.commit_id)}">${esc(item.name)}</a></strong><span class="pill">${esc(item.repository_name)}</span><a class="result-source" href="${esc(item.source_url)}" target="_blank" rel="noopener noreferrer">来源 ${icon("external")}</a></div><a href="${link(item.repository_id, item.kind === "document" ? "docs" : "code", item.path, item.start_line + start, item.commit_id)}"><pre>${esc(snippet)}</pre></a><div class="result-footer"><span>${esc(item.path)}:${item.start_line}–${item.end_line}</span><code>${esc(item.commit_id.slice(0, 10))}</code><span>${item.kind === "document" ? "仓库文档" : item.kind === "symbol" ? "代码符号" : "源码片段"}</span></div></article>`;
        })
        .join("")
    : empty("没有找到匹配证据", "试试更短的关键词，或切换仓库与内容范围。");
}
async function route() {
  if (!state.user) return;
  const routeId = ++state.routeId;
  const [pathname, query = ""] = (location.hash.slice(1) || "/").split("?");
  const params = new URLSearchParams(query);
  try {
    if (pathname === "/search") {
      await searchPage(params, routeId);
      return;
    }
    if (pathname.startsWith("/repo/")) {
      state.view = "repo";
      state.file = null;
      const repoId = decodeURIComponent(pathname.split("/")[2]);
      loading("正在读取仓库");
      const repo = await api("/api/repositories/" + encodeURIComponent(repoId));
      if (routeId !== state.routeId) return;
      state.repo = repo;
      const tab = params.get("tab") || "overview";
      const expectedCommit = params.get("commit");
      if (expectedCommit && expectedCommit !== repo.commit_id) {
        renderRepo(tab, empty(
          "索引版本已更新",
          `该链接引用 ${expectedCommit.slice(0, 12)}，当前索引为 ${(repo.commit_id || "尚未就绪").slice(0, 12)}。本地仅保留当前索引，请确认后查看新版本，行号可能已变化。`,
          `<a class="btn primary" href="${link(repo.id, tab, params.get("path") || "")}">查看当前版本</a>`,
        ));
        return;
      }
      if (tab === "docs" || tab === "code")
        await filesPage(tab, params, routeId);
      else if (tab === "symbols")
        await symbolsPage(params.get("q") || "", routeId);
      else if (tab === "activity") await activityPage(routeId);
      else overviewPage();
      return;
    }
    home();
  } catch (error) {
    if (state.user && routeId === state.routeId)
      shell(
        empty(
          "暂时无法读取",
          error.message,
          `<button class="btn" data-action="retry">重试</button>`,
        ),
        "读取失败",
      );
  }
}
function gitCredentialFields() {
  return `<div class="field-row"><div class="field"><label for="git-username">Git 账号</label><input class="input" id="git-username" name="username" autocomplete="off" placeholder="Git 用户名 / Deploy Token 用户名"></div><div class="field"><label for="git-token">访问 Token</label><input class="input" id="git-token" name="token" type="password" autocomplete="new-password" placeholder="只读 Token，不会回显"></div></div>`;
}
function dialogFrame(title, intro, content) {
  dialog.innerHTML = `<div class="dialog-head"><div><h2 id="import-title">${esc(title)}</h2><p>${esc(intro)}</p></div><button class="btn ghost small" data-action="close-dialog" aria-label="关闭">${icon("close")}</button></div>${content}`;
  if (!dialog.open) dialog.showModal();
}
async function importDialog(credentialsOnly = false) {
  if (credentialsOnly) {
    dialogFrame("更新仓库凭证", "仅更新此仓库，并取消它对平台凭证的引用；下次同步生效。", `<form id="credential-form" class="dialog-body">${gitCredentialFields()}<div class="field-error" role="alert"></div><div class="dialog-actions"><button class="btn ghost" type="button" data-action="close-dialog">取消</button><button class="btn primary" type="submit">保存凭证</button></div></form>`);
    return;
  }
  state.connections = await api("/api/connections");
  dialogFrame("导入仓库、组或组织", "单仓库直接建立索引；GitLab 组和 GitHub 组织可批量发现并导入。", `<form id="import-form" class="dialog-body">
    <div class="field-row"><div class="field"><label for="source-kind">导入类型</label><select class="input" id="source-kind" name="kind"><option value="repository">单个 GitHub / GitLab 仓库</option><option value="group">GitLab 组 / 子组</option><option value="organization">GitHub 组织 / Enterprise</option></select></div><div class="field"><label for="repo-visibility">知识库可见性</label><select id="repo-visibility" class="input" name="visibility"><option value="private">仅自己可见</option><option value="shared">与所有登录用户共享</option></select></div></div>
    <div class="field"><label for="repo-url">仓库、组或组织地址</label><input class="input" id="repo-url" name="url" type="url" required placeholder="https://gitlab.example.com/engineering/project.git"><small>组：https://gitlab.example.com/engineering；组织：https://github.com/example-org。</small></div>
    <div class="field"><label for="connection-id">平台凭证</label><select class="input" id="connection-id" name="connection_id"><option value="">自动使用同域名凭证；无配置时匿名访问</option>${state.connections.map(c=>`<option value="${esc(c.id)}">${esc(c.name)} · ${esc(c.base_url)}</option>`).join("")}</select><small>在侧栏“平台凭证”中配置一次，后续同域名导入会自动复用。</small></div>
    <div id="repository-options"><div class="field-row"><div class="field"><label for="repo-name">知识库名称</label><input class="input" id="repo-name" name="name" placeholder="留空使用仓库名称"></div><div class="field"><label for="repo-branch">分支</label><input class="input" id="repo-branch" name="branch" placeholder="留空检测默认分支"></div></div><div class="field"><label for="repo-platform">仓库平台</label><select class="input" id="repo-platform" name="platform"><option value="auto">自动识别 / 使用平台配置</option><option value="github">GitHub / GitHub Enterprise</option><option value="gitlab">GitLab / 自建 GitLab</option></select></div><details><summary>仅为此仓库填写凭证</summary><p class="small-note">填写后覆盖自动选择的平台凭证；不要同时手选平台凭证。</p>${gitCredentialFields()}</details></div>
    <div id="namespace-options" hidden><label id="subgroup-option"><input type="checkbox" name="include_subgroups" checked> 包含全部子组</label><p class="small-note" id="namespace-help"></p><p class="small-note">仅发现当前凭证可访问的仓库；已有分支跳过，无默认分支等情况会说明原因。再次导入可补充新仓库，不会定时扫描。</p><button class="btn small" type="button" data-action="preview-namespace">仅查看发现结果</button><p class="small-note" id="namespace-preview" role="status"></p></div>
    <div class="field-error" role="alert"></div><div class="dialog-actions"><button class="btn ghost" type="button" data-action="close-dialog">取消</button><button class="btn primary" type="submit">导入并建立索引</button></div></form>`);
}
function namespaceFields(fields) {
  const data = {url: fields.url, connection_id: fields.connection_id};
  if (fields.kind === "group") data.include_subgroups = fields.include_subgroups === "on";
  return data;
}
async function accountsDialog() {
  const accounts = await api("/api/admin/users");
  dialogFrame("团队成员", "由管理员开通账户。停用成员会撤销其登录会话和只读 Key，保留已有知识库与凭证。", `<div class="dialog-body"><div class="link-list">${accounts.map(a => `<div class="meta-row"><span><strong>${esc(a.username)}</strong><br>${a.is_admin ? "管理员" : a.disabled ? "已停用" : "正常"}</span>${a.is_admin ? '<span class="pill">管理员</span>' : `<button class="btn small" data-action="account-access" data-id="${esc(a.id)}" data-disabled="${!a.disabled}">${a.disabled ? "启用" : "停用"}</button>`}</div>`).join("")}</div><hr class="divider"><form id="member-form"><h3>新增普通成员</h3><div class="field-row"><div class="field"><label for="member-name">账号</label><input class="input" id="member-name" name="username" required minlength="2" maxlength="64" autocomplete="off"></div><div class="field"><label for="member-password">初始密码</label><input class="input" id="member-password" name="password" type="password" required minlength="10" maxlength="200" autocomplete="new-password" placeholder="至少 10 个字符"></div></div><p class="small-note">私有知识库仍仅所有者可读；共享知识库对所有启用的登录成员可读。</p><div class="field-error" role="alert"></div><div class="dialog-actions"><button class="btn ghost" type="button" data-action="close-dialog">关闭</button><button class="btn primary" type="submit">创建成员</button></div></form></div>`);
}
async function connectionsDialog(editId = "") {
  state.connections = await api("/api/connections");
  const current = state.connections.find(c=>c.id===editId);
  dialogFrame("平台凭证", "Token 按账号和平台域名隔离，加密保存在本机。同域名仓库可自动复用。", `<div class="dialog-body">
    ${!current ? `<div class="link-list">${state.connections.map(c=>`<div class="meta-row"><span><strong>${esc(c.name)}</strong><br>${esc(c.base_url)} · ${c.has_credentials ? "已加密保存" : "匿名访问"}</span><button class="btn small" data-action="edit-connection" data-id="${esc(c.id)}">更新</button></div>`).join("") || '<p class="small-note">尚未配置平台凭证。</p>'}</div><hr class="divider">` : ""}
    <form id="connection-form"><h3>${current ? "更新 " + esc(current.name) : "新增平台"}</h3><input type="hidden" name="id" value="${esc(current?.id || "")}"><div class="field-row"><div class="field"><label for="connection-name">名称</label><input class="input" id="connection-name" name="name" value="${esc(current?.name || "")}" placeholder="例如：工作 GitLab"></div><div class="field"><label for="connection-platform">平台</label><select class="input" id="connection-platform" name="platform" ${current ? "disabled" : ""}><option value="gitlab" ${current?.platform !== "github" ? "selected" : ""}>GitLab</option><option value="github" ${current?.platform === "github" ? "selected" : ""}>GitHub</option></select></div></div><div class="field"><label for="connection-url">平台地址（只填域名）</label><input class="input" type="url" id="connection-url" name="base_url" required value="${esc(current?.base_url || "")}" placeholder="https://gitlab.com 或自建 GitLab 地址" ${current ? "disabled" : ""}></div>${gitCredentialFields()}<p class="small-note">GitLab 组发现：read_api + read_repository；仅导入单仓库：read_repository。GitHub 组织使用 Metadata 读取 + 目标仓库 Contents 读取权限。${current ? "Token 留空保留原值；更新后，引用此凭证的仓库下次同步时使用新 Token。" : "Token 仅在此输入，不用发到聊天中。"}</p><div class="field-error" role="alert"></div><div class="dialog-actions"><button class="btn ghost" type="button" data-action="${current ? "connections" : "close-dialog"}">${current ? "返回" : "关闭"}</button><button class="btn primary" type="submit">保存平台凭证</button></div></form></div>`);
}
function groupResult(result) {
  dialogFrame("批量导入已加入队列", `发现 ${result.discovered} 个仓库，新增 ${result.created} 个，已有 ${result.existing} 个，跳过 ${result.skipped.length} 个。`, `<div class="dialog-body"><p class="small-note">索引在后台逐个建立。已存在的仓库保留当前设置；每个仓库的同步结果可独立查看。</p><div class="link-list">${result.repositories.slice(0,40).map(r=>`<a data-action="close-dialog" href="${link(r.id)}">${esc(r.name)}<small>${r.created ? "等待索引" : "已存在"}</small></a>`).join("")}</div>${result.skipped.slice(0,20).map(r=>`<p class="small-note">${esc(r.name)}：${esc(r.reason)}</p>`).join("")}<div class="dialog-actions"><button class="btn primary" data-action="close-dialog">查看知识空间中的全部仓库</button></div></div>`);
}

function showLogin() {
  if (!state.registrationEnabled) state.register = false;
  app.innerHTML = `<div class="login-screen"><section class="login-visual"><div class="brand"><span class="brand-mark">R</span>RepoKnow</div><h1>代码有脉络。<br>知识<span>有出处。</span></h1><p>连接你的 Git 仓库，把散落的代码和文档<br>汇成一处可以探索、检索与核对的知识空间。</p><div class="login-features"><div>${icon("git")}GitHub · GitLab · 自建实例</div><div>${icon("graph")}源码、文档、符号与调用线索</div><div>${icon("shield")}私有仓库与来源版本追溯</div></div></section><section class="login-form-side"><form id="account-form" class="login-box"><div class="eyebrow">WELCOME TO YOUR KNOWLEDGE SPACE</div><h2>${state.register ? "创建你的账户" : "欢迎回来"}</h2><p class="intro">${state.register ? "仓库默认仅自己可见。" : "登录并探索你的仓库知识库。"}</p><div class="field"><label for="username">账号</label><input class="input" id="username" name="username" required minlength="2" maxlength="64" autocomplete="username" placeholder="输入账号"></div><div class="field"><label for="password">密码</label><input class="input" id="password" name="password" type="password" required minlength="10" maxlength="200" autocomplete="${state.register ? "new-password" : "current-password"}" placeholder="至少 10 个字符"></div><div id="login-error" class="field-error" role="alert"></div><button class="btn primary" type="submit">${state.register ? "创建账户" : "登录知识空间"} ${icon("arrow")}</button><p class="login-bottom">${state.registrationEnabled ? `${state.register ? "已有账户？" : "还没有账户？"} <button type="button" data-action="register-toggle">${state.register ? "登录" : "创建账户"}</button>` : "本实例由管理员开通账户，请联系管理员。"}</p><p class="login-note">本地管理员首次登录信息保存在项目目录的 <code>.data/admin.json</code>。<br>默认在本机运行，内容不会自动发布到外网。</p></form></section></div>`;
}
document.addEventListener("click", async (event) => {
  const anchor = event.target.closest('.markdown a[data-document-anchor]');
  if (anchor) {
    event.preventDefault();
    document.getElementById(`doc-${anchor.dataset.documentAnchor || ""}`)?.scrollIntoView({ block: "start" });
    return;
  }
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "import") await importDialog();
    else if (action === "connections") await connectionsDialog();
    else if (action === "accounts") await accountsDialog();
    else if (action === "account-access") {
      button.disabled = true;
      const disabled = button.dataset.disabled === "true";
      await api(`/api/admin/users/${encodeURIComponent(button.dataset.id)}`, {disabled}, "PUT");
      await accountsDialog();
      toast(disabled ? "成员已停用，登录会话和只读 Key 已撤销" : "成员已启用，需要重新登录");
    }
    else if (action === "edit-connection") await connectionsDialog(button.dataset.id);
    else if (action === "preview-namespace") {
      const fields = Object.fromEntries(new FormData(document.querySelector("#import-form")));
      button.disabled = true;
      try {
        const result = await api(`/api/${fields.kind === "organization" ? "organizations" : "groups"}/discover`, namespaceFields(fields));
        document.querySelector("#namespace-preview").textContent = `发现 ${result.projects.length} 个可导入仓库，跳过 ${result.skipped.length} 个；尚未创建知识库。`;
      } finally { button.disabled = false; }
    }
    else if (action === "close-dialog") dialog.close();
    else if (action === "menu")
      document.querySelector("#sidebar")?.classList.toggle("open");
    else if (action === "filter") {
      state.filter = button.dataset.value;
      home();
    } else if (action === "focus-search") {
      go("#/search");
      setTimeout(() => document.querySelector("#search-input")?.focus(), 100);
    } else if (action === "register-toggle") {
      if (!state.registrationEnabled) return;
      state.register = !state.register;
      showLogin();
    } else if (action === "logout") {
      await api("/api/auth/logout", {});
      state.user = null;
      showLogin();
    } else if (action === "sync") {
      button.disabled = true;
      await api(`/api/repositories/${state.repo.id}/sync`, {});
      toast("已加入同步队列");
      await refreshRepos();
      await route();
    } else if (action === "credentials") await importDialog(true);
    else if (action === "retry") route();
    else if (action === "trace") await tracePage(button.dataset.id);
    else if (action === "symbols-back") await symbolsPage();
    else if (action === "search-kind") {
      const params = new URLSearchParams(location.hash.split("?")[1] || "");
      params.set("kind", button.dataset.value);
      go("#/search?" + params);
    }
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (
    ![
      "account-form",
      "import-form",
      "credential-form",
      "connection-form",
      "member-form",
      "knowledge-search",
      "symbol-search",
    ].includes(form.id)
  )
    return;
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(form));
  const submit = form.querySelector("button[type=submit]");
  try {
    if (submit) submit.disabled = true;
    if (form.id === "account-form") {
      if (state.register) await api("/api/auth/register", fields);
      await api("/api/auth/login", fields);
      state.user = await api("/api/auth/me");
      await refreshRepos();
      await route();
    } else if (form.id === "import-form") {
      if (["group", "organization"].includes(fields.kind)) {
        const result = await api(`/api/${fields.kind === "organization" ? "organizations" : "groups"}/import`, {...namespaceFields(fields), visibility:fields.visibility});
        await refreshRepos();
        go("#/");
        groupResult(result);
      } else {
        const result = await api("/api/repositories", fields);
        dialog.close();
        await refreshRepos();
        go(link(result.id));
        toast("仓库已添加，开始同步和建立索引");
      }
    } else if (form.id === "member-form") {
      await api("/api/admin/users", fields);
      await accountsDialog();
      toast("成员账户已创建");
    } else if (form.id === "connection-form") {
      const id = fields.id;
      delete fields.id;
      await api(id ? `/api/connections/${encodeURIComponent(id)}` : "/api/connections", fields, id ? "PUT" : "POST");
      await connectionsDialog();
      toast("平台凭证已保存；同域名导入会自动复用");
    } else if (form.id === "credential-form") {
      await api(
        `/api/repositories/${state.repo.id}/credentials`,
        fields,
        "PUT",
      );
      dialog.close();
      toast("凭证已保存，可重新同步");
    } else if (form.id === "knowledge-search") {
      go("#/search?" + new URLSearchParams(fields));
    } else {
      go(link(state.repo.id, "symbols") + "&q=" + encodeURIComponent(fields.q));
    }
  } catch (error) {
    const target = form.querySelector(".field-error");
    if (target) target.textContent = error.message;
    else toast(error.message, true);
  } finally {
    if (submit) submit.disabled = false;
  }
});
document.addEventListener("change", (event) => {
  if (event.target.id === "source-kind") {
    const kind = event.target.value;
    document.querySelector("#repository-options").hidden = kind !== "repository";
    document.querySelector("#namespace-options").hidden = kind === "repository";
    document.querySelector("#subgroup-option").hidden = kind !== "group";
    document.querySelector("#namespace-preview").textContent = "";
    document.querySelector("#namespace-help").textContent = kind === "organization" ? "GitHub Token：Metadata 读取 + 目标仓库 Contents 读取。Enterprise 使用已配置的 GitHub 平台凭证。" : "GitLab Token：read_api + read_repository。";
    document.querySelector("#repo-url").placeholder = kind === "organization" ? "https://github.com/example-org" : kind === "group" ? "https://gitlab.example.com/engineering" : "https://gitlab.example.com/engineering/project.git";
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "file-filter") {
    state.fileFilter = event.target.value;
    filterFileTree();
  }
});
window.addEventListener("hashchange", route);
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "k") {
    event.preventDefault();
    go("#/search");
    setTimeout(() => document.querySelector("#search-input")?.focus(), 100);
  }
});
async function refreshRepos() {
  state.repos = await api("/api/repositories");
}
let polling = false;
setInterval(async () => {
  if (
    !state.user ||
    polling ||
    !state.repos.some((r) => ["running", "queued"].includes(r.sync_state))
  )
    return;
  polling = true;
  try {
    const before = JSON.stringify(
      state.repos.map((r) => [r.id, r.sync_state, r.commit_id]),
    );
    await refreshRepos();
    const after = JSON.stringify(
      state.repos.map((r) => [r.id, r.sync_state, r.commit_id]),
    );
    if (before !== after) {
      if (state.view === "home") home();
      else if (state.view === "repo" && !state.file) await route();
      else {
        const current = state.repos.find((r) => r.id === state.repo?.id);
        if (current?.sync_state === "ready")
          toast("仓库索引已更新，重新打开页面可查看新版本");
      }
    }
  } catch (error) {
    if (state.user) console.warn("同步状态暂时不可用");
  } finally {
    polling = false;
  }
}, 2500);
(async () => {
  try {
    const config = await api("/api/auth/config");
    state.registrationEnabled = config.registration_enabled === true;
    state.user = await api("/api/auth/me");
    await refreshRepos();
    await route();
  } catch (error) {
    showLogin();
  }
})();
