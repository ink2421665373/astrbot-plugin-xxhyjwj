const $ = id => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const modes = {12:"天选双排",5000000:"天选单排",5000001:"天选三排",4:"天人单排",13:"天人双排",5:"天人三排",6:"快速单排",7:"快速三排"};
const states = {missing:"缺少字段 / 缺失标记",missing_value:"缺少 value 字段",null:"返回 null",empty_value:"空字符串",wrong_type:"字段类型异常",empty:"空列表 / 空对象",present:"已返回",absent:"未返回此名称",outside_preview:"前200项未匹配，无法判断完整结果",zero:"返回零值",value:"已返回数值",not_numeric:"非数值（未展示原值）"};
const errors = {HeyboxLoginRequired:"接口要求重新登录",HeyboxRateLimited:"接口限制请求频率",HeyboxNetworkError:"网络请求失败",TimeoutError:"请求超时",ValueError:"接口数据校验失败",request_failed:"接口请求失败"};
let records = [], busy = false, connected = false, clearArmed = false;
let cacheEntries = 0, cacheClearArmed = false;
function notice(text, error = false) { $("notice").textContent = text; $("notice").className = error ? "error" : ""; }
function time(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "时间未知" : date.toLocaleString("zh-CN", {hour12:false}); }
function controls() {
  $("refresh").disabled = !connected || busy;
  $("clear").disabled = !connected || busy || !records.length;
  $("clear-cache").disabled = !connected || busy || !cacheEntries;
  $("clear-cache").textContent = cacheClearArmed ? "确认清空战绩缓存" : "清空战绩缓存";
  $("cache-count").textContent = cacheEntries;
  $("record").disabled = busy || !records.length;
  $("clear").textContent = clearArmed ? "确认清空记录" : "清空记录";
  $("count").textContent = `内存记录 ${records.length} / 50`;
}
function cell(row, text) { const td = document.createElement("td"); td.textContent = text ?? "—"; row.append(td); }
function table(id, rows) {
  const fragment = document.createDocumentFragment();
  for (const values of rows) { const tr = document.createElement("tr"); values.forEach(value => cell(tr, value)); fragment.append(tr); }
  if (!rows.length) { const tr = document.createElement("tr"); cell(tr, "暂无可展示的条目"); fragment.append(tr); }
  $(id).replaceChildren(fragment);
}
function render() {
  const selected = records.find(item => String(item.id) === $("record").value);
  $("detail").hidden = !selected; $("empty").hidden = !!selected;
  if (!selected) { controls(); return; }
  const metadata = [["记录时间（请求结束）", time(selected.captured_at)], ["接口", selected.endpoint], ["模式", modes[selected.mode] || selected.mode || "未指定"], ["请求赛季编码", selected.season || "单局接口未传赛季参数"], ["接口耗时", `${selected.elapsed_ms} ms`], ["请求结果", selected.status === "ok" ? "请求完成" : "请求失败"]];
  $("metadata").replaceChildren();
  for (const [name, value] of metadata) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = name; dd.textContent = value; $("metadata").append(dt, dd); }
  $("fields").replaceChildren();
  for (const [name, state] of Object.entries(selected.fields || {})) { const span = document.createElement("span"); span.textContent = `${name}：${states[state] || state}`; $("fields").append(span); }
  $("error").hidden = selected.status === "ok";
  $("error").textContent = errors[selected.error] || "接口请求失败；未采集返回数据。";
  $("season-panel").hidden = selected.kind !== "home" || selected.status !== "ok";
  $("metric-panel").hidden = selected.status !== "ok";
  $("season-count").textContent = `（接口返回 ${selected.season_count || 0} 项）`;
  table("seasons", (selected.seasons || []).map(row => [row.code || "缺少编码", row.name || "缺少名称", row.selected, row.is_current, row.current]));
  $("season-warning").hidden = !selected.seasons_truncated;
  $("season-warning").textContent = "接口目录超过500项，诊断仅保留前500项；查询数据未修改。";
  const search = $("search").value.trim();
  const metrics = (selected.metrics || []).filter(item => !search || item.name.includes(search));
  table("metrics", metrics.map(row => [row.name, row.value ?? "—", states[row.state] || row.state]));
  $("metric-hint").textContent = `${selected.kind === "home" ? "主页 overview" : "单局 data"} 接口返回 ${selected.metric_count || 0} 项。以下为返回指标和缺失检查结果，值不经过场均计算或名称映射。`;
  $("metric-warning").hidden = !selected.metrics_truncated && selected.response_state === "object";
  $("metric-warning").textContent = selected.response_state !== "object" ? "接口返回类型异常，预期为对象。" : "指标超过200项，诊断仅保留前200项；查询数据未修改。";
  controls();
}
function accept(result) {
  if (!Array.isArray(result.records)) throw new Error("诊断数据格式异常");
  const previous = $("record").value;
  records = result.records;
  if (Number.isInteger(result.cache_entries)) cacheEntries = result.cache_entries;
  $("record").replaceChildren();
  for (const row of records) { const option = document.createElement("option"); option.value = row.id; option.textContent = `#${row.id} ${time(row.captured_at)} · ${row.kind === "home" ? "主页" : "单局详情"} · ${modes[row.mode] || row.mode} · ${row.season || "单局"}`; $("record").append(option); }
  if (records.some(row => String(row.id) === previous)) $("record").value = previous;
  clearArmed = false; render();
}
async function refresh() {
  busy = true; controls();
  try { accept(await bridge.apiGet("diagnostics")); notice("已读取内存诊断记录，本次刷新未请求小黑盒。最新记录在列表顶部。"); }
  catch (error) { notice(`读取失败：${error.message}`, true); }
  finally { busy = false; controls(); }
}
$("refresh").addEventListener("click", refresh);
$("record").addEventListener("change", render);
$("clear-cache").addEventListener("click", async () => {
  if (!cacheClearArmed) { cacheClearArmed = true; controls(); notice("再次点击确认清空单局详情缓存。下一次查询可能耗时更久，不影响登录和配置。"); return; }
  busy = true; controls();
  try { const result = await bridge.apiPost("diagnostics/cache/clear", {}); cacheEntries = result.cache_entries; cacheClearArmed = false; notice(`已清空 ${result.cleared_count} 条战绩缓存，下次查询将重新获取详情。`); }
  catch (error) { notice(`清空缓存失败：${error.message}`, true); }
  finally { busy = false; controls(); }
});
$("search").addEventListener("input", render);
$("clear").addEventListener("click", async () => {
  if (!clearArmed) { clearArmed = true; controls(); notice("再次点击确认清空内存诊断，不影响登录、配置和战绩缓存。"); return; }
  busy = true; controls();
  try { accept(await bridge.apiPost("diagnostics/clear", {})); notice("诊断记录已清空。后续接口请求将继续自动采集。"); }
  catch (error) { notice(`清空失败：${error.message}`, true); }
  finally { busy = false; controls(); }
});
if (!bridge) { notice("请从 AstrBot 插件详情 → 可视化视图 → 数据诊断打开此页面。", true); controls(); }
else {
  try { await bridge.ready(); connected = true; await refresh(); }
  catch (error) { notice(`连接失败：${error.message}`, true); controls(); }
}
