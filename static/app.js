// If the session expires or is missing (private mode), any API call returns
// 401 - bounce to the login page instead of showing a broken site.
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

function formatMmr(value) {
  return value == null ? "?" : Math.round(value).toLocaleString("ru-RU");
}

function heroIconUrl(slug) {
  return `https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/${slug}.png`;
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
        <img src="${heroIconUrl(e.hero_icon)}" alt="${e.hero}" title="${e.hero}">
        <span class="cell-label">${e.is_pick ? "PICK" : "BAN"}</span>
      </div>
    `)
    .join("");
  return `
    <div class="draft-team-row">
      <span class="draft-team-label">${teamName}</span>
      <div class="draft-cells">${cells || '<span class="hint">нет данных</span>'}</div>
    </div>
  `;
}

async function loadTeams() {
  const res = await fetch("/api/teams");
  const teams = await res.json();

  teamsEl.innerHTML = "";
  if (teams.length === 0) {
    teamsEl.innerHTML = '<p class="hint">Нет данных. Обновляется автоматически, зайдите чуть позже.</p>';
    return;
  }

  for (const team of teams) {
    const btn = document.createElement("button");
    btn.className = "team-btn" + (team.team_id === activeTeamId ? " active" : "");
    btn.textContent = `${team.name} (${team.player_count}) · ${formatMmr(team.total_mmr)} MMR`;
    btn.onclick = () => loadTeamDetail(team.team_id);
    teamsEl.appendChild(btn);
  }
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
        return `<li><span>${h.name}</span><span>${wr}<span class="count">×${h.games}</span></span></li>`;
      })
      .join("");
    const rolesLine = player.roles ? `<p class="roles">${formatRoles(player.roles)}</p>` : "";
    card.innerHTML = `
      <h3><button class="player-link" data-account-id="${player.account_id}">${player.name}</button><span class="profile-links">${profileLinks(player.account_id)}</span></h3>
      <p class="mmr">${formatMmr(player.mmr)} MMR</p>
      ${rolesLine}
      <ul>${heroItems || '<li><span class="hint">ещё не играл(а) за команду</span></li>'}</ul>
    `;
    card.querySelector(".player-link").addEventListener("click", () => loadPlayerPage(player.account_id));
    grid.appendChild(card);
  }

  const mmrLine = document.createElement("p");
  mmrLine.className = "total-mmr";
  mmrLine.textContent = `Суммарный MMR: ${formatMmr(team.total_mmr)}`;
  container.appendChild(mmrLine);

  if (team.next_opponent) {
    const opp = team.next_opponent;
    const when = opp.planned_time
      ? new Date(opp.planned_time).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" })
      : "время пока не назначено";
    const opponentLabel = opp.opponent_team_id != null
      ? `<button class="opponent-link" data-team-id="${opp.opponent_team_id}">${opp.opponent_name}</button>`
      : `<strong>${opp.opponent_name}</strong>`;
    const nextLine = document.createElement("p");
    nextLine.className = "next-opponent";
    nextLine.innerHTML = `Следующий соперник: ${opponentLabel} · ${when}`;
    const link = nextLine.querySelector(".opponent-link");
    if (link) {
      link.addEventListener("click", () => loadTeamDetail(opp.opponent_team_id));
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
    const vs = lm.opponent_name ? ` против ${lm.opponent_name}` : "";
    const names = lm.players
      .map((p) => `<button class="player-link" data-account-id="${p.account_id}">${p.name}</button><span class="profile-links">${profileLinks(p.account_id)}</span>`)
      .join(", ");
    const lineupLine = document.createElement("p");
    lineupLine.className = "last-lineup";
    lineupLine.innerHTML = `Состав в последнем матче${vs}${when ? ` (${when})` : ""}: ${names}`;
    for (const btn of lineupLine.querySelectorAll(".player-link")) {
      btn.addEventListener("click", () => loadPlayerPage(Number(btn.dataset.accountId)));
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
  return items.map((i) => `<span class="tag ${cssClass}">${i.hero} ×${i.count}</span>`).join("");
}

async function renderAnalysisTab(teamId, container) {
  container.innerHTML = '<p class="hint">Считаю аналитику...</p>';
  const res = await fetch(`/api/teams/${teamId}/analysis`);
  if (!res.ok) {
    container.innerHTML = '<p class="hint">Не удалось получить аналитику.</p>';
    return;
  }
  const a = await res.json();

  const signatureHtml = a.signature_heroes.length
    ? a.signature_heroes
        .map((h) => `<span class="tag tag-pick">${h.hero} — ${h.win_rate}% (${h.wins}/${h.games})</span>`)
        .join("")
    : '<span class="hint">нет данных</span>';

  container.innerHTML = `
    <p class="coach-text">${a.text}</p>
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
    </div>
  `;
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
        text = `<strong>${s.out}</strong> → <strong>${s.in}</strong>`;
      } else if (s.out) {
        text = `<strong>${s.out}</strong> вышел из состава`;
      } else {
        text = `<strong>${s.in}</strong> добавлен в состав`;
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

async function loadTeamDetail(teamId, tab) {
  activeTeamId = teamId;
  activeTab = tab || "composition";
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }

  const res = await fetch(`/api/teams/${teamId}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Команда не найдена.</p>';
    return;
  }
  const team = await res.json();

  detailEl.innerHTML = `
    <h2>${team.name}</h2>
    <div class="tabs">
      <button class="tab-btn" data-tab="composition">Состав</button>
      <button class="tab-btn" data-tab="analysis">Аналитика</button>
      <button class="tab-btn" data-tab="substitutions">Замены</button>
    </div>
    <div id="tab-content"></div>
  `;

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
      renderAnalysisTab(teamId, tabContent);
    } else {
      renderSubstitutionsTab(teamId, tabContent);
    }
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => showTab(btn.dataset.tab));
  }

  showTab(activeTab);
  loadTeams();
}

async function loadPlayerPage(accountId) {
  activeTeamId = null;
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }
  detailEl.innerHTML = '<p class="hint">Загружаю профиль игрока...</p>';
  const res = await fetch(`/api/players/${accountId}`);
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Игрок не найден.</p>';
    return;
  }
  const p = await res.json();

  const rolesLine = p.roles ? ` · ${formatRoles(p.roles)}` : "";
  const teamLine = p.current_team_id != null
    ? `Команда: <button class="opponent-link" data-team-id="${p.current_team_id}">${p.current_team_name}</button>`
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
              ? `<img class="hero-icon" src="${h.icon}" alt="" loading="lazy" onerror="this.remove()">`
              : "";
            return `<span class="tag tag-hero ${cls}">${icon}<span>${h.name} ×${h.games}${wr}</span></span>`;
          })
          .join("")
      : '<span class="hint">нет сыгранных матчей</span>';

  // One hero pool per tournament (the two mixer cups run concurrently).
  const heroPoolsHtml = (p.hero_pools && p.hero_pools.length)
    ? p.hero_pools
        .map((pool) => `
          <div class="analysis-block player-heroes-block">
            <h4>Пул героев · ${pool.label}</h4>
            <div class="tag-list">${heroTagsFor(pool.heroes)}</div>
          </div>
        `)
        .join("")
    : `<div class="analysis-block player-heroes-block">
         <h4>Пул героев</h4>
         <div class="tag-list"><span class="hint">нет сыгранных матчей</span></div>
       </div>`;

  let lastLabel = null;
  const matchRows = p.matches
    .map((m) => {
      const when = m.start_time
        ? new Date(m.start_time * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short" })
        : "";
      let result = '<span class="match-result result-unknown">?</span>';
      if (m.won === true) result = '<span class="match-result result-win">Победа</span>';
      if (m.won === false) result = '<span class="match-result result-loss">Поражение</span>';
      const formerBadge = (p.current_team_id != null && m.team_id !== p.current_team_id)
        ? ' <span class="former-team-badge">прошлая команда</span>'
        : "";
      // Divider row whenever the tournament changes (matches are newest-first,
      // so each tournament forms one contiguous block).
      let divider = "";
      if (m.tournament_label && m.tournament_label !== lastLabel) {
        lastLabel = m.tournament_label;
        divider = `<tr class="tournament-divider"><td colspan="5">${m.tournament_label}</td></tr>`;
      }
      return `
        ${divider}
        <tr class="match-row" data-match-id="${m.match_id}" title="Открыть страницу матча">
          <td class="subs-date">${when}</td>
          <td>${m.hero}</td>
          <td>${result}</td>
          <td><button class="opponent-link" data-team-id="${m.team_id}">${m.team_name}</button>${formerBadge}</td>
          <td>против ${m.opponent_name}</td>
        </tr>
      `;
    })
    .join("");

  detailEl.innerHTML = `
    <h2>${p.name}<span class="profile-links">${profileLinks(p.account_id)}</span></h2>
    <p class="player-meta">${formatMmr(p.mmr)} MMR${rolesLine}</p>
    <p class="next-opponent">${teamLine}</p>
    <div class="player-body">
      <div class="player-history">
        <h3 class="history-title">История матчей</h3>
        ${p.matches.length ? `
          <table class="subs-table">
            <thead><tr><th>Дата</th><th>Герой</th><th>Результат</th><th>За команду</th><th>Соперник</th></tr></thead>
            <tbody>${matchRows}</tbody>
          </table>` : '<p class="hint">Матчей пока нет.</p>'}
      </div>
      <aside class="player-pools">${heroPoolsHtml}</aside>
    </div>
  `;

  for (const link of detailEl.querySelectorAll(".opponent-link")) {
    link.addEventListener("click", () => loadTeamDetail(Number(link.dataset.teamId)));
  }
  for (const row of detailEl.querySelectorAll(".match-row")) {
    row.addEventListener("click", (e) => {
      // The team button inside the row keeps its own action.
      if (e.target.closest(".opponent-link")) return;
      loadMatchPage(Number(row.dataset.matchId), accountId);
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
        <td class="lineup-hero"><img class="hero-icon" src="${heroIconUrl(pl.hero_icon)}" alt="" loading="lazy" onerror="this.remove()">${pl.hero}</td>
        <td><button class="player-link" data-account-id="${pl.account_id}">${pl.name}</button></td>
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
      <h4>${side.name}${winBadge}</h4>
      <table class="subs-table lineup-table">
        <thead>${head}</thead>
        <tbody>${rows || `<tr><td colspan="${cols}" class="hint">состав неизвестен</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

async function loadMatchPage(matchId, backAccountId) {
  detailEl.innerHTML = '<p class="hint">Загружаю матч...</p>';
  const res = await fetch(`/api/matches/${matchId}`);
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
  const backBtn = backAccountId != null
    ? `<button class="back-link" id="match-back">← к игроку</button>`
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
    <h2>${m.radiant.name} <span class="vs">против</span> ${m.dire.name}</h2>
    <p class="player-meta">${m.tournament_label} · ${when}${durLine} · <a class="ext-link" href="https://www.dotabuff.com/matches/${m.match_id}" target="_blank" rel="noopener noreferrer">Dotabuff</a></p>
    <div class="lineups">
      ${lineupTable(m.radiant, winnerSide, "radiant")}
      ${lineupTable(m.dire, winnerSide, "dire")}
    </div>
    ${draftHtml}
  `;

  const back = document.getElementById("match-back");
  if (back) back.addEventListener("click", () => loadPlayerPage(backAccountId));
  for (const btn of detailEl.querySelectorAll(".player-link")) {
    btn.addEventListener("click", () => loadPlayerPage(Number(btn.dataset.accountId)));
  }
}

const playersBtn = document.getElementById("players-btn");
let leaderboardCache = null;

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
        .map((h) => `<img class="hero-icon" src="${heroIconUrl(h.icon)}" alt="${h.name}" title="${h.name} ×${h.games}" loading="lazy" onerror="this.remove()">`)
        .join("");
      const teamCell = p.team_id != null
        ? `<button class="opponent-link" data-team-id="${p.team_id}">${p.team_name}</button>`
        : '<span class="hint">—</span>';
      const wr = p.win_rate == null
        ? "—"
        : `<span class="${p.win_rate >= 50 ? "wr-good" : "wr-bad"}">${p.win_rate}%</span> <span class="hint">(${p.wins}–${p.losses})</span>`;
      return `
        <tr>
          <td class="lb-rank">${i + 1}</td>
          <td><button class="player-link" data-account-id="${p.account_id}">${p.name}</button></td>
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
    <h2>Игроки · ${data.tournament_label || "турнир"}</h2>
    <p class="hint">Винрейт и герои — только за текущий турнир. Клик по заголовку — сортировка.</p>
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

  for (const th of detailEl.querySelectorAll("th.sortable")) {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      // Second click on the same column flips the direction.
      renderLeaderboard(key, key === sortKey ? !sortDesc : key !== "name" && key !== "team");
    });
  }
  for (const btn of detailEl.querySelectorAll(".player-link")) {
    btn.addEventListener("click", () => loadPlayerPage(Number(btn.dataset.accountId)));
  }
  for (const link of detailEl.querySelectorAll(".opponent-link")) {
    link.addEventListener("click", () => loadTeamDetail(Number(link.dataset.teamId)));
  }
}

async function loadPlayersLeaderboard() {
  activeTeamId = null;
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }
  detailEl.innerHTML = '<p class="hint">Загружаю игроков...</p>';
  const res = await fetch("/api/players");
  if (!res.ok) {
    detailEl.innerHTML = '<p class="hint">Не удалось получить список игроков.</p>';
    return;
  }
  leaderboardCache = await res.json();
  renderLeaderboard("mmr", true);
}

playersBtn.addEventListener("click", loadPlayersLeaderboard);

const tournamentStatsBtn = document.getElementById("tournament-stats-btn");

function renderTournamentHeroStats(data) {
  const winRateHtml = data.top_win_rate.length
    ? data.top_win_rate
        .map((h) => `<span class="tag tag-pick">${h.hero} — ${h.win_rate}% (${h.wins}/${h.games})</span>`)
        .join("")
    : '<span class="hint">нет данных</span>';

  const bannedHtml = data.most_banned.length
    ? data.most_banned.map((h) => `<span class="tag tag-ban">${h.hero} ×${h.bans}</span>`).join("")
    : '<span class="hint">нет данных</span>';

  const monopolyHtml = data.signature_by_player.length
    ? data.signature_by_player
        .map((h) => {
          const players = h.top_players.map((p) => `${p.name} (${p.games})`).join(", ");
          return `<span class="tag tag-neutral">${h.hero} — ${h.concentration}%: ${players}</span>`;
        })
        .join("")
    : '<span class="hint">нет данных</span>';

  detailEl.innerHTML = `
    <h2>Статистика по героям турнира</h2>
    <p class="hint">Учитываются герои минимум с ${data.min_games} играми.</p>
    <div class="analysis-grid">
      <div class="analysis-block">
        <h4>Самые успешные герои (win rate)</h4>
        <div class="tag-list">${winRateHtml}</div>
      </div>
      <div class="analysis-block">
        <h4>Чаще всего банят</h4>
        <div class="tag-list">${bannedHtml}</div>
      </div>
      <div class="analysis-block">
        <h4>Играют почти всегда одни и те же</h4>
        <div class="tag-list">${monopolyHtml}</div>
      </div>
    </div>
  `;
}

async function loadTournamentStats() {
  activeTeamId = null;
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }
  detailEl.innerHTML = '<p class="hint">Считаю статистику...</p>';
  const res = await fetch("/api/tournament/heroes");
  const data = await res.json();
  renderTournamentHeroStats(data);
}

tournamentStatsBtn.addEventListener("click", loadTournamentStats);

const allSubsBtn = document.getElementById("all-subs-btn");

async function loadAllSubstitutions() {
  activeTeamId = null;
  for (const btn of teamsEl.querySelectorAll(".team-btn")) {
    btn.classList.remove("active");
  }
  detailEl.innerHTML = '<p class="hint">Загружаю замены...</p>';
  const res = await fetch("/api/substitutions");
  const data = await res.json();

  if (!data.substitutions.length) {
    detailEl.innerHTML = "<h2>Замены турнира</h2><p class=\"hint\">Замен пока не было.</p>";
    return;
  }

  const rows = data.substitutions
    .map((s) => {
      const when = new Date(s.at).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
      const outCell = s.out
        ? `${s.out}${s.out_rating != null ? ` <span class="sub-mmr">${formatMmr(s.out_rating)}</span>` : ""}`
        : "—";
      const inCell = s.in
        ? `${s.in}${s.in_rating != null ? ` <span class="sub-mmr">${formatMmr(s.in_rating)}</span>` : ""}`
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
          <td><button class="opponent-link" data-team-id="${s.team_id}">${s.team_name}</button></td>
          <td>${outCell}</td>
          <td>${inCell}</td>
          <td>${diffCell}</td>
          <td>${queueCell}</td>
        </tr>
      `;
    })
    .join("");

  detailEl.innerHTML = `
    <h2>Замены турнира</h2>
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
    link.addEventListener("click", () => loadTeamDetail(Number(link.dataset.teamId)));
  }
}

allSubsBtn.addEventListener("click", loadAllSubstitutions);

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
    loadTeams();
    if (activeTeamId != null) {
      loadTeamDetail(activeTeamId, activeTab);
    }
  }
  wasRunning = status.running;
}

// Show the logout control only in private mode.
const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) {
  fetch("/api/auth/status")
    .then((r) => r.json())
    .then((s) => {
      if (s.enabled) {
        logoutBtn.style.display = "";
        logoutBtn.addEventListener("click", async () => {
          try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) {}
          location.href = "/login";
        });
      }
    })
    .catch(() => {});
}

setInterval(pollCollectStatus, 15000);
pollCollectStatus();
loadTeams();
