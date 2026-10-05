import {tapTempo} from './rhythm.js';
import {classifyInstruction} from './commands.js';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const main = $('#main');
const dialog = $('#dialog');
const clone = value => structuredClone(value);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const size = value => [...value].length;
const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key,canonical(value[key])])) : value;
const same = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
const activeStatuses = new Set(['queued', 'generating', 'checking']);
const statusNames = {draft:'故事已保存',lyrics_ready:'歌词已保存',queued:'等待生成',generating:'正在生成',checking:'正在检查',ready:'小歌已就绪',failed:'生成失败',timed_out:'生成超时',cancelled:'已取消'};
const reviewReasonNames = {LYRICS_MISMATCH:'唱词与原文不一致',SINGING_FAILED:'未确认清晰演唱',
  NO_MISSING_WORDS_FAILED:'错唱、漏唱检查未通过',NO_EXTRA_WORDS_FAILED:'额外歌词检查未通过',
  QUALITY_FAILED:'音质检查未通过',COMPLETE_ENDING_FAILED:'结尾完整性检查未通过',
  INPUT_RIGHTS_FAILED:'输入使用权未确认',OUTPUT_RIGHTS_FAILED:'输出使用权未确认',
  DISPLAY_NOT_LICENSED:'展示使用权未确认',PROCESSING_NOT_LICENSED:'调速处理使用权未确认',
  TOO_SHORT:'短于15秒',TOO_LONG:'长于30秒'};
const sourceNames = {manual:'手写',ai:'AI 候选已确认',sample:'原创预置样例'};
const styleNames = {light:'轻快一点',slow:'慢慢摇',groove:'有点律动'};
const sample = {
  title:'冰块先下班',
  story:'今天的方案改了八遍，奶茶里的冰块都先下班了。想把电脑轻轻合上，留一点时间给晚风，也留一点心情给自己。',
  lines:['方案改了八遍，冰块先下班。','奶茶等到常温，我还没吃饭。','把今天轻轻合上，晚风替我转弯。','明天的事明天想，先把月亮看完。']
};
const paths = {
  spark:'m10 2 2.2 5.8L18 10l-5.8 2.2L10 18l-2.2-5.8L2 10l5.8-2.2L10 2Z',
  arrow:'M3 10h14m-5-5 5 5-5 5',
  mic:'M7 5a3 3 0 0 1 6 0v5a3 3 0 0 1-6 0V5Zm-3 5a6 6 0 0 0 12 0M10 16v3M7 19h6',
  pen:'m3 14-1 4 4-1L17 6l-3-3L3 14Zm9-9 3 3',
  lock:'M5 9h10v9H5V9Zm2 0V6a3 3 0 0 1 6 0v3m-3 4v2',
  unlock:'M5 9h10v9H5V9Zm2 0V6a3 3 0 0 1 5.6-1.5M10 13v2',
  plus:'M10 4v12M4 10h12',
  minus:'M4 10h12',
  sun:'M10 1v2m0 14v2M1 10h2m14 0h2M4 4l1 1m10 10 1 1M4 16l1-1M15 5l1-1M14 10a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  wave:'M1 10c3-9 6 9 9 0s6 9 9 0',
  headphones:'M3 12V9a7 7 0 0 1 14 0v3M3 10H1v7h4v-7H3Zm14 0h2v7h-4v-7h2Z',
  check:'m4 10 4 4 8-9',
  folder:'M2 5h6l2 2h8v10H2V5Z',
  close:'m5 5 10 10M15 5 5 15',
  trash:'M3 5h14M7 5V2h6v3M5 5l1 13h8l1-13M8 8v7m4-7v7',
  note:'M8 14V4l9-2v11M8 8l9-2M8 14c0 2-2 3-4 3s-3-2-1-3 5-2 5 0Zm9-1c0 2-2 3-4 3s-3-2-1-3 5-2 5 0Z',
  shield:'m10 2 7 3v5c0 4-7 8-7 8s-7-4-7-8V5l7-3Zm-4 8 3 3 5-6',
  undo:'M7 4 2 9l5 4M2 9h10a5 5 0 0 1 0 10',
  share:'M7 10 15 5M7 10l8 5M7 10a2 2 0 1 1-4 0 2 2 0 0 1 4 0ZM18 4a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm0 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z'
};
const icon = name => `<svg viewBox="0 0 20 20" aria-hidden="true"><path d="${paths[name] || paths.spark}"/></svg>`;
const btn = (action, text, css = '', disabled = false, extra = '') => `<button type="button" class="button ${css}" data-action="${action}" ${disabled ? 'disabled' : ''} ${extra}>${text}</button>`;
const emptyIntent = () => ({theme:'',attitude:'',preserve:'',avoid:'',recipient:''});
const emptyMeta = () => ({title:'',story:'',intent:emptyIntent(),style:'light',tempo:null});
const emptyLines = () => Array.from({length:2}, () => ({text:'',locked:false,locked_spans:[]}));
const metaOf = work => ({title:work.title,story:work.story,intent:clone(work.intent),style:work.style,tempo:clone(work.tempo??null)});
const currentLyric = () => S.work?.lyrics.find(item => item.id === S.work.current_lyric_id);
const currentLines = () => currentLyric()?.lines || emptyLines();
const S = {
  quick:false,view:'create',epoch:0,csrf:'',cap:null,work:null,meta:emptyMeta(),lines:emptyLines(),
  consent:false,source:'manual',sample:false,scene:'',candidate:null,candidateB:null,mixSelections:[],selected:null,history:'',
  instruction:'',operation:null,pollTimer:null,pollController:null,error:'',conflict:false,
  remote:null,editBase:null,availableDraft:null,pendingJob:null,audioId:'',shareURLs:new Map(),listens:new Map(),
  voice:null,pendingClip:null,transcribing:false,lyricsCallUncertain:false,dialogConfirm:null,dialogVersion:0,toastTimer:null,storageWarned:false
};
const baseVersion = () => S.editBase ?? S.work?.version;
const metaDirty = () => !same(S.meta, S.work ? metaOf(S.work) : emptyMeta());
const lyricsDirty = () => !same(S.lines, currentLines());
const isDirty = () => metaDirty() || lyricsDirty() || !!S.candidate || !!S.candidateB || !!S.pendingClip;
const draftKey = (id = S.work?.id || 'new') => `syp:draft:${S.csrf.slice(0,12)}:${id}`;
const jobKey = (id = S.work?.id) => `syp:pending-job:${S.csrf.slice(0,12)}:${id}`;

class APIError extends Error {
  constructor(message, status = 0, code = 'NETWORK_ERROR', requestId = '') {
    super(message); this.status = status; this.code = code; this.requestId = requestId;
  }
}
async function api(path, {method = 'GET', body, signal, publicRequest = false} = {}) {
  const headers = {Accept:'application/json'};
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    if (S.csrf) headers['X-CSRF-Token'] = S.csrf;
  }
  let response;
  try {
    response = await fetch(path, {method,headers,body:body === undefined ? undefined : JSON.stringify(body),
      credentials:publicRequest ? 'omit' : 'same-origin',cache:'no-store',signal});
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new APIError('无法连接本机服务。输入已保留，请检查服务后主动重试。');
  }
  let data;
  try { data = await response.json(); }
  catch { throw new APIError('服务响应无法读取。请求可能已到达，请先核对作品状态。', response.status, 'INVALID_RESPONSE'); }
  if (!response.ok) throw new APIError(data.error?.message || '操作未完成，请重试。', response.status, data.error?.code, data.request_id);
  return data;
}
const workPath = (suffix = '') => `/api/works/${encodeURIComponent(S.work.id)}${suffix}`;
function toast(message, error = false) {
  clearTimeout(S.toastTimer);
  const node = $('#toast');
  node.textContent = message; node.hidden = false; node.classList.toggle('error', error);
  S.toastTimer = setTimeout(() => { node.hidden = true; }, 5500);
}
function showError(error) {
  if (error.name === 'AbortError') return;
  S.error = error.message || String(error);
  S.conflict = error.status === 409 || S.conflict;
  const target = dialog.open ? $('#dialog-error') : $('#page-error');
  if (target) { target.textContent = S.error; target.hidden = false; }
  if (S.conflict && !dialog.open) renderError();
  toast(S.error, true);
}
function clearError() {
  S.error = ''; S.conflict = false; S.remote = null;
  for (const node of $$('#page-error, #dialog-error')) { node.replaceChildren(); node.hidden = true; }
}
function renderError() {
  const node = $('#page-error');
  if (!node) return;
  node.hidden = !S.error;
  node.innerHTML = `${esc(S.error)}${S.conflict && S.view==='create' ? `<p>本地输入和 AI 候选均已保留，不会自动覆盖。</p>${btn('inspect-conflict','查看服务端最新内容','small')}` : ''}`;
}
async function perform(task) {
  if (S.operation) { toast('正在处理上一个操作，请稍候。'); return; }
  const token = Symbol(); const epoch = S.epoch;
  S.operation = token; clearError();
  const controls = $$('button, input, textarea, select', main).concat($$('button, input, textarea, select', dialog));
  const disabled = controls.map(node => [node,node.disabled]);
  controls.forEach(node => { node.disabled = true; });
  main.setAttribute('aria-busy','true');
  const ctx = {valid:() => S.epoch === epoch, epoch};
  try { await task(ctx); }
  catch (error) { if (ctx.valid()) showError(error); }
  finally {
    disabled.forEach(([node,value]) => { if (node.isConnected) node.disabled = value; });
    if (S.operation === token) { S.operation = null; main.removeAttribute('aria-busy'); }
    if (ctx.valid()) updateDirtyIndicators();
  }
}
function storageGet(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; }
}
function storageSet(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); return true; }
  catch {
    if (!S.storageWarned) { toast('浏览器暂存不可用，请及时保存；离开页面可能丢失未保存内容。', true); S.storageWarned = true; }
    return false;
  }
}
function storageRemove(key) { try { sessionStorage.removeItem(key); } catch { /* 暂存不可用不影响服务端保存。 */ } }
function persistDraft() {
  if (!S.csrf || !S.consent) return;
  if (!isDirty()) { if (!S.availableDraft) storageRemove(draftKey()); return; }
  storageSet(draftKey(), {meta:S.meta,lines:S.lines,source:S.source,sample:S.sample,scene:S.scene,
    candidate:S.candidate,candidateB:S.candidateB,mixSelections:S.mixSelections,instruction:S.instruction,base_version:baseVersion() || null,consent:true});
}
function updateDirtyIndicators() {
  const story = $('#story-save-state');
  const lyrics = $('#lyrics-save-state');
  if (story) { story.textContent = metaDirty() || !S.work ? '未保存' : '已保存'; story.classList.toggle('dirty', metaDirty() || !S.work); }
  if (lyrics) { lyrics.textContent = lyricsDirty() || S.candidate || S.candidateB ? '未保存' : (currentLyric() ? '已保存' : '尚未写词'); lyrics.classList.toggle('dirty', lyricsDirty() || !!S.candidate || !!S.candidateB); }
  const count = $('#story-count');
  if (count) { count.textContent = `${size(S.meta.story)} / 500 字`; count.classList.toggle('over-limit',size(S.meta.story)>500); }
  const sampleNotice = $('#sample-lyric-notice');
  if (sampleNotice) sampleNotice.hidden = S.source !== 'sample';
  document.title = `${isDirty() ? '• ' : ''}${S.work?.title || '说一拍'} · 说一拍`;
}
function validateText(text, label, maximum, optional = false) {
  if ((!optional && !text.trim()) || size(text) > maximum || /[\uD800-\uDFFF]/u.test(text)) {
    throw new Error(`${label}请填写${optional ? '0' : '1'}—${maximum}个字符。`);
  }
}
function validateMeta() {
  validateText(S.meta.story, '故事', 500);
  validateText(S.meta.title || '我的一首小歌', '标题', 80);
  for (const [key,value] of Object.entries(S.meta.intent)) validateText(value, {theme:'主题',attitude:'态度',preserve:'必留原句',avoid:'避免词条',recipient:'写给谁'}[key],500,true);
}
function validateLines(lines) {
  if (lines.length < 2 || lines.length > 4) throw new Error('歌词需要 2—4 行。');
  for (const [index,line] of lines.entries()) {
    validateText(line.text, `第 ${index+1} 行歌词`,160);
    if (/[\r\n\v\f\x85\u2028\u2029]/u.test(line.text)) throw new Error(`第 ${index+1} 行不能包含换行，请拆成单独一行。`);
  }
}
function ensureWork() { if (!S.work) throw new Error('请先勾选同意并确认这段故事。'); }
function ensureSavedLyrics() {
  ensureWork();
  if (lyricsDirty()) throw new Error('歌词有未保存修改。请先保存歌词，再修改锁定、恢复版本或请求 AI；输入不会丢失。');
}
function ensureSavedAll() {
  ensureSavedLyrics();
  if (metaDirty()) throw new Error('故事、意图或音乐感觉有未保存修改，请先保存。');
  if (S.candidate || S.candidateB) throw new Error('还有待确认的 AI 候选，请先应用或丢弃。');
}
function adoptWork(work, {meta = false, lyrics = false} = {}) {
  S.work = work; S.editBase = null;
  if (meta) S.meta = metaOf(work);
  if (lyrics) {
    S.lines = clone(currentLines()); S.source = currentLyric()?.source || 'manual';
    S.candidate = null; S.candidateB = null; S.mixSelections = []; S.selected = null; S.history = '';
  }
  persistDraft();
}
function mergeAux(work) {
  if (!S.work || S.work.id !== work.id) return;
  if (work.version > S.work.version) {
    S.remote = work; S.conflict = true;
    S.error = '另一处操作更新了作品。当前输入未被替换，请对照最新内容。';
  }
  S.work.audios = work.audios; S.work.jobs = work.jobs; S.work.shares = work.shares;
  S.work.input_clips = work.input_clips || []; S.work.status = work.status;
}
function stopPoll() {
  clearTimeout(S.pollTimer); S.pollTimer = null;
  S.pollController?.abort(); S.pollController = null;
}
function pauseAudio() { stopBeat(); for (const audio of $$('audio')) audio.pause(); }
function clearPendingClip() {
  if (S.pendingClip) URL.revokeObjectURL(S.pendingClip.url);
  S.pendingClip = null;
}
function leaveAllowed() {
  if ((isDirty() || S.voice) && !window.confirm('还有未保存内容或正在进行的录音。离开此处吗？未保存音频只在当前页面内存中，离开后会丢失；录音将停止。')) return false;
  persistDraft(); return true;
}
function resetScope() {
  stopBeat();tapTimes=[];comparisonData=null,comparisonRequest=0;
  stopStepTracking();
  S.epoch++; S.operation = null; stopPoll(); pauseAudio(); closeDialog(); clearPendingClip();
  main.removeAttribute('aria-busy'); clearError();
}
function focusMain() { main.focus({preventScroll:true}); window.scrollTo({top:0,behavior:'instant'}); }
function navState() {
  const nav = {'create':'nav-create','library':'nav-library','status':'nav-status','diagnostics':'nav-diagnostics','diary':'nav-diary'}[S.view];
  for (const node of $$('.main-nav button')) {
    if (node.dataset.action === nav) node.setAttribute('aria-current','page'); else node.removeAttribute('aria-current');
  }
}
async function navigate(view, {fresh = false, force = false} = {}) {
  if (!force && !leaveAllowed()) return;
  resetScope(); S.view = view; navState();
  if (fresh) {
    S.work = null; S.editBase = null; S.meta = emptyMeta(); S.lines = emptyLines(); S.consent = false;
    S.source = 'manual'; S.sample = false; S.scene = ''; S.candidate = null; S.candidateB = null; S.mixSelections = []; S.lyricsCallUncertain = false;
    S.selected = null; S.history = ''; S.instruction = ''; S.pendingJob = null; S.audioId = '';
    S.availableDraft = storageGet(draftKey());
  }
  if (view === 'create') { renderEditor(); startPoll(); }
  if (view === 'library') await renderLibrary();
  if (view === 'diary') await renderDiary();
  if (view === 'status') renderStatus();
  if (view === 'diagnostics') await renderDiagnostics();
  if (view === 'create') sendEvent('create_entry_view',{source:'client'});
  focusMain();
}
function errorMarkup() { return '<div id="page-error" class="error-box" role="alert" hidden></div>'; }
function capabilityText(feature) { return S.cap ? (S.cap[feature] ? ({singing:'可发起候选',lyrics:'可请求候选',speech:'可请求转写'}[feature] || '已接入') : '未接入') : '状态未知'; }
function heroMarkup() {
  return `<section class="hero" aria-labelledby="hero-title"><div><div class="eyebrow">A LITTLE PLAY, A LITTLE SONG</div><h1 id="hero-title">今天的故事，<br>值得<em>一首歌。</em></h1><p>随口说一句，让生活有点旋律。<br>不用会写歌，也不用很隆重。</p><div class="actions">${btn('quick-enter','试试三步简易版','primary')}${storageGet(quickKey())?.work_id?btn('quick-resume','继续上次简易体验','quiet'):''}</div><div class="hero-tags"><span>${icon('pen')}从两句开始</span><span>${icon('lock')}喜欢的原话，留住</span></div></div><img class="hero-art" src="/record.svg" alt="淡紫唱片机上转着一张薄荷色唱片"></section>`;
}
function renderEditor() {
  if(S.quick)return renderQuick();
  pauseAudio();
  main.innerHTML = `${heroMarkup()}<div class="studio"><aside class="step-rail" aria-label="创作步骤"><button type="button" data-action="step-story" class="active"><span class="step-number">01</span>说故事</button><button type="button" data-action="step-lyrics"><span class="step-number">02</span>写两句</button><button type="button" data-action="step-music"><span class="step-number">03</span>成小歌</button></aside><div class="workspace">${errorMarkup()}${draftMarkup()}<section class="card" id="story-section" aria-labelledby="story-heading">${storyMarkup()}</section><section class="card" id="lyrics-section" aria-labelledby="lyrics-heading"></section><section class="card" id="music-section" aria-labelledby="music-heading"></section></div><aside class="aside">${asideMarkup()}</aside></div>`;
  renderLyrics(); renderMusic(); renderError(); updateDirtyIndicators();
  startStepTracking();
}
let stepFrame = null;
let stepResizeObserver = null;
function stopStepTracking() {
  if (stepFrame !== null) cancelAnimationFrame(stepFrame);
  stepFrame = null;
  stepResizeObserver?.disconnect(); stepResizeObserver = null;
}
function updateActiveStep() {
  if (S.view !== 'create' || !$('.step-rail',main)) return;
  const steps = ['story','lyrics','music'];
  const activationLine = Math.min(180,Math.max(64,window.innerHeight * .28));
  let active = 'story';
  for (const step of steps) {
    if ($(`#${step}-section`,main)?.getBoundingClientRect().top <= activationLine) active = step;
  }
  // The last section may be too short to reach the activation line.
  if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) active = 'music';
  for (const button of $$('.step-rail button',main)) {
    const current = button.dataset.action === `step-${active}`;
    button.classList.toggle('active',current);
    if (current) button.setAttribute('aria-current','step'); else button.removeAttribute('aria-current');
  }
}
function scheduleStepUpdate() {
  if (stepFrame !== null || S.view !== 'create' || !$('.step-rail',main)) return;
  stepFrame = requestAnimationFrame(()=>{stepFrame = null; updateActiveStep();});
}
function startStepTracking() {
  stopStepTracking();
  if (window.ResizeObserver) {
    stepResizeObserver = new ResizeObserver(scheduleStepUpdate);
    for (const step of ['story','lyrics','music']) stepResizeObserver.observe($(`#${step}-section`,main));
  }
  scheduleStepUpdate();
}
window.addEventListener('scroll',scheduleStepUpdate,{passive:true});
window.addEventListener('resize',scheduleStepUpdate,{passive:true});
function draftMarkup() {
  return S.availableDraft ? `<div class="notice warning"><strong>这个标签页留着一份未提交草稿。</strong><p>不会自动作为你的作品，也不会自动发送给模型。</p><div class="actions">${btn('resume-draft','恢复草稿','small')}${btn('discard-draft','丢弃暂存','small quiet')}</div></div>` : '';
}
function clipMarkup(role) {
  const saved = S.work?.input_clips?.find(clip => clip.role === role);
  const pending = S.pendingClip?.role === role ? S.pendingClip : null;
  const label = role === 'story' ? '故事原音' : '修改要求原音';
  return `<div class="input-clips" aria-label="${label}">${pending ? `<div class="clip-card"><strong>待保存的${label} · ${pending.source === 'record' ? '现场录音' : '本地文件'}</strong><audio controls preload="metadata" src="${esc(pending.url)}" aria-label="试听待保存的${label}"></audio><p>仅存在当前页面内存中，不会自动转写或发送给音乐模型。${S.work ? '点保存后，仅绑定到本机作品。' : '请先把故事文字填好并确认作品，才能保存这段原音。'}</p><div class="actions">${S.work ? btn('save-input-clip','保存原音到本机','small primary',false,`data-role="${role}"`) : ''}${btn('discard-input-clip','丢弃待保存原音','small quiet',false,`data-role="${role}"`)}</div></div>` : ''}${saved ? `<div class="clip-card"><strong>已保存的${label} · ${saved.source === 'record' ? '现场录音' : '本地文件'}</strong><span class="inline-small">${Math.round(saved.duration)} 秒 · ${formatDate(saved.created_at)}</span><audio controls preload="none" src="/api/input-clips/${encodeURIComponent(saved.id)}/audio" aria-label="试听已保存的${label}"></audio><div class="actions">${btn('remove-input-clip','删除这段原音','small quiet',false,`data-clip-id="${esc(saved.id)}"`)}</div></div>` : ''}</div>`;
}
function storyMarkup() {
  const m = S.meta;
  return `<div class="section-heading"><div><span class="section-index">01 / 你的日常</span><h2 id="story-heading">今天，有什么想说的？</h2><p>一件小事、一种心情，或者一个想念的人。</p></div><span id="story-save-state" class="save-badge">未保存</span></div>
    ${S.sample ? '<div class="notice purple"><strong>原创预置样例，非实时 AI 生成</strong><p>可编辑后保存；只有主动确认，才会成为你的本机作品。</p></div>' : ''}
    <label class="form-label" for="story">你的故事</label><textarea id="story" placeholder="比如：忙了一天，回家路上的晚风刚刚好。" aria-describedby="story-hint story-count">${esc(m.story)}</textarea><div class="field-meta"><span id="story-hint">不必押韵，先把想说的写下来。</span><span id="story-count">0 / 500 字</span></div>
    <div class="chips" aria-label="选择创作场景">${['轻松吐槽','记录心情','写给朋友'].map(scene => `<button type="button" class="chip" data-action="scene" data-scene="${scene}" aria-pressed="${S.scene===scene}">${scene}</button>`).join('')}</div>
    <div class="creation-guide" aria-label="从声音到歌词的创作路径"><strong>从一句话，到自己的歌词</strong><ol><li>录音或选文件，先试听；转写须单独勾选并可能计费</li><li>核对转写建议、确认故事文字，再保存作品</li><li>在故事中选中想保留的原话，设为必留并保存意图</li><li>主动请求 AI 歌词候选，对照后才应用新版本</li></ol></div><div class="actions">${btn('voice',`${icon('mic')}录一段 / 上传音频`,'small')}${btn('capture-preserve','把选中的原话设为必留','small quiet')}${!S.work ? btn('sample','试试原创歌词样例','small quiet') : ''}</div><p class="privacy-note">先在故事文字中选中一段原话，再点“设为必留”。无需 AI 也可手写歌词；原音不会自动作为生成歌曲的旋律或声音样本。</p>${clipMarkup('story')}
    <details class="details" ${S.work ? 'open' : ''}><summary>给这首小歌一点方向 <span class="optional">· 可编辑</span></summary><p>以下内容由你填写和确认，不是 AI 推断。留空也可以。</p><div class="field"><label class="form-label" for="work-title">小歌标题 <span class="optional">· 最多 80 字</span></label><input class="input" id="work-title" value="${esc(m.title)}" placeholder="我的一首小歌"></div><div class="intent-grid">${intentField('theme','主题','比如：下班后的小确幸')}${intentField('attitude','态度','比如：轻松一点，不要煽情')}${intentField('preserve','必留原句','每行一条，歌词中必须逐字包含',true)}${intentField('avoid','避免词条','每行一条，仅匹配精确文字',true)}${intentField('recipient','写给谁 · 选填','比如：一个朋友')}</div><p class="privacy-note">必留与避免项按精确文字检查，不做语义审核；请自行检查内容是否合适。</p></details>
    ${!S.work ? `<label class="consent"><input id="service-consent" type="checkbox" ${S.consent?'checked':''}><span>我同意将故事和歌词保存在本机。仅当我主动请求已配置模型生成或转写时，相关素材才会外传；本应用不用于训练；模型服务数据政策须由接入方确认。此浏览器会话有效期为 30 天，清除 Cookie、会话到期或换设备会失去访问。请勿填写敏感个人信息。</span></label>` : '<p class="privacy-note">已确认本机保存。模型仅在你主动生成或转写时接收相关素材。本应用不用于训练；模型服务数据政策须由接入方确认。访问依赖当前浏览器的 30 天会话。</p>'}
    <div class="actions end">${S.work ? btn('save-story','保存故事与意图','primary') : btn('create',`确认这段故事 ${icon('arrow')}`,'primary wide')}</div><p class="privacy-note">${S.work ? '已确认数据写入本机 SQLite；未保存修改仅在当前标签页暂存。' : '未同意前，不暂存输入，不调用模型。'}</p>`;
}
function capturePreserve() {
  const story = $('#story');
  if (!story) return;
  const phrase = story.value.slice(story.selectionStart,story.selectionEnd).trim();
  if (!phrase || size(phrase)>80 || /[\r\n\v\f\u2028\u2029]/u.test(phrase)) throw new Error('请在故事里选中一段不换行、最多 80 字的原话。');
  const previous = S.meta.intent.preserve.split('\n').filter(Boolean);
  if (previous.includes(phrase)) { toast('这段原话已经在必留列表中。'); return; }
  const next = [...previous,phrase].join('\n');
  validateText(next,'必留原句',500,true);
  S.meta.intent.preserve = next;
  $('#intent-preserve').value = next;
  persistDraft(); updateDirtyIndicators();
  toast('已加入必留原句；请先保存故事与意图，再主动请求歌词候选。');
}
function intentField(key,label,placeholder,multiline=false) {
  return `<div class="field ${key==='recipient'?'full':''}"><label class="form-label" for="intent-${key}">${label}</label>${multiline ? `<textarea id="intent-${key}" data-intent="${key}" placeholder="${placeholder}">${esc(S.meta.intent[key])}</textarea>` : `<input class="input" id="intent-${key}" data-intent="${key}" value="${esc(S.meta.intent[key])}" placeholder="${placeholder}">`}</div>`;
}
function asideMarkup() {
  return `<section class="note-card lavender">${icon('spark')}<div class="eyebrow">KEEP IT YOURS</div><h3>像你说的，才是你的。</h3><p>不用每句都漂亮。<br>那句有点笨拙、却很真实的话，<br>也值得留在歌里。</p><hr class="mini-divider"><p>写好后，锁住一句或选中一段。<br>后续修改会保护这些原文。</p></section><section class="note-card">${icon('note')}<h3>小小工作室 · 接入情况</h3>${[['lyrics','AI 写词'],['speech','语音转写'],['singing','真实演唱']].map(([key,label])=>`<div class="service-row"><span>${label}</span><span class="service-state ${S.cap?.[key]?'on':''}">${capabilityText(key)}</span></div>`).join('')}<hr class="mini-divider"><p>手写、录音、上传原音及本机私有试听已开放。AI 识别/写词只在明确授权后调用，真实效果尚待验收。<br>${S.cap?.song_preview_only?'成歌先进入私有试听，人工核对唱词、音质、时长和使用权后可转正式作品。':'没有接入，就不假装生成。'}</p><button type="button" class="link-button" data-action="nav-status">查看接入说明</button></section><div class="note-card">${icon('shield')}<h3>先在自己的小天地里。</h3><p>这是本地开发版，不是云端账号。分享链接也仅能在当前机器访问。</p></div>`;
}
function renderLyrics() {
  if(S.quick)return renderQuick();
  const node = $('#lyrics-section'); if (!node) return;
  const current = currentLyric();
  node.innerHTML = `<div class="section-heading"><div><span class="section-index">02 / 写成两句</span><h2 id="lyrics-heading">这几句，很有你的味道。</h2><p>喜欢的留住，其他的我们一起改。</p></div><span id="lyrics-save-state" class="save-badge">尚未写词</span></div>${!S.work ? `<div class="lyrics-placeholder">${icon('pen')}<h3>先说故事，再写自己的两句。</h3><p>确认故事后，可以手写 2—4 行歌词。不会自动生成或保存样例。</p>${S.sample ? `<ol class="sample-preview">${sample.lines.map(line=>`<li>${esc(line)}</li>`).join('')}</ol><p>原创预置样例，非实时 AI 生成。确认故事后仍需主动保存歌词。</p>` : ''}</div>` : `
    <div class="notice ${S.cap?.lyrics?'purple':''}">${S.cap?.lyrics ? 'AI 写词可请求 TokenHub 文字候选，可能计费；仅主动确认后发送故事、意图和歌词。模型实测质量待验收，候选需你对照确认后才保存。' : 'AI 写词未接入，先手写 / 体验预置样例。当前不会调用模型，也不会生成假音频。'}</div>
    ${current ? `<div class="history-bar"><label for="lyric-history">版本记录</label><select id="lyric-history"><option value="">当前 v${current.number} · ${sourceNames[current.source]}</option>${[...S.work.lyrics].reverse().filter(item=>item.id!==current.id).map(item=>`<option value="${esc(item.id)}" ${S.history===item.id?'selected':''}>v${item.number} · ${sourceNames[item.source]} · ${formatDate(item.created_at)}</option>`).join('')}</select>${btn('undo',`${icon('undo')}撤销至父版本`,'small quiet',!current.parent_id)}</div><div id="history-preview"></div>` : ''}
    <div class="notice purple" id="sample-lyric-notice" ${S.source==='sample'?'':'hidden'}>原创预置样例，非实时 AI 生成。修改后将按手写保存；不含演示音频。</div>
    <div class="lyric-list">${S.lines.map(lineMarkup).join('')}</div><div class="actions">${btn('add-line',`${icon('plus')}加一句`,'small',S.lines.length>=4)}<span class="inline-small">2—4 行 · 建议每行 6—18 字，最多 160 字</span></div>
    <div class="actions end">${btn('save-lyrics','保存这版歌词','primary')}</div><p class="privacy-note">每次保存都会新建歌词版本，旧版本不会被修改。锁定操作需与改字分开保存。</p>
    <div class="ai-tools"><label class="form-label" for="ai-instruction">${S.selected!==null ? `只改第 ${S.selected+1} 句` : '整首改写'} · 你的修改要求</label><textarea id="ai-instruction" placeholder="比如：更像随口说的话，不要太用力。">${esc(S.instruction)}</textarea><div class="actions">${btn('generate-lyrics',`${icon('spark')}请求候选 A`,'purple',!S.cap?.lyrics||!!S.candidate||!!S.candidateB)}${btn('generate-lyrics-b','再请求候选 B · 单独计费','small',!S.cap?.lyrics||!S.candidate||!!S.candidateB||S.candidate.base_version!==S.work.version)}${btn('voice-instruction',`${icon('mic')}说修改要求`,'small')}${S.selected!==null ? btn('clear-selection','改整首','small quiet') : ''}</div><p class="privacy-note">点选已保存的一行，可只改这一句。当前选择：${S.selected!==null?`第 ${S.selected+1} 句`:'整首'}。生成前请先保存所有修改。语音或上传音频只在你主动操作时录制、选取或保存；没有转写服务时可试听并手动填写文字，不会自动调用 AI。</p>${clipMarkup('instruction')}</div><div id="candidate-panel"></div>`}`;
  renderHistory(); renderCandidate(); updateDirtyIndicators();
}
function lineMarkup(line,index) {
  return `<div class="lyric-row ${line.locked?'is-locked':''} ${S.selected===index?'selected':''}" data-line-row="${index}"><div class="lyric-topline"><label for="line-${index}">第 ${String(index+1).padStart(2,'0')} 句${line.locked?' · 整句已锁':''}</label><div class="line-tools"><button type="button" class="icon-button" data-action="toggle-lock" data-index="${index}" aria-label="${line.locked?'解锁':'锁定'}第 ${index+1} 句" title="${line.locked?'解锁这句':'逐字锁定这句'}" ${!line.id?'disabled':''}>${icon(line.locked?'lock':'unlock')}</button><button type="button" class="icon-button" data-action="remove-line" data-index="${index}" aria-label="移除第 ${index+1} 句" ${S.lines.length<=2||line.locked||line.locked_spans.length?'disabled':''}>${icon('minus')}</button></div></div><textarea class="lyric-text" id="line-${index}" data-line="${index}" rows="2" aria-describedby="line-count-${index}" placeholder="写一句你想唱的话…" ${line.locked?'readonly':''}>${esc(line.text)}</textarea><div class="line-bottom"><button type="button" class="link-button" data-action="lock-span" data-index="${index}" ${!line.id||line.locked?'disabled':''}>${icon('lock')}锁住选中的原文</button>${btn('compare-line','原话 / 版本对照试听','small quiet',!line.id,`data-index="${index}"`)}<span id="line-count-${index}">${size(line.text)} / 160 字</span></div>${line.locked_spans.length ? `<div class="span-locks">${line.locked_spans.map((span,spanIndex)=>`<span class="span-tag">${esc(span)}<button type="button" data-action="remove-span" data-index="${index}" data-span-index="${spanIndex}" aria-label="解除片段锁：${esc(span)}">×</button></span>`).join('')}</div>` : ''}</div>`;
}
function renderHistory() {
  const node = $('#history-preview'); if (!node) return;
  const history = S.work.lyrics.find(item => item.id === S.history);
  node.innerHTML = history ? `<div class="history-preview"><strong>只读预览 · v${history.number}</strong><ol>${history.lines.map(line=>`<li>${esc(line.text)}${line.locked?'（已锁）':''}</li>`).join('')}</ol><p>下面仍是当前编辑版本，不会因预览丢失输入。恢复会创建新版本，且不能绕过当前锁定。</p>${btn('restore-history','恢复这一版','small')}</div>` : '';
}
function renderCandidate() {
  const node = $('#candidate-panel'); if (!node) return;
  if (!S.candidate) { node.replaceChildren(); return; }
  const old = currentLyric()?.lines || [];
  const a = S.candidate, b = S.candidateB, mixable = canMixCandidates();
  const stale = a.base_version!==S.work.version || (b && b.base_version!==S.work.version);
  const column = (candidate,label) => `<section class="candidate-column"><h4>候选 ${label}</h4>${candidate.lines.map((line,index)=>{const before=old.find(item=>item.id===line.id);return `<div class="diff-row"><small>第 ${index+1} 句 · ${before?.text===line.text?'未改动':before?'已修改':'新增'}</small>${before&&before.text!==line.text?`<del>${esc(before.text)}</del>`:''}<ins>${esc(line.text)}</ins></div>`;}).join('')}${btn(`apply-candidate-${label.toLowerCase()}`,`整版采用 ${label}`,'small',stale)}</section>`;
  node.innerHTML = `<div class="candidate"><h3>两版对照，每句由你决定。</h3><p>候选均未保存。${stale?'作品已更新；旧候选仅供对照，不能直接应用。':'A/B 是独立请求，B 需单独确认可能产生费用。'}${b&&!mixable?' 两版行数或稳定行 ID 不一致，只能整版采用，不能逐句拼接。':''}</p><div class="candidate-grid">${column(a,'A')}${b?column(b,'B'):''}</div>${b&&mixable?`<div class="mix-board"><h4>逐句采纳</h4>${a.lines.map((line,index)=>`<div class="mix-row"><strong>第 ${index+1} 句</strong><label><input type="radio" name="mix-${index}" data-mix="${index}" value="A" ${S.mixSelections[index]!=='B'?'checked':''}> A</label><label><input type="radio" name="mix-${index}" data-mix="${index}" value="B" ${S.mixSelections[index]==='B'?'checked':''}> B</label><span>${esc(S.mixSelections[index]==='B'?b.lines[index].text:line.text)}</span></div>`).join('')}${btn('apply-mixed','保存逐句组合为新版本','primary',stale)}</div>`:''}<div class="actions">${btn('discard-candidate','丢弃 A/B 候选','quiet')}</div><p class="privacy-note">最终组合仍经过服务端版本、必留原句、锁句与片段保护检查。不会自动生成音频。</p></div>`;
}
let tapTimes=[],beatTimer=null,beatContext=null,beatRunning=false,comparisonData=null,comparisonRequest=0;
function stopBeat(){clearTimeout(beatTimer);beatTimer=null;beatRunning=false;beatContext?.suspend();const button=$('[data-action="beat-preview"]');if(button)button.textContent="试听节拍";}
function clickBeat(){
  beatContext ||= new (window.AudioContext||window.webkitAudioContext)();beatContext.resume();
  const oscillator=beatContext.createOscillator(),gain=beatContext.createGain(),at=beatContext.currentTime;
  oscillator.frequency.value=880;gain.gain.setValueAtTime(.12,at);gain.gain.exponentialRampToValueAtTime(.001,at+.035);oscillator.connect(gain);gain.connect(beatContext.destination);oscillator.start(at);oscillator.stop(at+.04);
}
function renderTempo(){
  const node=$('#tempo-panel');if(!node)return;
  node.innerHTML=`<div class="rhythm-panel"><h3>拍出你想唱的节奏</h3><p>连续拍 4—16 下。先试听节拍，再保存；生成时带入 BPM 和拍击间隔，模型是否遵循需试听核对。</p><div class="actions">${btn('tap-beat','拍一下 · 空格也可以','primary')}${btn('reset-taps','重新拍','small')}${btn('beat-preview',beatRunning?'停止节拍':'试听节拍','small',!S.meta.tempo)}${btn('clear-tempo','清除节奏','small',!S.meta.tempo)}</div><p id="tap-status" role="status">${S.meta.tempo?`${S.meta.tempo.bpm} BPM · ${S.meta.tempo.taps_ms.length} 下`:'还没有节奏，至少拍 4 下'}${tapTimes.length&&tapTimes.length<4?` · 已拍 ${tapTimes.length} 下`:''}</p>${btn('save-tempo','保存这次节奏','small',!S.work||same(S.meta.tempo??null,S.work.tempo??null))}<p class="micro-note">保存的节奏：${S.work?.tempo?S.work.tempo.bpm+' BPM':'未设定'}。每次音乐请求绑定当时的歌词、感觉和节奏。</p></div>`;
}
async function rhythmAction(action){
  if(action==='tap-beat'){
    const now=Math.round(performance.now());if(tapTimes.length&&now-tapTimes.at(-1)>1500)tapTimes=[];
    if(tapTimes.length&&now-tapTimes.at(-1)<300){toast('拍得太快了，请保持 0.3—1.5 秒间隔。');return;}
    tapTimes.push(now);tapTimes=tapTimes.slice(-16);const tempo=tapTempo(tapTimes);if(tempo){S.meta.tempo=tempo;persistDraft();}renderTempo();$('[data-action="tap-beat"]')?.focus();updateDirtyIndicators();return;
  }
  if(action==='reset-taps'){tapTimes=[];stopBeat();renderTempo();return;}
  if(action==='clear-tempo'){tapTimes=[];S.meta.tempo=null;stopBeat();persistDraft();renderTempo();updateDirtyIndicators();return;}
  if(action==='beat-preview'){
    if(beatRunning){stopBeat();renderTempo();return;}pauseAudio();beatRunning=true;
    const bpm=S.meta.tempo.bpm;const tick=()=>{if(!beatRunning)return;clickBeat();beatTimer=setTimeout(tick,60000/bpm);};tick();renderTempo();return;
  }
  if(action==='save-tempo')return perform(async ctx=>{ensureWork();const work=await api(workPath(),{method:'PATCH',body:{base_version:baseVersion(),tempo:S.meta.tempo}});if(!ctx.valid())return;adoptWork(work);renderMusic();updateDirtyIndicators();toast('节奏已保存，下次生成会使用这次节奏。');});
}
function introDialog(){
  ensureSavedAll();pauseAudio();const audio=selectedAudio(),clips=(S.work.input_clips||[]).filter(c=>c.role==='story');
  if(!audio||audio.kind==='voice_intro')throw new Error('请选择一首原始正式歌曲，再添加原声开场。');
  if(!clips.length)throw new Error('先在“说故事”录音或上传原音，经确认保存在本作品中，再添加开场。');
  const phrase=audio.lyrics.map(l=>l.text).find(t=>S.work.intent.preserve.includes(t))||S.work.intent.preserve.split('\n')[0]||'';
  openDialog('把我的原话放进歌曲',`<p>本机选取 0.3—6 秒原音，间隔 0.12 秒后接上完整歌曲，不发送给模型。带开场总时长会比原短歌长。</p><label>故事原音<select id="intro-clip">${clips.map(c=>`<option value="${esc(c.id)}" data-duration="${c.duration}">${c.source==='record'?'录音':'上传'} · ${c.duration.toFixed(2)} 秒</option>`).join('')}</select></label><audio id="intro-source-player" controls preload="none" src="/api/input-clips/${encodeURIComponent(clips[0].id)}/audio"></audio><div class="intro-range"><label>开始秒数<input class="input" id="intro-start" type="number" min="0" step="0.01" value="0"></label><label>结束秒数<input class="input" id="intro-end" type="number" min="0.3" step="0.01" value="${Math.min(3,clips[0].duration).toFixed(2)}"></label></div><div class="actions">${btn('preview-intro-range','试听选取的原话','small')}</div><label class="field">实际说出的原话（须出现在这版歌曲歌词中）<input class="input" id="intro-phrase" maxlength="160" value="${esc(phrase)}"></label><details><summary>歌曲绑定的唱词</summary><ol>${audio.lyrics.map(l=>`<li>${esc(l.text)}</li>`).join('')}</ol></details>${[['heard_phrase_confirmed','已听选取片段，确实说出了上面的原话'],['voice_rights','有权使用这段声音'],['processing_rights','歌曲许可允许本次本机拼接'],['share_voice','允许把原声随歌曲分享'],['export_voice','允许把原声随歌曲导出']].map(([key,label])=>`<label class="consent"><input id="intro-${key}" type="checkbox"><span>${label}</span></label>`).join('')}<p>分享、导出同时受原歌曲许可限制。删除或替换这段原音，也会删除相关开场版本并使其分享失效。</p>`,async ctx=>{
    const body={base_version:baseVersion(),clip_id:$('#intro-clip').value,start_seconds:Number($('#intro-start').value),end_seconds:Number($('#intro-end').value),phrase:$('#intro-phrase').value,confirm:true};
    for(const key of ['heard_phrase_confirmed','voice_rights','processing_rights','share_voice','export_voice'])body[key]=$('#intro-'+key).checked;
    const composed=await api(workPath(`/audios/${encodeURIComponent(audio.id)}/intro`),{method:'POST',body});if(!ctx.valid())return;
    const work=await api(workPath());if(!ctx.valid())return;S.work=work;S.audioId=composed.id;closeDialog();renderMusic();toast('原声开场版已保存，请试听后再保留或分享。');
  },'确认本机拼接');
}
function previewIntroRange(){
  const player=$('#intro-source-player'),start=Number($('#intro-start').value),end=Number($('#intro-end').value),duration=Number($('#intro-clip').selectedOptions[0].dataset.duration);
  if(!Number.isFinite(start)||!Number.isFinite(end)||start<0||end>duration||end-start<.3||end-start>6)throw new Error('请选择录音内 0.3—6 秒的片段。');
  pauseAudio();player.currentTime=start;player.ontimeupdate=()=>{if(player.currentTime>=end){player.pause();player.ontimeupdate=null;}};player.play().catch(()=>toast('请点击播放器开始试听。',true));
}
function comparisonColumn(side,defaultId){
  const timeline=comparisonData.timeline,version=timeline.find(v=>v.lyric_id===defaultId)||timeline[0];
  return `<section class="comparison-column"><label>${side==='left'?'修改前':'修改后'}<select id="comparison-${side}">${timeline.map(v=>`<option value="${esc(v.lyric_id)}" ${v.lyric_id===version.lyric_id?'selected':''}>歌词 v${v.number}${v.text===null?' · 此行不存在':''}</option>`).join('')}</select></label><div id="comparison-audio-${side}"></div></section>`;
}
function renderComparisonAudio(side){
  const version=comparisonData.timeline.find(v=>v.lyric_id===$('#comparison-'+side).value),node=$('#comparison-audio-'+side);
  node.innerHTML=`<blockquote>${version.text===null?'此版本尚未加入或已删除这一行':esc(version.text)}</blockquote>${version.audios.map(a=>`<p>${a.kind==='voice_intro'?'带原声开场':'正式歌曲'} · ${a.duration.toFixed(2)} 秒</p><audio controls preload="none" src="/api/audio/${encodeURIComponent(a.id)}" aria-label="${side==='left'?'修改前':'修改后'}版本 ${version.number} 整曲试听"></audio>`).join('')}${version.candidates.map(c=>`<p>待听审私有候选 · ${esc(styleNames[c.style])}</p><audio controls preload="none" src="/api/jobs/${encodeURIComponent(c.job_id)}/preview"></audio>`).join('')}${!version.audios.length&&!version.candidates.length?'<p>这版尚无音频。保存歌词不会自动生成歌曲。</p>':''}`;
  node.querySelectorAll('audio[src^="/api/audio/"]').forEach((element,index)=>{const audio=S.work.audios.find(a=>a.id===version.audios[index].id);if(audio)bindAudio(element,audio,S.work.id);});
}
async function comparisonDialog(index){
  ensureSavedAll();pauseAudio();const line=S.lines[index];if(!line?.id)throw new Error('先保存这句歌词，再对照历史。');
  const epoch=S.epoch,request=++comparisonRequest,dialogVersion=S.dialogVersion,data=await api(workPath('/comparison')+'?line_id='+encodeURIComponent(line.id));if(epoch!==S.epoch||request!==comparisonRequest||dialogVersion!==S.dialogVersion)return;comparisonData=data;
  openDialog('我的原话，如何变成了这句歌',`<h3>我的原话</h3>${data.origin?`<blockquote>${esc(data.origin.quote)}</blockquote><p>来自故事版本 ${data.origin.story_version}，由你明确关联。</p>`:'<p>尚未关联原话。请从下面已保存故事中逐字选一句，避免把 AI 改写误当原话。</p>'}<details><summary>查看当前故事</summary><p>${esc(data.story)}</p></details><label class="field">关联故事原句<input class="input" id="origin-quote" maxlength="160" value="${esc(data.origin?.quote||'')}"></label>${btn('save-origin','保存原话关联','small',false,`data-line-id="${esc(line.id)}"`)}<h3>第一版歌词 · v${data.first.number}</h3><blockquote>${esc(data.first.text)}</blockquote><p>同一稳定行 ID 的历史；新增、删除不按行号硬配。下面试听完整版本，尚未自动定位单句。</p><div class="comparison-grid">${comparisonColumn('left',data.first.lyric_id)}${comparisonColumn('right',S.work.current_lyric_id)}</div>`,null);
  renderComparisonAudio('left');renderComparisonAudio('right');
}
async function saveOrigin(lid,ctx){
  const quote=$('#origin-quote').value;const work=await api(workPath('/line-origins'),{method:'POST',body:{base_version:baseVersion(),line_id:lid,quote}});if(!ctx.valid())return;adoptWork(work);closeDialog();renderLyrics();renderMusic();toast('原话来源已保存；没有自动修改歌词或调用模型。');
}

function renderMusic() {
  if(S.quick)return renderQuick();
  const node = $('#music-section'); if (!node) return;
  pauseAudio();
  node.innerHTML = `<div class="section-heading"><div><span class="section-index">03 / 加一点旋律</span><h2 id="music-heading">让这段话，摇一摇晃晃。</h2><p>选一种感觉，等真实的声音到来。</p></div>${icon('headphones')}</div><div class="style-options" aria-label="音乐感觉">${[['light','sun','明亮 / 轻盈'],['slow','wave','松弛 / 慢拍'],['groove','headphones','摇摆 / 节奏']].map(([key,shape,sub])=>`<button type="button" class="style-option" data-action="style" data-style="${key}" aria-pressed="${S.meta.style===key}">${icon(shape)}${styleNames[key]}<span>${sub}</span></button>`).join('')}</div>${S.work ? `<div class="actions end">${btn('save-style','保存音乐感觉','small',S.meta.style===S.work.style)}</div>` : ''}<div class="notice">${S.cap?.singing ? (S.cap.song_preview_only ? '可请求 TokenHub 生成真实音频候选，需你确认可能产生费用。返回后仅供本人试听；未核对歌声、唱词及分享授权前，不会作为正式作品或开放分享。供应商仍可能拒绝请求。' : '真实演唱已接入。生成前需确认已保存歌词及素材授权；本地网站不限制每日次数，每次请求仍可能计费。') : '真实演唱未接入。以上三项只是可保存的创作预设，不代表已实现真实音色或演唱风格控制。可以先写词、保存；不提供模拟试听，也不伪造进度。'}</div>${btn('song',`${icon('spark')}把这几句唱出来`,'primary wide',!S.work||!currentLyric()||!S.cap?.singing||(S.cap?.song_preview_only ? S.work.jobs.some(job=>['queued','generating'].includes(job.status)) || S.work.jobs.filter(job=>job.status==='checking').length>=3 || S.work.jobs.some(job=>job.status==='checking'&&job.lyric_id===S.work.current_lyric_id&&job.style===S.work.style&&same(job.snapshot?.tempo??null,S.work.tempo??null)) : S.work.jobs.some(job=>activeStatuses.has(job.status)))||!!S.pendingJob)}${S.pendingJob?`<div class="notice warning"><strong>上一次生成请求的结果尚未明确。</strong><p>不会新建另一请求。重试将沿用原始参数和同一幂等键。</p>${btn('retry-job','核对 / 重试原请求','small')}</div>`:''}<div id="tempo-panel"></div><div id="job-panel"></div><div id="feel-compare"></div><div id="audio-panel"></div>`;
  renderTempo();renderJob(); renderCompare(); renderAudio();
}
function latestJob() { return S.work?.jobs.at(-1); }
function renderJob() {
  if(S.quick)return renderQuickResult();
  const node = $('#job-panel'); if (!node) return;
  const job = latestJob();
  if (!job) { node.replaceChildren(); return; }
  const active = activeStatuses.has(job.status);
  const preview = job.status==='checking' && job.preview_ready;
  node.innerHTML = `<div class="job-panel" role="status"><h3>${preview?'待你试听的私有候选':active?'<span class="activity-dot" aria-hidden="true"></span>'+statusNames[job.status]:icon(job.status==='ready'?'check':'note')+(statusNames[job.status] || '未知任务状态')}</h3>${active?`<div class="job-stages">${['queued','generating','checking'].map(status=>`<span class="${status===job.status?'current':''}">${statusNames[status]}</span>`).join('<span aria-hidden="true">/</span>')}</div>${preview?`<p>这是供应商实际返回的音频，仅供当前浏览器会话试听。请对照歌词逐句试听，再进入审核；审核通过后才能成为正式作品，按授权分享和导出。</p><audio controls preload="none" src="/api/jobs/${encodeURIComponent(job.id)}/preview" aria-label="试听未经核验的成歌候选"></audio><p>不满意可停止接收；重新生成可能再次产生费用。</p>`:'<p>后台处理中的是真实任务，没有预估百分比。你可以离开此页，回来时会恢复状态。</p>'}${preview?btn('review-audio','核对唱词与使用权','small primary',false,`data-job-id="${esc(job.id)}"`):''}${btn('cancel-job',preview?'丢弃私有候选':'停止接收本次生成','small quiet')}`:`<p>${esc(job.error?.message || (job.status==='ready'?'音频已通过服务端检查。点击播放器后才会播放。':'歌词已保留，可以主动重新生成。'))}</p>${job.error?.code?`<p>错误代码：${esc(job.error.code)}</p>`:''}${btn('refresh-job','刷新实际结果','small quiet')}`}<p class="micro-note">任务绑定${styleNames[job.style] || '原'}风格与当时的歌词快照。取消不承诺远端停止或退还已产生的供应商费用。</p></div>`;
}
function renderCompare() {
  const node = $('#feel-compare'); if (!node || !S.work?.current_lyric_id) return;
  const lyricId = S.work.current_lyric_id;
  const previews = S.work.jobs.filter(job=>job.lyric_id===lyricId&&job.status==='checking'&&job.preview_ready);
  const verified = S.work.audios.filter(audio=>audio.lyric_id===lyricId);
  if (!previews.length && !verified.length) {
    node.innerHTML = '<div class="notice">保存同一版歌词，依次切换并保存不同音乐感觉，逐次确认生成后可在这里对照真实音频。当前没有可比的声音；不会播放示意音频。</div>';
    return;
  }
  const cards = [...previews.map(job=>({id:job.id,style:job.style,preview:true,created_at:job.created_at})),
    ...verified.map(audio=>({id:audio.id,style:audio.style,preview:false,created_at:audio.created_at}))]
    .sort((a,b)=>a.created_at.localeCompare(b.created_at));
  node.innerHTML = `<section class="feel-compare"><h3>同一版词，换种感觉听</h3><p>仅对比当前已保存歌词版本；每个播放器都对应真实任务/音频。切换感觉不会改变已有歌词，生成一次仍可能计费。</p><div class="feel-grid">${cards.map(item=>`<article class="feel-card"><span class="pill">${styleNames[item.style]}</span><small>${item.preview?'私有候选 · 未核实唱词/授权':'已核验音频'}</small><audio controls preload="none" src="${item.preview?`/api/jobs/${encodeURIComponent(item.id)}/preview`:`/api/audio/${encodeURIComponent(item.id)}`}" aria-label="试听${styleNames[item.style]}的${item.preview?'私有候选':'已核验音频'}"></audio>${item.preview?btn('review-audio','审核此候选','small primary',false,`data-job-id="${esc(item.id)}"`)+btn('cancel-compare','丢弃此候选','small quiet',false,`data-job-id="${esc(item.id)}"`):''}</article>`).join('')}</div></section>`;
}
async function prepareShort(jobId) {
  openDialog('整曲调速，保留完整内容',`<p>仅在本机处理，不产生模型费用。保持音高，调速幅度限制为 0.8—1.25 倍，保留整曲及处理尾音；偏差过大的曲目需要修改歌词或重新生成。输出为 WAV，原始 MP3 继续保留。</p><label class="consent"><input id="prepare-rights" type="checkbox"><span>我确认相应使用权允许本次调速处理。处理后会重新试听，不直接批准为作品。</span></label>`,async ctx=>{
    const version = S.dialogVersion;
    if (!$('#prepare-rights').checked) throw new Error('请先确认调速处理使用权。');
    await api(`/api/jobs/${encodeURIComponent(jobId)}/prepare`,{method:'POST',body:{confirm:true,processing_rights:true}});
    if (!ctx.valid() || !dialog.open || version!==S.dialogVersion) return;
    closeDialog(); await reviewAudio(jobId);
  },'生成本机调速候选');
}
async function reviewAudio(jobId) {
  const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
  const candidate = job.snapshot?.candidate;
  if (!job.preview_ready || !candidate?.sha256) throw new Error('候选还未就绪，请刷新。');
  const checks = [['singing','听到的是演唱，有清晰人声'],['no_missing_words','逐句核对，无错唱、漏唱'],
    ['no_extra_words','没有额外歌词，包括额外重复'],['quality','音质可用，无异常噪声、爆音'],
    ['complete_ending','歌词和结尾完整，无中途截断'],['input_rights','已核对输入歌词及素材的使用权'],
    ['output_rights','已核对本次输出的供应商条款和使用权'],['display_allowed','允许本机正式作品展示和播放'],
    ['share_allowed','允许分享音频'],['export_allowed','允许导出音频']];
  const prepared = job.snapshot.prepared;
  const selected = prepared || candidate;
  if (prepared) checks.push(['processing_rights','允许本次整曲调速处理，已试听处理后完整唱词与结尾']);
  const expected = job.snapshot.lyrics.map(line=>line.text).join('\n');
  openDialog('逐句试听，再审核这首歌',`<p>审核绑定这份 MP3 和生成时的歌词。系统会完整解码并检查 15—30 秒；过长、过短不通过。可主动尝试轻微整曲调速，保留原始 MP3，不裁切歌词；仍须重新听审。人工确认不等于平台已取得许可。</p><p>本次审核：${prepared?'整曲调速 WAV':'原始 MP3'} · ${esc(prepared?.duration ?? candidate.duration_seconds_probed ?? '未知')} 秒</p><audio controls preload="none" src="/api/jobs/${encodeURIComponent(jobId)}/preview${prepared?'?variant=short':''}"></audio>${prepared?`<details><summary>对照原始 MP3（${esc(candidate.duration_seconds_probed)} 秒）</summary><audio controls preload="none" src="/api/jobs/${encodeURIComponent(jobId)}/preview"></audio></details>`:btn('prepare-short','尝试整曲调速到短歌时长','small quiet',false,`data-job-id="${esc(jobId)}"`)}<div class="conflict-preview">${esc(expected)}</div><div class="field"><label class="form-label" for="heard-lyrics">填写实际听到的全部唱词（含错词、重复和额外歌词）</label><textarea id="heard-lyrics" rows="4" maxlength="2000"></textarea></div>${checks.map(([key,label])=>`<label class="consent"><input type="checkbox" id="audit-${key}"><span>${label}</span></label>`).join('')}<div class="field"><label class="form-label" for="rights-reference">使用权依据：条款链接、版本/日期、许可或授权说明</label><textarea id="rights-reference" rows="3" maxlength="1000"></textarea></div><p class="privacy-note">填写结果如实保存。不通过可重新核对或丢弃；新的生成需要另一次付费确认。分享与导出可分别关闭。</p>`,async ctx=>{
    const body = {confirm:true, variant:prepared?'short':'original', sha256:selected.sha256, heard_lyrics:$('#heard-lyrics').value,
      rights_reference:$('#rights-reference').value};
    const version = S.dialogVersion;
    checks.forEach(([key])=>{body[key]=$(`#audit-${key}`).checked;});
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/review`,{method:'POST',body});
    if (!ctx.valid() || !dialog.open || version!==S.dialogVersion) return;
    if (!result.approved) throw new Error(`审核结果已保存，未通过：${result.reasons.map(code=>reviewReasonNames[code]||'待进一步核对').join('、')}。请如实核对，勿为通过而改写实际唱词。`);
    const work = await api(workPath()); if (!ctx.valid() || !dialog.open || version!==S.dialogVersion) return;
    S.work = work; S.audioId = result.job.audio_id; closeDialog(); renderMusic();
    toast('审核已通过，正式作品已保存。分享和导出按本次授权开放。');
  },'保存审核结果');
}
function selectedAudio() { return S.work?.audios.find(item=>item.id===S.audioId) || S.work?.audios.at(-1); }
function renderAudio() {
  if(S.quick)return renderQuickResult();
  const node = $('#audio-panel'); if (!node) return;
  const audio = selectedAudio();
  if (!audio) { node.innerHTML = '<div class="lyrics-placeholder"><h3>这里，等着你的第一段声音。</h3><p>目前没有可播放音频。歌词保存不等于演唱生成完成。</p></div>'; return; }
  S.audioId = audio.id;
  const lyric = S.work.lyrics.find(item=>item.id===audio.lyric_id);
  const workId = S.work.id;
  node.innerHTML = `<div class="audio-card"><label class="form-label" for="audio-version">听哪一版？</label><select id="audio-version">${[...S.work.audios].reverse().map(item=>`<option value="${esc(item.id)}" ${item.id===audio.id?'selected':''}>歌词 v${S.work.lyrics.find(v=>v.id===item.lyric_id)?.number || '?'} · ${styleNames[item.style]} · ${item.kind==='voice_intro'?'原声开场 · ':''}${item.tempo?item.tempo.bpm+' BPM · ':''}${item.kept?'已保留 · ':''}${formatDate(item.created_at)}</option>`).join('')}</select><div class="audio-heading"><span class="mini-record" aria-hidden="true"></span><div><h3>听听这一版 · 歌词 v${lyric?.number || '?'}</h3><p>${audio.kind==='voice_intro'?'本人原声开场 + AI 演唱':'AI 生成演唱'} · ${styleNames[audio.style]} · ${Math.round(audio.duration)} 秒${audio.kind==='voice_intro'?' · 原声开场 '+audio.intro.duration.toFixed(2)+' 秒 + 完整歌曲':''}${audio.tempo?' · 目标 '+audio.tempo.bpm+' BPM':''}${audio.lyric_id!==S.work.current_lyric_id?' · 非当前歌词版本':''}</p></div></div><audio id="work-audio" controls preload="none" src="/api/audio/${encodeURIComponent(audio.id)}" aria-label="试听已生成的小歌"></audio><details class="snapshot"><summary>这段演唱绑定的歌词快照</summary><ol>${audio.lyrics.map(line=>`<li>${esc(line.text)}</li>`).join('')}</ol></details>${audio.audit?`<details class="snapshot"><summary>音频审核及使用范围</summary><p>审核方式：本人逐句试听确认 · ${formatDate(audio.audit.reviewed_at)}</p><p>展示：${audio.display_allowed?'允许':'未授权'}；分享：${audio.share_allowed?'允许':'未授权'}；导出：${audio.export_allowed?'允许':'未授权'}</p><p>${esc(audio.audit.rights_reference)}</p><p class="micro-note">音频 SHA-256：${esc(audio.audit.inspection.sha256)}</p></details>`:''}<div class="actions">${btn('add-intro','加入我的原声开场','small',audio.kind==='voice_intro')}${btn('keep-audio',audio.kept?`${icon('check')}已保留`:'保留这一版','small',audio.kept)}${btn('share',`${icon('share')}分享`,'small',audio.share_allowed===false)}${audio.export_allowed?`<a class="button small" href="/api/audio/${encodeURIComponent(audio.id)}?download=1" download>导出 ${audio.mime==='audio/mpeg'?'MP3':'WAV'}</a>`:'<span class="inline-small">当前音频未获导出授权</span>'}</div><p class="privacy-note">播放器仅由你的点击启动；切换歌词预览或音频版本会暂停旧音频。</p></div><div id="shares-panel">${sharesMarkup()}</div><div class="feedback"><label for="feedback-category">这一版听起来</label><select id="feedback-category"><option value="">选一个反馈类别</option value="lyrics_mismatch">歌词不匹配</option><option value="audio_quality">音频质量问题</option><option value="style_mismatch">风格不匹配</option><option value="other">其他问题</option></select>${btn('feedback','提交反馈','small')}</div><p class="privacy-note">仅收集类别，不收集反馈正文。</p>`;
  bindAudio($('#work-audio'), audio, workId);
}
function sharesMarkup() {
  if (!S.work.shares.length) return '';
  return `<h3 class="inline-small">已创建的分享</h3>${[...S.work.shares].reverse().map(share=>{
    const expired = new Date(share.expires_at).getTime() <= Date.now();
    return `<div class="share-row"><div><strong>${share.revoked?'已撤回':expired?'已失效':'本地分享'} · ${share.show_lyrics?'显示歌词':'隐藏歌词'}</strong><p>${esc(share.card_title || '未命名短歌')} · ${formatDate(share.expires_at)} 到期 · ${esc(share.id.slice(-6))}</p>${S.shareURLs.has(share.id)&&!share.revoked&&!expired?`<button type="button" class="link-button" data-action="copy-share" data-share-id="${esc(share.id)}">复制本地链接</button>`:''}</div>${!share.revoked&&!expired?btn('revoke-share','撤回','small quiet',false,`data-share-id="${esc(share.id)}"`):''}</div>`;
  }).join('')}<p class="privacy-note">分享原始链接仅在创建时返回；刷新后可撤回旧分享，不能重新取回原令牌。</p>`;
}
function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
}
async function renderLibrary() {
  const epoch = S.epoch;
  main.innerHTML = `<div class="page-heading"><div><div class="eyebrow">YOUR LITTLE COLLECTION</div><h1>我的小歌</h1><p>故事、歌词，还有真正生成的声音，都在这里。</p></div>${btn('new-work',`${icon('plus')}写一首`,'primary')}</div>${errorMarkup()}<div id="library-content" role="status">正在读取本机作品…</div>`;
  try {
    const works = await api('/api/works'); if (epoch!==S.epoch) return;
    $('#library-content').removeAttribute('role');
    $('#library-content').innerHTML = works.length ? `<div class="library-grid">${works.map(work=>`<article class="card work-card"><div><span class="pill">${statusNames[work.status] || '状态未知'}</span></div><button type="button" class="work-title" data-action="open-work" data-work-id="${esc(work.id)}">${esc(work.title)}</button><p class="work-excerpt">${esc(work.story)}</p><div class="work-meta"><span>${formatDate(work.updated_at)} 更新</span><button type="button" class="icon-button" data-action="delete-work" data-work-id="${esc(work.id)}" data-version="${work.version}" aria-label="删除作品：${esc(work.title)}">${icon('trash')}</button></div></article>`).join('')}</div>` : `<div class="empty-state"><img src="/record.svg" alt="等待第一首小歌的唱片机"><h2>第一首，从今天开始。</h2><p>这里还没有你的作品。写一段故事，存两句歌词；不需要等到有灵感，也不需要先接入 AI。</p>${btn('new-work','开始我的第一首','primary')}</div>`;
  } catch (error) {
    if (epoch!==S.epoch) return;
    $('#library-content').innerHTML = btn('refresh-library','重试读取','quiet'); showError(error);
  }
}
let diaryMonth=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit'}).format(new Date()), diaryData=null;
const diarySelection=new Set();
let diaryRender=0,diaryRequest=0;
async function renderDiary() {
  const ticket=++diaryRender,epoch=S.epoch;pauseAudio();
  main.innerHTML=`<div class="page-heading"><div><div class="eyebrow">ONE DAY, ONE MEMORY</div><h1>声音日记</h1><p>每天留一句话，让小歌成为自己的记忆。</p></div>${btn('diary-today','写下今天','primary')}</div>${errorMarkup()}<div class="diary-toolbar"><label>查看月份 <input class="input" id="diary-month" type="month" value="${esc(diaryMonth)}"></label>${btn('diary-month','查看','quiet')}</div><div id="diary-content" role="status">正在读取本机日记…</div>`;
  try {
    const data=await api('/api/diary?month='+encodeURIComponent(diaryMonth));if(epoch!==S.epoch||ticket!==diaryRender)return;
    diaryData=data;for(const day of diarySelection)if(!data.entries.some(e=>e.day===day))diarySelection.delete(day);
    const first=new Date(data.month+'-01T12:00:00+08:00').getDay(),byDay=new Map(data.entries.map(e=>[e.day,e]));
    $('#diary-content').removeAttribute('role');
    $('#diary-content').innerHTML=`<section class="card diary-calendar" aria-label="日记日历"><div class="calendar-week">${['日','一','二','三','四','五','六'].map(x=>`<span>${x}</span>`).join('')}</div><div class="calendar-days">${'<span></span>'.repeat(first)}${Array.from({length:data.days},(_,i)=>{const day=data.month+'-'+String(i+1).padStart(2,'0'),entry=byDay.get(day);return `<button type="button" class="calendar-day ${entry?'recorded':''}" data-action="diary-edit" data-day="${day}" ${day>data.today?'disabled':''} aria-label="${day}${entry?' 已记录':' 写日记'}"><span>${i+1}</span><small>${entry?(entry.audios.length?'有小歌':'有原话'):'·'}</small></button>`}).join('')}</div><p class="micro-note">按北京时间归档。可补记过去的日期；日记仅由本机浏览器身份访问。</p></section><section class="card diary-monthly"><h2>这个月的我</h2><p>从下方挑选 2—4 天，按日期排列成歌词。原话先锁定保留，再进入作曲；不会自动付费生成。</p><div class="actions">${btn('diary-monthly','用选中的原话创作','primary',false)}<span id="diary-selection-count">已选 ${diarySelection.size} / 4 天</span></div><p class="micro-note">月度作品保存所选原话的快照。之后修改或删除日记，不会改动已保存的歌曲；删除歌曲请到“我的小歌”。</p>${data.monthly.map(w=>`<p>${btn('open-work',esc(w.title),'quiet',false,`data-work-id="${esc(w.id)}"`)} · ${w.sources.length} 段记忆</p>`).join('')}</section><div class="diary-entries">${data.entries.length?data.entries.map(e=>`<article class="card diary-entry"><div class="diary-entry-heading"><label class="consent"><input type="checkbox" data-diary-day="${e.day}" ${diarySelection.has(e.day)?'checked':''}><span>${e.day}</span></label>${btn('diary-edit','编辑','small quiet',false,`data-day="${e.day}"`)}</div><blockquote>${esc(e.phrase)}</blockquote>${e.clips.map(c=>`<p>这一天的故事原声 · ${c.duration.toFixed(1)} 秒</p><audio controls preload="none" src="/api/input-clips/${encodeURIComponent(c.id)}/audio" aria-label="${e.day} 原声"></audio>`).join('')}${e.audios.map(a=>`<p>唱有这句原话的小歌 · ${a.duration.toFixed(1)} 秒</p><audio controls preload="none" src="/api/audio/${encodeURIComponent(a.id)}" aria-label="${e.day} 小歌"></audio>`).join('')}${!e.audios.length?'<p class="micro-note">还没有唱出这句原话的正式小歌。生成后先完成试听审核。</p>':''}<div class="actions">${e.work?btn('open-work','继续这一天的创作','small',false,`data-work-id="${esc(e.work.id)}"`):btn('diary-work','录下原声 / 创作小歌','small',false,`data-day="${e.day}"`)}${btn('diary-delete','删除日记','small quiet',false,`data-day="${e.day}"`)}</div></article>`).join(''):'<div class="empty-state"><h2>这个月的第一句，会是什么？</h2><p>点击日历记下一句，也可以先从今天开始。</p></div>'}</div>`;
  }catch(error){if(epoch===S.epoch&&ticket===diaryRender)showError(error);}
}
async function diaryAction(action,target) {
  if(action==='diary-month'){const value=$('#diary-month').value;if(!value)throw new Error('请选择月份');diaryMonth=value;diarySelection.clear();return renderDiary();}
  if(action==='diary-today'||action==='diary-edit'){
    const day=action==='diary-today'?new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date()):target.dataset.day;
    const ticket=++diaryRequest,epoch=S.epoch,works=await api('/api/works');if(epoch!==S.epoch||ticket!==diaryRequest)return;
    // Today can belong to a different month from the currently displayed calendar.
    const data=day.slice(0,7)===diaryData?.month?diaryData:await api('/api/diary?month='+day.slice(0,7));if(epoch!==S.epoch||ticket!==diaryRequest)return;
    const entry=data.entries.find(e=>e.day===day);
    openDialog(day+' · 留一句话',`<label class="field">今天想记住的原话<input class="input" id="diary-phrase" maxlength="80" value="${esc(entry?.phrase||'')}" placeholder="比如：别急，我在呢"></label><label class="field">关联作品（可选）<select id="diary-linked-work"><option value="">暂不关联</option>${works.map(w=>`<option value="${esc(w.id)}" ${w.id===entry?.work_id?'selected':''}>${esc(w.title)}</option>`).join('')}</select></label><p>保存到本机日记。关联后可继续录音和创作；只显示歌词中包含这句原话的正式小歌。</p>`,async ctx=>{
      await api('/api/diary/'+day,{method:'PUT',body:{phrase:$('#diary-phrase').value,base_version:entry?.version||0,work_id:$('#diary-linked-work').value||null}});
      if(ctx.valid()){diaryMonth=day.slice(0,7);await renderDiary();}
    },'保存这一天');return;
  }
  if(action==='diary-delete'){
    const entry=diaryData.entries.find(e=>e.day===target.dataset.day);
    openDialog('删除这一天的日记？','<p>删除日期与原话记录。另行保存的原声、歌曲和月度作品仍保留；可以在对应作品中删除。</p>',async ctx=>{await api('/api/diary/'+entry.day,{method:'DELETE',body:{base_version:entry.version,confirm:true}});if(ctx.valid()){diarySelection.delete(entry.day);await renderDiary();}},'删除日记');return;
  }
  if(action==='diary-work'||action==='diary-monthly'){
    const selected=diaryData.entries.filter(e=>diarySelection.has(e.day));
    if(action==='diary-monthly'&&(selected.length<2||selected.length>4))throw new Error('请选择 2—4 天的原话');
    const entry=diaryData.entries.find(e=>e.day===target.dataset.day),month=diaryData.month;
    openDialog(action==='diary-work'?'记录声音，创作今日小歌':'创作“这个月的我”',`<p>${action==='diary-work'?'进入现有创作页后，可录音或上传原声、生成歌词与短歌。':selected.slice().sort((a,b)=>a.day.localeCompare(b.day)).map(e=>esc(e.day)+'：'+esc(e.phrase)).join('<br>')}</p><label class="consent"><input type="checkbox" id="diary-service-consent"><span>同意使用创作服务；发起 AI 生成时，会把相关文字提交到生成服务。</span></label><p>本次只创建本机作品，生成歌曲需要在创作页另行确认。</p>`,async ctx=>{
      if(!$('#diary-service-consent').checked)throw new Error('请先同意创作服务');
      const path=action==='diary-work'?'/api/diary/'+entry.day+'/work':'/api/diary/monthly';
      const body=action==='diary-work'?{base_version:entry.version,service_consent:true}:{month,entries:selected.map(e=>({day:e.day,base_version:e.version})),service_consent:true};
      const work=await api(path,{method:'POST',body});if(ctx.valid())await openWork(work.id);
    },'创建并进入创作');
  }
}
document.addEventListener('change',event=>{
  const day=event.target.dataset.diaryDay;if(!day)return;
  if(event.target.checked){if(diarySelection.size>=4){event.target.checked=false;showError(new Error('最多选择 4 天'));return;}diarySelection.add(day);}else diarySelection.delete(day);
  $('#diary-selection-count').textContent=`已选 ${diarySelection.size} / 4 天`;
});
let diagnosticsDays=7, diagnosticsTraffic='user', diagnosticsDevice='all';
async function renderDiagnostics() {
  const epoch=S.epoch;
  main.innerHTML='<div class="boot-state" role="status">正在读取本浏览器身份的诊断记录…</div>';
  try {
    const data=await api(`/api/internal/diagnostics?days=${diagnosticsDays}&traffic=${diagnosticsTraffic}&device=${diagnosticsDevice}`);
    const context=await api('/api/analytics/context'); if(epoch!==S.epoch)return;
    const money=value=>value===null?'待核账':`¥${(value/1000000).toFixed(6)}`;
    const options=(values,selected)=>values.map(([value,label])=>`<option value="${value}" ${value==selected?'selected':''}>${label}</option>`).join('');
    main.innerHTML=`<div class="page-heading"><div><div class="eyebrow">PRIVATE DIAGNOSTICS</div><h1>创作诊断</h1><p>仅当前浏览器匿名身份 · 上海时区 · 30 分钟无操作切分会话。没有数据就显示空结果。</p></div>${btn('refresh-diagnostics','刷新','quiet')}</div>${errorMarkup()}
    <section class="card"><h2>统计范围</h2><div class="status-grid"><label>时间<select id="diagnostic-days">${options([[1,'最近 1 天'],[7,'最近 7 天'],[30,'最近 30 天']],diagnosticsDays)}</select></label><label>流量<select id="diagnostic-traffic">${options([['user','真实用户'],['team','团队内部'],['test','自动测试'],['all','全部']],diagnosticsTraffic)}</select></label><label>设备<select id="diagnostic-device">${options([['all','全部'],['desktop','电脑'],['mobile','手机'],['tablet','平板'],['unknown','未知']],diagnosticsDevice)}</select></label></div><p>当前后续事件分组：${esc(context.context.traffic)}。切换为团队内部后再试玩，可避免混入真实用户漏斗。</p><label>后续事件分组<select id="analytics-traffic">${options([['user','真实用户'],['team','团队内部'],['test','自动测试']],context.context.traffic)}</select></label>${btn('analytics-context','保存分组','small')}</section>
    <section class="card"><h2>完整漏斗 · 会话去重</h2><p>同一作品按顺序到达。分母为上一阶段会话数；试听是客户端报告，不等同于满意度。</p><div class="diagnostics-table"><table><thead><tr><th>阶段</th><th>会话</th><th>上一阶段</th><th>转化</th></tr></thead><tbody>${data.funnel.map(row=>`<tr><td>${esc(row.stage)}</td><td>${row.sessions}</td><td>${row.denominator}</td><td>${row.denominator?Math.round(row.sessions/row.denominator*100)+'%':'—'}</td></tr>`).join('')}</tbody></table></div></section>
    <div class="status-grid"><section class="card"><h2>生成任务</h2><p>按任务 ID 去重，共 ${data.jobs.total} 个；取消计入分母。</p>${Object.entries(data.jobs.statuses).map(([key,count])=>`<p>${esc(statusNames[key]||key)}：${count}</p>`).join('')||'<p>暂无任务</p>'}<h3>失败原因</h3>${Object.entries(data.jobs.failure_codes).map(([key,count])=>`<p>${esc(key)}：${count}</p>`).join('')||'<p>暂无失败记录</p>'}</section><section class="card"><h2>试听、保留、再改</h2><p>有效试听作品：${data.behavior.qualified_works}</p><p>保留作品：${data.behavior.kept_works}</p><p>再改作品：${data.behavior.revision_works}</p><p>保存歌词版本：${data.behavior.lyric_versions}</p><p>已发送导出：${data.behavior.exports_sent}（不证明已保存文件）</p><p>D1：${data.retention_d1.returned_owners} / ${data.retention_d1.eligible_owners} 个到期匿名身份；待观察 ${data.retention_d1.pending_owners} 个。</p></section><section class="card"><h2>耗时及实际费用</h2><p>请求记录：${data.cost.attempts}，耗时样本 ${data.latency.count}。</p><p>中位等待：${data.latency.median_ms===null?'暂无':(data.latency.median_ms/1000).toFixed(2)+' 秒'}；P95：${data.latency.p95_ms===null?'暂无':(data.latency.p95_ms/1000).toFixed(2)+' 秒'}</p><p>已核账金额：${money(data.cost.known_cost_micros)}；待核账 ${data.cost.unknown_attempts} 笔。</p><p>每个有效试听并保留作品费用：${data.cost.effective_kept_works?money(data.cost.cost_per_effective_kept_micros):'暂无有效试听并保留作品'}</p></section></div>
    <section class="card"><h2>调用记录与账单核对</h2><p>最近 50 条请求，包含失败。没有供应商金额时保留未知；只录入已核对的人民币实际金额。</p><div class="diagnostics-table"><table><thead><tr><th>类型 / 模型</th><th>状态</th><th>耗时</th><th>实际金额</th><th>核账</th></tr></thead><tbody>${data.calls.map(call=>`<tr><td>${esc(call.kind)} / ${esc(call.model)}</td><td>${esc(call.status)} ${esc(call.error_code||'')}</td><td>${call.latency_ms===null?'等待中':call.latency_ms+' ms'}</td><td>${money(call.cost_micros)}</td><td><button type="button" class="button small" data-action="record-cost" data-id="${esc(call.id)}">录入账单</button></td></tr>`).join('')||'<tr><td colspan="5">暂无记录</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>独立试玩记录</h2><p>评测仅使用团队原创难例，不采集用户故事、歌词或录音作为评测样本。拒绝以下试玩记录仍可正常创作。</p><p>自愿记录起止时间及四项自评：独立完成、认出原句、愿意再改、需要帮助。不录屏、不录音，可撤回删除。</p><label class="consent"><input id="study-consent" type="checkbox"><span>我单独同意记录这次试玩的时间和上述行为自评。</span></label>${btn('start-study','开始自愿试玩','small')}<p>已同意 ${data.study_summary.consented} 次，完成 ${data.study_summary.completed} 次；中位耗时 ${data.study_summary.median_seconds===null?'暂无':data.study_summary.median_seconds.toFixed(1)+' 秒'}。自评结果不替代观察员验收。</p>${data.studies.map(study=>`<div class="study-row"><span>${esc(formatDate(study.started_at))} · ${study.ended_at?'已完成':'进行中'}${study.results?` · 独立完成 ${study.results.independent_completion?'是':'否'} · 认出原句 ${study.results.recognized_original?'是':'否'} · 愿意再改 ${study.results.wanted_revision?'是':'否'} · 需要帮助 ${study.results.needed_help?'是':'否'}`:''}</span>${!study.ended_at?`<button class="button small" data-action="finish-study" data-id="${esc(study.id)}">结束并自评</button>`:''}<button class="button small" data-action="withdraw-study" data-id="${esc(study.id)}">撤回并删除</button></div>`).join('')}</section>
    <section class="card"><h2>记录缺失</h2><p>旧事件 ${data.missing.legacy_events} 条，旧任务 ${data.missing.legacy_jobs} 个缺少分析上下文，未补造会话；未知设备会话 ${data.missing.unknown_device_sessions} 个。</p>${data.notes.map(note=>`<p>${esc(note)}</p>`).join('')}</section>`;
  }catch(error){if(epoch!==S.epoch)return;main.innerHTML=`${errorMarkup()}${btn('refresh-diagnostics','重试','quiet')}`;showError(error);}
}
async function diagnosticsAction(action,target) {
  if(action==='refresh-diagnostics') {diagnosticsDays=Number($('#diagnostic-days')?.value||7);diagnosticsTraffic=$('#diagnostic-traffic')?.value||'user';diagnosticsDevice=$('#diagnostic-device')?.value||'all';return renderDiagnostics();}
  if(action==='analytics-context') {await api('/api/analytics/context',{method:'POST',body:{traffic:$('#analytics-traffic').value}});return renderDiagnostics();}
  if(action==='start-study') {if(!$('#study-consent').checked)throw new Error('请独立同意试玩记录；不勾选仍可点击开始创作。');const study=await api('/api/studies',{method:'POST',body:{consent:true}});diagnosticsTraffic=(await api('/api/analytics/context')).context.traffic;toast('试玩计时已开始，请点击“开始创作”；结束后回到创作诊断填写自评。');return renderDiagnostics();}
  const id=target.dataset.id;
  if(action==='withdraw-study') {if(!window.confirm('撤回并删除这次试玩记录？作品仍保留。'))return;await api('/api/studies/'+encodeURIComponent(id),{method:'DELETE',body:{}});return renderDiagnostics();}
  if(action==='finish-study') {openDialog('结束试玩并如实自评',`<p>以下是本人自评，不会被标为观察员验证。</p>${[['independent_completion','独立完成创作'],['recognized_original','听出了自己保留的原句'],['wanted_revision','主动想再修改一次'],['needed_help','过程中需要别人帮助']].map(([key,label])=>`<label>${label}<select id="study-${key}"><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select></label>`).join('')}`,async()=>{const body={};for(const key of ['independent_completion','recognized_original','wanted_revision','needed_help']){const value=$('#study-'+key).value;if(!value)throw new Error('请逐项选择，不预填答案。');body[key]=value==='true';}if(S.work)body.work_id=S.work.id;await api('/api/studies/'+encodeURIComponent(id),{method:'POST',body});closeDialog();await renderDiagnostics();},'保存自评');return;}
  if(action==='record-cost') {openDialog('录入实际账单',`<label>人民币金额<input id="bill-amount" inputmode="decimal" placeholder="例如 0.012345"></label><label>账单依据<input id="bill-reference" maxlength="160" placeholder="账单日期及条目编号；不要填密钥"></label><label class="consent"><input id="bill-confirm" type="checkbox"><span>已核对供应商实际账单，非估算费用。</span></label>`,async()=>{const amount=$('#bill-amount').value.trim();if(!/^\d+(\.\d{1,6})?$/.test(amount)||Number(amount)>1000000)throw new Error('金额需为非负数，最多 6 位小数。');await api('/api/internal/calls/'+encodeURIComponent(id)+'/cost',{method:'POST',body:{cost_micros:Math.round(Number(amount)*1000000),reference:$('#bill-reference').value,confirmed:$('#bill-confirm').checked}});closeDialog();await renderDiagnostics();},'保存已核账金额');}
}

function renderStatus() {
  main.innerHTML = `<div class="page-heading"><div><div class="eyebrow">HONESTLY, LOCAL FIRST</div><h1>接入到哪，就做到哪。</h1><p>以下状态来自本机 /api/capabilities，不把样例当作生成结果。</p></div>${btn('refresh-capabilities','刷新状态','quiet')}</div>${errorMarkup()}<div class="status-grid">${[['lyrics','pen','AI 写词','未接入时仍可手写、锁定原文、保存和恢复歌词版本。'],['speech','mic','外部语音转写','录音和上传后可本机试听、保存，不依赖转写服务。外部识别可能计费；仅在单独勾选录音后自动转写，或明确点击主动转写时外传音频。'],['singing','headphones','成歌候选','接入 TokenHub 时可发起真实生成并私有试听；供应商可能失败，未核验的候选不能分享或导出。']].map(([key,shape,title,copy])=>`<section class="card status-card">${icon(shape)}<h2>${title}</h2><span class="pill">${capabilityText(key)}</span><p>${copy}</p></section>`).join('')}</div><div class="status-explanation"><section class="card soft"><h2>这个版本可以做什么</h2><ul><li>在本机确认故事，编辑创作意图，手写 2—4 行歌词。</li><li>录音或上传原音，经试听和确认后私有保存在作品中；删除作品一并清理原音，不会自动用于成歌。</li><li>按精确文字保护整句和片段，保留版本历史。</li><li>能力接入后主动请求候选、转写与真实演唱。</li><li>${S.cap?.song_preview_only?'TokenHub 候选先私有试听；逐项审核通过后可保留，根据使用权记录分享或导出。':'通过核验的正式音频可保留、创建 7 天本地分享；有授权才导出。'}</li></ul></section><section class="card soft"><h2>这不是一个云端账号</h2><ul><li>已确认的故事、歌词及原音保存在本机 SQLite，当前浏览器会话为 30 天；待保存录音不写入浏览器暂存，刷新页面会丢失。</li><li>清除 Cookie、会话到期或更换设备后会失去访问。</li><li>分享仅限当前机器；未部署公网，也不是生产服务。</li><li>本地网站不设置每日生成次数限制；TokenHub 自身的计费、配额和限流仍以供应商控制台为准。</li><li>不收集事件正文；客户端事件不等于服务端验证过的试听指标。</li></ul></section></div>`;
  renderError();
}
async function openWork(id) {
  if (!leaveAllowed()) return;
  S.quick=false;
  resetScope(); const epoch = S.epoch;
  main.innerHTML = '<div class="boot-state" role="status">正在读取作品…</div>';
  try {
    const work = await api(`/api/works/${encodeURIComponent(id)}`); if (epoch!==S.epoch) return;
    S.view = 'create'; S.work = work; S.editBase = null; S.meta = metaOf(work); S.lines = clone(currentLines());
    S.consent = true; S.source = currentLyric()?.source || 'manual'; S.sample = S.source==='sample';
    S.scene = ''; S.candidate = null; S.candidateB = null; S.mixSelections = []; S.lyricsCallUncertain = false; S.selected = null; S.history = ''; S.instruction = ''; S.audioId = '';
    S.availableDraft = storageGet(draftKey()); S.pendingJob = storageGet(jobKey());
    navState(); renderEditor(); startPoll(); focusMain();
  } catch (error) {
    if (epoch!==S.epoch) return;
    main.innerHTML = `${errorMarkup()}${btn('nav-library','回到我的小歌','quiet')}`; showError(error);
  }
}
async function saveStory(ctx, creating) {
  validateMeta();
  if (creating && !S.consent) throw new Error('请先阅读并勾选本机保存与服务说明。');
  const metadata = clone(S.meta); metadata.title ||= '我的一首小歌';
  const oldDraftKey = draftKey();
  const payload = creating ? {...metadata,service_consent:true} : {base_version:baseVersion(),title:metadata.title,story:metadata.story,intent:metadata.intent};
  const work = await api(creating?'/api/works':workPath(),{method:creating?'POST':'PATCH',body:payload});
  if (!ctx.valid()) return;
  const localStyle = S.meta.style,localTempo=S.meta.tempo??null;
  adoptWork(work,{meta:true});
  if (!creating) {S.meta.style = localStyle;S.meta.tempo=localTempo;}
  S.consent = true; S.availableDraft = null;
  if (creating) storageRemove(oldDraftKey);
  persistDraft(); renderEditor(); startPoll(); toast(creating?'故事已保存在本机，现在写两句吧。':'故事与意图已保存。');
  if (creating) { sendEvent('input_confirmed',{work_id:work.id,input_mode:S.sample?'sample':'text'}); $('#lyrics-section').scrollIntoView({behavior:'smooth',block:'start'}); }
}
async function saveLines(ctx, lines = S.lines, source = S.source, base = baseVersion()) {
  ensureWork();
  if ((S.candidate || S.candidateB) && source!=='ai') throw new Error('还有待确认的 AI 候选。请先应用或丢弃，再保存手写稿或修改锁定；两份内容都已保留。');
  validateLines(lines);
  const work = await api(workPath('/lyrics'),{method:'POST',body:{base_version:base,lines:clone(lines),source}});
  if (!ctx.valid()) return;
  adoptWork(work,{lyrics:true}); renderLyrics(); renderMusic(); toast('已保存为新的歌词版本。');
  sendEvent('lyric_revision_applied',{work_id:work.id,source});
}
function useSample() {
  if (isDirty() && !window.confirm('用原创预置样例替换当前未保存的故事和歌词？不会自动保存，也不会自动生成音频。')) return;
  S.meta = {title:sample.title,story:sample.story,intent:{theme:'下班后的心情',attitude:'轻松吐槽，不要煽情',preserve:'',avoid:'',recipient:''},style:'light',tempo:null};
  S.lines = sample.lines.map(text=>({text,locked:false,locked_spans:[]}));
  S.source = 'sample'; S.sample = true; S.scene = '轻松吐槽'; S.candidate = null; S.candidateB = null; S.mixSelections = [];
  persistDraft(); renderEditor(); toast('已填入原创预置样例。请阅读保存说明后主动确认。');
}
function resumeDraft() {
  const draft = S.availableDraft;
  if (!draft?.meta || !Array.isArray(draft.lines)) { storageRemove(draftKey()); S.availableDraft = null; renderEditor(); toast('暂存草稿无法读取。',true); return; }
  if (isDirty() && !window.confirm('恢复暂存内容将替换当前未保存输入，继续吗？')) return;
  S.meta = {...draft.meta,tempo:draft.meta.tempo??null}; S.lines = draft.lines; S.source = draft.source || 'manual';
  S.sample = !!draft.sample; S.scene = draft.scene || ''; S.candidate = draft.candidate;
  S.candidateB = draft.candidateB || null; S.mixSelections = Array.isArray(draft.mixSelections) ? draft.mixSelections : [];
  S.instruction = draft.instruction || ''; S.consent = true; S.availableDraft = null;
  S.editBase = S.work ? draft.base_version : null;
  if (S.work && draft.base_version!==S.work.version) {
    S.conflict = true; S.error = '草稿基于旧版本，已恢复输入，但尚未合并。不会擅自采用最新版本号，请先查看服务端最新内容。';
    if (S.candidate) S.candidate.base_version = draft.base_version;
    if (S.candidateB) S.candidateB.base_version = draft.base_version;
  }
  renderEditor(); toast('已恢复标签页草稿，尚未提交。');
}
async function changeLock(ctx,index,mode,spanIndex) {
  ensureSavedLyrics();
  const lines = clone(S.lines); const line = lines[index];
  if (!line?.id) throw new Error('请先保存这一行，再锁定原文。');
  if (mode==='toggle') line.locked = !line.locked;
  if (mode==='remove-span') line.locked_spans.splice(spanIndex,1);
  if (mode==='span') {
    const textarea = $(`#line-${index}`);
    const span = textarea.value.slice(textarea.selectionStart,textarea.selectionEnd);
    if (!span || !span.trim()) throw new Error('先在这一行中选中想保留的原文，再点“锁住选中的原文”。');
    if (!line.text.includes(span)) throw new Error('选中内容不属于当前已保存原文，请重新选择。');
    if (line.locked_spans.includes(span)) throw new Error('这段原文已经锁定。');
    line.locked_spans.push(span);
  }
  await saveLines(ctx,lines,'manual');
  if (ctx.valid()) sendEvent('lyric_line_locked',{work_id:S.work.id,status:mode==='remove-span'||(mode==='toggle'&&!line.locked)?'unlocked':'locked'});
}
async function restoreLyrics(ctx,id) {
  ensureSavedLyrics();
  if (S.candidate || S.candidateB) throw new Error('请先应用或丢弃 AI 候选，再恢复版本。');
  if (!id) throw new Error('没有可恢复的父版本。');
  const work = await api(workPath('/lyrics/restore'),{method:'POST',body:{base_version:baseVersion(),lyric_id:id}});
  if (!ctx.valid()) return;
  adoptWork(work,{lyrics:true}); renderLyrics(); renderMusic(); toast('已从选定版本创建新版本，原有历史仍保留。');
}
function requestLyrics(slot = 'A') {
  ensureSavedLyrics();
  if (metaDirty()) throw new Error('请先保存故事、意图和音乐感觉。');
  if (slot === 'A' && (S.candidate || S.candidateB)) throw new Error('请先应用或丢弃已有候选，再生成新的一组。');
  if (slot === 'B' && (!S.candidate || S.candidateB || S.candidate.base_version!==S.work.version)) throw new Error('先获取同一作品版本的候选 A；旧版 A 不能与新版 B 混合。');
  if (S.lyricsCallUncertain && !window.confirm('上一笔 AI 写词的结果不明确，可能已计费。请先核对 TokenHub 用量；确认仍要发起另一笔可能收费的新请求吗？')) return;
  if (!S.cap?.lyrics) throw new Error('AI 写词未接入，可以继续手写。');
  const line = S.selected===null ? null : S.lines[S.selected];
  if (line && (!line.id || line.locked)) throw new Error('请选一行已保存且未整句锁定的歌词。');
  const instruction = S.instruction.trim() || '写成自然口语的中文短歌词'; validateText(instruction,'修改要求',500);
  if (/(只|仅|单独).{0,8}(贝斯|bass|鼓点|鼓轨|人声轨|伴奏轨)/i.test(instruction)) throw new Error('当前不能只改独立音轨。请到“成小歌”选择另一种感觉，确认后重新生成整曲。');
  openDialog(`请求歌词候选 ${slot}`,`<p>将把已保存故事、创作意图、当前歌词和修改要求发送给 TokenHub 文字模型。候选 ${slot} 是${slot==='B'?'与 A 独立的第二笔':'第一笔'}可能计费的请求；不会自动连续生成 A/B。返回内容不会自动保存。</p><p>范围：${line?`只改第 ${S.selected+1} 句`:'整首'}。服务端检查必留原句、锁定内容和行 ID。</p><label class="consent"><input id="lyrics-paid-consent" type="checkbox"><span>我有权提交这些文字，并主动确认这一次候选 ${slot} 请求可能产生供应商费用。</span></label>`,async ctx=>{
    if (!$('#lyrics-paid-consent').checked) throw new Error('请先确认本次 AI 写词调用与可能产生的费用。');
    const body = {base_version:baseVersion(),instruction,paid_call_confirmed:true}; if (line) body.line_id = line.id;
    let candidate;
    try { candidate = await api(workPath('/lyrics/generate'),{method:'POST',body}); }
    catch (error) {
      if (ctx.valid() && (!error.status || error.status>=500 || error.code==='INVALID_RESPONSE')) S.lyricsCallUncertain = true;
      throw error;
    }
    if (!ctx.valid()) return;
    S.lyricsCallUncertain = false;
    if (slot === 'A') { S.candidate = candidate; S.candidateB = null; S.mixSelections = []; }
    else S.candidateB = candidate;
    persistDraft(); renderLyrics(); toast(`候选 ${slot} 已返回；逐句核对，再主动保存。`);
  },`确认请求候选 ${slot}`);
}
function canMixCandidates() {
  const a = S.candidate, b = S.candidateB;
  return !!(a && b && a.base_version===b.base_version && a.lines.length===b.lines.length &&
    a.lines.every((line,index)=>line.id===b.lines[index].id));
}
function chosenCandidateLines() {
  if (!S.candidate || !S.candidateB || !canMixCandidates()) throw new Error('两个候选的行数或稳定行 ID 不同，请选择一版完整候选。');
  return S.candidate.lines.map((line,index)=>clone(S.mixSelections[index]==='B'?S.candidateB.lines[index]:line));
}
async function saveStyle(ctx) {
  ensureWork();
  await api(workPath(),{method:'PATCH',body:{base_version:baseVersion(),style:S.meta.style}});
  const work = await api(workPath());
  if (!ctx.valid()) return;
  adoptWork(work); persistDraft(); renderMusic(); renderCandidate(); toast('音乐感觉已保存；如要生成新的感觉，仍需逐次确认可能产生的费用。');
}
function songDialog() {
  ensureSavedAll();
  const lyric = currentLyric(); if (!lyric) throw new Error('请先保存歌词。');
  if (!S.cap?.singing) throw new Error('真实演唱服务未接入。');
  openDialog('把这一版，唱出来',`<div class="notice purple">AI 生成演唱 · 歌词 v${lyric.number} · ${styleNames[S.work.style]}${S.work.tempo?' · 目标 '+S.work.tempo.bpm+' BPM':''}</div><ol class="dialog-list">${lyric.lines.map(line=>`<li>${esc(line.text)}</li>`).join('')}</ol><p>本地网站不设置每日生成次数限制。每次确认都会发起可能收费的请求；失败或取消不代表供应商一定免单。</p><p>将发送这版歌词和创作意图给已配置服务。本应用不用于训练；模型服务数据政策须由接入方确认。${S.cap.song_preview_only?'请先在腾讯云 TokenHub 控制台的广州地域「在线推理 → 语音模型」确认 Mureka-Music-v9（mureka-music-v9）已开启后付费、账号无欠费且模型未受限；否则请求可能被供应商拒绝。TokenHub 可能收取生成费用，失败或取消也未必免单；供应商不保证 15—30 秒或逐字唱对。返回音频先作为当前设备的私有试听候选，不自动变成正式作品，也不开放分享/导出。':'只有通过真实检查后的作品才能播放。'}</p><label class="consent"><input type="checkbox" id="song-consent"><span>我已核对这版歌词，并拥有相关文字的使用授权；${S.cap.song_preview_only?'理解本次可能产生供应商费用，愿意主动发起一次调用。':'同意本次主动生成及可能产生的费用。'}</span></label>`,async ctx=>{
    if (!$('#song-consent').checked) throw new Error('请先确认歌词、授权及本次可能产生的费用。');
    S.pendingJob = {base_version:baseVersion(),lyric_id:lyric.id,idempotency_key:crypto.randomUUID(),confirmed:true};
    if (S.cap.song_preview_only) S.pendingJob.paid_call_confirmed = true;
    storageSet(jobKey(),S.pendingJob);
    await submitJob(ctx);
  },'确认生成');
}
async function submitJob(ctx) {
  if (!S.pendingJob) throw new Error('没有需要重试的原请求。');
  const pending = clone(S.pendingJob);
  try {
    const job = await api(workPath('/jobs'),{method:'POST',body:pending});
    if (!ctx.valid()) return;
    S.pendingJob = null; storageRemove(jobKey());
    S.work.jobs = [...S.work.jobs.filter(item=>item.id!==job.id),job];
    if (!activeStatuses.has(job.status)) {
      const work = await api(workPath());
      if (!ctx.valid()) return;
      mergeAux(work);
    }
    renderMusic(); renderError(); startPoll(); toast(`任务状态：${statusNames[job.status] || job.status}`);
    sendEvent('song_job_created',{work_id:S.work.id,job_id:job.id,status:job.status});
  } catch (error) {
    if (!ctx.valid()) return;
    // 传输失败、无法解析和 5xx 不能证明没有建单，始终保留同一请求键。
    if (error.status>=400 && error.status<500 && error.code!=='INVALID_RESPONSE') {
      S.pendingJob = null; storageRemove(jobKey());
    }
    renderMusic(); throw error;
  }
}
function startPoll() {
  stopPoll();
  if (S.view!=='create' || !S.work) return;
  const job = latestJob(); if (!job) return;
  if (!activeStatuses.has(job.status)) return;
  const epoch = S.epoch; const wid = S.work.id; const controller = new AbortController();
  S.pollController = controller;
  const poll = async () => {
    if (epoch!==S.epoch || S.work?.id!==wid || controller.signal.aborted) return;
    if (S.operation) { S.pollTimer = setTimeout(poll,1500); return; }
    try {
      const latest = await api(`/api/jobs/${encodeURIComponent(job.id)}`,{signal:controller.signal});
      if (epoch!==S.epoch || S.work?.id!==wid || controller.signal.aborted) return;
      if (S.operation) { S.pollTimer = setTimeout(poll,1500); return; }
      S.work.jobs = S.work.jobs.map(item=>item.id===latest.id?latest:item); renderJob();
      if (activeStatuses.has(latest.status) && !latest.preview_ready) S.pollTimer = setTimeout(poll,2500);
      else if (latest.preview_ready) { renderMusic(); toast('真实音频候选已返回，请主动试听并核对歌词；尚不是正式作品。'); }
      else {
        const work = await api(`/api/works/${encodeURIComponent(wid)}`,{signal:controller.signal});
        if (epoch!==S.epoch || S.work?.id!==wid || controller.signal.aborted) return;
        if (S.operation) { S.pollTimer = setTimeout(poll,1500); return; }
        mergeAux(work); renderMusic(); renderError();
        sendEvent('song_job_terminal',{work_id:wid,job_id:latest.id,status:latest.status});
        toast(latest.status==='ready'?'真实音频已就绪，点击后试听。':`任务已结束：${statusNames[latest.status]}`);
      }
    } catch (error) {
      if (error.name==='AbortError'||epoch!==S.epoch||controller.signal.aborted) return;
      showError(error);
      const node = $('#job-panel'); if (node) node.insertAdjacentHTML('beforeend',btn('refresh-job','重新读取任务状态','small'));
    }
  };
  S.pollTimer = setTimeout(poll,800);
}
async function refreshJob(ctx) {
  const work = await api(workPath()); if (!ctx.valid()) return;
  mergeAux(work); renderMusic(); renderError(); startPoll();
}
function cancelJob(jobId = null) {
  const job = jobId ? S.work?.jobs.find(item=>item.id===jobId) : latestJob();
  if (!job || !activeStatuses.has(job.status)) return;
  openDialog('停止接收这次生成？','<p>本机将不再接收这次任务结果，歌词仍保留。这不代表远端服务一定停止，也不承诺退还已产生的供应商费用。</p>',async ctx=>{
    const latest = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`,{method:'POST',body:{}});
    if (!ctx.valid()) return;
    stopPoll(); pauseAudio(); S.work.jobs = S.work.jobs.map(item=>item.id===latest.id?latest:item);
    const work = await api(workPath()); if (!ctx.valid()) return;
    mergeAux(work); renderMusic(); renderError();
    toast(latest.status==='cancelled'?'已停止接收，不会自动播放。':'任务已结束，已读取实际状态，不会自动播放。');
  },'确认停止接收');
}
async function keepAudio(ctx) {
  const audio = selectedAudio(); if (!audio) throw new Error('没有可保留的真实音频。');
  const work = await api(workPath(`/audios/${encodeURIComponent(audio.id)}/keep`),{method:'POST',body:{}});
  if (!ctx.valid()) return;
  mergeAux(work); pauseAudio(); renderAudio(); renderError(); toast('已保留这一版音频。');
  sendEvent('version_kept',{work_id:S.work.id,audio_id:audio.id,status:'kept'});
}
function renderShareCardPreview() {
  const box = $('#share-card-preview'); if (!box) return;
  const title = $('#share-card-title')?.value.trim() || S.work.title;
  const theme = ['mint','lavender','cream'].includes($('#share-card-theme')?.value) ? $('#share-card-theme').value : 'mint';
  const audio = selectedAudio();
  box.className = `share-card ${theme}`;
  box.innerHTML = `<small>说一拍 · AI 生成演唱</small><strong>${esc(title)}</strong><span>${styleNames[audio.style]} · ${Math.round(audio.duration)} 秒</span><p>${$('#share-lyrics')?.checked?'将公开展示音频绑定的歌词快照':'不展示歌词文字；歌声里仍可听到歌词'} · ${$('#share-days')?.value || '7'} 天有效</p>`;
}
function shareDialog() {
  const audio = selectedAudio(); if (!audio) throw new Error('请先获得经过核验的真实音频；私有候选不可分享。');
  openDialog('定制这张分享卡',`<p>仅使用已核验音频。先预览并决定分享标题、配色、歌词文字和有效期；创建后不可编辑，可随时撤回。链接仅当前机器可访问。</p><div class="field"><label class="form-label" for="share-card-title">对外显示的标题 · 最多 80 字</label><input class="input" id="share-card-title" maxlength="80" value="${esc(S.work.title)}"></div><div class="field"><label class="form-label" for="share-card-theme">卡片配色</label><select id="share-card-theme"><option value="mint">奶油薄荷</option><option value="lavender">柔和淡紫</option><option value="cream">暖奶油</option></select></div><div class="field"><label class="form-label" for="share-days">链接有效期</label><select id="share-days"><option value="1">1 天</option><option value="3">3 天</option><option value="7" selected>7 天</option></select></div><label class="consent"><input type="checkbox" id="share-lyrics"><span>公开音频绑定的歌词快照（默认不显示；隐藏文字仍能从歌声听到歌词）。</span></label><div id="share-card-preview" aria-label="分享卡预览"></div><p class="privacy-note">撤回后链接和音频请求都会失效；但无法撤回他人先前保存或录制的内容。不会自动发布到公网。</p>`,async ctx=>{
    const title = $('#share-card-title').value.trim(); validateText(title,'分享标题',80);
    const share = await api(workPath('/shares'),{method:'POST',body:{confirm:true,audio_id:audio.id,
      card_title:title,card_theme:$('#share-card-theme').value,expires_days:Number($('#share-days').value),show_lyrics:$('#share-lyrics').checked}});
    if (!ctx.valid()) return;
    const url = `${location.origin}/s/${encodeURIComponent(share.token)}`;
    S.shareURLs.set(share.id,url); S.work.shares.push(share);
    $('#shares-panel').innerHTML = sharesMarkup();
    sendEvent('share_created_or_revoked',{work_id:S.work.id,audio_id:audio.id,status:'created'});
    openShareResult(url,share);
  },'确认创建本地分享');
  renderShareCardPreview();
}
function openShareResult(url,share) {
  openDialog('本地分享已创建',`<div class="share-card ${esc(share.card_theme)}"><small>说一拍 · AI 生成演唱</small><strong>${esc(share.card_title)}</strong><span>${share.show_lyrics?'显示歌词文字':'不展示歌词文字'}</span></div><p>${formatDate(share.expires_at)} 到期。仅当前机器可访问；其他设备不能直接打开。</p><label class="form-label" for="share-url">本地分享链接</label><input class="input" id="share-url" readonly value="${esc(url)}"><div class="actions">${btn('copy-current-share','复制链接','primary')}<a class="button" href="${esc(url)}" target="_blank" rel="noopener noreferrer">打开只读页</a></div>`,null);
}
async function copyText(value) {
  try { await navigator.clipboard.writeText(value); toast('已复制。本地链接仅当前机器可访问。'); }
  catch {
    openDialog('请手动复制',`<p>浏览器未允许自动复制。请选中下方完整文本后复制。</p><textarea id="copy-text" readonly>${esc(value)}</textarea>`,null);
    $('#copy-text').select();
  }
}
function revokeShare(id) {
  openDialog('撤回这条分享？','<p>撤回后，这条分享链接立即失效。已下载或录制的内容无法撤回。</p>',async ctx=>{
    await api(workPath(`/shares/${encodeURIComponent(id)}/revoke`),{method:'POST',body:{}});
    if (!ctx.valid()) return;
    const share = S.work.shares.find(item=>item.id===id); share.revoked = true; S.shareURLs.delete(id);
    $('#shares-panel').innerHTML = sharesMarkup(); toast('分享已撤回。');
    sendEvent('share_created_or_revoked',{work_id:S.work.id,audio_id:share.audio_id,status:'revoked'});
  },'确认撤回');
}
function deleteDialog(id,version) {
  openDialog('删除这首小歌？','<p>将删除本机保存的故事、歌词历史与音频，并撤回全部分享。已下载或录制的内容无法撤回。删除不能撤销。</p>',async ctx=>{
    await api(`/api/works/${encodeURIComponent(id)}`,{method:'DELETE',body:{base_version:version,confirm:true}});
    if (!ctx.valid()) return;
    storageRemove(draftKey(id)); storageRemove(jobKey(id));
    if (S.work?.id===id) {
      S.work = null; S.meta = emptyMeta(); S.lines = emptyLines(); S.candidate = null; S.candidateB = null; S.mixSelections = [];
      S.consent = false; S.pendingJob = null; S.availableDraft = null;
    }
    await navigate('library',{force:true}); toast('本机作品已删除，相关分享已撤回。');
  },'确认删除','danger');
}
function openDialog(title,html,onConfirm=null,label='确认',css='primary') {
  if (dialog.open) closeDialog();
  S.dialogVersion++;
  $('#dialog-content').innerHTML = `<button type="button" class="icon-button dialog-close" data-action="dialog-close" aria-label="关闭对话框">${icon('close')}</button><div class="eyebrow">SHUOYIPAI / 小小确认</div><h2 id="dialog-title">${esc(title)}</h2><div id="dialog-error" class="error-box" role="alert" hidden></div>${html}<div class="actions end">${btn('dialog-close',onConfirm?'再想想':'关闭','quiet')}${onConfirm?btn('dialog-confirm',label,css):''}</div>`;
  S.dialogConfirm = onConfirm; dialog.showModal();
}
function closeDialog() {
  for(const audio of $$("audio",dialog))audio.pause();
  stopVoice(true); S.dialogConfirm = null; S.dialogVersion++;
  if (dialog.open) dialog.close();
}
async function inspectConflict(ctx) {
  if (!S.work) throw new Error('请回到“我的小歌”重新读取作品列表，当前内容仍保留。');
  const remote = await api(workPath()); if (!ctx.valid()) return;
  S.remote = remote;
  const lyric = remote.lyrics.find(item=>item.id===remote.current_lyric_id);
  openDialog('对照最新版本',`<p>本地基于版本 ${baseVersion()}，服务端为版本 ${remote.version}。输入与候选不会自动丢弃或合并。</p><div class="conflict-preview">${esc(remote.title)}\n\n${esc(remote.story)}\n\n${esc(lyric?.lines.map(line=>line.text).join('\n') || '尚未保存歌词')}</div><p>同步版本号会保留本地文本，服务端仍会检查稳定行 ID、锁定原文及意图。旧 AI 候选不能直接应用，需丢弃后重新生成。</p><div class="actions">${btn('rebase-local','同步版本号，保留本地输入','primary')}${btn('load-remote','放弃本地修改，载入最新','quiet')}</div>`,null);
}
async function sendEvent(name,properties,strict=false,eventId=crypto.randomUUID()) {
  if (!S.csrf || S.view==='public') return;
  try { return await api('/api/events',{method:'POST',body:{event_id:eventId,name,properties,client_at:new Date().toISOString()}}); }
  catch (error) { if (strict) throw error; }
}
function mergeRanges(ranges) {
  const sorted = ranges.filter(([start,end])=>Number.isFinite(start)&&Number.isFinite(end)&&end>start).sort((a,b)=>a[0]-b[0]);
  const merged = [];
  for (const [start,end] of sorted) {
    const previous = merged.at(-1);
    if (previous && start<=previous[1]) previous[1] = Math.max(previous[1],end);
    else merged.push([start,end]);
  }
  return merged;
}
function bindAudio(element,audio,workId) {
  element.addEventListener('play',()=>{
    for (const other of $$('audio')) if (other!==element) other.pause();
    sendEvent('audio_play_started',{work_id:workId,audio_id:audio.id,status:'started'});
  });
  // played 是实际播过的时间段，不把 seek 跨过的区间或重复播放当成新增覆盖。
  const collect = () => {
    const record = S.listens.get(audio.id) || {ranges:[],sent:false};
    const ranges = [...record.ranges];
    for (let index=0;index<element.played.length;index++) ranges.push([element.played.start(index),element.played.end(index)]);
    record.ranges = mergeRanges(ranges);
    const covered = record.ranges.reduce((total,[start,end])=>total+Math.max(0,Math.min(end,audio.duration)-Math.max(0,start)),0);
    if (!record.sent && !record.pending && Date.now()>=(record.retryAt||0) && audio.duration>0 && covered>=audio.duration*.5) {
      record.pending=true;record.eventId ||= crypto.randomUUID();
      record.properties ||= {work_id:workId,audio_id:audio.id,covered_ms:Math.floor(covered*1000),duration_ms:Math.round(audio.duration*1000)};
      sendEvent('audio_listen_qualified',record.properties,true,record.eventId).then(()=>{record.sent=true;}).catch(()=>{record.retryAt=Date.now()+5000;}).finally(()=>{record.pending=false;});
    }
    S.listens.set(audio.id,record);
  };
  for (const name of ['timeupdate','pause','ended']) element.addEventListener(name,collect);
  element.addEventListener('error',()=>showError(new Error('音频文件暂不可用。没有自动改播其他音频，请检查服务后重新选择这一版。')));
}
function voiceDialog(target = 'story') {
  const role = target === 'instruction' ? 'instruction' : 'story';
  const label = role === 'story' ? '故事' : '修改要求';
  openDialog(role === 'story' ? '把这段话留个声音' : '说说你想怎么改',`<p>录音或选择音频后先在本机试听，由你决定是否保存。单段不超过 60 秒 / 6 MB；仅接受 PCM16 WAV、MP3 或纯音频 M4A/MP4。未单独勾选自动转写时，不会向外发送音频。</p><label class="consent"><input id="voice-consent" type="checkbox"><span>我同意仅在点击“开始录音”时使用麦克风；关闭后立即释放。</span></label>${S.cap?.speech ? '<label class="consent"><input id="auto-transcribe" type="checkbox"><span>我同意本次录音结束或选取文件后，自动将该段音频发给 TokenHub 转写；可能产生供应商费用。识别文字仅为待确认草稿，不自动保存或执行改词。</span></label>' : '<p class="privacy-note">外部转写尚不可用；可以先试听，再手动填写文字。</p>'}<div class="record-control"><span id="record-time" class="record-time" role="timer">00:00</span>${btn('record-start',`${icon('mic')}开始录音`,'primary',false,`data-voice-target="${role}"`)}${btn('record-stop','结束录音','danger',true)}</div><div class="field"><label class="form-label" for="clip-file">或从电脑选择一段音频</label><input id="clip-file" type="file" accept=".wav,.mp3,.m4a,.mp4,audio/wav,audio/mpeg,audio/mp4" data-voice-target="${role}"></div><p id="record-status" role="status">未使用麦克风，也没有把音频发送给模型。</p><div id="clip-preview"></div><div class="field"><label class="form-label" for="transcript">${label}文字 · 可自己边听边写，最多 ${S.quick&&role==='story'?80:500} 字</label><textarea id="transcript" rows="4" maxlength="${S.quick&&role==='story'?80:500}" placeholder="听一听，再把要写进${label}的文字填在这里。"></textarea><div id="transcription-result" class="transcription-result" hidden></div><div class="actions">${btn('use-transcript',`确认文字并填入${label}`,'small primary',false,`data-voice-target="${role}"`)}</div><p class="privacy-note">转写建议不会盖掉已输入文字；确认后仍需主动保存。故事中可选中想保留的原话，再申请歌词候选。</p></div>`,null);
  renderInputDialogState(role);
}
function clipMime(file) {
  const declared = (file.type || '').split(';')[0].toLowerCase();
  const extension = (file.name || '').toLowerCase().split('.').at(-1);
  const fromName = {wav:'audio/wav',mp3:'audio/mpeg',m4a:'audio/mp4',mp4:'audio/mp4'}[extension];
  const normalized = {'audio/x-wav':'audio/wav','audio/wave':'audio/wav','audio/mp3':'audio/mpeg','audio/x-m4a':'audio/mp4'}[declared] || declared;
  if (!fromName || (normalized && normalized !== fromName)) throw new Error('仅支持 WAV、MP3 或纯音频 M4A/MP4；文件内容也会在保存时复核。');
  return fromName;
}
function renderInputDialogState(role) {
  const node = $('#clip-preview'); if (!node) return;
  const clip = S.pendingClip?.role === role ? S.pendingClip : null;
  if (!clip) { node.replaceChildren(); return; }
  const canTranscribe = S.cap?.speech && ['audio/wav','audio/mpeg','audio/mp4'].includes(clip.blob?.type);
  node.innerHTML = `<div class="clip-card"><strong>已选音频 · ${clip.source === 'record' ? '录音' : '文件'} · 待你确认</strong><audio controls preload="metadata" src="${esc(clip.url)}" aria-label="试听待保存音频"></audio><div class="actions">${S.work ? btn('save-input-clip','保存原音到本机作品','small primary',false,`data-role="${role}"`) : '<span class="inline-small">先确认故事作品，再保存原音。</span>'}${btn('discard-input-clip','丢弃原音','small quiet',false,`data-role="${role}"`)}</div>${canTranscribe ? `<label class="consent"><input id="transcribe-consent" type="checkbox" ${$('#auto-transcribe')?.checked?'checked':''}><span>我同意将这段音频发送给 TokenHub 转写，并知悉本次请求可能计费。未勾选自动转写时，只有点击下方按钮才会发送。</span></label>${btn('transcribe-clip','主动转写这段音频','small',false,`data-role="${role}"`)}` : '<p class="privacy-note">当前没有可用的此格式转写入口，请试听后手动填写文字。不会生成假转写。</p>'}</div>`;
}
function setPendingClip(blob, source, role, transcribeBlob = blob) {
  if (S.pendingClip && !window.confirm('用新音频替换当前待保存原音？旧录音不会发送或保存。')) return false;
  pauseAudio(); clearPendingClip();
  S.pendingClip = {blob,source,role,transcribeBlob,url:URL.createObjectURL(blob)};
  renderInputDialogState(role); updateDirtyIndicators();
  return true;
}
async function selectInputFile(file, role) {
  if (!file) return;
  const mime = clipMime(file);
  if (file.size <= 100 || file.size > 6 * 1024 * 1024) throw new Error('音频文件须大于 100 字节且不超过 6 MB。');
  if (S.voice) throw new Error('请先结束正在进行的录音。');
  const blob = new Blob([file],{type:mime});
  if (setPendingClip(blob,'upload',role)) {
    $('#record-status').textContent = '文件已在本页待确认；不会自动保存到作品。';
    if ($('#auto-transcribe')?.checked) await transcribePending(role,true);
  }
}
async function blobBase64(blob) {
  return await new Promise((resolve,reject)=>{
    const reader = new FileReader();
    reader.onload = ()=>resolve(String(reader.result).split(',')[1]);
    reader.onerror = ()=>reject(new Error('无法读取音频文件，请重新选择。'));
    reader.readAsDataURL(blob);
  });
}
async function recordedWav(blob) {
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await blob.arrayBuffer());
    if (!Number.isFinite(decoded.duration) || decoded.duration <= 0 || decoded.duration > 60) throw new Error('录音须大于0秒且不超过60秒。');
    const frames = Math.ceil(decoded.duration * 16000);
    const offline = new OfflineAudioContext(1,frames,16000);
    const source = offline.createBufferSource(); source.buffer = decoded;
    source.connect(offline.destination); source.start();
    const mono = (await offline.startRendering()).getChannelData(0);
    const bytes = new ArrayBuffer(44 + mono.length * 2);
    const wav = new DataView(bytes);
    const label = (at,text) => { for (let i=0;i<text.length;i++) wav.setUint8(at+i,text.charCodeAt(i)); };
    label(0,'RIFF'); wav.setUint32(4,bytes.byteLength-8,true); label(8,'WAVE'); label(12,'fmt ');
    wav.setUint32(16,16,true); wav.setUint16(20,1,true); wav.setUint16(22,1,true);
    wav.setUint32(24,16000,true); wav.setUint32(28,32000,true); wav.setUint16(32,2,true); wav.setUint16(34,16,true);
    label(36,'data'); wav.setUint32(40,mono.length*2,true);
    for (let i=0;i<mono.length;i++) { const sample=Math.max(-1,Math.min(1,mono[i])); wav.setInt16(44+i*2,sample<0?sample*32768:sample*32767,true); }
    return new Blob([bytes],{type:'audio/wav'});
  } finally { await context.close(); }
}
async function saveInputClip(ctx, role) {
  ensureWork();
  const clip = S.pendingClip;
  if (!clip || clip.role !== role) throw new Error('没有待保存的原音。');
  if (!window.confirm('只将这段原音保存在当前本机作品中？同一用途的旧原音将被替换，不会发送给模型。')) return;
  const id = S.work.id;
  const audio_base64 = await blobBase64(clip.blob);
  const result = await api(`/api/works/${encodeURIComponent(id)}/input-clips`,{method:'POST',body:{role,source:clip.source,mime:clip.blob.type,audio_base64,confirm:true}});
  if (!ctx.valid() || S.work?.id !== id) return;
  S.work.input_clips = [...(S.work.input_clips || []).filter(item=>item.role!==role),result];
  clearPendingClip(); closeDialog(); renderEditor(); toast('原音已保存到本机作品；未发送给模型。');
}
async function removeInputClip(ctx, id) {
  if (!window.confirm('确定删除这段本机原音？相关原声开场版本及分享也会删除，不能恢复。')) return;
  await api(`/api/input-clips/${encodeURIComponent(id)}`,{method:'DELETE',body:{confirm:true}});
  if (!ctx.valid()) return;
  S.work = await api(workPath());
  if(!ctx.valid())return;
  renderEditor(); toast('原音已从本机作品中删除。');
}
async function transcribePending(role, automatic = false) {
  const clip = S.pendingClip;
  if (!clip || clip.role !== role || !S.cap?.speech || !(automatic ? $('#auto-transcribe')?.checked : $('#transcribe-consent')?.checked)) throw new Error('请先选择音频，并单独确认外部转写及可能产生的费用。');
  if (S.transcribing) throw new Error('当前已有一笔转写请求，请等待结果；不会重复提交。');
  const raw = clip.blob;
  if (!['audio/wav','audio/mpeg','audio/mp4'].includes(raw.type)) throw new Error('这个格式尚未接入外部转写，请手动输入文字。');
  if (clip.transcribeAttempted && !window.confirm('这段音频先前已提交转写。即使上次没有返回文字，也可能已经计费；请先核对 TokenHub 用量。确定再次发起一笔可能收费的新请求吗？')) return;
  $('#record-status').textContent = '正在请求外部转写，可能计费；原音和原文字不会被覆盖…';
  const version = S.dialogVersion;
  const audio_base64 = await blobBase64(raw);
  if (!dialog.open || S.pendingClip !== clip || S.dialogVersion !== version || !(automatic ? $('#auto-transcribe')?.checked : $('#transcribe-consent')?.checked)) return;
  clip.transcribeAttempted = true;
  S.transcribing = true;
  try {
    const result = await api('/api/transcribe',{method:'POST',body:{audio_base64,mime:raw.type,consent:true,paid_call_confirmed:true}});
    if (!dialog.open || S.pendingClip !== clip || S.dialogVersion !== version) return;
    const suggestion = $('#transcription-result');
    suggestion.replaceChildren();
    const title = document.createElement('strong'); title.textContent = '外部转写建议 · 先核对';
    const text = document.createElement('p'); text.textContent = result.text;
    const use = document.createElement('button'); use.type = 'button'; use.className = 'button small';
    use.dataset.action = 'accept-transcription'; use.textContent = '采用为待确认文字';
    suggestion.append(title,text,use); suggestion.hidden = false;
    $('#record-status').textContent = '转写已返回但没有覆盖文字。先试听、核对并点击采用，再确认填入作品。';
  } catch (error) {
    if (dialog.open && S.pendingClip === clip && S.dialogVersion === version) $('#record-status').textContent = '本次转写结果不明或失败；原音与手写文字仍在。请核对供应商用量，勿连续重试。';
    throw error;
  } finally { S.transcribing = false; }
}
async function startVoice(target = 'story') {
  if (!$('#voice-consent')?.checked) throw new Error('请先同意本次使用麦克风。');
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder || !window.AudioContext || !window.OfflineAudioContext) throw new Error('此浏览器暂不支持录音处理，请上传音频或继续打字。');
  if (S.voice) return;
  pauseAudio();
  const voice = {target:target === 'instruction' ? 'instruction' : 'story',epoch:S.epoch,dialogVersion:S.dialogVersion,stream:null,recorder:null,timer:null,chunks:[],discard:false,autoTranscribe:!!$('#auto-transcribe')?.checked};
  S.voice = voice;
  const valid = () => S.voice === voice && S.epoch === voice.epoch && S.dialogVersion === voice.dialogVersion && dialog.open && !voice.discard;
  $('[data-action="record-start"]').disabled = true;
  $('#record-status').textContent = '等待麦克风权限…';
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    voice.stream = stream;
    if (!valid()) { stream.getTracks().forEach(track=>track.stop()); return; }
    const mime = ['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus','audio/webm'].find(type=>MediaRecorder.isTypeSupported(type));
    if (!mime) throw new Error('当前浏览器没有可用的录音格式，请上传音频或改用文字。');
    const recorder = new MediaRecorder(stream,{mimeType:mime,audioBitsPerSecond:64000});
    voice.recorder = recorder;
    recorder.addEventListener('dataavailable',event=>{ if (event.data.size && !voice.discard) voice.chunks.push(event.data); });
    recorder.addEventListener('error',()=>{
      if (!valid()) return;
      stopVoice(true); showError(new Error('录音失败，麦克风已释放；之前待保存的音频仍保留。'));
    });
    recorder.addEventListener('stop',async ()=>{
      stream.getTracks().forEach(track=>track.stop()); clearInterval(voice.timer);
      if (!valid()) return;
      $('[data-action="record-stop"]').disabled = true;
      $('#record-status').textContent = '录音已结束，正在本机准备可试听的音频…';
      try {
        const raw = new Blob(voice.chunks,{type:mime}); voice.chunks = [];
        if (raw.size <= 100 || raw.size > 6*1024*1024) throw new Error('录音为空或超过 6 MB，请重录。');
        const wav = await recordedWav(raw);
        if (!valid()) return;
        if (wav.size > 6*1024*1024) throw new Error('录音超过 6 MB，请缩短后重录。');
        const accepted = setPendingClip(wav,'record',voice.target,raw);
        $('#record-status').textContent = accepted ? '录音已在本页待确认。请先试听并检查识别文字。' : '已保留之前待保存的原音。';
        if (accepted && voice.autoTranscribe) await transcribePending(voice.target,true);
      } catch (error) {
        if (valid()) { showError(error); $('#record-status').textContent = '录音或转写未完成；若试听音频仍在，可手动填写文字或主动重试转写。'; }
      } finally {
        if (valid()) { S.voice = null; $('[data-action="record-start"]').disabled = false; }
      }
    });
    recorder.start(1000); voice.started = performance.now();
    $('#record-status').textContent = '录音中；点击结束，或在 60 秒后自动结束。';
    $('#record-time').classList.add('recording'); $('[data-action="record-stop"]').disabled = false;
    voice.timer = setInterval(()=>{
      if (!valid()) return;
      const seconds = Math.min(60,Math.floor((performance.now()-voice.started)/1000));
      $('#record-time').textContent = `${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;
      // Allow a small buffer for MediaRecorder/container padding at the upper bound.
      if (performance.now()-voice.started >= 59500) stopVoice(false);
    },250);
  } catch (error) {
    voice.stream?.getTracks().forEach(track=>track.stop()); clearInterval(voice.timer);
    if (!valid()) return;
    S.voice = null; $('[data-action="record-start"]').disabled = false;
    $('#record-status').textContent = '麦克风未启动，可以继续打字或选择音频文件。';
    throw new Error(error.name==='NotAllowedError'?'未获得麦克风权限。可以继续打字或上传音频。':error.message || '录音不可用，请继续打字。');
  }
}
function stopVoice(discard) {
  const voice = S.voice; if (!voice) return;
  voice.discard = discard; clearInterval(voice.timer);
  if (voice.recorder?.state==='recording' || voice.recorder?.state==='paused') voice.recorder.stop();
  voice.stream?.getTracks().forEach(track=>track.stop());
  if (discard) { voice.chunks = []; S.voice = null; }
  $('#record-time')?.classList.remove('recording');
}
function voiceLyricAction(text) {
  const action=classifyInstruction(text);
  if(action.kind==='clarify') throw new Error('这段话包含引用、否定、纠正或多个目标。请手动选定歌词行，或改成单一明确指令；不会自动执行。');
  if(action.kind==='unsupported') throw new Error('当前不能只改独立音轨。请选择另一种音乐感觉，确认后重新生成整曲。');
  return action;
}
async function useTranscript(target = 'story') {
  if (S.voice) throw new Error('请先结束本次录音并等待转写结果。');
  const text = $('#transcript')?.value || '';
  validateText(text,target === 'instruction' ? '转写修改要求' : '转写故事',S.quick&&target==='story'?80:500);
  if (target === 'instruction') {
    const action = voiceLyricAction(text);
    if (action.index!==null && (!S.work || !currentLyric() || action.index >= currentLines().length)) throw new Error('语音提到的歌词行不存在。请先保存相应歌词版本。');
    if (action.kind !== 'revise') {
      if (lyricsDirty() || S.candidate) throw new Error('请先保存手写歌词并处理 AI 候选，再执行锁句指令。');
      const line = S.lines[action.index];
      if (line.locked === (action.kind==='lock')) throw new Error('这一句已经处于所要求的锁定状态。');
      if (!window.confirm(`确认${action.kind==='lock'?'锁定':'解锁'}第 ${action.index+1} 句？这将创建新的歌词版本，不会调用 AI。`)) return;
      closeDialog(); await perform(ctx=>changeLock(ctx,action.index,'toggle'));
      return;
    }
    if (S.instruction && !window.confirm('用确认后的语音文字替换当前修改要求？不会自动调用 AI。')) return;
    S.instruction = text; S.selected = action.index;
    closeDialog(); persistDraft(); renderLyrics(); updateDirtyIndicators();
    $('#ai-instruction').focus(); toast(action.index===null?'已填入整首改写要求；核对后主动请求 AI 候选。':`已定位第 ${action.index+1} 句；核对后主动请求 AI 候选。`);
    return;
  }
  if (S.meta.story && !window.confirm('把确认后的转写替换当前故事文字？其他意图和歌词不变，不会自动保存。')) return;
  S.meta.story = text; S.sample = false; closeDialog(); persistDraft(); renderEditor();
  $('#story').focus(); toast('故事已填入但未保存；选中想保留的原话，点“设为必留”，再保存故事与意图。');
}
async function renderPublic(token) {
  S.view = 'public'; navState();
  $('.main-nav').hidden = true;
  main.innerHTML = '<div class="boot-state" role="status">正在读取这首小歌…</div>';
  try {
    const share = await api(`/api/public/${encodeURIComponent(token)}`,{publicRequest:true});
    const cardTheme = ['mint','lavender','cream'].includes(share.card_theme) ? share.card_theme : 'mint';
    main.innerHTML = `<section class="public-page"><div class="eyebrow">A LITTLE SONG, SHARED WITH YOU</div><div class="share-card ${cardTheme}"><small>说一拍 · 一首分享给你的小歌</small><img class="hero-art" src="/record.svg" alt="一张薄荷色唱片"><span class="pill">${share.audio.contains_original_voice?'本人原声开场 + AI 演唱':'AI 生成演唱'}</span><h1>${esc(share.title)}</h1><span>同一版歌词 · ${Math.round(share.audio.duration)} 秒</span></div><div class="card"><audio id="public-audio" controls preload="none" src="/api/public/${encodeURIComponent(token)}/audio" aria-label="播放分享的短歌"></audio>${share.audio.lyrics?`<ol class="public-lyrics">${share.audio.lyrics.map(line=>`<li>${esc(line.text)}</li>`).join('')}</ol>`:'<p class="micro-note">分享者隐藏了歌词文字，演唱声音中仍包含歌词。</p>'}<div id="page-error" class="error-box" role="alert" hidden></div><p class="privacy-note">${formatDate(share.expires_at)} 到期 · 仅供只读试听</p></div><a class="button primary" href="/">我也做一首 ${icon('arrow')}</a><p class="privacy-note">本地开发分享 · 仅当前机器可访问</p></section>`;
    document.title = `${share.title} · 说一拍`;
    $('#public-audio').addEventListener('error',()=>showError(new Error('分享音频不可用或链接已失效。')));
  } catch {
    main.innerHTML = '<section class="public-page"><img class="hero-art" src="/record.svg" alt="静静等待的唱片"><h1>这段分享，暂时听不到了。</h1><p>链接不存在、已到期或已撤回。若本机服务暂不可用，请稍后重试。</p><a class="button primary" href="/">我也做一首</a></section>';
  }
}

// 所有用户素材仅进入受转义的文本或表单字段，不作为 HTML、脚本或工具指令执行。
document.addEventListener('input',event=>{
  const target = event.target;
  if (target.id==='story') S.meta.story = target.value;
  else if (target.id==='work-title') S.meta.title = target.value;
  else if (target.dataset.intent) S.meta.intent[target.dataset.intent] = target.value;
  else if (target.dataset.line!==undefined) {
    const index = Number(target.dataset.line); S.lines[index].text = target.value; S.source = 'manual';
    const count = $(`#line-count-${index}`); count.textContent = `${size(target.value)} / 160 字`; count.classList.toggle('over-limit',size(target.value)>160);
  } else if (target.id==='ai-instruction') S.instruction = target.value;
  else if (target.id==='share-card-title') { renderShareCardPreview(); return; }
  else return;
  persistDraft(); updateDirtyIndicators();
});
document.addEventListener('change',async event=>{
  const target = event.target;
  if (['share-card-theme','share-days','share-lyrics'].includes(target.id)) { renderShareCardPreview(); return; }
  if(target.id==='quick-paid-lyrics'){Q.paidLyrics=target.checked;return;}
  if(target.id==='quick-paid-song'){Q.paidSong=target.checked;return;}
  if(target.id==='quick-style'){S.meta.style=target.value;persistDraft();return;}
  if (target.id==='clip-file') {
    try { await selectInputFile(target.files?.[0],target.dataset.voiceTarget); }
    catch (error) { showError(error); }
    finally { target.value = ''; }
    return;
  }
  if (target.dataset.mix !== undefined) {
    S.mixSelections[Number(target.dataset.mix)] = target.value === 'B' ? 'B' : 'A';
    persistDraft(); renderCandidate(); return;
  }
  if (target.id==='service-consent') {
    S.consent = target.checked;
    if (S.consent) persistDraft(); else storageRemove(draftKey());
  }
  if (target.id==='lyric-history') { pauseAudio(); S.history = target.value; renderHistory(); }
  if(target.id==='intro-clip'){pauseAudio();const player=$('#intro-source-player');player.ontimeupdate=null;player.src='/api/input-clips/'+encodeURIComponent(target.value)+'/audio';$('#intro-start').value='0';$('#intro-end').value=Math.min(3,Number(target.selectedOptions[0].dataset.duration)).toFixed(2);}
  if(target.id==='comparison-left'||target.id==='comparison-right'){pauseAudio();renderComparisonAudio(target.id.split('-')[1]);}
  if (target.id==='audio-version') { pauseAudio(); S.audioId = target.value; renderAudio(); }
});
document.addEventListener('focusin',event=>{
  if (event.target.dataset.line===undefined) return;
  S.selected = Number(event.target.dataset.line);
  for (const row of $$('[data-line-row]')) row.classList.toggle('selected',Number(row.dataset.lineRow)===S.selected);
  const label = $('label[for="ai-instruction"]'); if (label) label.textContent = `只改第 ${S.selected+1} 句 · 你的修改要求`;
  const tools = $('.ai-tools .actions');
  if (tools && !$('[data-action="clear-selection"]',tools)) tools.insertAdjacentHTML('beforeend',btn('clear-selection','改整首','small quiet'));
});
document.addEventListener('click',async event=>{
  const target = event.target.closest('[data-action]'); if (!target || target.disabled) return;
  const action = target.dataset.action;
  try {
    if(action.startsWith('quick-'))return await quickAction(action);
    if (['tap-beat','reset-taps','beat-preview','save-tempo','clear-tempo'].includes(action)) return await rhythmAction(action);
    if(action==='add-intro')return introDialog();
    if(action==='preview-intro-range')return previewIntroRange();
    if(action==='compare-line')return await comparisonDialog(Number(target.dataset.index));
    if(action==='save-origin')return await perform(ctx=>saveOrigin(target.dataset.lineId,ctx));
    if (action==='nav-create') return await navigate('create');
    if (action==='nav-diary') return await navigate('diary');
    if (action.startsWith('diary-')) return await diaryAction(action,target);
    if (action==='nav-library') return await navigate('library');
    if (action==='nav-diagnostics') return await navigate('diagnostics');
    if (['refresh-diagnostics','analytics-context','start-study','finish-study','withdraw-study','record-cost'].includes(action)) return await diagnosticsAction(action,target);
    if (action==='nav-status') return await navigate('status');
    if (action==='new-work') { S.quick=false; return await navigate('create',{fresh:true}); }
    if (action==='open-work') return await openWork(target.dataset.workId);
    if (action==='refresh-library') return await renderLibrary();
    if (action==='dialog-close') { closeDialog(); return; }
    if (action==='dialog-confirm') {
      const callback = S.dialogConfirm; const version = S.dialogVersion;
      if (callback) await perform(async ctx=>{ await callback(ctx); if (ctx.valid()&&version===S.dialogVersion) closeDialog(); });
      return;
    }
    if (action.startsWith('step-')) {
      const step = action.slice(5); $(`#${step}-section`)?.scrollIntoView({behavior:'smooth',block:'start'});
      scheduleStepUpdate();
      return;
    }
    if (action==='voice') { voiceDialog(); return; }
    if (action==='capture-preserve') { capturePreserve(); return; }
    if (action==='voice-instruction') { voiceDialog('instruction'); return; }
    if (action==='record-start') { await startVoice(target.dataset.voiceTarget); return; }
    if (action==='record-stop') { stopVoice(false); return; }
    if (action==='transcribe-clip') { await perform(()=>transcribePending(target.dataset.role)); return; }
    if (action==='accept-transcription') {
      const suggestion = $('#transcription-result p');
      if (!suggestion) return;
      if ($('#transcript').value.trim() && !window.confirm('采用转写建议会替换弹窗内手写文字。确定替换吗？')) return;
      $('#transcript').value = suggestion.textContent;
      $('#transcript').focus(); toast('建议已填入待确认文字；请核对，再主动填入作品。'); return;
    }
    if (action==='discard-input-clip') {
      const role = target.dataset.role;
      if (S.pendingClip?.role === role) { pauseAudio(); clearPendingClip(); renderInputDialogState(role); if (S.work) renderEditor(); else if ($('#story-section')) $('#story-section').innerHTML = storyMarkup(); updateDirtyIndicators(); toast('待保存原音已丢弃。'); }
      return;
    }
    if (action==='use-transcript') { await useTranscript(target.dataset.voiceTarget); return; }
    if (action==='save-input-clip') { await perform(ctx=>saveInputClip(ctx,target.dataset.role)); return; }
    if (action==='remove-input-clip') { await perform(ctx=>removeInputClip(ctx,target.dataset.clipId)); return; }
    if (action==='sample') { if (!S.work) useSample(); return; }
    if (action==='resume-draft') { resumeDraft(); return; }
    if (action==='discard-draft') {
      if (!window.confirm('丢弃此标签页暂存的未提交草稿？')) return;
      storageRemove(draftKey()); S.availableDraft = null; renderEditor(); return;
    }
    if (action==='scene') {
      S.scene = target.dataset.scene; S.meta.intent.attitude = S.scene;
      if (S.scene==='写给朋友' && !S.meta.intent.recipient) S.meta.intent.recipient = '朋友';
      for (const button of $$('.chip')) button.setAttribute('aria-pressed',String(button===target));
      $('#intent-attitude').value = S.meta.intent.attitude; $('#intent-recipient').value = S.meta.intent.recipient;
      persistDraft(); updateDirtyIndicators(); return;
    }
    if (action==='add-line') {
      if (S.lines.length<4) { S.lines.push({text:'',locked:false,locked_spans:[]}); S.source='manual'; persistDraft(); renderLyrics(); $(`#line-${S.lines.length-1}`).focus(); } return;
    }
    if (action==='remove-line') {
      const index = Number(target.dataset.index); const line = S.lines[index];
      if (S.lines.length>2&&!line.locked&&!line.locked_spans.length) {
        if (line.text && !window.confirm('从当前编辑稿移除这句？保存后会生成新版本，旧历史仍保留。')) return;
        S.lines.splice(index,1); S.selected=null; S.source='manual'; persistDraft(); renderLyrics();
      } return;
    }
    if (action==='clear-selection') { S.selected=null; renderLyrics(); return; }
    if (action==='discard-candidate') { S.candidate=null; S.candidateB=null; S.mixSelections=[]; persistDraft(); renderLyrics(); return; }
    if (action==='style') {
      S.meta.style=target.dataset.style;
      for (const button of $$('.style-option')) button.setAttribute('aria-pressed',String(button===target));
      const save = $('[data-action="save-style"]'); if (save) save.disabled = S.meta.style===S.work?.style;
      persistDraft(); updateDirtyIndicators(); return;
    }
    if (action==='generate-lyrics') { requestLyrics('A'); return; }
    if (action==='generate-lyrics-b') { requestLyrics('B'); return; }
    if (action==='song') { songDialog(); return; }
    if (action==='prepare-short') { await prepareShort(target.dataset.jobId); return; }
    if (action==='review-audio') { await reviewAudio(target.dataset.jobId); return; }
    if (action==='cancel-job') { cancelJob(); return; }
    if (action==='cancel-compare') { cancelJob(target.dataset.jobId); return; }
    if (action==='share') { shareDialog(); return; }
    if (action==='copy-current-share') { await copyText($('#share-url').value); return; }
    if (action==='copy-share') { const value=S.shareURLs.get(target.dataset.shareId); if (value) await copyText(value); return; }
    if (action==='revoke-share') { revokeShare(target.dataset.shareId); return; }
    if (action==='delete-work') { deleteDialog(target.dataset.workId,Number(target.dataset.version)); return; }
    if (action==='rebase-local') {
      if (!S.remote) return;
      const remote = S.remote; S.work = remote; S.editBase = null; closeDialog(); clearError(); persistDraft(); renderEditor(); startPoll();
      toast('已同步版本号，本地输入仍保留。请核对后保存，旧候选需重新生成。'); return;
    }
    if (action==='load-remote') {
      if (!S.remote || !window.confirm('确实放弃全部本地未保存修改和 AI 候选，载入服务端最新内容？')) return;
      const remote=S.remote; storageRemove(draftKey()); adoptWork(remote,{meta:true,lyrics:true}); closeDialog(); clearError(); renderEditor(); startPoll(); return;
    }
    await perform(async ctx=>{
      if (action==='create') await saveStory(ctx,true);
      if (action==='save-story') { ensureWork(); await saveStory(ctx,false); }
      if (action==='save-lyrics') await saveLines(ctx);
      if (action==='toggle-lock') await changeLock(ctx,Number(target.dataset.index),'toggle');
      if (action==='lock-span') await changeLock(ctx,Number(target.dataset.index),'span');
      if (action==='remove-span') await changeLock(ctx,Number(target.dataset.index),'remove-span',Number(target.dataset.spanIndex));
      if (action==='undo') await restoreLyrics(ctx,currentLyric()?.parent_id);
      if (action==='restore-history') await restoreLyrics(ctx,S.history);
      if (['apply-candidate-a','apply-candidate-b','apply-mixed'].includes(action)) {
        if (lyricsDirty()) throw new Error('编辑稿有未保存改动，请先保存或处理，再应用候选。');
        const chosen = action==='apply-candidate-b' ? S.candidateB : S.candidate;
        if (!chosen || chosen.base_version!==S.work.version || (S.candidateB && S.candidateB.base_version!==S.work.version)) throw new Error('候选版本已过期，请对照后重新生成。');
        const lines = action==='apply-mixed' ? chosenCandidateLines() : chosen.lines;
        await saveLines(ctx,lines,'ai',chosen.base_version);
      }
      if (action==='save-style') await saveStyle(ctx);
      if (action==='retry-job') await submitJob(ctx);
      if (action==='refresh-job') await refreshJob(ctx);
      if (action==='keep-audio') await keepAudio(ctx);
      if (action==='inspect-conflict') await inspectConflict(ctx);
      if (action==='feedback') {
        const category=$('#feedback-category').value; const audio=selectedAudio();
        if (!category||!audio) throw new Error('请先选择一个反馈类别。');
        await sendEvent('feedback_submitted',{work_id:S.work.id,audio_id:audio.id,category},true);
        if (ctx.valid()) toast('反馈类别已提交，不含正文。');
      }
      if (action==='refresh-capabilities') {
        const cap=await api('/api/capabilities'); if (!ctx.valid()) return;
        S.cap=cap; renderStatus(); toast('已读取最新接入状态。');
      }
      if (action==='retry-boot') await boot();
    });
  } catch (error) { showError(error); }
});
dialog.addEventListener('cancel',event=>{ event.preventDefault(); closeDialog(); });
dialog.addEventListener('click',event=>{ if (event.target===dialog) { const rect=dialog.getBoundingClientRect(); if (event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom) closeDialog(); } });
window.addEventListener('beforeunload',event=>{
  persistDraft();
  if (isDirty() || S.voice || S.operation) { event.preventDefault(); event.returnValue=''; }
});
document.addEventListener('play',event=>{ stopBeat();if (event.target instanceof HTMLAudioElement) for (const audio of $$('audio')) if (audio!==event.target) audio.pause(); },true);
window.addEventListener('pagehide',()=>{ persistDraft(); stopVoice(true); stopPoll(); pauseAudio(); clearPendingClip(); S.epoch++; });
window.addEventListener('pageshow',event=>{ if (event.persisted && S.view==='create') startPoll(); });
const Q={step:1,paidLyrics:false,paidSong:false};
const quickKey=()=>`syp:quick:${S.csrf.slice(0,12)}`;
function renderQuick(){
  stopStepTracking();pauseAudio();
  if(S.work)storageSet(quickKey(),{work_id:S.work.id,step:Q.step});
  main.innerHTML=`<div class="page-heading"><div><div class="eyebrow">YOUR FIRST LITTLE SONG</div><h1>三步，听见自己的小歌。</h1><p>留一句话，确认歌词，然后听你的歌。</p></div>${btn('quick-full','打开完整工作室','quiet')}</div><ol class="quick-steps" aria-label="简易创作步骤">${['留一句话','确认歌词','听我的歌'].map((x,i)=>`<li ${Q.step===i+1?'aria-current="step"':''}><span>${i+1}</span>${x}</li>`).join('')}</ol><div class="quick-workspace">${errorMarkup()}${draftMarkup()}<section class="card quick-card" id="${Q.step===1?'story':Q.step===2?'lyrics':'music'}-section">${Q.step===1?quickStory():Q.step===2?quickLyrics():`<h2>听我的歌</h2><p>歌词已经保存。生成期间可以离开，回来继续听。</p><div id="quick-result"></div><div class="actions">${btn('quick-back-lyrics','回到歌词','quiet')}${btn('quick-full','版本、审核与更多创作工具','quiet')}</div>`}</section></div>`;
  if(Q.step===3)renderQuickResult();renderError();updateDirtyIndicators();
}
function quickStory(){return `<h2>今天想留住哪句话？</h2><label class="form-label" for="story">一句原话，最多 80 字</label><textarea id="story" rows="3" maxlength="80" placeholder="比如：别急，我在呢">${esc(S.meta.story)}</textarea><p class="micro-note">这句原话会保留在歌词中。简易版默认“轻快一点”，后面可以换。</p><div class="actions">${btn('voice',`${icon('mic')}录音 / 上传原声`,'quiet')}</div>${clipMarkup('story')}<label class="consent"><input id="service-consent" type="checkbox" ${S.consent?'checked':''}><span>同意将故事和作品保存在本机；主动请求 AI 时，相关文字会提交给生成服务。</span></label>${S.cap?.lyrics?`<label class="consent"><input id="quick-paid-lyrics" type="checkbox" ${Q.paidLyrics?'checked':''}><span>有权提交这些文字，同意本次 AI 写词可能产生费用。</span></label>${S.lyricsCallUncertain?'<p class="notice warning">上次写词结果不明确，可能已计费。请先核对用量；再次点击会发起一笔新请求。</p>':''}${btn('quick-ai','生成短歌词 · 可能计费','primary wide')}`:'<p>AI 写词未接入，可以自己写两句。</p>'}${btn('quick-manual','下一步 · 我自己写两句','quiet wide')}`;}
function quickLyrics(){return `<h2>像你说的话吗？</h2><p>可以直接改字。确认后保存歌词，再开始生成歌曲。</p>${S.lines.map((line,i)=>`<label class="form-label" for="line-${i}">第 ${i+1} 句${line.locked?' · 已锁定，请到完整工作室解锁':''}</label><textarea id="line-${i}" data-line="${i}" rows="2" ${line.locked?'readonly':''}>${esc(line.text)}</textarea><p id="line-count-${i}" class="micro-note">${size(line.text)} / 160 字</p>`).join('')}<details class="quick-details"><summary>音乐感觉与原声（可选）</summary><label>音乐感觉<select id="quick-style">${Object.entries(styleNames).map(([key,name])=>`<option value="${key}" ${S.meta.style===key?'selected':''}>${name}</option>`).join('')}</select></label>${clipMarkup('story')}<p>原声开场可在歌曲完成后选择添加，不会自动分享你的声音。</p></details>${S.cap?.singing?`<label class="consent"><input id="quick-paid-song" type="checkbox" ${Q.paidSong?'checked':''}><span>已核对歌词并有权使用；同意本次歌曲生成可能产生费用。失败或取消也可能计费。</span></label>${btn('quick-song','确认歌词并生成歌曲','primary wide',!!S.pendingJob||S.work?.jobs.some(j=>['queued','generating'].includes(j.status))||S.work?.jobs.filter(j=>j.status==='checking').length>=3)}`:'<p>演唱服务未接入，可以先保存歌词。</p>'}${btn('quick-save','只保存歌词，暂不生成','quiet wide')}${btn('quick-back-story','上一步 · 原话','quiet')}`;}
function renderQuickResult(){
  const node=$('#quick-result');if(!node)return;
  const job=latestJob(),audio=selectedAudio();
  node.innerHTML=`${S.pendingJob?`<div class="notice warning">生成请求结果未明确。核对会沿用同一个请求，不重复建单。</div>${btn('retry-job','核对上次生成请求','primary')}`:''}${job?`<div class="job-panel"><h3>${job.preview_ready?'你的歌已返回，先听听':statusNames[job.status]||'读取状态'}</h3>${job.status==='checking'&&job.preview_ready?`<p>这是未经唱词、音质和使用权核验的私有试听，不会自动分享或导出。</p><audio controls preload="none" src="/api/jobs/${encodeURIComponent(job.id)}/preview" aria-label="简易版私有小歌试听"></audio><details class="quick-details"><summary>核对歌词、审核与保存为正式作品</summary><ol>${job.snapshot.lyrics.map(l=>`<li>${esc(l.text)}</li>`).join('')}</ol>${btn('review-audio','进入完整试听审核','small primary',false,`data-job-id="${esc(job.id)}"`)}${btn('cancel-job','丢弃这次候选','small quiet')}</details>`:job.status==='queued'||job.status==='generating'?`<p>正在处理真实任务，暂时没有可试听的音频。</p>${btn('cancel-job','停止接收本次生成','small quiet')}`:job.status==='failed'||job.status==='timed_out'||job.status==='cancelled'?`<p>${esc(job.error?.message||'本次没有生成歌曲，原话和歌词已保留。')}</p>${btn('quick-back-lyrics','检查歌词再试一次','quiet')}`:''}${btn('refresh-job','刷新实际结果','small quiet')}</div>`:''}${audio?`<div class="audio-card"><h3>已保存的小歌${audio.lyric_id!==S.work.current_lyric_id?' · 较早的歌词版本':''}</h3><audio id="work-audio" controls preload="none" src="/api/audio/${encodeURIComponent(audio.id)}" aria-label="简易版正式小歌试听"></audio><div class="actions">${btn('keep-audio',audio.kept?'已保留':'保留这首歌','primary',audio.kept)}${btn('add-intro','加入我的原声开场','quiet',audio.kind==='voice_intro')}${btn('quick-full','分享 / 导出与版本','quiet')}</div></div>`:!job&&!S.pendingJob?'<p>歌词已保存；想听到歌曲，请回到歌词页主动确认一次生成。</p>':''}`;
  if(audio)bindAudio($('#work-audio'),audio,S.work.id);
}
async function quickSaveStory(ctx){
  validateText(S.meta.story,'这一句原话',80);
  if(/[\r\n]/u.test(S.meta.story))throw new Error('简易版请留一句话，不要换行；长故事可以到完整工作室写。');
  if(!S.consent)throw new Error('请先同意本机保存与创作服务');
  if(S.candidate||S.candidateB)throw new Error('请先在完整工作室处理已有歌词候选');
  if(!S.work||S.meta.intent.preserve===S.work.story)S.meta.intent.preserve=S.meta.story;
  else if(!S.meta.intent.preserve.split('\n').includes(S.meta.story))S.meta.intent.preserve=[S.meta.intent.preserve,S.meta.story].filter(Boolean).join('\n');
  S.meta.intent.theme||='我的日常';S.meta.intent.attitude||='自然口语';S.meta.title||=S.meta.story.slice(0,30);
  const creating=!S.work,oldKey=draftKey(),payload=creating?{...clone(S.meta),service_consent:true}:{...clone(S.meta),base_version:baseVersion()};
  const work=await api(creating?'/api/works':workPath(),{method:creating?'POST':'PATCH',body:payload});
  if(!ctx.valid())return false;adoptWork(work,{meta:true});S.consent=true;if(creating)storageRemove(oldKey);storageSet(quickKey(),{work_id:work.id,step:1});return true;
}
async function quickAction(action){
  if(action==='quick-enter'){if(S.candidate||S.candidateB)throw new Error('请先在完整工作室应用或丢弃已有歌词候选');if(size(S.meta.story)>80)throw new Error('这段故事超过 80 字，请继续使用完整工作室；简易版适合一句原话');S.quick=true;Q.step=S.work?(currentLyric()?3:2):1;renderEditor();startPoll();focusMain();return;}
  if(action==='quick-resume'){const saved=storageGet(quickKey());if(!saved?.work_id)throw new Error('尚无简易版作品');await openWork(saved.work_id);if(S.work?.id===saved.work_id){S.quick=true;Q.step=currentLyric()?3:1;renderEditor();startPoll();}return;}
  if(action==='quick-full'){S.quick=false;renderEditor();startPoll();return;}
  if(action.startsWith('quick-back-')){pauseAudio();Q.step=action.endsWith('story')?1:2;renderQuick();focusMain();return;}
  await perform(async ctx=>{
    if(action==='quick-ai'||action==='quick-manual'){
      if(action==='quick-ai'&&!Q.paidLyrics)throw new Error('请确认本次 AI 写词调用和可能产生的费用');
      if(S.lyricsCallUncertain&&action==='quick-ai'&&!window.confirm('上一笔写词可能已计费。核对用量后，仍要发起一笔新请求吗？'))return;
      if(!await quickSaveStory(ctx))return;
      if(action==='quick-ai'){
        try{const candidate=await api(workPath('/lyrics/generate'),{method:'POST',body:{base_version:baseVersion(),instruction:'写成两行自然口语的短歌词，逐字保留必留原话，不添加额外人物和经历',paid_call_confirmed:true}});if(!ctx.valid())return;S.lines=clone(candidate.lines);S.source='ai';S.lyricsCallUncertain=false;}
        catch(error){if(ctx.valid()){if(!error.status||error.status>=500||error.code==='INVALID_RESPONSE')S.lyricsCallUncertain=true;renderQuick();}throw error;}
      }else if(!currentLyric()&&!S.lines.some(l=>l.text)){S.lines=[{text:S.meta.story,locked:false,locked_spans:[]},{text:'',locked:false,locked_spans:[]}];S.source='manual';}
      Q.paidLyrics=false;Q.step=2;persistDraft();renderQuick();focusMain();return;
    }
    if(action==='quick-song'||action==='quick-save'){
      if(action==='quick-song'&&!Q.paidSong)throw new Error('请确认歌词使用权及本次歌曲生成费用');
      validateLines(S.lines);
      if(metaDirty()){
        const work=await api(workPath(),{method:'PATCH',body:{...clone(S.meta),base_version:baseVersion()}});if(!ctx.valid())return;adoptWork(work,{meta:true});
      }
      if(lyricsDirty()||!currentLyric())await saveLines(ctx);
      if(!ctx.valid())return;Q.step=3;Q.paidSong=false;renderQuick();focusMain();
      if(action==='quick-song'){
        ensureSavedAll();S.pendingJob={base_version:baseVersion(),lyric_id:currentLyric().id,idempotency_key:crypto.randomUUID(),confirmed:true,paid_call_confirmed:true};storageSet(jobKey(),S.pendingJob);await submitJob(ctx);
      }return;
    }
  });
}

async function boot() {
  const publicMatch=location.pathname.match(/^\/s\/([A-Za-z0-9_-]+)\/?$/);
  if (publicMatch) { await renderPublic(publicMatch[1]); return; }
  if (location.pathname.startsWith('/s/')) { await renderPublic('invalid'); return; }
  try {
    const session=await api('/api/session',{method:'POST',body:{}}); S.csrf=session.csrf;
    S.cap=await api('/api/capabilities');
    await api('/api/analytics/context',{method:'POST',body:{device:matchMedia('(max-width: 600px)').matches?'mobile':matchMedia('(max-width: 1024px)').matches?'tablet':'desktop',entry_source:'direct'}});
    S.availableDraft=storageGet(draftKey());
    renderEditor(); navState();
    sendEvent('create_entry_view',{source:'client'});
  } catch (error) {
    main.innerHTML=`<div class="boot-state"><h1>还没连上本机工作室。</h1><p>请确认后端服务正在运行。你的内容不会被替换成演示数据。</p>${errorMarkup()}${btn('retry-boot','重新连接','primary')}</div>`; showError(error);
  }
}
boot();

document.addEventListener('keydown',event=>{if(event.code==='Space'&&event.target?.dataset.action==='tap-beat'&&!event.repeat){event.preventDefault();rhythmAction('tap-beat');}});
