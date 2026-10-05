// Isolated offline E2E: no vendor calls, no existing user database touched.
const {chromium}=require('/Users/y/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const {spawn}=require('node:child_process'), fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),python='/Users/y/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
(async()=>{
 const data=fs.mkdtempSync(path.join(os.tmpdir(),'syp-diagnostics-'));const evidence=process.argv[2]||data;fs.mkdirSync(evidence,{recursive:true});
 const server=spawn(python,['-u','-c',"from server import make_server;from backend.providers import Gateway;import sys;s=make_server(0,sys.argv[1],Gateway());print(s.server_port,flush=True);s.serve_forever()",data],{cwd:root,stdio:['ignore','pipe','pipe']});
 let browser;const results=[],errors=[];let external=0;
 const port=await new Promise((resolve,reject)=>{server.stdout.once('data',d=>resolve(Number(d.toString().trim())));server.once('exit',c=>reject(new Error('Test server exited '+c)));server.stderr.on('data',d=>process.stderr.write(d));});
 const url='http://127.0.0.1:'+port;
 try{
  browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});const context=await browser.newContext({viewport:{width:1280,height:900}});const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(!r.url().startsWith(url)&&!r.url().startsWith('blob:'))external++});page.on('dialog',d=>d.accept());
  await page.goto(url);await page.locator('#story').waitFor();
  const api=async(method,route,body)=>page.evaluate(async({method,route,body})=>{const session=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json());const response=await fetch(route,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf},...(method==='GET'?{}:{body:JSON.stringify(body||{})})});return {status:response.status,body:await response.json()};},{method,route,body});
  await api('POST','/api/analytics/context',{traffic:'test',device:'desktop'});await api('POST','/api/events',{event_id:'test-entry',name:'create_entry_view',client_at:new Date().toISOString()});
  await page.getByRole('button',{name:'创作诊断',exact:true}).click();await page.getByRole('heading',{name:'完整漏斗 · 会话去重'}).waitFor();
  assert.equal((await api('GET','/api/internal/diagnostics')).body.funnel[1].sessions,0);results.push({check:'test_traffic_excluded_by_default',passed:true});
  await page.getByRole('button',{name:'开始自愿试玩',exact:true}).click();await page.locator('#page-error').filter({hasText:'独立同意'}).waitFor();assert.equal((await api('GET','/api/internal/diagnostics?traffic=test')).body.studies.length,0);
  await page.getByRole('button',{name:'开始创作',exact:true}).click();await page.locator('#story').fill('团队原创测试：冰块先下班');
  const work=(await api('POST','/api/works',{story:'团队原创测试：冰块先下班',service_consent:true,intent:{theme:'日常',attitude:'平静',preserve:'',avoid:'',recipient:''}})).body;
  assert.ok(work.id);results.push({check:'refuse_study_can_create_no_sampling',passed:true});
  await page.getByRole('button',{name:'创作诊断',exact:true}).click();await page.locator('#study-consent').check();await page.getByRole('button',{name:'开始自愿试玩',exact:true}).click();await page.getByRole('button',{name:'结束并自评',exact:true}).waitFor();
  await page.getByRole('button',{name:'结束并自评',exact:true}).click();for(const key of ['independent_completion','recognized_original','wanted_revision','needed_help'])await page.locator('#study-'+key).selectOption('false');await page.getByRole('button',{name:'保存自评',exact:true}).click();await page.locator('.study-row').filter({hasText:'已完成'}).waitFor();
  let diag=(await api('GET','/api/internal/diagnostics?traffic=test')).body;assert.equal(diag.study_summary.completed,1);assert.equal(diag.studies[0].results.recognized_original,false);results.push({check:'independent_consent_timer_false_results_not_prefilled',passed:true,method:'automated form test; not participant research'});
  await page.getByRole('button',{name:'撤回并删除',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.study-row'));assert.equal((await api('GET','/api/internal/diagnostics?traffic=test')).body.studies.length,0);results.push({check:'withdraw_study_purges_record_keeps_work',passed:true});
  await page.locator('#diagnostic-traffic').selectOption('test');await page.getByRole('button',{name:'刷新',exact:true}).click();await page.locator('#diagnostic-traffic').waitFor();assert.equal((await api('GET','/api/internal/diagnostics?traffic=test')).body.funnel[1].sessions,1);
  await page.screenshot({path:path.join(evidence,'diagnostics-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(evidence,'diagnostics-mobile.png'),fullPage:true});results.push({check:'desktop_mobile_diagnostics_no_document_overflow',passed:true});
  // A second anonymous browser cannot read this identity's studies or works.
  const other=await browser.newContext();const second=await other.newPage();await second.goto(url);await second.locator('#story').waitFor();const privateResult=await second.evaluate(async wid=>({work:(await fetch('/api/works/'+wid)).status,diag:await fetch('/api/internal/diagnostics?traffic=all').then(r=>r.json())}),work.id);assert.equal(privateResult.work,404);assert.equal(privateResult.diag.study_summary.consented,0);results.push({check:'diagnostics_owner_isolation',passed:true});await other.close();
  assert.deepEqual(errors,[]);assert.equal(external,0);results.push({check:'no_browser_errors_or_external_requests',passed:true});
  fs.writeFileSync(path.join(evidence,'diagnostics-browser-results.json'),JSON.stringify({scope:'isolated_automated_test',results,errors,external_requests:external},null,2)+'\n');console.log(JSON.stringify({passed:results.length,evidence}));
 }finally{if(browser)await browser.close();server.kill('SIGTERM');fs.rmSync(data,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
