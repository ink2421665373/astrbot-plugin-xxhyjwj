const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let state = {hero_mappings: [], season_mappings: [], revision: ""};
let active = "hero", dirty = false, busy = false, connected = false, reloadArmed = false;
let loginStatus = null;
const field = () => active === "hero" ? "hero_mappings" : "season_mappings";
const keys = () => active === "hero" ? ["hero_id", "hero_name"] : ["season_id", "season_name"];

function notice(text, type = "") { $("notice").textContent = text; $("notice").className = type; }
function controls() {
  for (const id of ["add", "automatic", "reload", "refresh-login"]) $(id).disabled = !connected || busy;
  $("automatic").disabled = !connected || busy || !state.season_mappings.some(row => row.is_current);
  $("save").disabled = !connected || busy || !dirty;
  $("sync-seasons").disabled = !connected || busy || dirty;
  $("sync-nickname").disabled = !connected || busy;
  $("sync-mode").disabled = !connected || busy;
  $("sync-seasons").title = dirty ? "请先保存或放弃尚未保存的修改，再同步赛季" : "读取并收录接口返回的完整赛季目录";
  $("save").textContent = busy ? "正在处理…" : "保存全部修改";
  $("reload").textContent = reloadArmed ? "确认放弃修改" : "重新载入";
  $("save-state").textContent = !connected ? "未连接" : dirty ? "有未保存修改" : "配置已载入";
  $("save-state").className = "badge" + (dirty ? " dirty" : "");
  $("hero-count").textContent = state.hero_mappings.length;
  $("season-count").textContent = state.season_mappings.length;
  const selected = state.season_mappings.find(row => row.is_current);
  const scope = selected ? `${selected.season_id === "pre-01" ? "全部赛季" : selected.season_name || "未命名"}（${selected.season_id || "编码未填"}）` : "接口自动识别";
  $("current-season").textContent = `${dirty ? "页面选择（有未保存修改）" : "已保存范围"}：${scope}`;
  if (loginStatus) {
    const fileLabels = {saved:"已保存",missing:"文件不存在",empty:"未保存凭证",invalid:"文件格式错误",unreadable:"文件读取失败"};
    $("login-status").textContent = `数据模式：${loginStatus.data_mode} ｜ 登录文件：${fileLabels[loginStatus.credential_file_state] || "未知"} ｜ 内存凭证：${loginStatus.has_credentials ? "已载入（未联网校验）" : "未载入"}`;
  }
}
async function refreshLoginStatus(announce = true) {
  try {
    loginStatus = await bridge.apiGet("mappings/login-status"); controls();
    if (announce) notice(loginStatus.data_mode === "none" ? "当前为免登录模式，请在插件配置切换为 heybox 或 wechat。" : loginStatus.has_credentials ? "已重新读取本地登录凭证，可以重试同步；有效性由接口请求确认。" : "当前插件未读取到凭证，请按下方登录状态检查同实例登录及文件状态。");
  } catch (error) { if (announce) notice(`读取登录状态失败：${error.message}`, "error"); }
}
function changed() { dirty = true; reloadArmed = false; controls(); notice("修改尚未保存。点击“保存全部修改”后，下一次查询生效。"); }

function render() {
  $("hero-tab").classList.toggle("active", active === "hero");
  $("season-tab").classList.toggle("active", active === "season");
  $("hero-tab").setAttribute("aria-selected", active === "hero");
  $("season-tab").setAttribute("aria-selected", active === "season");
  $("hero-help").hidden = active !== "hero";
  $("season-help").hidden = active !== "season";
  $("current-heading").hidden = active !== "season";
  const [idKey, nameKey] = keys();
  const query = $("search").value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  let count = 0;
  state[field()].forEach((row, index) => {
    if (query && !`${row[idKey]} ${row[nameKey]}`.toLowerCase().includes(query)) return;
    count++;
    const tr = document.createElement("tr");
    for (const [key, label] of [[idKey, "编码"], [nameKey, "名称"]]) {
      const td = document.createElement("td"), input = document.createElement("input");
      input.type = "text"; input.value = row[key] || "";
      input.placeholder = label === "编码" ? (active === "hero" ? "例如 03 或完整英雄 ID" : "填写真实接口编码") : "显示名称";
      input.setAttribute("aria-label", `第${index + 1}项${label}`);
      input.maxLength = key === idKey ? 96 : 80;
      input.disabled = busy || !connected;
      input.addEventListener("input", () => { row[key] = input.value; changed(); });
      td.append(input); tr.append(td);
    }
    if (active === "season") {
      const td = document.createElement("td"), label = document.createElement("label"), checkbox = document.createElement("input");
      td.className = "current-cell"; checkbox.type = "checkbox"; checkbox.checked = Boolean(row.is_current);
      checkbox.setAttribute("aria-label", row.season_id === "pre-01" ? "战绩使用全部赛季" : `战绩查询使用${row.season_name || "此赛季"}`);
      checkbox.disabled = busy || !connected;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.season_mappings.forEach(item => { item.is_current = false; });
        row.is_current = checkbox.checked; changed(); render();
      });
      label.append(checkbox, document.createTextNode(row.season_id === "pre-01" ? "使用全部" : "用于查询")); td.append(label); tr.append(td);
    }
    const td = document.createElement("td"), button = document.createElement("button");
    button.className = "remove"; button.textContent = "删除"; button.disabled = busy || !connected;
    button.setAttribute("aria-label", `删除${row[nameKey] || "此条目"}`);
    button.addEventListener("click", () => { state[field()].splice(index, 1); changed(); render(); });
    td.append(button); tr.append(td); fragment.append(tr);
  });
  $("rows").replaceChildren(fragment);
  $("empty").hidden = count > 0;
  $("empty").textContent = query ? "没有匹配的条目。" : active === "season" ? "尚未同步赛季。请在上方输入昵称，点击“从接口同步全部赛季”。" : "暂无条目，点击“新增条目”开始配置。";
  $("summary").textContent = `显示 ${count} / ${state[field()].length} 项 · 英雄与赛季一并保存`;
  controls();
}

async function load() {
  busy = true; controls();
  try {
    state = await bridge.apiGet("mappings");
    loginStatus = state.login_status || null;
    if (!Array.isArray(state.hero_mappings) || !Array.isArray(state.season_mappings) || !state.revision) throw new Error("返回的配置格式不正确");
    dirty = false; reloadArmed = false; connected = true; notice("配置已载入。可以直接编辑，保存后下一次查询生效。", "success");
  } catch (error) { notice(`载入失败：${error.message}。请在 AstrBot 插件详情的可视化视图中打开。`, "error"); }
  finally { busy = false; render(); }
}

$("hero-tab").addEventListener("click", () => { active = "hero"; $("search").value = ""; render(); });
$("season-tab").addEventListener("click", () => { active = "season"; $("search").value = ""; render(); });
$("search").addEventListener("input", render);
$("add").addEventListener("click", () => {
  const [idKey, nameKey] = keys();
  state[field()].push({__template_key: active, [idKey]: "", [nameKey]: "", ...(active === "season" ? {is_current: false} : {})});
  $("search").value = ""; changed(); render();
  $("rows").lastElementChild?.querySelector("input")?.focus();
});
$("automatic").addEventListener("click", () => { state.season_mappings.forEach(row => { row.is_current = false; }); changed(); render(); });
$("sync-seasons").addEventListener("click", async () => {
  const nickname = $("sync-nickname").value.trim();
  if (!nickname) { notice("请先填写用于同步赛季的玩家昵称。", "error"); $("sync-nickname").focus(); return; }
  busy = true; render(); notice("正在搜索玩家并读取完整赛季目录…");
  try {
    const result = await bridge.apiPost("mappings/seasons/sync", {nickname, mode: $("sync-mode").value, revision: state.revision});
    state = result; dirty = false; reloadArmed = false; $("search").value = "";
    notice(`同步完成：接口返回 ${result.received_count} 项，有效赛季 ${result.catalog_count} 个，新增 ${result.added_count} 个，已保存。` + (result.skipped_count ? ` ${result.skipped_count} 项缺少有效编码或名称，未收录。` : "") + " 已有手动配置保留。", "success");
  } catch (error) { notice(`同步失败：${error.message}`, "error"); await refreshLoginStatus(false); }
  finally { busy = false; render(); }
});
$("refresh-login").addEventListener("click", () => refreshLoginStatus());
$("reload").addEventListener("click", () => {
  if (dirty && !reloadArmed) {
    reloadArmed = true; controls();
    notice("重新载入会放弃尚未保存的修改。确定放弃时，请点击“确认放弃修改”。");
    return;
  }
  load();
});
$("save").addEventListener("click", async () => {
  busy = true; render();
  try {
    const result = await bridge.apiPost("mappings/save", state);
    state = result; dirty = false; reloadArmed = false; notice("已保存。英雄名称和赛季配置将在下一次查询时生效。", "success");
  } catch (error) { notice(`保存失败：${error.message}`, "error"); }
  finally { busy = false; render(); }
});
if (!bridge) { notice("未检测到 AstrBot 视图连接。请从插件详情 → 可视化视图打开“英雄与赛季”。", "error"); render(); }
else {
  try { await bridge.ready(); await load(); }
  catch (error) { notice(`AstrBot 视图连接失败：${error.message}`, "error"); render(); }
}
