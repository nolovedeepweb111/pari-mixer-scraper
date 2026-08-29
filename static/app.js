// 401 means a session that used to be valid no longer is - bounce to the
// login page instead of showing a broken site. A 403 is different: it is a
// visitor without a key reaching for the running cup, which is a normal state
// with its own panel (see renderLockPanel), so it must NOT redirect.
(function () {
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const resp = await origFetch.apply(this, args);
    if (resp.status === 401) {
      location.href = "/login";
    }
    return resp;
  };
})();

const teamsEl = document.getElementById("teams");
const detailEl = document.getElementById("team-detail");
const collectStatusEl = document.getElementById("collect-status");

let activeTeamId = null;
let activeTab = "composition";

// --- Routing ---------------------------------------------------------------
// Every view has an address (/mixercup2/team/123), so pages can be linked to
// and the browser's back button works. The server answers all of them with the
// same shell (see app.spa_route); this router reads location.pathname and
// decides what to render. Which cup's incarnation of a team is on screen comes
// from the URL too - a Steam team id is reused cup after cup.
let route = { view: "cup" };
let cups = { activeId: null, activeSlug: null, list: [], bySlug: new Map(), byId: new Map() };
let sidebarTournamentId; // undefined until the sidebar has been filled once
// Как отсортирован список команд и какая неделя выбрана. Неделя есть только у
// кубков с решафлами (супермиксер): там команда живёт одну неделю, и список
// показывает ту, что выбрана, по умолчанию последнюю.
let teamSort = "mmr";
let selectedWeek = null;
let lastTeamList = null; // последний полученный список - для перерисовки без запроса
// Whether the running cup is paid-only for this visitor, and what to offer
// them if so. Filled from /api/auth/status before the first render.
let access = { enabled: false, authenticated: true, publicArchive: false, offer: null };

// «1 матч / 2 матча / 5 матчей» - иначе заголовок группы читается как отчёт.
function plural(n, one, few, many) {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return many;
  const mod10 = n % 10;
  if (mod10 === 1) return one;
  if (mod10 >= 2 && mod10 <= 4) return few;
  return many;
}

function cupIsLocked(tournamentId) {
  const cup = cups.byId.get(tournamentId);
  return !!(cup && cup.locked);
}

// Shown in place of the content when a visitor without a key opens the
// running cup. Deliberately not a redirect: a link shared into a chat should
// land where it points and explain itself.
function renderLockPanel(container) {
  const o = access.offer || {};
  container.innerHTML = `
    <div class="lock-panel">
      <h2>Текущий турнир — по ключу</h2>
      <p>Статистика идущего турнира: составы, пулы героев, драфты, замены и аналитика
         соперников. Прошедшие турниры открыты полностью — выберите их в шапке.</p>
      <p class="lock-offer">Для получения доступа отправьте <b>${escapeHtml(o.price || "—")}</b>
         пользователю <b>${escapeHtml(o.recipient || "—")}</b> и напишите в дискорде
         <b>${escapeHtml(o.discord || "—")}</b> или в телеграмме <b>${escapeHtml(o.telegram || "—")}</b>.</p>
      <button id="lock-login">У меня есть ключ</button>
    </div>
  `;
  const btn = container.querySelector("#lock-login");
  if (btn) btn.addEventListener("click", () => { location.href = "/login"; });
}

async function loadTournaments() {
  const res = await fetch("/api/tournaments");
  const data = await res.json();
  cups = {
    activeId: data.active_id,
    activeSlug: data.active_slug,
    list: data.tournaments,
    bySlug: new Map(data.tournaments.map((t) => [t.slug, t])),
    byId: new Map(data.tournaments.map((t) => [t.id, t])),
  };
  renderCupSwitcher();
}

function renderCupSwitcher() {
  const sel = document.getElementById("cup-switcher");
  if (!sel) return;
  // A cup with no games of its own is only worth listing while it's the live
  // one (a freshly opened cup has rosters before it has matches).
  const shown = cups.list.filter((t) => t.has_matches || t.is_active);
  if (shown.length < 2) return;
  sel.innerHTML = shown
    .map((t) => {
      const mark = t.locked ? " 🔒" : t.is_active ? " · сейчас" : "";
      // The label is the tournament's name as mixer-cup reports it.
      return `<option value="${escapeHtml(t.slug)}">${escapeHtml(t.label)}${mark}</option>`;
    })
    .join("");
  sel.onchange = () => navigate(`/${sel.value}`);
  sel.style.display = "";
}

function syncCupSwitcher() {
  const sel = document.getElementById("cup-switcher");
  const slug = slugFor(currentCupId());
  if (!sel || !slug) return;
  // Looked up by walking the options rather than with an attribute selector
  // built from the slug, which would be a query-injection hazard.
  for (const option of sel.options) {
    if (option.value === slug) {
      sel.value = slug;
      return;
    }
  }
}

function slugFor(tournamentId) {
  const cup = cups.byId.get(tournamentId);
  return (cup && cup.slug) || cups.activeSlug;
}

function cupPath(tournamentId, rest) {
  const slug = slugFor(tournamentId);
  // No cup resolved yet (mixer-cup unreachable on a cold start): stay on the
  // root, which always means "whatever cup is current".
  if (!slug) return rest ? `/${rest}` : "/";
  return rest ? `/${slug}/${rest}` : `/${slug}`;
}

function parsePath(pathname) {
  const parts = pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
  if (parts.length === 0) {
    return { view: "cup", slug: cups.activeSlug, tournamentId: cups.activeId };
  }
  // Careers across every cup, as opposed to /<cup>/players for one of them.
  if (parts[0] === "players") return { view: "allPlayers" };
  if (parts[0] === "player") return { view: "player", accountId: Number(parts[1]) };
  if (parts[0] === "match") return { view: "match", matchId: Number(parts[1]) };

  const slug = parts[0];
  const cup = cups.bySlug.get(slug);
  const tournamentId = cup ? cup.id : null;
  if (parts[1] === "players") return { view: "players", slug, tournamentId };
  if (parts[1] === "subs") return { view: "subs", slug, tournamentId };
  if (parts[1] === "team" && parts[2]) {
    // "subs" reads better in an address than the internal tab name.
    const tab = parts[3] === "subs" ? "substitutions" : parts[3] || "composition";
    return { view: "team", slug, tournamentId, teamId: Number(parts[2]), tab };
  }
  return { view: "cup", slug, tournamentId };
}

// The cup the visitor is currently browsing - the header's buttons stay
// inside it, so "Игроки" on /mixercup1 means that cup's players.
function currentCupId() {
  return route.tournamentId != null ? route.tournamentId : cups.activeId;
}

function navigate(path, replace) {
  if (path === location.pathname) return renderRoute();
  if (replace) history.replaceState({}, "", path);
  else history.pushState({}, "", path);
  return renderRoute();
}

// The scope every list endpoint takes; omitted for the live cup so the root
// page keeps working even before the cup list has loaded.
function scopeQuery(tournamentId) {
  return tournamentId != null && tournamentId !== cups.activeId
    ? `?tournament=${tournamentId}`
    : "";
}

async function renderRoute() {
  route = parsePath(location.pathname);
  // A slug we have no tournament for: say so instead of quietly showing the
  // current cup under an address that promises another one.
  if (route.slug && !cups.bySlug.has(route.slug)) {
    detailEl.innerHTML = '<p class="hint">Такого турнира нет. Выберите турнир в шапке.</p>';
    return;
  }
  let cupId = currentCupId();
  // The career page belongs to no single cup, so it borrows the sidebar of one
  // the visitor can actually open rather than showing a locked one.
  if (route.view === "allPlayers" && cupIsLocked(cupId)) {
    const open = cups.list.find((t) => !t.locked && t.has_matches);
    if (open) cupId = open.id;
  }
  syncCupSwitcher();
  if (route.view !== "team") activeTeamId = null;

  // The team list belongs to its cup, so a locked cup means a locked sidebar.
  // Player and match pages still get rendered: the player page keeps its
  // archive sections (the server strips the running cup's), and the match page
  // shows the offer only if that particular match is locked.
  if (cupIsLocked(cupId)) {
    teamsEl.innerHTML = '<p class="hint">Список команд — по ключу.</p>';
    sidebarTournamentId = undefined;
    if (!["player", "match", "allPlayers"].includes(route.view)) {
      return renderLockPanel(detailEl);
    }
  } else if (sidebarTournamentId !== cupId) {
    await loadTeams(cupId);
  } else {
    highlightSidebar();
  }

  switch (route.view) {
    case "team":
      return loadTeamDetail(route.teamId, route.tab, route.tournamentId);
    case "players":
      return loadPlayersLeaderboard(cupId);
    case "allPlayers":
      return loadPlayersLeaderboard("all");
    case "subs":
      return loadAllSubstitutions(cupId);
    case "player":
      return loadPlayerPage(route.accountId);
    case "match":
      return loadMatchPage(route.matchId);
    default:
      detailEl.innerHTML = '<p class="hint">Выберите команду слева</p>';
      highlightSidebar();
  }
}

window.addEventListener("popstate", renderRoute);

const homeLink = document.getElementById("home-link");
if (homeLink) {
  homeLink.style.cursor = "pointer";
  homeLink.addEventListener("click", () => navigate(cupPath(currentCupId())));
}

function formatMmr(value) {
  return value == null ? "?" : Math.round(value).toLocaleString("ru-RU");
}

// Every value that reaches innerHTML goes through this. Most of them are typed
// in by somebody: notes are written here, and nicknames, team names and
// tournament names all come from what players registered on mixer-cup. Any of
// those can carry "<img src=x onerror=...>", and on a site where people share a
// key that runs in the next visitor's browser.
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function heroIconUrl(slug) {
  // encodeURIComponent, not plain interpolation: the slug lands inside a src=""
  // attribute, so anything odd in it would otherwise break out of the URL.
  return `https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/${encodeURIComponent(slug || "")}.png`;
}

const STEAM_ID64_BASE = 76561197960265728n;

function steamProfileUrl(accountId) {
  return `https://steamcommunity.com/profiles/${BigInt(accountId) + STEAM_ID64_BASE}`;
}

function dotabuffProfileUrl(accountId) {
  return `https://www.dotabuff.com/players/${accountId}`;
}

const STEAM_ICON_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 2C6.5 2 2 6.5 2 12c0 4.6 3.1 8.4 7.3 9.6l1.3-3.2c-.4-.5-.6-1.1-.6-1.8 0-1.7 1.3-3 3-3 .3 0 .6 0 .8.1l2.1-3c-.1-.3-.1-.6-.1-1 0-2.2 1.8-4 4-4s4 1.8 4 4-1.8 4-4 4c-.1 0-.2 0-.3 0l-2.9 2.1c0 .1 0 .3 0 .4 0 1.7-1.3 3-3 3-1.5 0-2.7-1-3-2.3l-3-1.2C8.6 20.9 10.2 22 12 22c5.5 0 10-4.5 10-10S17.5 2 12 2zm4.5 6.8a2 2 0 100 4 2 2 0 000-4z"/></svg>`;
const DOTABUFF_ICON_SVG = `<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M4 20V10h4v10H4zm6 0V4h4v16h-4zm6 0v-7h4v7h-4z"/></svg>`;

function profileLinks(accountId) {
  return `
    <a class="profile-link" href="${steamProfileUrl(accountId)}" target="_blank" rel="noopener noreferrer" title="Steam профиль">${STEAM_ICON_SVG}</a>
    <a class="profile-link" href="${dotabuffProfileUrl(accountId)}" target="_blank" rel="noopener noreferrer" title="Dotabuff профиль">${DOTABUFF_ICON_SVG}</a>
  `;
}

function renderDraftTeamRow(teamName, entries) {
  const cells = entries
    .map((e) => `
      <div class="draft-cell ${e.is_pick ? "cell-pick" : "cell-ban"}">
        <span class="cell-order">${e.order + 1}</span>
        <img src="${escapeHtml(heroIconUrl(e.hero_icon))}" alt="${escapeHtml(e.hero)}" title="${escapeHtml(e.hero)}">
        <span class="cell-label">${e.is_pick ? "PICK" : "BAN"}</span>
      </div>
    `)
    .join("");
  // Escaped here rather than at the call sites, so passing a team name in is
  // safe by default.
  return `
    <div class="draft-team-row">
      <span class="draft-team-label">${escapeHtml(teamName)}</span>
      <div class="draft-cells">${cells || '<span class="hint">нет данных</span>'}</div>
    </div>
  `;
}

function highlightSidebar() {
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.toggle("active", Number(btn.dataset.teamId) === activeTeamId);
  }
}

function sortTeams(teams) {
  const byMmr = (a, b) => (b.total_mmr || 0) - (a.total_mmr || 0);
  if (teamSort === "wins") {
    // При равном числе побед выше тот, кто меньше проиграл, а уже потом - по
    // рейтингу: иначе команда, сыгравшая один матч, вставала бы рядом с той,
    // что выиграла столько же из десяти.
    return teams.slice().sort((a, b) =>
      (b.wins || 0) - (a.wins || 0) || (a.losses || 0) - (b.losses || 0) || byMmr(a, b));
  }
  return teams.slice().sort(byMmr);
}

function clearSortSlot() {
  const slot = document.getElementById("team-sort-slot");
  if (slot) slot.innerHTML = "";
}

// Переключатели над списком: неделя (если кубок с решафлами) и сортировка.
// Неделя осталась выпадающим списком - их со временем станет много, кнопками
// такое не выложить. Сортировка - две кнопки: вариантов всего два, и нажатие
// понятнее выбора из списка.
function renderTeamControls(tournamentId, weeks) {
  const bar = document.createElement("div");
  bar.className = "team-controls";
  const weekPart = weeks.length > 1
    ? `<label>Неделя
         <select id="week-select">${weeks.map((w) =>
           `<option value="${w}"${w === selectedWeek ? " selected" : ""}>${w}</option>`).join("")}
         </select>
       </label>`
    : "";
  bar.innerHTML = weekPart;
  // Сортировка живёт в строке заголовка, а не здесь: см. .sidebar-head.
  const slot = document.getElementById("team-sort-slot");
  if (slot) {
    slot.innerHTML = `
      <div class="sort-toggle" role="group" aria-label="Сортировка команд">
        <button class="sort-btn${teamSort === "mmr" ? " active" : ""}" data-sort="mmr">MMR</button>
        <button class="sort-btn${teamSort === "wins" ? " active" : ""}" data-sort="wins">Победы</button>
      </div>`;
    for (const btn of slot.querySelectorAll(".sort-btn")) {
      btn.addEventListener("click", () => {
        if (btn.dataset.sort === teamSort) return;
        teamSort = btn.dataset.sort;
        renderTeamList();
      });
    }
  }
  const weekSel = bar.querySelector("#week-select");
  if (weekSel) weekSel.addEventListener("change", () => {
    selectedWeek = Number(weekSel.value);
    loadTeams(tournamentId);
  });
  return bar;
}

async function loadTeams(tournamentId) {
  const cup = cups.byId.get(tournamentId);
  const weeks = (cup && cup.weeks) || [];
  // Неделя сбрасывается при переходе в другой кубок: её номера свои у каждого.
  if (selectedWeek != null && !weeks.includes(selectedWeek)) selectedWeek = null;
  if (selectedWeek == null && weeks.length) selectedWeek = weeks[weeks.length - 1];
  const weekQuery = selectedWeek != null
    ? (scopeQuery(tournamentId) ? `&week=${selectedWeek}` : `?week=${selectedWeek}`)
    : "";
  const res = await fetch(`/api/teams${scopeQuery(tournamentId)}${weekQuery}`);
  const teams = await res.json();
  sidebarTournamentId = tournamentId;

  teamsEl.innerHTML = "";
  if (!Array.isArray(teams)) {
    // Locked cup (403) - the panel in the main area explains it.
    teamsEl.innerHTML = '<p class="hint">Список команд — по ключу.</p>';
    clearSortSlot();
    sidebarTournamentId = undefined;
    lastTeamList = null;
    return;
  }
  if (teams.length === 0) {
    teamsEl.innerHTML = '<p class="hint">Нет данных. Обновляется автоматически, зайдите чуть позже.</p>';
    clearSortSlot();
    lastTeamList = null;
    return;
  }

  lastTeamList = { tournamentId, teams, weeks };
  renderTeamList();
}

// Отрисовка уже полученного списка. Отдельно от загрузки, чтобы смена
// сортировки не ходила лишний раз на сервер: данные те же, меняется порядок.
function renderTeamList() {
  if (!lastTeamList) return;
  const { tournamentId, teams, weeks } = lastTeamList;
  teamsEl.innerHTML = "";

  if (teams.length > 1) {
    // renderTeamControls заодно наполняет строку заголовка сортировкой, поэтому
    // вызывается всегда; сам он добавляет в колонку только выбор недели.
    const bar = renderTeamControls(tournamentId, weeks);
    if (bar.innerHTML.trim()) teamsEl.appendChild(bar);
  }

  for (const team of sortTeams(teams)) {
    const btn = document.createElement("button");
    btn.className = "team-btn";
    btn.dataset.teamId = team.team_id;
    // Название отдельной строкой, всё остальное - строкой помельче под ним:
    // одной строкой это переносилось как попало, а со счётом стало бы совсем
    // нечитаемо.
    const meta = [`(${team.player_count})`];
    if ((team.wins || 0) + (team.losses || 0) > 0) meta.push(`${team.wins}–${team.losses}`);
    // A finished cup has no meaningful team total (see api_team_detail), so
    // the MMR half of the label is dropped rather than shown as "?".
    if (team.total_mmr != null) meta.push(`${formatMmr(team.total_mmr)} MMR`);
    btn.innerHTML = `<span class="team-name">${escapeHtml(team.name)}</span>` +
      `<span class="team-meta">${escapeHtml(meta.join(" · "))}</span>`;
    btn.onclick = () => navigate(cupPath(tournamentId, `team/${team.team_id}`));
    teamsEl.appendChild(btn);
  }
  highlightSidebar();
}

const ROLE_LABELS = {
  CARRY: "Керри",
  MIDLANER: "Мид",
  OFFLANER: "Оффлейн",
  SOFT_SUPPORT: "Саппорт",
  HARD_SUPPORT: "Фулл-саппорт",
};

function formatRoles(roles) {
  if (!roles) return "";
  return roles.split(",").map((r) => ROLE_LABELS[r] || r).join(" / ");
}

function renderComposition(team) {
  const container = document.createElement("div");

  const grid = document.createElement("div");
  grid.className = "players-grid";
  for (const player of team.players) {
    const card = document.createElement("div");
    card.className = "player-card";
    const heroItems = player.heroes
      .map((h) => {
        const wr = h.win_rate == null ? "" : `<span class="winrate ${h.win_rate >= 50 ? "wr-good" : "wr-bad"}">${h.win_rate}%</span>`;
        return `<li><span>${escapeHtml(h.name)}</span><span>${wr}<span class="count">×${h.games}</span></span></li>`;
      })
      .join("");
    const rolesLine = player.roles ? `<p class="roles">${escapeHtml(formatRoles(player.roles))}</p>` : "";
    // Игрок, которого mixer-cup не связал со Steam: аккаунта нет, поэтому ни
    // ссылки на его страницу, ни статистики быть не может. Показываем то, что
    // известно, и говорим почему пусто, вместо того чтобы прятать человека и
    // оставлять в составе дырку.
    const nameCell = player.unlinked
      ? `<span class="player-unlinked">${escapeHtml(player.name)}</span>`
      : `<button class="player-link" data-account-id="${player.account_id}">${escapeHtml(player.name)}</button><span class="profile-links">${profileLinks(player.account_id)}</span>`;
    const emptyHint = player.unlinked
      ? "mixer-cup не указал Steam — статистики нет"
      : (team.hero_pools_locked ? "пул героев — по ключу" : "ещё не играл(а) за команду");
    // За служебным слотом «Замена капитана» никого нет, пока замену не выбрали:
    // объяснять нечего, показываем только подпись и рейтинг.
    const heroBlock = player.placeholder && !heroItems
      ? ""
      : `<ul>${heroItems || `<li><span class="hint">${emptyHint}</span></li>`}</ul>`;
    card.innerHTML = `
      <h3>${nameCell}</h3>
      <p class="mmr">${formatMmr(player.mmr)} MMR</p>
      ${rolesLine}
      ${heroBlock}
    `;
    const link = card.querySelector(".player-link");
    if (link) link.addEventListener("click", () => navigate(`/player/${player.account_id}`));
    grid.appendChild(card);
  }

  // Null for a past cup's page (a squad that changed all cup has no single
  // total) and for teams whose ratings we never learned - in both cases the
  // line says nothing, so it's left out entirely.
  if (team.total_mmr != null) {
    const mmrLine = document.createElement("p");
    mmrLine.className = "total-mmr";
    mmrLine.textContent = `Суммарный MMR: ${formatMmr(team.total_mmr)}`;
    container.appendChild(mmrLine);
  }

  if (team.next_opponent) {
    const opp = team.next_opponent;
    const when = opp.planned_time
      ? new Date(opp.planned_time).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" })
      : "время пока не назначено";
    const opponentLabel = opp.opponent_team_id != null
      ? `<button class="opponent-link" data-team-id="${opp.opponent_team_id}">${escapeHtml(opp.opponent_name)}</button>`
      : `<strong>${escapeHtml(opp.opponent_name)}</strong>`;
    const nextLine = document.createElement("p");
    nextLine.className = "next-opponent";
    nextLine.innerHTML = `Следующий соперник: ${opponentLabel} · ${when}`;
    const link = nextLine.querySelector(".opponent-link");
    if (link) {
      // Кубок, который сейчас открыт, а не кубок по умолчанию: живых теперь
      // два (PARI и супермиксер), и переход к следующему сопернику
      // выбрасывал из супермиксера в PARI.
      link.addEventListener("click", () =>
        navigate(cupPath(currentCupId(), `team/${opp.opponent_team_id}`)));
    }
    container.appendChild(nextLine);
  }

  // Roster cards only cover confirmed players with at least one game, so a
  // team can show fewer than five - complement it with who actually played
  // their most recent match.
  const lm = team.last_match_lineup;
  if (team.players.length < 5 && lm && lm.players.length > 0) {
    const when = lm.start_time
      ? new Date(lm.start_time * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" })
      : "";
    const vs = lm.opponent_name ? ` против ${escapeHtml(lm.opponent_name)}` : "";
    const names = lm.players
      .map((p) => `<button class="player-link" data-account-id="${p.account_id}">${escapeHtml(p.name)}</button><span class="profile-links">${profileLinks(p.account_id)}</span>`)
      .join(", ");
    const lineupLine = document.createElement("p");
    lineupLine.className = "last-lineup";
    lineupLine.innerHTML = `Состав в последнем матче${vs}${when ? ` (${when})` : ""}: ${names}`;
    for (const btn of lineupLine.querySelectorAll(".player-link")) {
      btn.addEventListener("click", () => navigate(`/player/${btn.dataset.accountId}`));
    }
    container.appendChild(lineupLine);
  }

  container.appendChild(grid);

  if (team.recent_drafts && team.recent_drafts.length > 0) {
    const draftsSection = document.createElement("div");
    draftsSection.className = "drafts-section";
    draftsSection.innerHTML = "<h3>Последние драфты</h3>";
    for (const draft of team.recent_drafts) {
      const match = document.createElement("div");
      match.className = "draft-match";
      let resultBadge = '<span class="match-result result-unknown">Результат неизвестен</span>';
      if (draft.team_won === true) {
        resultBadge = '<span class="match-result result-win">Победа</span>';
      } else if (draft.team_won === false) {
        resultBadge = '<span class="match-result result-loss">Поражение</span>';
      }
      match.innerHTML =
        resultBadge +
        renderDraftTeamRow(team.name, draft.team_entries) +
        renderDraftTeamRow(draft.opponent_name, draft.opponent_entries);
      draftsSection.appendChild(match);
    }
    container.appendChild(draftsSection);
  }

  return container;
}

function heroTagList(items, cssClass) {
  if (!items || items.length === 0) return '<span class="hint">нет данных</span>';
  return items.map((i) => `<span class="tag ${cssClass}">${escapeHtml(i.hero)} ×${i.count}</span>`).join("");
}

async function renderAnalysisTab(teamId, container, scope) {
  container.innerHTML = '<p class="hint">Считаю аналитику...</p>';
  const res = await fetch(`/api/teams/${teamId}/analysis${scope || ""}`);
  if (!res.ok) {
    container.innerHTML = '<p class="hint">Не удалось получить аналитику.</p>';
    return;
  }
  const a = await res.json();

  const signatureHtml = a.signature_heroes.length
    ? a.signature_heroes
        .map((h) => `<span class="tag tag-pick">${escapeHtml(h.hero)} — ${h.win_rate}% (${h.wins}/${h.games})</span>`)
        .join("")
    : '<span class="hint">нет данных</span>';

  // Считается по всем кубкам сразу: у только что начавшегося турнира игр ещё
  // нет, а знать, на ком силён соперник, нужно с первого дня.
  const playerHeroesHtml = a.player_heroes_locked
    ? '<span class="hint">по ключу — здесь видно, на каких героях силён каждый игрок</span>'
    : a.player_heroes && a.player_heroes.length
      ? a.player_heroes
          .map((p) => {
            const tags = p.heroes
              .map((h) => `<span class="tag tag-pick"><img class="hero-icon" src="${escapeHtml(heroIconUrl(h.hero_icon))}" alt="" loading="lazy" onerror="this.remove()">${escapeHtml(h.hero)} — ${h.win_rate}% (${h.wins}/${h.games})</span>`)
              .join("");
            return `
              <div class="player-heroes-row">
                <button class="player-link" data-account-id="${p.account_id}">${escapeHtml(p.name)}</button>
                <div class="tag-list">${tags}</div>
              </div>`;
          })
          .join("")
      : `<span class="hint">ни у кого нет героя с ${a.player_heroes_min_games}+ играми и винрейтом выше ${a.player_heroes_min_win_rate}%</span>`;

  // Кого банят игрокам этой команды. Оценка по банам соперника (см.
  // build_ban_context на сервере), поэтому блок идёт с предупреждением: тут
  // сказано не «так есть», а «так похоже».
  const playerBansHtml = a.player_heroes_locked
    ? '<span class="hint">по ключу — здесь видно, кого сопернику приходится банить</span>'
    : (a.player_bans && a.player_bans.length)
      ? a.player_bans
          .map((p) => {
            const tags = p.heroes
              .map((h) => {
                const own = h.played ? `сыграл ${h.played}` : "сыграть не дали";
                return `<span class="tag tag-ban"><img class="hero-icon" src="${escapeHtml(h.hero_icon)}" alt="" loading="lazy" onerror="this.remove()">${escapeHtml(h.hero)} — ${h.bans} из ${h.games}, обычно ${h.base_rate}% · ${own}</span>`;
              })
              .join("");
            return `
              <div class="player-heroes-row">
                <button class="player-link" data-account-id="${p.account_id}">${escapeHtml(p.name)}</button>
                <div class="tag-list">${tags}</div>
              </div>`;
          })
          .join("")
      : `<span class="hint">никому ничего не банят заметно чаще обычного — либо матчей пока меньше ${a.player_bans_min_games}</span>`;

  container.innerHTML = `
    <!-- Assembled on the server, but out of the team's name - so it carries
         whatever the captain called their team. -->
    <p class="coach-text">${escapeHtml(a.text)}</p>
    <div class="analysis-grid">
      <div class="analysis-block">
        <h4>Топ пиков</h4>
        <div class="tag-list">${heroTagList(a.top_picks, "tag-neutral")}</div>
      </div>
      <div class="analysis-block">
        <h4>Коронные герои (win rate)</h4>
        <div class="tag-list">${signatureHtml}</div>
      </div>
      <div class="analysis-block">
        <h4>Первый пик</h4>
        <div class="tag-list">${heroTagList(a.first_picks, "tag-neutral")}</div>
      </div>
      <div class="analysis-block">
        <h4>Что банят соперники</h4>
        <div class="tag-list">${heroTagList(a.enemy_bans, "tag-ban")}</div>
      </div>
      <div class="analysis-block">
        <h4>Что банит команда сама</h4>
        <div class="tag-list">${heroTagList(a.own_bans, "tag-ban")}</div>
      </div>
      <div class="analysis-block analysis-block-wide">
        <h4>Лучшие герои игроков</h4>
        <p class="hint">От ${a.player_heroes_min_games} игр и винрейта выше ${a.player_heroes_min_win_rate}%, по всем турнирам.</p>
        ${playerHeroesHtml}
      </div>
      <div class="analysis-block analysis-block-wide">
        <h4>⚠️ Кого им банят</h4>
        <p class="hint warn-note">Это оценка, а не факт: считается по тому, каких
           героев соперник забирает баном в матчах игрока чаще обычного. Бан мог
           быть нацелен и на соседа по составу, и просто в мету. Полагаться на
           этот блок стоит с осторожностью — но он показывает то, чего не видно
           в пуле героев: героя, которого игроку не дают взять.
           Игроки меньше чем с ${a.player_bans_min_games} матчами сюда не
           попадают — на короткой выборке это гадание.</p>
        ${playerBansHtml}
      </div>
    </div>
  `;

  for (const btn of container.querySelectorAll(".player-heroes-row .player-link")) {
    btn.addEventListener("click", () => navigate(`/player/${btn.dataset.accountId}`));
  }
}

async function renderSubstitutionsTab(teamId, container) {
  container.innerHTML = '<p class="hint">Загружаю историю замен...</p>';
  const res = await fetch(`/api/teams/${teamId}/substitutions`);
  if (!res.ok) {
    container.innerHTML = '<p class="hint">Не удалось получить историю замен.</p>';
    return;
  }
  const data = await res.json();

  if (!data.substitutions.length) {
    container.innerHTML = '<p class="hint">Замен в составе не было.</p>';
    return;
  }

  const rows = data.substitutions
    .map((s) => {
      const when = new Date(s.at).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
      let text;
      if (s.out && s.in) {
        text = `<strong>${escapeHtml(s.out)}</strong> → <strong>${escapeHtml(s.in)}</strong>`;
      } else if (s.out) {
        text = `<strong>${escapeHtml(s.out)}</strong> вышел из состава`;
      } else {
        text = `<strong>${escapeHtml(s.in)}</strong> добавлен в состав`;
      }
      if (s.rating_diff != null) {
        const cls = s.rating_diff >= 0 ? "rating-diff-up" : "rating-diff-down";
        const sign = s.rating_diff > 0 ? "+" : "";
        text += ` <span class="rating-diff ${cls}">${sign}${s.rating_diff} pts</span>`;
      }
      let teamLine = "";
      if (s.team_rating_before != null && s.team_rating_after != null) {
        const teamDiff = s.team_rating_after - s.team_rating_before;
        const cls = teamDiff >= 0 ? "rating-diff-up" : "rating-diff-down";
        teamLine = `
          <div class="sub-team-rating">
            Командный рейтинг: ${formatMmr(s.team_rating_before)} → ${formatMmr(s.team_rating_after)}
            <span class="rating-diff ${cls}">${teamDiff >= 0 ? "+" : ""}${Math.round(teamDiff)}</span>
          </div>
        `;
      }
      return `<li class="sub-item"><span class="sub-date">${when}</span>${text}${teamLine}</li>`;
    })
    .join("");

  container.innerHTML = `<ul class="sub-list">${rows}</ul>`;
}

async function loadTeamDetail(teamId, tab, tournamentId) {
  activeTeamId = teamId;
  activeTab = tab || "composition";
  highlightSidebar();

  // Opens the incarnation of this team that played in the requested cup;
  // without it a link out of a past cup's match lands on today's squad.
  const scope = scopeQuery(tournamentId);
  const res = await fetch(`/api/teams/${teamId}${scope}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Команда не найдена.</p>';
    return;
  }
  const team = await res.json();

  // Past cups keep no substitution history (only the active cup's is stored),
  // so that tab would always be empty there.
  const historyBadge = team.is_historical && team.tournament_label
    ? ` <span class="tournament-badge">${escapeHtml(team.tournament_label)}</span>`
    : "";
  const subsTab = team.is_historical
    ? ""
    : '<button class="tab-btn" data-tab="substitutions">Замены</button>';
  detailEl.innerHTML = `
    <h2>${escapeHtml(team.name)}${historyBadge}</h2>
    <div class="tabs">
      <button class="tab-btn" data-tab="composition">Состав</button>
      <button class="tab-btn" data-tab="analysis">Аналитика</button>
      ${subsTab}
    </div>
    <div id="tab-content"></div>
  `;
  if (team.is_historical && activeTab === "substitutions") {
    activeTab = "composition";
  }

  const tabContent = detailEl.querySelector("#tab-content");
  const tabButtons = detailEl.querySelectorAll(".tab-btn");

  function showTab(tab) {
    activeTab = tab;
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    }
    if (tab === "composition") {
      tabContent.innerHTML = "";
      tabContent.appendChild(renderComposition(team));
    } else if (tab === "analysis") {
      renderAnalysisTab(teamId, tabContent, scope);
    } else {
      renderSubstitutionsTab(teamId, tabContent);
    }
  }

  for (const btn of tabButtons) {
    const segment = { composition: "", analysis: "/analysis", substitutions: "/subs" }[btn.dataset.tab];
    const base = cupPath(tournamentId, `team/${teamId}`);
    btn.addEventListener("click", () => navigate(base + segment));
  }

  showTab(activeTab);
}

// --- Player notes ----------------------------------------------------------
// Shared between everyone holding a key: each note carries the nickname its
// author typed, so a scouting board reads as a conversation. The nickname is
// remembered locally so it doesn't have to be retyped.
const NOTE_AUTHOR_STORAGE = "pmc_note_author";

function renderNotes(container, accountId, notes) {
  const saved = localStorage.getItem(NOTE_AUTHOR_STORAGE) || "";
  const items = notes.length
    ? notes
        .map((n) => {
          const when = new Date(n.created_at).toLocaleString("ru-RU", {
            dateStyle: "medium", timeStyle: "short",
          });
          const del = n.can_delete
            ? `<button class="note-delete" data-note-id="${escapeHtml(n.note_id)}" title="Удалить">×</button>`
            : "";
          return `
            <li class="note">
              <div class="note-head">
                <b>${escapeHtml(n.author)}</b>
                <span class="note-date">${when}</span>
                ${del}
              </div>
              <div class="note-text">${escapeHtml(n.text)}</div>
            </li>`;
        })
        .join("")
    : '<li class="hint">Пока никто ничего не написал.</li>';

  container.innerHTML = `
    <h4>Заметки</h4>
    <ul class="note-list">${items}</ul>
    <form class="note-form">
      <input class="note-author" placeholder="Ваш ник" maxlength="40" value="${escapeHtml(saved)}">
      <textarea class="note-text-input" placeholder="Что стоит помнить об этом игроке" rows="3" maxlength="2000"></textarea>
      <button type="submit">Добавить</button>
      <span class="note-error"></span>
    </form>
  `;

  const form = container.querySelector(".note-form");
  const authorEl = container.querySelector(".note-author");
  const textEl = container.querySelector(".note-text-input");
  const errEl = container.querySelector(".note-error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.textContent = "";
    const author = authorEl.value.trim();
    const text = textEl.value.trim();
    if (!author || !text) {
      errEl.textContent = "Заполните ник и текст.";
      return;
    }
    localStorage.setItem(NOTE_AUTHOR_STORAGE, author);
    const res = await fetch(`/api/players/${accountId}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ author, text }),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      errEl.textContent = d.error || d.message || "Не удалось сохранить.";
      return;
    }
    renderNotes(container, accountId, (await res.json()).notes);
  });

  for (const btn of container.querySelectorAll(".note-delete")) {
    btn.addEventListener("click", async () => {
      const res = await fetch(`/api/players/${accountId}/notes/${btn.dataset.noteId}`, {
        method: "DELETE",
      });
      if (res.ok) renderNotes(container, accountId, (await res.json()).notes);
    });
  }
}

async function loadNotes(container, accountId) {
  const res = await fetch(`/api/players/${accountId}/notes`);
  if (!res.ok) {
    // Without a key notes are neither readable nor writable.
    container.innerHTML = '<h4>Заметки</h4><p class="hint">По ключу.</p>';
    return;
  }
  renderNotes(container, accountId, (await res.json()).notes);
}

async function loadPlayerPage(accountId) {
  detailEl.innerHTML = '<p class="hint">Загружаю профиль игрока...</p>';
  const res = await fetch(`/api/players/${accountId}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Игрок не найден.</p>';
    return;
  }
  const p = await res.json();

  const rolesLine = p.roles ? ` · ${escapeHtml(formatRoles(p.roles))}` : "";
  const teamLine = p.current_team_id != null
    ? `Команда: <button class="opponent-link" data-team-id="${p.current_team_id}">${escapeHtml(p.current_team_name)}</button>`
    // Don't claim they are on no team when we are simply not showing which.
    : p.current_team_locked
      ? "Команда текущего турнира — по ключу"
      : "Сейчас не в составе команды";

  const heroTagsFor = (heroes) =>
    heroes.length
      ? heroes
          .map((h) => {
            const wr = h.win_rate == null ? "" : ` — ${h.win_rate}%`;
            const cls = h.win_rate == null ? "tag-neutral" : (h.win_rate >= 50 ? "tag-pick" : "tag-ban");
            // onerror strips the icon rather than leaving a broken-image glyph:
            // it's Valve's CDN, so it can fail where the site still works.
            const icon = h.icon
              ? `<img class="hero-icon" src="${escapeHtml(h.icon)}" alt="" loading="lazy" onerror="this.remove()">`
              : "";
            return `<span class="tag tag-hero ${cls}">${icon}<span>${escapeHtml(h.name)} ×${h.games}${wr}</span></span>`;
          })
          .join("")
      : '<span class="hint">нет сыгранных матчей</span>';

  // One hero pool per tournament (the cups can run concurrently).
  const emptyPools = (note) => `
    <div class="analysis-block player-heroes-block">
      <h4>Пул героев</h4>
      <div class="tag-list"><span class="hint">${note}</span></div>
    </div>`;
  const heroPoolsHtml = p.hero_pools_locked
    ? emptyPools("по ключу — здесь видно, на кого игрок играет и с каким винрейтом")
    : (p.hero_pools && p.hero_pools.length)
      ? p.hero_pools
          .map((pool) => `
            <div class="analysis-block player-heroes-block">
              <h4>Пул героев · ${escapeHtml(pool.label)}</h4>
              <div class="tag-list">${heroTagsFor(pool.heroes)}</div>
            </div>
          `)
          .join("")
      : emptyPools("нет сыгранных матчей");

  // Кого ему банят: герои, которых соперник забирает баном заметно чаще, чем
  // их банят вообще. Смысл блока в том, что сильнейший герой может не попасть
  // в пул выше именно потому, что сыграть его не дают.
  const banRows = (p.targeted_bans || [])
    .map((b) => {
      const icon = b.hero_icon
        ? `<img class="hero-icon" src="${escapeHtml(b.hero_icon)}" alt="" loading="lazy" onerror="this.remove()">`
        : "";
      const own = b.played ? `сыграл ${b.played}` : "сыграть не дали";
      return `<span class="tag tag-hero tag-ban">${icon}<span>${escapeHtml(b.hero)} — ` +
        `${b.bans} из ${b.games} игр, обычно ${b.base_rate}% · ${own}</span></span>`;
    })
    .join("");
  const targetedBansHtml = p.hero_pools_locked
    ? ""
    : `<div class="analysis-block player-heroes-block">
         <h4>Кого ему банят</h4>
         <div class="tag-list">${banRows || '<span class="hint">ничего не банят заметно чаще обычного</span>'}</div>
         <p class="hint">Оценка по банам соперника в его матчах: насколько чаще
            героя забирают именно против него, чем банят вообще. Сокомандники
            учтены — чужие цели отсеиваются.</p>
       </div>`;

  const matchRowHtml = (m) => {
      const when = m.start_time
        ? new Date(m.start_time * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" })
        : "";
      let result = '<span class="match-result result-unknown">?</span>';
      if (m.won === true) result = '<span class="match-result result-win">Победа</span>';
      if (m.won === false) result = '<span class="match-result result-loss">Поражение</span>';
      const formerBadge = (p.current_team_id != null && m.team_id !== p.current_team_id)
        ? ' <span class="former-team-badge">прошлая команда</span>'
        : "";
      return `
        <tr class="match-row" data-match-id="${m.match_id}" title="Открыть страницу матча">
          <td class="subs-date">${when}</td>
          <td>${escapeHtml(m.hero)}</td>
          <td>${result}</td>
          <td><button class="opponent-link" data-team-id="${m.team_id}" data-tournament-id="${m.mixer_tournament_id ?? ""}">${escapeHtml(m.team_name)}</button>${formerBadge}</td>
          <td>против ${escapeHtml(m.opponent_name)}</td>
        </tr>
      `;
  };

  // История по турнирам, каждый - сворачиваемый блок. Раньше это была одна
  // простыня со строками-разделителями: у игрока с несколькими кубками она
  // разрослась так, что до пулов героев приходилось листать. Открытым остаётся
  // самый свежий турнир, остальные сворачиваются - развернуть можно любой.
  // Группируем по турниру, а не по соседству в списке: кубки идут параллельно
  // и их матчи чередуются по времени, поэтому «пока метка та же» давало один и
  // тот же кубок по нескольку раз. Порядок групп - по первому (то есть самому
  // свежему) матчу каждого кубка, матчи внутри уже отсортированы.
  const byCup = new Map();
  for (const m of p.matches) {
    const key = m.mixer_tournament_id ?? `league:${m.league_id ?? "?"}`;
    if (!byCup.has(key)) {
      byCup.set(key, { label: m.tournament_label || "Прочие матчи", matches: [] });
    }
    byCup.get(key).matches.push(m);
  }
  const groups = [...byCup.values()];
  const matchGroups = groups
    .map((g, i) => {
      const wins = g.matches.filter((m) => m.won === true).length;
      const losses = g.matches.filter((m) => m.won === false).length;
      const score = wins + losses ? ` · ${wins}–${losses}` : "";
      return `
        <details class="match-group"${i === 0 ? " open" : ""}>
          <summary>${escapeHtml(g.label)} <span class="match-group-meta">${g.matches.length} ${plural(g.matches.length, "матч", "матча", "матчей")}${score}</span></summary>
          <table class="subs-table">
            <thead><tr><th>Дата</th><th>Герой</th><th>Результат</th><th>За команду</th><th>Соперник</th></tr></thead>
            <tbody>${g.matches.map(matchRowHtml).join("")}</tbody>
          </table>
        </details>`;
    })
    .join("");

  detailEl.innerHTML = `
    <h2>${escapeHtml(p.name)}<span class="profile-links">${profileLinks(p.account_id)}</span></h2>
    <p class="player-meta">${formatMmr(p.mmr)} MMR${rolesLine}</p>
    <p class="next-opponent">${teamLine}</p>
    <div class="player-body">
      <div class="player-history">
        <h3 class="history-title">История матчей</h3>
        ${p.matches.length ? matchGroups : '<p class="hint">Матчей пока нет.</p>'}
      </div>
      <aside class="player-pools">
        ${heroPoolsHtml}
      ${targetedBansHtml}
        <div class="analysis-block player-notes-block" id="player-notes"></div>
      </aside>
    </div>
  `;

  loadNotes(detailEl.querySelector("#player-notes"), accountId);

  for (const link of detailEl.querySelectorAll(".opponent-link")) {
    // A row from a past cup carries that cup's id, so the link points at that
    // cup's address and opens the squad that actually played the match.
    const tid = link.dataset.tournamentId;
    // Без своего номера строка относится к открытому кубку - опять же не к
    // кубку по умолчанию, иначе ссылка уводит в чужой турнир.
    link.addEventListener("click", () =>
      navigate(cupPath(tid ? Number(tid) : currentCupId(), `team/${link.dataset.teamId}`)));
  }
  for (const row of detailEl.querySelectorAll(".match-row")) {
    row.addEventListener("click", (e) => {
      // The team button inside the row keeps its own action.
      if (e.target.closest(".opponent-link")) return;
      navigate(`/match/${row.dataset.matchId}`);
    });
  }
}

function lineupTable(side, winnerSide, side_key) {
  const winBadge = winnerSide == null
    ? ""
    : (winnerSide === side_key
        ? ' <span class="match-result result-win">Победа</span>'
        : ' <span class="match-result result-loss">Поражение</span>');
  // These columns exist only for OpenDota-sourced matches; a Steam-only match
  // has none, so each column shows itself only if any player carries it.
  const hasKda = side.players.some((pl) => pl.kills != null);
  const hasGpm = side.players.some((pl) => pl.gpm != null);
  const hasXpm = side.players.some((pl) => pl.xpm != null);
  const hasNw = side.players.some((pl) => pl.net_worth != null);
  const num = (v) => (v == null ? "—" : v.toLocaleString("ru-RU"));
  const rows = side.players
    .map((pl) => `
      <tr>
        <td class="lineup-hero"><img class="hero-icon" src="${escapeHtml(heroIconUrl(pl.hero_icon))}" alt="" loading="lazy" onerror="this.remove()">${escapeHtml(pl.hero)}</td>
        <td><button class="player-link" data-account-id="${pl.account_id}">${escapeHtml(pl.name)}</button></td>
        ${hasKda ? `<td class="lineup-kda">${pl.kills ?? "—"}/${pl.deaths ?? "—"}/${pl.assists ?? "—"}</td>` : ""}
        ${hasNw ? `<td class="lineup-num">${num(pl.net_worth)}</td>` : ""}
        ${hasGpm ? `<td class="lineup-num">${num(pl.gpm)}</td>` : ""}
        ${hasXpm ? `<td class="lineup-num">${num(pl.xpm)}</td>` : ""}
      </tr>
    `)
    .join("");
  const head = `<tr><th>Герой</th><th>Игрок</th>${hasKda ? "<th>K/D/A</th>" : ""}${hasNw ? '<th title="Нетворс">NW</th>' : ""}${hasGpm ? '<th title="Золото в минуту">GPM</th>' : ""}${hasXpm ? '<th title="Опыт в минуту">XPM</th>' : ""}</tr>`;
  const cols = 2 + [hasKda, hasNw, hasGpm, hasXpm].filter(Boolean).length;
  return `
    <div class="lineup-block">
      <h4>${escapeHtml(side.name)}${winBadge}</h4>
      <table class="subs-table lineup-table">
        <thead>${head}</thead>
        <tbody>${rows || `<tr><td colspan="${cols}" class="hint">состав неизвестен</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

async function loadMatchPage(matchId) {
  detailEl.innerHTML = '<p class="hint">Загружаю матч...</p>';
  const res = await fetch(`/api/matches/${matchId}`);
  if (res.status === 403) return renderLockPanel(detailEl);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Матч не найден.</p>';
    return;
  }
  const m = await res.json();

  const when = m.start_time
    ? new Date(m.start_time * 1000).toLocaleString("ru-RU", { dateStyle: "long", timeStyle: "short" })
    : "";
  const durLine = m.duration
    ? ` · ${Math.floor(m.duration / 60)}:${String(m.duration % 60).padStart(2, "0")}`
    : "";
  const winnerSide = m.radiant_win == null ? null : (m.radiant_win ? "radiant" : "dire");
  // With real addresses the browser's own history is the way back, so the
  // button just steps back rather than needing to know where it came from.
  const backBtn = history.length > 1
    ? `<button class="back-link" id="match-back">← назад</button>`
    : "";

  const draftHtml = m.has_draft
    ? `<h3 class="history-title">Драфт</h3>
       <div class="draft-match">
         ${renderDraftTeamRow(m.radiant.name, m.radiant.draft)}
         ${renderDraftTeamRow(m.dire.name, m.dire.draft)}
       </div>`
    : '<p class="hint">Драфт этого матча ещё не подгружен.</p>';

  detailEl.innerHTML = `
    ${backBtn}
    <h2>${escapeHtml(m.radiant.name)} <span class="vs">против</span> ${escapeHtml(m.dire.name)}</h2>
    <p class="player-meta">${escapeHtml(m.tournament_label)} · ${when}${durLine} · <a class="ext-link" href="https://www.dotabuff.com/matches/${m.match_id}" target="_blank" rel="noopener noreferrer">Dotabuff</a></p>
    <div class="lineups">
      ${lineupTable(m.radiant, winnerSide, "radiant")}
      ${lineupTable(m.dire, winnerSide, "dire")}
    </div>
    ${draftHtml}
  `;

  const back = document.getElementById("match-back");
  if (back) back.addEventListener("click", () => history.back());
  for (const btn of detailEl.querySelectorAll(".player-link")) {
    btn.addEventListener("click", () => navigate(`/player/${btn.dataset.accountId}`));
  }
}

const playersBtn = document.getElementById("players-btn");
let leaderboardCache = null;

// Which single cup the career page offers to drill into. Not simply the
// active one: for a visitor without a key that cup is locked, and sending
// them there would land them on the offer panel with no way back. The sidebar
// already borrows an open cup - go to the same one.
function oneCupId() {
  return sidebarTournamentId != null ? sidebarTournamentId : currentCupId();
}

function oneCupLabel() {
  const cup = cups.byId.get(oneCupId());
  return cup ? cup.label : "турнир";
}

function renderLeaderboard(sortKey, sortDesc) {
  const data = leaderboardCache;
  const players = [...data.players];
  const numeric = (v) => (v == null ? -Infinity : v);
  const keyFns = {
    name: (p) => (p.name || "").toLowerCase(),
    team: (p) => (p.team_name || "").toLowerCase(),
    mmr: (p) => numeric(p.mmr),
    games: (p) => numeric(p.games),
    win_rate: (p) => numeric(p.win_rate),
  };
  const fn = keyFns[sortKey] || keyFns.mmr;
  players.sort((a, b) => {
    const x = fn(a), y = fn(b);
    if (x < y) return sortDesc ? 1 : -1;
    if (x > y) return sortDesc ? -1 : 1;
    return 0;
  });

  const rows = players
    .map((p, i) => {
      const heroesHtml = p.top_heroes
        .map((h) => `<img class="hero-icon" src="${escapeHtml(heroIconUrl(h.icon))}" alt="${escapeHtml(h.name)}" title="${escapeHtml(h.name)} ×${h.games}" loading="lazy" onerror="this.remove()">`)
        .join("");
      const teamCell = p.team_id != null
        ? `<button class="opponent-link" data-team-id="${p.team_id}">${escapeHtml(p.team_name)}</button>`
        : '<span class="hint">—</span>';
      const wr = p.win_rate == null
        ? "—"
        : `<span class="${p.win_rate >= 50 ? "wr-good" : "wr-bad"}">${p.win_rate}%</span> <span class="hint">(${p.wins}–${p.losses})</span>`;
      return `
        <tr>
          <td class="lb-rank">${i + 1}</td>
          <td><button class="player-link" data-account-id="${p.account_id}">${escapeHtml(p.name)}</button></td>
          <td>${teamCell}</td>
          <td>${formatMmr(p.mmr)}</td>
          <td>${p.games}</td>
          <td>${wr}</td>
          <td class="lb-heroes">${heroesHtml || '<span class="hint">—</span>'}</td>
        </tr>
      `;
    })
    .join("");

  const arrow = (k) => (k === sortKey ? (sortDesc ? " ↓" : " ↑") : "");
  detailEl.innerHTML = `
    <h2>Игроки · ${escapeHtml(data.tournament_label || "турнир")}</h2>
    <p class="hint">${
      data.all_tournaments
        ? `Счёт по всем турнирам сразу: ${escapeHtml((data.tournaments_counted || []).map((t) => t.label).join(", "))}.`
        : `Винрейт${data.hero_pools_locked ? "" : " и герои"} — только за этот турнир.`
    } Клик по заголовку — сортировка.${data.hero_pools_locked ? " Топ героев — по ключу." : ""}
      <button class="scope-toggle" id="lb-scope">${escapeHtml(
        data.all_tournaments ? `только ${oneCupLabel()}` : "показать за все турниры")}</button></p>
    <table class="subs-table leaderboard-table">
      <thead><tr>
        <th></th>
        <th class="sortable" data-sort="name">Игрок${arrow("name")}</th>
        <th class="sortable" data-sort="team">Команда${arrow("team")}</th>
        <th class="sortable" data-sort="mmr">MMR${arrow("mmr")}</th>
        <th class="sortable" data-sort="games">Игры${arrow("games")}</th>
        <th class="sortable" data-sort="win_rate">Винрейт${arrow("win_rate")}</th>
        <th>Топ героев</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;

  const scopeToggle = detailEl.querySelector("#lb-scope");
  if (scopeToggle) {
    scopeToggle.addEventListener("click", () =>
      navigate(data.all_tournaments ? cupPath(oneCupId(), "players") : "/players"));
  }
  for (const th of detailEl.querySelectorAll("th.sortable")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      // Second click on the same column flips the direction.
      renderLeaderboard(key, key === sortKey ? !sortDesc : key !== "name" && key !== "team");
    });
  }
  for (const btn of detailEl.querySelectorAll(".player-link")) {
    btn.addEventListener("click", () => navigate(`/player/${btn.dataset.accountId}`));
  }
  for (const link of detailEl.querySelectorAll(".opponent-link")) {
    link.addEventListener("click", () =>
      navigate(cupPath(data.tournament_id, `team/${link.dataset.teamId}`)));
  }
}

async function loadPlayersLeaderboard(tournamentId) {
  detailEl.innerHTML = '<p class="hint">Загружаю игроков...</p>';
  const scope = tournamentId === "all" ? "?tournament=all" : scopeQuery(tournamentId);
  const res = await fetch(`/api/players${scope}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Не удалось получить список игроков.</p>';
    return;
  }
  leaderboardCache = await res.json();
  renderLeaderboard("mmr", true);
}

playersBtn.addEventListener("click", () => navigate(cupPath(currentCupId(), "players")));

const allSubsBtn = document.getElementById("all-subs-btn");

async function loadAllSubstitutions(tournamentId) {
  detailEl.innerHTML = '<p class="hint">Загружаю замены...</p>';
  const res = await fetch(`/api/substitutions${scopeQuery(tournamentId)}`);
  const data = await res.json();
  const title = `Замены · ${escapeHtml(data.tournament_label || "турнир")}`;

  if (!data.substitutions.length) {
    detailEl.innerHTML = `<h2>${title}</h2><p class="hint">Замен в этом турнире не сохранилось.</p>`;
    return;
  }

  const rows = data.substitutions
    .map((s) => {
      const when = new Date(s.at).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
      const outCell = s.out
        ? `${escapeHtml(s.out)}${s.out_rating != null ? ` <span class="sub-mmr">${formatMmr(s.out_rating)}</span>` : ""}`
        : "—";
      const inCell = s.in
        ? `${escapeHtml(s.in)}${s.in_rating != null ? ` <span class="sub-mmr">${formatMmr(s.in_rating)}</span>` : ""}`
        : "—";
      let diffCell = "—";
      if (s.rating_diff != null) {
        const cls = s.rating_diff >= 0 ? "rating-diff-up" : "rating-diff-down";
        diffCell = `<span class="rating-diff ${cls}">${s.rating_diff > 0 ? "+" : ""}${s.rating_diff}</span>`;
      }
      const queueCell = s.queue_position != null ? `#${s.queue_position}` : "—";
      return `
        <tr>
          <td class="subs-date">${when}</td>
          <td><button class="opponent-link" data-team-id="${s.team_id}">${escapeHtml(s.team_name)}</button></td>
          <td>${outCell}</td>
          <td>${inCell}</td>
          <td>${diffCell}</td>
          <td>${queueCell}</td>
        </tr>
      `;
    })
    .join("");

  detailEl.innerHTML = `
    <h2>${title}</h2>
    <table class="subs-table">
      <thead>
        <tr>
          <th>Дата</th><th>Команда</th><th>Кто вышел (MMR)</th><th>Кто зашёл (MMR)</th><th>Разница</th><th>Место в очереди</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint">Место в очереди известно только для замен после того, как мы начали сохранять снимки очереди.</p>
  `;

  for (const link of detailEl.querySelectorAll(".opponent-link")) {
    link.addEventListener("click", () =>
      navigate(cupPath(data.tournament_id, `team/${link.dataset.teamId}`)));
  }
}

allSubsBtn.addEventListener("click", () => navigate(cupPath(currentCupId(), "subs")));

// Матчи обновляются сами (внутренний таймер на сервере + внешний пинг раз
// в 10 минут) - здесь просто пассивно отражаем текущий статус и
// перезагружаем данные, когда фоновое обновление завершается.
let wasRunning = false;

async function pollCollectStatus() {
  const res = await fetch("/api/collect/status");
  const status = await res.json();

  // Keep the user-facing text friendly - never leak internal collector
  // details (pids, log lines, stack traces) into the header.
  if (status.running) {
    collectStatusEl.textContent = "Обновление данных…";
  } else {
    collectStatusEl.textContent = "";
  }

  // Refresh when a collection finishes, and also mid-run if the sidebar is
  // still empty - the collector publishes core team data partway through
  // (before the slow draft backfill), so this lets an already-open page
  // show teams as soon as that first stage lands instead of waiting for the
  // whole run to complete.
  const sidebarEmpty = teamsEl.querySelectorAll(".team-btn").length === 0;
  if ((wasRunning && !status.running) || (status.running && sidebarEmpty)) {
    // Re-render whatever address is open, sidebar included.
    sidebarTournamentId = undefined;
    renderRoute();
  }
  wasRunning = status.running;
}

async function loadAccessStatus() {
  try {
    const s = await (await fetch("/api/auth/status")).json();
    access = {
      enabled: !!s.enabled,
      authenticated: !!s.authenticated,
      publicArchive: !!s.public_archive,
      offer: s.offer || null,
    };
  } catch (_) { /* keep the permissive default; the API is the real gate */ }

  // "Выйти" only makes sense for someone who is actually logged in.
  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn && access.enabled && access.authenticated) {
    logoutBtn.style.display = "";
    logoutBtn.addEventListener("click", async () => {
      try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) {}
      location.href = "/login";
    });
  }
  // A visitor without a key gets a "У меня есть ключ" entry point instead.
  const loginBtn = document.getElementById("login-btn");
  if (loginBtn && access.enabled && !access.authenticated) {
    loginBtn.style.display = "";
    loginBtn.addEventListener("click", () => { location.href = "/login"; });
  }
}

setInterval(pollCollectStatus, 15000);
pollCollectStatus();

// Access state and the cup list both have to be known before the first
// render: one decides what may be shown, the other turns the slug in the
// address into a tournament id.
Promise.all([loadAccessStatus(), loadTournaments()]).then(() => {
  // Land a visitor without a key on the newest cup they CAN read, rather than
  // on a lock panel. Only for the bare root - a shared deep link keeps its
  // address and explains itself there.
  if (location.pathname === "/" && cupIsLocked(cups.activeId)) {
    const open = cups.list.find((t) => !t.locked && t.has_matches);
    if (open) history.replaceState({}, "", `/${open.slug}`);
  }
  renderRoute();
});
