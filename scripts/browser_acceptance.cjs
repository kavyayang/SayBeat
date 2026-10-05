// Isolated Chrome profile + temporary server/database. No paid model requests.
const {chromium} = require('/Users/y/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const {execFileSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname,'..');
const python = '/Users/y/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3';
const evidence = '/tmp/shuoyipai-review';
const url = 'http://127.0.0.1:8875';

(async()=>{
  const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();
  const results=[];const errors=[];let externalRequests=0;let microphoneCalls=0;
  page.on('pageerror',e=>errors.push(e.message));
  page.on('dialog',d=>d.accept()); // Isolated-test discard confirmation only.
  page.on('request',r=>{if(!r.url().startsWith(url)&&!r.url().startsWith('blob:')) externalRequests++;});
  await page.addInitScript(()=>{
    window.__microphoneCalls=0;
    if(navigator.mediaDevices) navigator.mediaDevices.getUserMedia=async()=>{
      window.__microphoneCalls++;throw new DOMException('Permission denied for test','NotAllowedError');
    };
  });
  try {
    await page.goto(url);
    await page.getByRole('button',{name:'录一段 / 上传音频',exact:true}).waitFor();
    await page.getByRole('button',{name:'录一段 / 上传音频',exact:true}).click();
    await page.getByRole('button',{name:'开始录音',exact:true}).click();
    await page.locator('#dialog-error').filter({hasText:'请先同意'}).waitFor();
    assert.equal(await page.evaluate(()=>window.__microphoneCalls),0);
    await page.locator('#voice-consent').check();
    await page.getByRole('button',{name:'开始录音',exact:true}).click();
    await page.locator('#dialog-error').filter({hasText:'未获得麦克风权限'}).waitFor();
    assert.equal(await page.evaluate(()=>window.__microphoneCalls),1);
    assert.equal(await page.getByRole('button',{name:'开始录音',exact:true}).isEnabled(),true);
    results.push({check:'microphone_consent_and_permission_denial',passed:true,method:'simulated NotAllowedError; not a phone test'});
    await page.locator('#clip-file').setInputFiles(path.join(root,'data/acceptance-20261004/live-original.mp3'));
    await page.locator('#clip-preview audio').waitFor();
    assert.equal(await page.locator('#auto-transcribe').isChecked(),false);
    assert.equal(await page.locator('#transcribe-consent').isChecked(),false);
    results.push({check:'real_mp3_upload_private_preview_no_auto_transcription',passed:true,source:'AI generated MP3'});
    await page.screenshot({path:path.join(evidence,'upload-desktop.png')});
    await page.getByRole('button',{name:'关闭对话框',exact:true}).click();

    const cookies=await context.cookies();
    const token=cookies.find(v=>v.name==='syp_session').value;
    const seed=JSON.parse(execFileSync(python,['scripts/seed_acceptance.py'],{cwd:root,input:JSON.stringify({
      data_dir:path.join(evidence,'browser-data'),session_token:token,
      mp3:path.join(root,'data/acceptance-20261004/live-original.mp3'),short_wav:path.join(root,'data/acceptance-20261004/live-short.wav')
    }),encoding:'utf8'}));
    await page.getByRole('button',{name:'我的小歌',exact:true}).click();
    await page.getByRole('button',{name:'验收专用 · 非正式作品',exact:true}).click();
    await page.locator('[data-action="review-audio"]').first().click();
    await page.locator('#heard-lyrics').waitFor();
    assert.match(await page.locator('#dialog-content').textContent(),/整曲调速 WAV.*29\.5/);
    assert.equal(await page.locator('#audit-processing_rights').isChecked(),false);
    await page.locator('#heard-lyrics').fill('别急我在呢\n晚风陪你慢慢走\n额外测试歌词');
    await page.locator('#rights-reference').fill('自动化负向验收，未核验使用权，不得批准正式音频');
    await page.getByRole('button',{name:'保存审核结果',exact:true}).click();
    await page.locator('#dialog-error').filter({hasText:'唱词与原文不一致'}).waitFor();
    const details=await page.evaluate(async wid=>(await fetch('/api/works/'+wid)).json(),seed.work_id);
    assert.equal(details.audios.length,0);
    assert.equal(details.jobs[0].snapshot.reviews.length,1);
    results.push({check:'short_variant_review_wrong_lyrics_and_no_rights_block_promotion',passed:true});
    await page.screenshot({path:path.join(evidence,'audio-review-desktop.png')});
    await page.setViewportSize({width:390,height:844});
    await page.locator('dialog').evaluate(d=>d.scrollTop=0);
    await page.screenshot({path:path.join(evidence,'audio-review-mobile.png')});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    results.push({check:'mobile_width_review_dialog',passed:true,method:'390px Chrome viewport; not a physical phone'});
    assert.equal(externalRequests,0);
    assert.deepEqual(errors,[]);
    results.push({check:'no_external_requests_or_javascript_errors',passed:true});
  } finally {
    fs.writeFileSync(path.join(evidence,'browser-results.json'),JSON.stringify({results,errors,externalRequests},null,2));
    await browser.close();
  }
  console.log(JSON.stringify(results));
})().catch(e=>{console.error(e.message);process.exitCode=1;});
