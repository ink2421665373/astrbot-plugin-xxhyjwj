const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let state = {nickname_mappings: [], revision: ""};
let dirty = false, busy = false, connected = false, reloadArmed = false;

function notice(text, type = "") { $("notice").textContent = text; $("notice").className = type; }

function controls() {
  for (const id of ["add", "reload", "resolve"]) if ($(id)) $(id).disabled = !connected || busy;
  $("resolve-nickname").disabled = !connected || busy;
  $("save").disabled = !connected || busy || !dirty;
  $("save").textContent = busy ? "正在处理…" : "保存全部修改";
  $("reload").textContent = reloadArmed ? "确认放弃修改" : "重新载入";
  $("save-state").textContent = !connected ? "未连接" : dirty ? "有未保存修改" : "配置已载入";
  $("save-state").className = "badge" + (dirty ? " dirty" : "");
}

function changed() { dirty = true; reloadArmed = false; controls(); notice("修改尚未保存。点击“保存全部修改”后，下一次查询生效。"); }

function render() {
  const rows = state.nickname_mappings || [];
  const fragment = document.createDocumentFragment();
  rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    // 昵称：可编辑（改别名用）
    const tdName = document.createElement("td"), inputName = document.createElement("input");
    inputName.type = "text"; inputName.value = row.nickname || ""; inputName.maxLength = 80;
    inputName.setAttribute("aria-label", `第${index + 1}项昵称`); inputName.disabled = busy || !connected;
    inputName.addEventListener("input", () => { row.nickname = inputName.value; changed(); });
    tdName.append(inputName); tr.append(tdName);
    // 角色ID：只读（唯一标识，不允许手改）
    const tdId = document.createElement("td"), inputId = document.createElement("input");
    inputId.type = "text"; inputId.value = row.role_id || ""; inputId.readOnly = true; inputId.className = "mono";
    inputId.setAttribute("aria-label", `第${index + 1}项角色ID，只读`); tdId.append(inputId); tr.append(tdId);
    // 接口角色名 / 服务器：只读展示
    for (const [key, placeholder] of [["role_name", "—"], ["server", "—"]]) {
      const td = document.createElement("td"), input = document.createElement("input");
      input.type = "text"; input.value = row[key] || ""; input.readOnly = true; input.placeholder = placeholder;
      input.setAttribute("aria-label", `第${index + 1}项${key === "role_name" ? "接口角色名" : "服务器"}，只读`);
      td.append(input); tr.append(td);
    }
    const td = document.createElement("td"), button = document.createElement("button");
    button.className = "remove"; button.textContent = "删除"; button.disabled = busy || !connected;
    button.setAttribute("aria-label", `删除${row.nickname || "此条目"}`);
    button.addEventListener("click", () => { state.nickname_mappings.splice(index, 1); changed(); render(); });
    td.append(button); tr.append(td); fragment.append(tr);
  });
  $("rows").replaceChildren(fragment);
  $("empty").hidden = rows.length > 0;
  $("summary").textContent = `共 ${rows.length} 项`;
  controls();
}

function renderCandidates(list) {
  const fragment = document.createDocumentFragment();
  list.forEach(item => {
    const tr = document.createElement("tr");
    for (const key of ["role_name", "role_id", "server"]) {
      const td = document.createElement("td");
      td.textContent = item[key] || "—";
      if (key === "role_id") td.className = "mono";
      tr.append(td);
    }
    const td = document.createElement("td"), button = document.createElement("button");
    button.className = "remove"; button.textContent = "登记";
    button.addEventListener("click", () => { addResolved(item); $("candidates").hidden = true; });
    td.append(button); tr.append(td); fragment.append(tr);
  });
  $("candidate-rows").replaceChildren(fragment);
  $("candidates").hidden = false;
}

function addResolved(item) {
  const nickname = $("resolve-nickname").value.trim() || item.role_name || "";
  if (!nickname) { notice("请先填写昵称。", "error"); return; }
  if ((state.nickname_mappings || []).some(row => row.role_id === item.role_id)) {
    notice(`角色ID ${item.role_id} 已经登记过了。`, "error"); return;
  }
  state.nickname_mappings = state.nickname_mappings || [];
  state.nickname_mappings.push({nickname, role_id: item.role_id, role_name: item.role_name || "", server: item.server || ""});
  $("resolve-nickname").value = "";
  changed(); render();
  notice(`已加入「${nickname}」→ ${item.role_id}，记得点击“保存全部修改”。`, "success");
}

async function load() {
  busy = true; controls();
  try {
    const result = await bridge.apiGet("nicknames");
    state = {nickname_mappings: result.nickname_mappings || [], revision: result.revision || ""};
    if (!Array.isArray(state.nickname_mappings) || !state.revision) throw new Error("返回的配置格式不正确");
    dirty = false; reloadArmed = false; connected = true;
    notice("配置已载入。登记后保存，下一次查询生效。", "success");
  } catch (error) { notice(`载入失败：${error.message}。请在 AstrBot 插件详情的可视化视图中打开。`, "error"); }
  finally { busy = false; render(); }
}

$("resolve").addEventListener("click", async () => {
  const nickname = $("resolve-nickname").value.trim();
  if (!nickname) { notice("请先填写玩家昵称。", "error"); $("resolve-nickname").focus(); return; }
  busy = true; controls(); notice("正在解析角色唯一标识…");
  try {
    const item = await bridge.apiPost("nicknames/resolve", {nickname});
    addResolved(item);
  } catch (error) {
    if (Array.isArray(error.extra)) renderCandidates(error.extra);
    notice(`解析失败：${error.message}`, "error");
  } finally { busy = false; render(); }
});

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
    const result = await bridge.apiPost("nicknames/save", state);
    state = {nickname_mappings: result.nickname_mappings || [], revision: result.revision || ""};
    dirty = false; reloadArmed = false;
    notice("已保存。昵称对应的角色将在下一次查询时生效。", "success");
  } catch (error) { notice(`保存失败：${error.message}`, "error"); }
  finally { busy = false; render(); }
});

if (!bridge) { notice("未检测到 AstrBot 视图连接。请从插件详情 → 可视化视图打开“昵称与角色”。", "error"); render(); }
else {
  try { await bridge.ready(); await load(); }
  catch (error) { notice(`AstrBot 视图连接失败：${error.message}`, "error"); render(); }
}
