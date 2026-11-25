const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let state = {roles: [], revision: ""};
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

/* 与后端一致的比较键：去空白、统一小写、去零宽字符 */
function key(text) {
  return String(text || "")
    .replace(/[\u200b\u200c\u200d\ufeff]/g, "")
    .trim()
    .toLowerCase();
}

function findRoleByNickname(nickname) {
  const target = key(nickname);
  if (!target) return null;
  for (const role of state.roles || []) {
    if ((role.nicknames || []).some(item => key(item) === target)) return role;
  }
  return null;
}

function findRoleByRoleId(roleId) {
  return (state.roles || []).find(role => role.role_id === roleId) || null;
}

function totalNicknames() {
  return (state.roles || []).reduce((sum, role) => sum + (role.nicknames || []).length, 0);
}

function render() {
  const roles = state.roles || [];
  const fragment = document.createDocumentFragment();

  roles.forEach((role, index) => {
    const tr = document.createElement("tr");

    // 昵称：一个角色可以有多个，逐个可删，也能就地补一个
    const tdNames = document.createElement("td"), chips = document.createElement("div");
    chips.className = "chips";
    (role.nicknames || []).forEach((nickname, nickIndex) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const label = document.createElement("span");
      label.textContent = nickname;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-remove";
      remove.textContent = "×";
      remove.title = `移除昵称 ${nickname}`;
      remove.setAttribute("aria-label", `第${index + 1}个角色的第${nickIndex + 1}个昵称：移除`);
      remove.disabled = busy || !connected;
      remove.addEventListener("click", () => {
        if ((role.nicknames || []).length <= 1) {
          notice("每个角色至少要保留一个昵称；要移除整个角色，请点该行的“删除”。", "error");
          return;
        }
        role.nicknames.splice(nickIndex, 1);
        changed(); render();
      });
      chip.append(label, remove);
      chips.append(chip);
    });
    const add = document.createElement("input");
    add.type = "text";
    add.className = "nickname-add";
    add.maxLength = 80;
    add.placeholder = "＋ 添加昵称";
    add.disabled = busy || !connected;
    add.setAttribute("aria-label", `给第${index + 1}个角色添加昵称`);
    const commit = () => {
      const value = add.value.trim();
      if (!value) return;
      const owner = findRoleByNickname(value);
      if (owner && owner.role_id !== role.role_id) {
        notice(`昵称「${value}」已经登记在角色 ${owner.role_id} 上了。`, "error");
        return;
      }
      role.nicknames = role.nicknames || [];
      if (role.nicknames.some(item => key(item) === key(value))) {
        notice(`这个角色已经有昵称「${value}」了。`, "error");
        add.value = "";
        return;
      }
      role.nicknames.push(value);
      add.value = "";
      changed(); render();
    };
    add.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
    });
    add.addEventListener("blur", commit);
    chips.append(add);
    tdNames.append(chips);
    tr.append(tdNames);

    // 角色ID：只读（唯一标识，不允许手改）
    const tdId = document.createElement("td"), inputId = document.createElement("input");
    inputId.type = "text"; inputId.value = role.role_id || ""; inputId.readOnly = true; inputId.className = "mono";
    inputId.setAttribute("aria-label", `第${index + 1}个角色的角色ID，只读`); tdId.append(inputId); tr.append(tdId);

    // 接口角色名 / 服务器：只读展示
    for (const [field, placeholder] of [["role_name", "—"], ["server", "—"]]) {
      const td = document.createElement("td"), input = document.createElement("input");
      input.type = "text"; input.value = role[field] || ""; input.readOnly = true; input.placeholder = placeholder;
      input.setAttribute("aria-label", `第${index + 1}个角色的${field === "role_name" ? "接口角色名" : "服务器"}，只读`);
      td.append(input); tr.append(td);
    }

    const td = document.createElement("td"), button = document.createElement("button");
    button.className = "remove"; button.textContent = "删除";
    button.disabled = busy || !connected;
    button.setAttribute("aria-label", `删除角色${(role.nicknames || [])[0] || role.role_id}`);
    button.addEventListener("click", () => {
      state.roles.splice(index, 1); changed(); render();
    });
    td.append(button); tr.append(td); fragment.append(tr);
  });

  $("rows").replaceChildren(fragment);
  $("empty").hidden = roles.length > 0;
  $("summary").textContent = `共 ${roles.length} 个角色 · ${totalNicknames()} 个昵称`;
  controls();
}

function renderCandidates(list) {
  const fragment = document.createDocumentFragment();
  list.forEach(item => {
    const tr = document.createElement("tr");
    for (const field of ["role_name", "role_id", "server"]) {
      const td = document.createElement("td");
      td.textContent = item[field] || "—";
      if (field === "role_id") td.className = "mono";
      tr.append(td);
    }
    const td = document.createElement("td"), button = document.createElement("button");
    button.className = "remove"; button.textContent = "登记";
    button.addEventListener("click", () => { addResolved(item); $("candidates").hidden = true; });
    td.append(button); tr.append(td);
    fragment.append(tr);
  });
  $("candidate-rows").replaceChildren(fragment);
  $("candidates").hidden = false;
}

function addResolved(item) {
  const nickname = $("resolve-nickname").value.trim() || item.role_name || "";
  if (!nickname) { notice("请先填写昵称。", "error"); return; }
  const owner = findRoleByNickname(nickname);
  if (owner && owner.role_id !== item.role_id) {
    notice(`昵称「${nickname}」已经登记在角色 ${owner.role_id} 上了，请先删掉那条再登记。`, "error");
    return;
  }
  state.roles = state.roles || [];
  let role = findRoleByRoleId(item.role_id);
  if (role) {
    role.nicknames = role.nicknames || [];
    if (role.nicknames.some(value => key(value) === key(nickname))) {
      notice(`角色 ${item.role_id} 已经有昵称「${nickname}」了。`, "error");
      return;
    }
    role.nicknames.push(nickname);
    if (!role.role_name && item.role_name) role.role_name = item.role_name;
    if (!role.server && item.server) role.server = item.server;
    notice(`已给角色 ${item.role_id} 增加昵称「${nickname}」。`, "success");
  } else {
    state.roles.push({
      role_id: item.role_id,
      role_name: item.role_name || "",
      server: item.server || "",
      nicknames: [nickname],
    });
    notice(`已新增角色 ${item.role_id}，昵称「${nickname}」。`, "success");
  }
  $("resolve-nickname").value = "";
  changed(); render();
  notice("记得点击“保存全部修改”。", "success");
}

async function load() {
  busy = true; controls();
  try {
    const result = await bridge.apiGet("nicknames");
    state = {roles: result.roles || [], revision: result.revision || ""};
    if (!Array.isArray(state.roles) || !state.revision) throw new Error("返回的配置格式不正确");
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
    const result = await bridge.apiPost("nicknames/save", {revision: state.revision, roles: state.roles});
    state = {roles: result.roles || [], revision: result.revision || ""};
    dirty = false; reloadArmed = false;
    notice("已保存。这些昵称对应的角色将在下一次查询时生效。", "success");
  } catch (error) { notice(`保存失败：${error.message}`, "error"); }
  finally { busy = false; render(); }
});

if (!bridge) { notice("未检测到 AstrBot 视图连接。请从插件详情 → 可视化视图打开“昵称与角色”。", "error"); render(); }
else {
  try { await bridge.ready(); await load(); }
  catch (error) { notice(`AstrBot 视图连接失败：${error.message}`, "error"); render(); }
}
