import test from "node:test";
import assert from "node:assert/strict";
import {webcrypto} from "node:crypto";
import {readFileSync} from "node:fs";
import {FriendsHub,handle,digestHex} from "../src/worker.js";
import worker from "../src/worker.js";
import {fingerprint,slot,tick,status,NBG_URL,DAILY_CHANGE_CAP} from "../src/scheduler.js";
globalThis.crypto ??= webcrypto;

const env={TELEGRAM_TOKEN:"test-token",TELEGRAM_CHAT_ID:"123",GITHUB_DISPATCH_TOKEN:"test-github"};
const FIXTURE=readFileSync(new URL("../../tests/fixtures/nbg-2026-09-14.html",import.meta.url),"utf8");
class Storage {
  constructor(){this.data=new Map();}
  async get(k){return structuredClone(this.data.get(k));}
  async put(k,v){this.data.set(k,structuredClone(v));}
}
const hub=()=>({storage:new Storage(),env});
const at=(text)=>+new Date(text+"+04:00");           // Tbilisi wall clock
const MON="2026-11-16", SAT="2026-11-21";

/** Scriptable network. `page` may be HTML, a status code or "timeout". */
function network(options={}) {
  const o={page:FIXTURE,queued:[],runs:[],dispatch:204,...options}, calls=[];
  const net=async(url,opts={})=>{
    const body=opts.body?JSON.parse(opts.body):null;calls.push({url,body,headers:opts.headers});
    if(url===NBG_URL) {
      if(o.page==="timeout"){const e=Error("timed out");e.name="TimeoutError";throw e;}
      return typeof o.page==="number"?new Response("blocked",{status:o.page}):new Response(o.page);
    }
    if(url.includes("api.telegram.org"))return Response.json({ok:true,result:{}});
    if(url.includes("/actions/runs?"))return Response.json({workflow_runs:url.includes("status=queued")?o.queued:[]});
    if(url.includes("/workflows/monitor.yml/runs"))return Response.json({workflow_runs:o.runs});
    if(url.endsWith("/workflows/monitor.yml/dispatches"))return new Response(null,{status:o.dispatch});
    throw Error("Unexpected network request: "+url);
  };
  const count=part=>calls.filter(c=>c.url.includes(part)).length;
  return {net,calls,o,started:()=>calls.filter(c=>c.url.endsWith("/dispatches")).map(c=>c.body.inputs.mode),
    notices:()=>calls.filter(c=>c.url.endsWith("/sendMessage")).map(c=>c.body),polls:()=>count("nbg.gov.ge")};
}
const withDocument=(html,from,to)=>{assert.ok(html.includes(from));return html.replace(from,to);};
const DOC="რიკო-წინასწარი-პროსპექტი-signed.pdf";

test("schedule follows Tbilisi business hours whatever the cron string says",()=>{
  assert.equal(slot(at(MON+"T08:58")),"idle");
  assert.equal(slot(at(MON+"T09:00")),"poll");
  assert.equal(slot(at(MON+"T18:58")),"poll");
  assert.equal(slot(at(MON+"T19:00")),"idle");
  assert.equal(slot(at(SAT+"T11:00")),"idle");
  assert.equal(slot(at(SAT+"T20:45")),"evening");
  assert.equal(slot(at(SAT+"T21:00")),"digest");
  assert.equal(slot(at(MON+"T21:55")),"digest");
  assert.equal(slot(at(MON+"T22:00")),"idle");
});

test("fingerprint ignores cache suffixes, issuer spelling and dates but sees real changes",async()=>{
  const base=await fingerprint(FIXTURE);
  assert.match(base,/^[a-f0-9]{64}$/);
  assert.equal(await fingerprint(withDocument(FIXTURE,DOC,DOC+"?v=83712&utm_source=x")),base);
  const edit=(pattern,to)=>{assert.match(FIXTURE,pattern);return FIXTURE.replace(pattern,to);};
  assert.equal(await fingerprint(edit(/"releaseDate": null/,'"releaseDate": "2026-09-01T00:00:00Z"')),base);
  assert.equal(await fingerprint(edit(/("title": ")([^"]+)/,"$1$2 (renamed)")),base);
  assert.notEqual(await fingerprint(withDocument(FIXTURE,DOC,"რიკო-საბოლოო-პირობები.pdf")),base);
  assert.notEqual(await fingerprint(edit(/"licenseType": ""/,'"licenseType": "GE2700609999"')),base);
  await assert.rejects(fingerprint("<html>Maintenance</html>"),/unreadable/);
  await assert.rejects(fingerprint('<script id="__NEXT_DATA__">{"props":{"initialProps":{"pageProps":{"organizations":[]}}}}</script>'),/empty/);
});

test("an unchanged page costs one request and starts nothing",async()=>{
  const h=hub(),n=network();
  await tick(h,at(MON+"T10:00"),n.net);           // first poll establishes the fingerprint
  assert.deepEqual(n.started(),["check"]);
  n.o.runs=[{display_title:"NBG check",created_at:new Date(at(MON+"T10:00")).toISOString(),status:"completed",conclusion:"success"}];
  for(const t of ["10:02","10:04","10:06"])await tick(h,at(MON+"T"+t),n.net);
  assert.deepEqual(n.started(),["check"]);
  assert.equal(n.polls(),4);
  assert.equal(n.notices().length,0);
  assert.match(n.calls.find(c=>c.url===NBG_URL).headers["User-Agent"],/NBG-Publication-Monitor/);
});

test("a new document starts exactly one full check",async()=>{
  const h=hub(),n=network();
  await tick(h,at(MON+"T10:00"),n.net);
  n.o.runs=[{display_title:"NBG check",created_at:new Date(at(MON+"T10:00")).toISOString(),status:"completed",conclusion:"success"}];
  await tick(h,at(MON+"T10:02"),n.net);
  n.o.page=withDocument(FIXTURE,DOC,"new-prospectus.pdf");
  await tick(h,at(MON+"T10:04"),n.net);
  assert.deepEqual(n.started(),["check","check"]);
  n.o.runs.push({display_title:"NBG check",created_at:new Date(at(MON+"T10:04")).toISOString(),status:"in_progress",conclusion:null});
  await tick(h,at(MON+"T10:06"),n.net);
  assert.deepEqual(n.started(),["check","check"]);
});

test("a change waits rather than displacing a job already queued",async()=>{
  const h=hub(),n=network({queued:[{head_branch:"main",path:".github/workflows/on-demand.yml"}]});
  await tick(h,at(MON+"T10:00"),n.net);
  assert.deepEqual(n.started(),[]);
  n.o.queued=[];
  await tick(h,at(MON+"T10:02"),n.net);
  assert.deepEqual(n.started(),["check"]);
});

test("a failed full check is retried, then reported to the owner only",async()=>{
  const h=hub(),n=network();
  const failed=t=>({display_title:"NBG check",created_at:new Date(at(MON+"T"+t)).toISOString(),status:"completed",conclusion:"failure"});
  await tick(h,at(MON+"T10:00"),n.net);
  n.o.runs=[failed("10:00")];
  await tick(h,at(MON+"T10:02"),n.net);
  n.o.runs.push(failed("10:02"));
  await tick(h,at(MON+"T10:04"),n.net);
  n.o.runs.push(failed("10:04"));
  assert.deepEqual(n.started(),["check","check","check"]);
  await tick(h,at(MON+"T10:06"),n.net);
  assert.deepEqual(n.started(),["check","check","check"]);
  assert.equal(n.notices().length,1);
  assert.equal(n.notices()[0].chat_id,"123");
  assert.match(n.notices()[0].text,/could not be processed/);
});

test("blocked polling falls back to a full check every ten minutes and reports recovery",async()=>{
  const h=hub(),n=network({page:403});
  let minute=0;
  const step=async()=>{await tick(h,at(MON+"T10:00")+minute*60000,n.net);minute+=2;};
  for(let i=0;i<4;i++)await step();
  assert.equal(n.notices().length,0);
  assert.deepEqual(n.started(),[]);
  await step();                                     // fifth failure: 10:08
  assert.match(n.notices()[0].text,/not getting through/);
  assert.match(n.notices()[0].text,/HTTP 403/);
  assert.deepEqual(n.started(),["check"]);
  for(let i=0;i<4;i++)await step();               // 10:10–10:16
  assert.deepEqual(n.started(),["check"]);
  await step();                                     // 10:18
  assert.deepEqual(n.started(),["check","check"]);
  assert.equal(n.notices().length,1);
  assert.match(await status(h),/blocked \(HTTP 403\)/);
  n.o.page=FIXTURE;
  await step();
  assert.match(n.notices()[1].text,/recovered/);
  assert.match(await status(h),/Fast polling: working/);
});

test("a timeout is one failure, not a crash",async()=>{
  const h=hub(),n=network({page:"timeout"});
  assert.equal(await tick(h,at(MON+"T10:00"),n.net),"poll");
  assert.match(await status(h),/retrying \(1 failed, timeout\)/);
});

test("the digest starts at 21:00 once, is confirmed, and retries a failure",async()=>{
  const h=hub(),n=network();
  await tick(h,at(MON+"T21:00"),n.net);
  assert.deepEqual(n.started(),["digest"]);
  assert.equal(n.polls(),0);                        // the digest reads saved data only
  n.o.runs=[{display_title:"NBG digest",created_at:new Date(at(MON+"T21:00")).toISOString(),status:"completed",conclusion:"failure"}];
  await tick(h,at(MON+"T21:05"),n.net);
  assert.deepEqual(n.started(),["digest","digest"]);
  n.o.runs.push({display_title:"NBG digest",created_at:new Date(at(MON+"T21:05")).toISOString(),status:"completed",conclusion:"success"});
  for(const t of ["21:10","21:15","21:55"])await tick(h,at(MON+"T"+t),n.net);
  assert.deepEqual(n.started(),["digest","digest"]);
  assert.equal(n.notices().length,0);
  await tick(h,at("2026-11-17T21:00"),n.net);       // next evening starts afresh
  assert.deepEqual(n.started(),["digest","digest","digest"]);
});

test("an unconfirmed digest stops after three attempts and tells the owner",async()=>{
  const h=hub(),n=network();
  for(const t of ["21:00","21:05","21:10","21:15","21:20","21:25","21:30","21:35","21:40"])await tick(h,at(MON+"T"+t),n.net);
  assert.deepEqual(n.started(),["digest","digest","digest"]);
  assert.equal(n.notices().length,1);
  assert.match(n.notices()[0].text,/digest could not be confirmed/);
});

test("the evening check runs once a day, weekends included",async()=>{
  const h=hub(),n=network();
  await tick(h,at(SAT+"T20:45"),n.net);
  await tick(h,at(SAT+"T20:45"),n.net);
  assert.deepEqual(n.started(),["check"]);
  assert.equal(await tick(h,at(SAT+"T11:00"),n.net),"idle");
  assert.equal(n.polls(),0);
});

test("an expired GitHub token is reported once and does not lose the change",async()=>{
  const h=hub(),n=network({dispatch:401});
  for(const t of ["10:00","10:02","10:04","10:06"])await tick(h,at(MON+"T"+t),n.net);
  assert.equal(n.notices().length,1);
  assert.match(n.notices()[0].text,/GitHub is not starting jobs/);
  n.o.dispatch=204;
  await tick(h,at(MON+"T10:08"),n.net);
  assert.equal((await h.storage.get("scheduler")).pending.tries,1);
});

test("a flapping page is throttled instead of starting hundreds of jobs",async()=>{
  const h=hub(),n=network();
  const ok=t=>({display_title:"NBG check",created_at:new Date(t).toISOString(),status:"completed",conclusion:"success"});
  let t=at(MON+"T09:00");
  for(let i=0;i<DAILY_CHANGE_CAP+20;i++,t+=120000) {
    n.o.page=withDocument(FIXTURE,DOC,"flap-"+i+".pdf");
    await tick(h,t,n.net);
    n.o.runs=[ok(t)];
  }
  assert.ok(n.started().length<=DAILY_CHANGE_CAP+2,String(n.started().length));
  assert.equal(n.notices().filter(m=>/unusually many/.test(m.text)).length,1);
});

test("cron reaches the hub; the internal route is not reachable from the internet",async()=>{
  const seen=[];
  const stub={fetch:async(url,opts)=>{seen.push([String(url),JSON.parse(opts.body)]);return new Response("OK");}};
  await worker.scheduled({cron:"*/2 5-14 * * *",scheduledTime:at(MON+"T10:00")},{FRIENDS:{idFromName:()=>"id",get:()=>stub}});
  assert.deepEqual(seen,[["https://scheduler.internal/tick",{time:at(MON+"T10:00")}]]);
  await worker.scheduled({scheduledTime:1},{});      // no hub configured: nothing to do
  const r=await handle(new Request("https://example.workers.dev/tick",{method:"POST",body:"{}"}),env,async()=>{throw Error("no network");},null,null);
  assert.equal(r.status,404);
});

test("/status is owner-only",async()=>{
  const storage=new Storage();
  storage.list=async()=>new Map();storage.delete=async()=>{};storage.transaction=async fn=>fn(storage);
  const h=new FriendsHub({storage},env),n=network();
  await storage.put("member:456",{chat:"456",name:"Friend",active:true,alerts:true});
  const send=async(chat,id)=>handle(new Request("https://example.workers.dev/telegram",{method:"POST",
    headers:{"X-Telegram-Bot-Api-Secret-Token":await digestHex("nbg-webhook-v1:"+env.TELEGRAM_TOKEN)},
    body:JSON.stringify({update_id:id,message:{text:"/status",chat:{id:chat,type:"private"},from:{id:chat,first_name:"A",is_bot:false}}})}),env,n.net,null,h);
  await send(123,1);await send(456,2);
  assert.match(n.notices()[0].text,/Monitor status/);
  assert.match(n.notices()[1].text,/Only the owner/);
});
