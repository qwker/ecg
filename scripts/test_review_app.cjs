// Browser tests use an isolated temporary profile and synthetic manual clicks.
// Test downloads are not independent heart-beat references.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.ECG_PLAYWRIGHT_PATH || 'playwright');
const root = path.resolve(__dirname, '..');
const out = process.env.ECG_TEST_OUTPUT_DIR || path.join(root, 'outputs/reference_review/annotator_test');
fs.mkdirSync(out, {recursive:true});
const url = pathToFileURL(path.join(root, 'outputs/reference_review/blind/annotate.html')).href;
const refs = JSON.parse(fs.readFileSync(path.join(root, 'outputs/reference_review/blind/reference_template.json')));
const meta = id => refs.find(x=>x.case_id===id);
const checks=[];
let browser;
(async()=>{
  browser=await chromium.launch({headless:true, executablePath:process.env.ECG_BROWSER_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context=await browser.newContext({acceptDownloads:true, viewport:{width:1600,height:1400}});
  const page=await context.newPage(), errors=[], requests=[];
  page.on('pageerror',e=>errors.push(e.message)); page.on('request',r=>requests.push(r.url()));
  await page.goto(url); await page.waitForFunction(()=>document.querySelector('#loading').hidden);
  async function draft(){return page.evaluate(()=>{const id=document.querySelector('#caseSelect').value;const key=Object.keys(localStorage).find(k=>k.startsWith('ecg-manual-v2:'+id+':'));return key?JSON.parse(localStorage.getItem(key)):null;});}
  async function geometry(){await page.locator('#wave').scrollIntoViewIfNeeded();return page.locator('#wave').boundingBox();}
  async function clickAt(fraction,channel=1,span=2500){const b=await geometry(),start=Number(await page.locator('#position').inputValue());const x=88+fraction*(b.width-108),y=28+(channel-.5)*(788-58)/8;await page.mouse.click(b.x+x,b.y+y);return Math.round(start+fraction*span);}
  async function interval(a,b){const box=await geometry();const x=f=>box.x+88+f*(box.width-108);await page.mouse.move(x(a),box.y+80);await page.mouse.down();await page.mouse.move(x(b),box.y+80,{steps:8});await page.mouse.up();}
  async function open(id){await page.locator('#caseSelect').selectOption(id);await page.waitForFunction(()=>document.querySelector('#loading').hidden && !document.querySelector('#save').disabled);}
  async function saveDoc(){const pending=page.waitForEvent('download');await page.locator('#save').click();const download=await pending;const p=path.join(out,'synthetic-test-export.json');await download.saveAs(p);return {doc:JSON.parse(fs.readFileSync(p)),filename:download.suggestedFilename()};}
  await page.locator('#reviewer').fill('AUTOMATED_UI_TEST_NOT_REFERENCE');await page.locator('#reviewer').press('Tab');
  await page.locator('#exposure').selectOption('false');
  const expected=await clickAt(.314159,2);
  let s=await draft();assert.equal(s.events.length,1);assert.equal(s.events[0].sample_index,expected);assert.equal(s.events[0].channel,2);assert.equal(s.events[0].beat_type,'unknown');assert.equal(s.events[0].position_source,'manual_click_no_peak_snap');
  checks.push('Exact coordinate-to-sample conversion; unknown type default; no peak snapping');
  await page.locator('#beatType').selectOption('normal_sinus_supported');await page.locator('#apply').click();assert.equal((await draft()).events[0].beat_type,'normal_sinus_supported');
  await page.locator('#undo').click();assert.equal((await draft()).events[0].beat_type,'unknown');
  await page.locator('#redo').click();assert.equal((await draft()).events[0].beat_type,'normal_sinus_supported');
  await clickAt(.66,7);assert.equal((await draft()).events.length,2);
  await page.locator('#delete').click();assert.equal((await draft()).events.length,1);await page.locator('#undo').click();assert.equal((await draft()).events.length,2);
  checks.push('Type changes, deletion, undo and redo preserve manual events');
  await page.locator('[data-mode="select"]').click();
  let box=await geometry(),start=Number(await page.locator('#position').inputValue()),firstX=box.x+88+(expected-start)/2500*(box.width-108),newX=box.x+88+.47*(box.width-108);
  await page.mouse.move(firstX,box.y+80);await page.mouse.down();await page.mouse.move(newX,box.y+80,{steps:10});await page.mouse.up();
  assert.equal((await draft()).events[0].sample_index,Math.round(start+.47*2500));
  checks.push('Dragging an existing manual marker uses the released coordinate without snapping');
  await page.locator('[data-mode="beat"]').click();await page.locator('[data-span="1"]').click();
  const zoomExpected=await clickAt(.2718,1,250);assert((await draft()).events.some(e=>e.sample_index===zoomExpected));
  const beforeResize=(await draft()).events.length;
  await page.setViewportSize({width:1200,height:1400});await page.waitForTimeout(100);
  const resizedExpected=await clickAt(.8123,3,250);assert.equal((await draft()).events.length,beforeResize+1);assert((await draft()).events.some(e=>e.sample_index===resizedExpected));
  checks.push('Time zoom and responsive canvas preserve global sample coordinates');
  await page.setViewportSize({width:1600,height:1400});await page.locator('[data-span="60"]').click();await page.locator('[data-mode="interval"]').click();
  await interval(.15,.35);s=await draft();assert.equal(s.intervals.length,1);assert.equal(s.intervals[0].start_sample,meta('case_001').evaluation_start_sample+2250);assert.equal(s.intervals[0].end_sample,meta('case_001').evaluation_start_sample+5250);
  const revision=s.revision;await interval(.2,.4);assert.equal((await draft()).revision,revision);
  await page.locator('#intervalType').selectOption('unreadable');await page.locator('#reason').selectOption('noise');await interval(.6,.7);assert.equal((await draft()).intervals[1].reason,'noise');
  checks.push('Half-open readable/unreadable intervals, reasons, and overlap rejection');
  await page.locator('#completed').click();assert.equal(await page.locator('#completed').isChecked(),false);
  await page.locator('[data-mode="beat"]').click();await page.locator('[data-span="all"]').click();
  const beforeBuffer=(await draft()).events.length;await clickAt(.02,1,20000);assert.equal((await draft()).events.length,beforeBuffer);
  checks.push('Buffers cannot be annotated; incomplete coverage cannot be marked complete');
  const saved=await saveDoc();assert.equal(saved.doc.case_id,'case_001');assert(saved.doc.reviewed_at);assert(saved.doc.exported_at);assert(saved.doc.revision>0);assert(saved.filename.startsWith('case_001_r'));assert.equal(saved.doc.adjudication_status,'in_progress');assert.deepEqual(saved.doc.events,(await draft()).events);
  await page.reload();await page.waitForFunction(()=>document.querySelector('#loading').hidden);assert.deepEqual((await draft()).events,saved.doc.events);
  await open('case_002');assert.equal(await page.locator('#beatType').inputValue(),'unknown');assert.equal(await draft(),null);await open('case_001');assert.deepEqual((await draft()).events,saved.doc.events);
  checks.push('JSON download includes case, timestamps, revisions; reload and case switch restore isolated drafts');
  const beforeImport=(await draft()).revision;
  const wrong={...saved.doc,case_id:'case_002'};
  await page.locator('#importFile').setInputFiles({name:'wrong-case.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(wrong))});await page.waitForFunction(()=>document.querySelector('#message').textContent.startsWith('未导入'));assert.equal((await draft()).revision,beforeImport);
  const wrongPosition=JSON.parse(JSON.stringify(saved.doc));wrongPosition.events[0].sample_index=meta('case_001').evaluation_end_sample;
  await page.locator('#importFile').setInputFiles({name:'out-of-range.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(wrongPosition))});await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('越界'));assert.equal((await draft()).revision,beforeImport);
  const wrongRate={...saved.doc,fs:500};
  await page.locator('#importFile').setInputFiles({name:'wrong-rate.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(wrongRate))});await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('采样率'));assert.equal((await draft()).revision,beforeImport);
  await page.locator('#importFile').setInputFiles({name:'valid.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(saved.doc))});await page.waitForFunction(()=>document.querySelector('#message').textContent.startsWith('标注已导入'));assert.deepEqual((await draft()).events,saved.doc.events);assert((await draft()).revision>beforeImport);
  await page.locator('#undo').click();assert.deepEqual((await draft()).events,saved.doc.events);
  checks.push('Import validates source identity and bounds; valid import remains undoable');
  await open('case_002');await page.locator('#reviewer').fill('AUTOMATED_UI_TEST_NOT_REFERENCE');await page.locator('#reviewer').press('Tab');await page.locator('#exposure').selectOption('false');
  await page.locator('[data-span="60"]').click();await page.locator('[data-mode="interval"]').click();await page.locator('#intervalType').selectOption('unreadable');await interval(0,1);
  await page.locator('#completed').check();assert.equal((await draft()).completed,true);const complete=await saveDoc();assert.equal(complete.doc.adjudication_status,'completed_unadjudicated');assert.deepEqual(complete.doc.unreadable_intervals,[[meta('case_002').evaluation_start_sample,meta('case_002').evaluation_end_sample]]);
  checks.push('Completion requires reviewer, exposure response and exact coverage; remains unadjudicated');
  await open('case_010');await page.locator('[data-mode="beat"]').click();await clickAt(.25);await page.locator('#gain').selectOption('4');await page.locator('#gain').selectOption('1');
  await page.screenshot({path:path.join(out,'ui-preview.png'),fullPage:true});
  for(const ref of refs) await open(ref.case_id);
  assert(!requests.some(u=>/^https?:/.test(u)));assert(!requests.some(u=>u.includes('/comparison/') || u.includes('detector_comparison')));
  assert.deepEqual(errors,[]);checks.push('All 85 cases open offline with no network or algorithm-result requests');
  const fallback=await browser.newContext({acceptDownloads:true,viewport:{width:1200,height:1400},deviceScaleFactor:2});
  await fallback.addInitScript(()=>{Storage.prototype.setItem=function(){throw new DOMException('Test quota exhausted','QuotaExceededError');};});
  const second=await fallback.newPage();await second.goto(url);await second.waitForFunction(()=>document.querySelector('#loading').hidden);
  const b=await second.locator('#wave').boundingBox(),fraction=.4137;
  await second.mouse.click(b.x+88+fraction*(b.width-108),b.y+70);
  assert((await second.locator('#saveStatus').textContent()).includes('缓存失败'));
  await second.locator('#reviewer').fill('QUOTA_AND_DPR_TEST');
  const dp=second.waitForEvent('download');await second.keyboard.press('Control+s');const dd=await dp;
  const fp=path.join(out,'synthetic-fallback-export.json');await dd.saveAs(fp);const fd=JSON.parse(fs.readFileSync(fp));
  assert.equal(fd.events[0].sample_index,Math.round(meta('case_001').evaluation_start_sample+fraction*2500));assert.equal(fd.reviewer_id,'QUOTA_AND_DPR_TEST');
  await fallback.close();checks.push('DPR=2 coordinates and Ctrl+S work; storage quota failure still permits JSON download');
  fs.writeFileSync(path.join(out,'test_results.json'),JSON.stringify({passed:true,checks,browser:await browser.version(),cases_loaded:refs.length,clinical_reference_generated:false,notes:'All clicks and downloads are synthetic UI tests in an isolated browser profile.'},null,2));
  console.log('PASS: '+checks.length+' browser checks; 85 cases; no page errors.');
  await context.close();await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exitCode=1;});
